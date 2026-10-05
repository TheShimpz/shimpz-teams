"""Holding a Routine run as an incident: every crash window, Pular, Pausar, deletion, and sealed state (ADR-0092)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import tempfile
import time
import unittest
from unittest import mock

import routine_fixture
from test_local_routine_service import KEY, RoutineServiceCase, Runtime

from action import journal as action_journal
from local import app as local_app
from local.routine import incident as routine_incident
from local.routine import lifecycle as routine_lifecycle
from local.routine import manage as routine_manage
from local.routine import store as routine_store
from local.routine import watchdog as routine_watchdog
from routine import cursor as routine_cursor
from routine import hold as routine_hold
from routine import plan as routine_plan
from routine import record
from tests.test_routine_plan import CONTRACTS, _document


def _operation(name: str) -> action_journal.Operation:
    return action_journal.Operation(name, hashlib.sha256(name.encode()).hexdigest())


class IncidentCase(RoutineServiceCase):
    def held_run(self, directory: str, *, batch: bool = True):
        """One claimed run with its generation bound and, optionally, an uncertain archivable batch in it."""
        controller, service = self.service(directory, Runtime())
        value = self.routine(service)
        claim = service.claim_routine_run()
        lease = record.lease_of(claim["lease_token"], KEY)
        network = controller.assistant_lifecycle._network("team_1").id
        service.routine_store.update(
            "team_1",
            lambda state: (record.bind_generation(state, claim["run_id"], lease, int(time.time()), network), None),
        )
        generation = record.generation_for(network, claim["run_id"])
        prepared = None
        if batch:
            first, second = _operation("first"), _operation("second")
            prepared = service.action_state.prepare_batch(generation, "thread", (first, second), archivable=True)
            service.action_state.begin(prepared, first)
            service.action_state.complete(prepared, first, {"ok": 1})
            service.action_state.begin(prepared, second)
        return controller, service, value, claim["run_id"], lease, generation, prepared

    def fence(self, service, run_id: str, lease: record.Lease) -> None:
        service.routine_store.update(
            "team_1", lambda state: (routine_hold.fence(state, run_id, lease, int(time.time())), None)
        )


class HoldTests(IncidentCase):
    def test_a_hold_seals_evidence_archives_the_batch_and_holds_the_routine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, value, run_id, lease, generation, batch = self.held_run(directory)
            routine_incident.hold(service, "team_1", run_id, lease)
            state = self.state(service)
            self.assertEqual(state.runs, ())
            self.assertEqual(
                [(item.incident_id, item.routine_id, item.status) for item in state.incidents],
                [(run_id, value.routine_id, "unresolved")],
            )
            self.assertEqual(service.action_state.current_batch(generation), (batch.fingerprint, "archived"))
            evidence = routine_incident.read_evidence(service.routine_store.incident("team_1", run_id), run_id)
            self.assertEqual(evidence["fingerprint"], batch.fingerprint)
            self.assertEqual(
                [(item[0], item[2], item[3]) for item in evidence["operations"]],
                [("first", "completed", 1), ("second", "executing", 1)],
            )
            self.assertNotIn("ok", json.dumps(evidence))
            # The held Routine is not claimed again, even when due, until its incident is resolved.
            self.assertIn(value.routine_id, record.held_routines(state))
            self.assertIsNone(record.claimable(state, int(time.time()) + 86_400 * 2))
            with self.assertRaises(local_app.ApiProblem) as stale:
                routine_incident.hold(service, "team_1", run_id, lease)
            self.assertEqual(stale.exception.code, "routine-lease-invalid")

    def test_every_crash_window_finishes_the_hold_without_dispatching(self) -> None:
        for window in ("fenced", "sealed", "archived", "settled"):
            with self.subTest(window=window), tempfile.TemporaryDirectory() as directory:
                _controller, service, _value, run_id, lease, generation, batch = self.held_run(directory)
                self.fence(service, run_id, lease)
                if window != "fenced":
                    held = record.run(self.state(service), run_id)
                    operations = service.action_state.snapshot(generation, batch.fingerprint)
                    sealed = routine_incident.evidence(run_id, held, batch.fingerprint, operations)
                    service.routine_store.put_incident("team_1", run_id, sealed)
                if window in {"archived", "settled"}:
                    service.action_state.archive(generation, batch.fingerprint)
                if window == "settled":
                    self.assertTrue(routine_incident.reconcile(service, "team_1", run_id))
                with mock.patch.object(service.action_state, "begin", side_effect=AssertionError("dispatched")):
                    routine_watchdog.check(service)
                self.assertFalse(routine_incident.reconcile(service, "team_1", run_id))
                state = self.state(service)
                self.assertEqual([item.status for item in state.incidents], ["unresolved"])
                self.assertEqual(service.action_state.current_batch(generation), (batch.fingerprint, "archived"))

    def test_a_run_held_before_any_action_holds_without_a_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, _value, run_id, lease, generation, _batch = self.held_run(directory, batch=False)
            routine_incident.hold(service, "team_1", run_id, lease)
            evidence = routine_incident.read_evidence(service.routine_store.incident("team_1", run_id), run_id)
            self.assertEqual((evidence["fingerprint"], evidence["operations"]), (None, []))
            self.assertIsNone(service.action_state.current_batch(generation))
            routine_fixture.set_aside(service, "team_1", run_id)
            self.assertIsNone(service.routine_store.incident("team_1", run_id))
            # A cursor a crash left behind, whose run and incident are gone, is removed by the next pass.
            plan = routine_plan.admit(_document(output={"mode": "chain", "step": None}), CONTRACTS)
            orphan = routine_cursor.Binding("a" * 64, "b" * 32, 1, "e" * 32)
            service.routine_store.put_cursor("team_1", routine_cursor.start(plan, orphan, 0))
            routine_watchdog.check(service)
            self.assertEqual(service.routine_store.cursors("team_1"), ())

    def test_lost_or_mismatched_evidence_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, _value, run_id, lease, generation, batch = self.held_run(directory)
            self.fence(service, run_id, lease)
            service.action_state.archive(generation, batch.fingerprint)
            with self.assertRaises(local_app.ApiProblem) as lost:
                routine_incident.reconcile(service, "team_1", run_id)
            self.assertEqual(lost.exception.code, "routine-state-unavailable")
            held = record.run(self.state(service), run_id)
            service.routine_store.put_incident("team_1", run_id, routine_incident.evidence(run_id, held, "0" * 64, ()))
            with self.assertRaises(local_app.ApiProblem) as mismatch:
                routine_incident.reconcile(service, "team_1", run_id)
            self.assertEqual(mismatch.exception.code, "routine-state-unavailable")
            for payload in (b"not json", json.dumps({"version": 1}).encode()):
                with self.subTest(payload=payload), self.assertRaises(local_app.ApiProblem):
                    routine_incident.read_evidence(payload, run_id)
            with self.assertRaises(local_app.ApiProblem):
                routine_incident.read_evidence(routine_incident.evidence("f" * 32, held, None, ()), run_id)
            self.assertEqual(record.run(self.state(service), run_id).status, "held")

    def test_an_unavailable_journal_keeps_the_run_held(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, _value, run_id, lease, _generation, _batch = self.held_run(directory)
            self.fence(service, run_id, lease)
            with (
                mock.patch.object(
                    service.action_state, "current_batch", side_effect=action_journal.ActionJournalError("down")
                ),
                self.assertRaises(local_app.ApiProblem) as caught,
            ):
                routine_incident.reconcile(service, "team_1", run_id)
            self.assertEqual(caught.exception.code, "action-state-unavailable")
            self.assertEqual(record.run(self.state(service), run_id).status, "held")
            with mock.patch.object(routine_hold, "settle_hold", side_effect=record.RoutineStateError("run-not-held")):
                self.assertFalse(routine_incident.reconcile(service, "team_1", run_id))


class ResolutionTests(IncidentCase):
    def test_pular_permits_future_cycles_and_then_releases_what_the_incident_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, value, run_id, lease, generation, batch = self.held_run(directory)
            plan = routine_plan.admit(_document(output={"mode": "chain", "step": None}), CONTRACTS)
            binding = routine_cursor.Binding("a" * 64, value.routine_id, 1, run_id)
            service.routine_store.put_cursor("team_1", routine_cursor.start(plan, binding, 0))
            routine_incident.hold(service, "team_1", run_id, lease)
            routine_manage.drain(service, "team_1")
            routine_watchdog.check(service)
            # The run's discard and every pass keep the cursor and archive marker its unresolved incident needs.
            self.assertIsNotNone(service.routine_store.cursor("team_1", binding))
            self.assertEqual(service.action_state.current_batch(generation), (batch.fingerprint, "archived"))
            routine_fixture.set_aside(service, "team_1", run_id)
            state = self.state(service)
            self.assertEqual([item.status for item in state.incidents], ["released"])
            self.assertNotIn(value.routine_id, record.held_routines(state))
            self.assertIsNone(service.action_state.current_batch(generation))
            self.assertIsNone(service.routine_store.cursor("team_1", binding))
            self.assertIsNone(service.routine_store.incident("team_1", run_id))
            routine_incident.reconcile_team(service, "team_1")
            with self.assertRaises(local_app.ApiProblem) as again:
                routine_fixture.set_aside(service, "team_1", run_id)
            self.assertEqual(again.exception.code, "routine-incident-unavailable")
            self.assertEqual(
                record.claimable(self.state(service), int(time.time()) + 86_400 * 2),
                record.routine(self.state(service), value.routine_id),
            )

    def test_a_crashed_release_is_retried_and_a_release_failure_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, _value, run_id, lease, generation, _batch = self.held_run(directory)
            routine_incident.hold(service, "team_1", run_id, lease)
            service.routine_store.update(
                "team_1", lambda state: (routine_hold.skip_incident(state, run_id, 0, choice="run"), None)
            )
            with (
                mock.patch.object(
                    service.action_state, "release_archive", side_effect=action_journal.ActionJournalError("down")
                ),
                self.assertRaises(local_app.ApiProblem) as caught,
            ):
                routine_incident.reconcile_team(service, "team_1")
            self.assertEqual(caught.exception.code, "action-state-unavailable")
            routine_watchdog.check(service)
            self.assertIsNone(service.action_state.current_batch(generation))
            self.assertIsNone(service.routine_store.incident("team_1", run_id))

    def test_pausar_holds_dispatch_and_resuming_never_bypasses_an_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, value, run_id, lease, _generation, _batch = self.held_run(directory)
            routine_incident.hold(service, "team_1", run_id, lease)
            routine_incident.set_paused(service, "team_1", value.routine_id, True)
            self.assertTrue(record.routine(self.state(service), value.routine_id).paused)
            routine_incident.set_paused(service, "team_1", value.routine_id, False)
            self.assertIsNone(record.claimable(self.state(service), int(time.time()) + 86_400 * 2))
            routine_fixture.set_aside(service, "team_1", run_id)
            routine_incident.set_paused(service, "team_1", value.routine_id, True)
            self.assertIsNone(record.claimable(self.state(service), int(time.time()) + 86_400 * 2))
            routine_incident.set_paused(service, "team_1", value.routine_id, False)
            self.assertIsNotNone(record.claimable(self.state(service), int(time.time()) + 86_400 * 2))
            with self.assertRaises(local_app.ApiProblem) as missing:
                routine_incident.set_paused(service, "team_1", "f" * 32, True)
            self.assertEqual(missing.exception.code, "routine-not-found")

    def test_deleting_the_routine_sets_its_held_run_aside_as_it_is_indexed(self) -> None:
        """No incident is left for an answer its deleted Routine can no longer take; nothing is rolled back."""
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, value, run_id, lease, generation, batch = self.held_run(directory)
            self.fence(service, run_id, lease)
            starts = self.state(service).starts
            deleting = service.delete_routine("team_1", value.routine_id)
            self.assertFalse(deleting["deleted"])
            self.assertEqual(record.run(self.state(service), run_id).status, "held")
            routine_watchdog.check(service)
            routine_watchdog.check(service)
            state = self.state(service)
            self.assertEqual((state.routines, state.starts), ((), starts))
            self.assertEqual([item.status for item in state.incidents], ["released"])
            notice = next(item for item in state.notices if item.notice_id == run_id)
            self.assertEqual((notice.outcome, notice.detail["choice"]), ("user-skipped", "delete"))
            self.assertIsNone(service.action_state.current_batch(generation))
            del batch

    def test_deleting_the_team_purges_every_incident_marker_and_sealed_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service, _value, run_id, lease, generation, _batch = self.held_run(directory)
            routine_incident.hold(service, "team_1", run_id, lease)
            routine_lifecycle._delete_team_routines(controller, "team_1")
            self.assertIsNone(service.action_state.current_batch(generation))
            self.assertEqual(service.routine_store.load("team_1"), record.TeamRoutines())
            self.assertIsNone(service.routine_store.incident("team_1", run_id))


class RecoverySnapshotTests(IncidentCase):
    def compiled(self, service, value: record.Routine, run_id: str, generation: str, revision: int):
        """Seal the run's recovery snapshot and its first cursor, as the executor does before its first dispatch."""
        incarnation = generation.removesuffix(f":routine:{run_id}")
        document = _document(output={"mode": "chain", "step": None})
        plan = routine_plan.admit(document, CONTRACTS)
        binding = routine_cursor.Binding(incarnation, value.routine_id, revision, run_id)
        snapshot = routine_incident.Recovery(binding, value.quote, document)
        routine_incident.seal_recovery(service, "team_1", snapshot)
        cursor = routine_cursor.dispatch(
            routine_cursor.start(plan, binding, 1_800_000_000), plan, "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6", "d" * 64
        )
        service.routine_store.put_cursor("team_1", cursor)
        return snapshot, cursor

    def test_a_held_runs_incident_reopens_its_cursor_after_a_restart_until_its_routine_is_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, value, run_id, lease, generation, batch = self.held_run(directory)
            # The run executes revision 3; the Routine is updated to revision 4 while the run is held.
            service.routine_store.update(
                "team_1",
                lambda state: (
                    record._replace_routine(
                        state,
                        routine_fixture.granted(
                            dataclasses.replace(record.routine(state, value.routine_id), revision=3)
                        ),
                    ),
                    None,
                ),
            )
            snapshot, cursor = self.compiled(service, value, run_id, generation, 3)
            # A live run keeps its snapshot through every pass.
            routine_watchdog.check(service)
            self.assertEqual(service.routine_store.recoveries("team_1"), (run_id,))
            self.fence(service, run_id, lease)
            service.routine_store.update(
                "team_1",
                lambda state: (
                    record._replace_routine(
                        state,
                        routine_fixture.granted(
                            dataclasses.replace(record.routine(state, value.routine_id), revision=4)
                        ),
                    ),
                    None,
                ),
            )
            routine_incident.reconcile(service, "team_1", run_id)
            self.assertEqual([item.revision for item in self.state(service).incidents], [3])
            routine_watchdog.check(service)
            self.assertEqual(service.routine_store.recoveries("team_1"), ())
            # A restart reopens every store from disk; only the incident's own sealed copy names the binding.
            service.action_state.close()
            reopened_store = routine_store.RoutineStore(service.routine_store.root, service.routine_store.key_path)
            reopened_journal = action_journal.ActionJournal(service.action_state.path)
            self.addCleanup(reopened_journal.close)
            service.routine_store, service.action_state = reopened_store, reopened_journal
            opened = routine_incident.open_recovery(service, "team_1", run_id)
            self.assertEqual(opened.recovery, snapshot)
            self.assertEqual(opened.cursor, cursor)
            self.assertEqual(opened.cursor.operation_id, "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6")
            self.assertEqual(opened.recovery.plan_digest, cursor.plan)
            self.assertEqual(reopened_journal.current_batch(generation), (batch.fingerprint, "archived"))
            # Deleting the Routine sets the held run aside: there is nothing left to reopen.
            service.delete_routine("team_1", value.routine_id)
            self.assertEqual(self.state(service).routines, ())
            with self.assertRaises(local_app.ApiProblem) as skipped:
                routine_incident.open_recovery(service, "team_1", run_id)
            self.assertEqual(skipped.exception.code, "routine-incident-unavailable")

    def test_a_recovery_snapshot_is_write_once_even_after_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, value, run_id, _lease, generation, _batch = self.held_run(directory, batch=False)
            snapshot, _cursor = self.compiled(service, value, run_id, generation, 1)
            original = service.routine_store.recovery("team_1", run_id)
            # Resealing the exact same snapshot is idempotent.
            routine_incident.seal_recovery(service, "team_1", snapshot)
            restarted = routine_store.RoutineStore(service.routine_store.root, service.routine_store.key_path)
            for store in (service.routine_store, restarted):
                service.routine_store = store
                for changed in (
                    dataclasses.replace(snapshot, quote="Every day at 10, delete my zones"),
                    dataclasses.replace(snapshot, plan=_document(timezone="UTC")),
                    dataclasses.replace(snapshot, binding=dataclasses.replace(snapshot.binding, revision=2)),
                    dataclasses.replace(snapshot, binding=dataclasses.replace(snapshot.binding, incarnation="f" * 64)),
                ):
                    with self.subTest(store=store, changed=changed), self.assertRaises(local_app.ApiProblem):
                        routine_incident.seal_recovery(service, "team_1", changed)
                    self.assertEqual(store.recovery("team_1", run_id), original)
                with self.assertRaisesRegex(routine_store.RoutineStoreError, "immutable"):
                    store.put_recovery("team_1", run_id, original + b" ")
                store.put_recovery("team_1", run_id, original)
            self.assertEqual(routine_incident.read_recovery(original, run_id), snapshot)

    def test_recovery_fails_closed_without_a_matching_snapshot_or_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, value, run_id, lease, generation, _batch = self.held_run(directory)
            routine_incident.hold(service, "team_1", run_id, lease)
            with self.assertRaises(local_app.ApiProblem) as uncompiled:
                routine_incident.open_recovery(service, "team_1", run_id)
            self.assertEqual(uncompiled.exception.code, "routine-incident-unverifiable")
            with self.assertRaises(local_app.ApiProblem) as missing:
                routine_incident.open_recovery(service, "team_1", "f" * 32)
            self.assertEqual(missing.exception.code, "routine-incident-unavailable")
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, value, run_id, lease, generation, _batch = self.held_run(directory)
            snapshot, _cursor = self.compiled(service, value, run_id, generation, 1)
            routine_incident.hold(service, "team_1", run_id, lease)
            self.assertEqual(routine_incident.open_recovery(service, "team_1", run_id).recovery, snapshot)
            store = service.routine_store
            original = store.incident("team_1", run_id)
            evidence = json.loads(original)
            failures = (
                lambda: store.delete_cursor("team_1", run_id),
                lambda: store.put_cursor(
                    "team_1",
                    routine_cursor.start(routine_plan.admit(_document(timezone="UTC"), CONTRACTS), snapshot.binding, 0),
                ),
                lambda: store.put_incident(
                    "team_1",
                    run_id,
                    json.dumps(
                        {
                            **evidence,
                            "recovery": {
                                **evidence["recovery"],
                                "binding": [snapshot.binding.incarnation, value.routine_id, 2, run_id],
                            },
                        }
                    ).encode(),
                ),
                lambda: store.delete_incident("team_1", run_id),
                lambda: mock.patch.object(store, "cursor", side_effect=routine_store.RoutineStoreError("x")).start(),
            )
            for damage in failures:
                with self.subTest(damage=damage):
                    damage()
                    with self.assertRaises(local_app.ApiProblem) as caught:
                        routine_incident.open_recovery(service, "team_1", run_id)
                    self.assertEqual(caught.exception.code, "routine-state-unavailable")
                    mock.patch.stopall()
                    store.put_incident("team_1", run_id, original)
                    store.put_cursor("team_1", _cursor)

    def test_a_snapshot_must_belong_to_exactly_its_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, value, run_id, lease, generation, _batch = self.held_run(directory)
            incarnation = generation.removesuffix(f":routine:{run_id}")
            self.fence(service, run_id, lease)
            for binding in (
                routine_cursor.Binding(incarnation, "f" * 32, 1, run_id),
                routine_cursor.Binding("f" * 64, value.routine_id, 1, run_id),
            ):
                with self.subTest(binding=binding):
                    service.routine_store.delete_recovery("team_1", run_id)
                    routine_incident.seal_recovery(service, "team_1", routine_incident.Recovery(binding, "Q", {}))
                    with self.assertRaises(local_app.ApiProblem):
                        routine_incident.reconcile(service, "team_1", run_id)
            self.assertEqual(record.run(self.state(service), run_id).status, "held")
            for invalid in (
                b"not json",
                routine_plan.canonical({"version": 2}),
                routine_plan.canonical({"version": 1, "binding": "x", "quote": "Q", "plan": {}}),
                routine_plan.canonical(
                    {"version": 1, "binding": [incarnation, value.routine_id, 0, run_id], "quote": "Q", "plan": {}}
                ),
                routine_plan.canonical(
                    {"version": 1, "binding": [incarnation, value.routine_id, 1, run_id], "quote": 1, "plan": {}}
                ),
                routine_plan.canonical(
                    {"version": 1, "binding": [incarnation, value.routine_id, 1, "e" * 32], "quote": "Q", "plan": {}}
                ),
                routine_plan.canonical({"version": 1, "extra": 1}),
            ):
                with self.subTest(invalid=invalid), self.assertRaises(local_app.ApiProblem):
                    routine_incident.read_recovery(invalid, run_id)
            held = record.run(self.state(service), run_id)
            foreign = routine_incident.Recovery(routine_cursor.Binding(incarnation, "f" * 32, 1, run_id), "Q", {})
            with self.assertRaises(local_app.ApiProblem):
                routine_incident.read_evidence(routine_incident.evidence(run_id, held, None, (), foreign), run_id)
            with self.assertRaises(record.RoutineStateError):
                routine_hold.settle_hold(self.state(service), run_id, 0, 0)
            for payload in (b"", None, b"x" * (routine_store.MAX_RECOVERY_BYTES + 1)):
                with self.subTest(payload=payload), self.assertRaisesRegex(routine_store.RoutineStoreError, "invalid"):
                    service.routine_store.put_recovery("team_1", run_id, payload)
            # A snapshot a crash left behind, whose run is gone, is removed by the next pass.
            orphan = routine_incident.Recovery(
                routine_cursor.Binding(incarnation, value.routine_id, 1, "e" * 32), "Q", {}
            )
            routine_incident.seal_recovery(service, "team_1", orphan)
            service.routine_store.delete_recovery("team_1", run_id)
            routine_watchdog.check(service)
            self.assertEqual(service.routine_store.recoveries("team_1"), ())
            self.assertEqual([item.incident_id for item in self.state(service).incidents], [run_id])


class CapacityTests(IncidentCase):
    def test_an_interrupted_release_at_capacity_is_never_evicted_or_orphaned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service, value, run_id, lease, generation, batch = self.held_run(directory)
            routine_incident.hold(service, "team_1", run_id, lease)
            # Setting it aside commits, then its cleanup crashes before the archive marker is released: the person's
            # decision stands, and the cleanup waits for the watchdog.
            with mock.patch.object(
                service.action_state, "release_archive", side_effect=action_journal.ActionJournalError("crash")
            ):
                routine_fixture.set_aside(service, "team_1", run_id)
            self.assertEqual([item.status for item in self.state(service).incidents], ["skipped"])
            # Fill the index to its bound with released records and give the Team a second held run.
            filler = tuple(
                record.Incident(
                    f"{index:032x}",
                    value.routine_id,
                    f"{'f' * 64}:routine:{index:032x}",
                    index,
                    1,
                    "released",
                    quote=value.quote,
                )
                for index in range(1, record.MAX_INCIDENTS)
            )
            second = "e" * 32
            held = record.Run(second, value.routine_id, "held", 0, generation=record.generation_for("f" * 64, second))
            service.routine_store.update(
                "team_1",
                lambda state: (
                    dataclasses.replace(state, incidents=(*state.incidents, *filler), runs=(held,)),
                    None,
                ),
            )
            self.assertTrue(routine_incident.reconcile(service, "team_1", second))
            incidents = {item.incident_id: item.status for item in self.state(service).incidents}
            self.assertEqual(len(incidents), record.MAX_INCIDENTS)
            self.assertEqual(incidents[run_id], "skipped")
            self.assertEqual(incidents[second], "unresolved")
            self.assertEqual(service.action_state.current_batch(generation), (batch.fingerprint, "archived"))
            # A restart's pass finishes the interrupted cleanup; only then may that record give way.
            service.action_state.close()
            controller.action_state = service.action_state = action_journal.ActionJournal(service.action_state.path)
            self.addCleanup(service.action_state.close)
            routine_watchdog.check(service, startup=True)
            self.assertEqual(
                {item.incident_id: item.status for item in self.state(service).incidents}[run_id], "released"
            )
            self.assertIsNone(service.action_state.current_batch(generation))
            routine_lifecycle._delete_team_routines(controller, "team_1")
            self.assertEqual(service.routine_store.load("team_1"), record.TeamRoutines())

    def test_a_release_that_crashed_after_removing_the_evidence_is_finished(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, _value, run_id, lease, generation, batch = self.held_run(directory)
            routine_incident.hold(service, "team_1", run_id, lease)
            service.routine_store.update(
                "team_1", lambda state: (routine_hold.skip_incident(state, run_id, 0, choice="run"), None)
            )
            service.action_state.release_archive(generation, batch.fingerprint)
            service.routine_store.delete_incident("team_1", run_id)
            routine_incident.reconcile_team(service, "team_1")
            self.assertEqual([item.status for item in self.state(service).incidents], ["released"])

    def test_deleting_the_team_purges_a_pending_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service, _value, run_id, lease, generation, _batch = self.held_run(directory)
            routine_incident.hold(service, "team_1", run_id, lease)
            with mock.patch.object(
                service.action_state, "release_archive", side_effect=action_journal.ActionJournalError("crash")
            ):
                routine_fixture.set_aside(service, "team_1", run_id)
            self.assertEqual([item.status for item in self.state(service).incidents], ["skipped"])
            routine_lifecycle._delete_team_routines(controller, "team_1")
            self.assertIsNone(service.action_state.current_batch(generation))
            self.assertIsNone(service.routine_store.incident("team_1", run_id))


class SealedStateTests(IncidentCase):
    def test_cursor_and_incident_seals_bind_exactly_what_they_belong_to(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, value, run_id, _lease, _generation, _batch = self.held_run(directory, batch=False)
            store = service.routine_store
            plan = routine_plan.admit(_document(output={"mode": "chain", "step": None}), CONTRACTS)
            binding = routine_cursor.Binding("a" * 64, value.routine_id, 1, run_id)
            cursor = routine_cursor.start(plan, binding, 0)
            store.put_cursor("team_1", cursor)
            self.assertEqual(store.cursor("team_1", binding), cursor)
            self.assertEqual(store.cursors("team_1"), (run_id,))
            for other in (dataclasses.replace(binding, incarnation="b" * 64), dataclasses.replace(binding, revision=2)):
                with (
                    self.subTest(other=other),
                    self.assertRaisesRegex(routine_store.RoutineStoreError, "authentication"),
                ):
                    store.cursor("team_1", other)
            with self.assertRaisesRegex(routine_store.RoutineStoreError, "invalid"):
                store.put_cursor("team_1", dataclasses.replace(cursor, step=-1))
            with (
                mock.patch.object(routine_cursor, "decode", side_effect=routine_cursor.CursorError("cursor-invalid")),
                self.assertRaisesRegex(routine_store.RoutineStoreError, "malformed"),
            ):
                store.cursor("team_1", binding)
            for payload in (b"", None, b"x" * (routine_store.MAX_INCIDENT_BYTES + 1)):
                with self.subTest(payload=payload), self.assertRaisesRegex(routine_store.RoutineStoreError, "invalid"):
                    store.put_incident("team_1", run_id, payload)
            store.put_incident("team_1", run_id, b"evidence")
            (store._team_dir("team_1") / f"{run_id}.incident").rename(
                store._team_dir("team_1") / ("f" * 32 + ".incident")
            )
            with self.assertRaisesRegex(routine_store.RoutineStoreError, "authentication"):
                store.incident("team_1", "f" * 32)
            (store._team_dir("team_1") / ("f" * 32 + ".incident")).write_text("{}")
            with self.assertRaisesRegex(routine_store.RoutineStoreError, "malformed"):
                store.incident("team_1", "f" * 32)
            (store._team_dir("team_1") / ("f" * 32 + ".incident")).write_text("not json")
            with self.assertRaisesRegex(routine_store.RoutineStoreError, "malformed"):
                store.incident("team_1", "f" * 32)
            store.delete_incident("team_1", "f" * 32)
            store.delete_cursor("team_1", run_id)
            self.assertEqual(store.cursors("team_1"), ())
            self.assertEqual(store.cursors("team_2"), ())
            with (
                mock.patch.object(routine_store.Path, "iterdir", side_effect=PermissionError("denied")),
                self.assertRaisesRegex(routine_store.RoutineStoreError, "cursors could not be listed"),
            ):
                store.cursors("team_1")

    def test_the_receipt_handoff_seals_the_cursor_before_receipts_go(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, value, run_id, _lease, generation, _batch = self.held_run(directory, batch=False)
            plan = routine_plan.admit(_document(output={"mode": "chain", "step": None}), CONTRACTS)
            binding = routine_cursor.Binding("a" * 64, value.routine_id, 1, run_id)
            operation = _operation("publish")
            prepared = service.action_state.prepare_batch(generation, "thread", (operation,), archivable=True)
            started = service.action_state.begin(prepared, operation)
            result = {"id": "post-1", "meta": {"a/b": [["news"]]}}
            service.action_state.complete(prepared, operation, result)
            dispatched = routine_cursor.dispatch(
                routine_cursor.start(plan, binding, 0), plan, started.operation_id, "d" * 64
            )
            advanced = routine_cursor.complete(dispatched, plan, result)
            order: list[str] = []
            with (
                mock.patch.object(
                    service.routine_store, "put_cursor", side_effect=routine_store.RoutineStoreError("full")
                ),
                self.assertRaises(routine_store.RoutineStoreError),
            ):
                service.routine_store.handoff("team_1", advanced, lambda: order.append("delivered"))
            self.assertEqual(order, [])
            self.assertEqual(service.action_state.current_batch(generation), (prepared.fingerprint, "open"))
            # A crash right after the cursor is sealed leaves receipts the next delivery removes; the step never reruns.
            service.routine_store.handoff("team_1", advanced, lambda: order.append("crashed"))
            self.assertEqual(service.routine_store.cursor("team_1", binding).step, 1)
            service.routine_store.handoff("team_1", advanced, lambda: service.action_state.delivered(prepared))
            self.assertIsNone(service.action_state.current_batch(generation))
            self.assertEqual(order, ["crashed"])

    def test_routine_state_version_eight_admits_held_runs_incidents_plans_grants_receipts_and_output_digests(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, value, run_id, lease, _generation, _batch = self.held_run(directory)
            routine_incident.hold(service, "team_1", run_id, lease)
            routine_incident.set_paused(service, "team_1", value.routine_id, True)
            path = service.routine_store._team_dir("team_1") / "state.json"
            document = json.loads(path.read_bytes())
            self.assertEqual(document["schema"], 8)
            self.assertEqual(document["routines"][0]["run_requested"], 0)
            self.assertEqual(document["routines"][0]["output_digest"], "")
            self.assertEqual(document["routines"][0]["plan"]["version"], 2)
            digest = dict(document, routines=[{**document["routines"][0], "output_digest": "d" * 64}])
            self.assertEqual(
                routine_store._decode(json.dumps(digest).encode(), "team_1").routines[0].output_digest, "d" * 64
            )
            receipt = ["c" * 64, 2_000_000_000]
            document["receipts"] = [receipt]
            self.assertEqual(routine_store._decode(json.dumps(document).encode(), "team_1").receipts, (tuple(receipt),))
            self.assertEqual(document["routines"][0]["revision"], 1)
            self.assertEqual(document["incidents"][0]["status"], "unresolved")
            for mutate in (
                lambda value: value["incidents"][0].update(status="resolved"),
                lambda value: value["incidents"][0].update(generation=""),
                lambda value: value["incidents"].append(dict(value["incidents"][0])),
                lambda value: value["routines"][0].update(revision=0),
                lambda value: value["routines"][0].update(paused="yes"),
                lambda value: value["routines"][0].update(run_requested=-1),
                lambda value: value["routines"][0].pop("run_requested"),
                lambda value: value["routines"][0].pop("output_digest"),
                lambda value: value["routines"][0].update(output_digest="D" * 64),
                lambda value: value["routines"][0].update(output_digest=None),
                lambda value: value["routines"][0].update(name=""),
                lambda value: value["routines"][0].update(plan={"version": 1}),
                lambda value: value["routines"][0].update(grant=None),
                lambda value: value["routines"][0]["grant"].update(revision=2),
                lambda value: value["routines"][0]["grant"].update(plan="sha256:" + "0" * 64),
                lambda value: value.update(receipts=[["C" * 64, 1]]),
                lambda value: value.update(receipts=[["c" * 64, -1]]),
                lambda value: value.update(receipts=[["c" * 64, 1], ["c" * 64, 2]]),
                lambda value: value.update(receipts=[["c" * 64]]),
                lambda value: value.update(receipts={}),
            ):
                tampered = json.loads(path.read_bytes())
                mutate(tampered)
                with self.subTest(mutate=mutate), self.assertRaises(routine_store.RoutineStoreError):
                    routine_store._decode(json.dumps(tampered).encode(), "team_1")


class IncidentRecordTests(IncidentCase):
    def test_claims_reserve_incident_room_and_never_displace_unresolved_evidence(self) -> None:
        base = record.TeamRoutines()
        routine = routine_fixture.granted(
            record.Routine(
                "a" * 32,
                "N",
                "Q",
                {"kind": "daily", "time": "09:00"},
                "UTC",
                (("dns", "sha256:" + "0" * 64),),
                routine_fixture.plan_document(),
                0,
                0,
            )
        )
        unresolved = tuple(
            record.Incident(f"{index:032x}", "a" * 32, f"{'b' * 64}:routine:{index:032x}", index)
            for index in range(record.MAX_UNRESOLVED_INCIDENTS)
        )
        full = dataclasses.replace(base, routines=(routine,), incidents=unresolved)
        self.assertFalse(record.incident_capacity(full))
        self.assertIsNone(record.claimable(full, 10))
        held = record.Run("c" * 32, "a" * 32, "held", 0, generation=f"{'b' * 64}:routine:{'c' * 32}")
        with self.assertRaisesRegex(record.RoutineStateError, "incident-limit"):
            routine_hold.settle_hold(dataclasses.replace(full, runs=(held,), incidents=unresolved * 2), "c" * 32, 1)

        def renamed(status: str) -> tuple[record.Incident, ...]:
            return tuple(
                dataclasses.replace(item, incident_id=f"{index + 100:032x}", status=status)
                for index, item in enumerate(unresolved * 2)
            )

        # A skipped incident whose cleanup is pending never gives way; a released one does, oldest first.
        with self.assertRaisesRegex(record.RoutineStateError, "incident-limit"):
            routine_hold.settle_hold(dataclasses.replace(full, runs=(held,), incidents=renamed("skipped")), "c" * 32, 1)
        settled = routine_hold.settle_hold(
            dataclasses.replace(full, runs=(held,), incidents=renamed("released")), "c" * 32, 1
        )
        self.assertEqual(len(settled.incidents), record.MAX_INCIDENTS)
        self.assertEqual(settled.incidents[-1].incident_id, "c" * 32)
        self.assertNotIn(f"{100:032x}", {item.incident_id for item in settled.incidents})
        pending = dataclasses.replace(base, routines=(routine,), incidents=renamed("skipped"))
        self.assertFalse(record.incident_capacity(pending))
        self.assertTrue(record.incident_capacity(dataclasses.replace(pending, incidents=renamed("released"))))
        with self.assertRaisesRegex(record.RoutineStateError, "incident-not-skipped"):
            routine_hold.release_incident(full, unresolved[0].incident_id)
        released = routine_hold.release_incident(
            routine_hold.skip_incident(full, unresolved[0].incident_id, 1, choice="run"), unresolved[0].incident_id
        )
        self.assertEqual(routine_hold.release_incident(released, unresolved[0].incident_id), released)
        with self.assertRaisesRegex(record.RoutineStateError, "run-not-held"):
            routine_hold.settle_hold(
                dataclasses.replace(full, runs=(dataclasses.replace(held, status="frozen"),)), "c" * 32, 1
            )
        with self.assertRaisesRegex(record.RoutineStateError, "incident-not-found"):
            routine_hold.skip_incident(full, "f" * 32, 1, choice="run")
        unbound = record.Run(
            "d" * 32, "a" * 32, "leased", 0, lease_sha256="1" * 64, lease_key="2" * 64, lease_expires_at=99
        )
        with self.assertRaisesRegex(record.RoutineStateError, "generation-invalid"):
            routine_hold.fence(
                dataclasses.replace(base, routines=(routine,), runs=(unbound,)),
                "d" * 32,
                record.Lease("1" * 64, "2" * 64),
                1,
            )


if __name__ == "__main__":
    unittest.main()
