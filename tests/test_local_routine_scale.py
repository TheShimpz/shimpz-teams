"""A Routine of 120 steps, one Action used 118 times with its own inputs, from its confirmed plan to its shown result.

Its plan pages, Brain listing, claim, execution through the real turn loop, notices, and run step records all stay
within the bounds the admission budget derives (ADR-0092 amendment, 2026-10-05, scale; ADR-0101).
"""

from __future__ import annotations

import dataclasses
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

from test_local_chat_scope import LOOKUP_RESULT
from test_local_routine_diagnostics import FAILURE
from test_local_routine_service import KEY, RoutineServiceCase, Runtime

from action import failure as action_failure
from chat import orchestrator as chat_orchestrator
from local.chat import segment as chat_segment
from local.errors import ApiProblemError
from local.routine import compiled as routine_compiled
from local.routine import contracts as routine_contracts
from local.routine import diagnostics as routine_diagnostics
from local.routine import human as routine_human
from local.routine import incident as routine_incident
from local.routine import manage as routine_manage
from local.routine import protection as routine_protection
from local.routine import run as routine_run
from local.routine import store as routine_store
from protocol.http.v1 import routine as http_routine
from routine import cursor as routine_cursor
from routine import definition as routine_definition
from routine import plan as routine_plan
from routine import record

ASSISTANT = "shimpz-cloudflare"
ZONE = "c" * 32
PAGES = range(1, 119)


def _literal(value: object) -> dict[str, object]:
    return {"kind": "literal", "value": value}


def _scaled_plan(service) -> dict[str, object]:
    """A plan of 120 steps: list-zones once per page, then the first zone's records by position and by its name."""
    _name, _network, active = service._team_assistants("team_1")
    contracts = routine_contracts.contracts(tuple(active.values()))
    pin = {action: contracts[(ASSISTANT, action)].pin for action in ("list-zones", "list-dns-records")}
    steps = [
        {
            "id": f"zones{page}",
            "assistant": ASSISTANT,
            "action": "list-zones",
            "pin": pin["list-zones"],
            "input": {"page": _literal(page), "per_page": _literal(5)},
        }
        for page in PAGES
    ]
    first = {"kind": "step_output", "step": "zones1", "pointer": "/zones/0/id"}
    named = {"kind": "step_output", "step": "zones1", "pointer": "/zones", "where": {"name": "example.com"}}
    for step_id, zone in (("records", first), ("again", {**named, "item": "/id"})):
        steps.append(
            {
                "id": step_id,
                "assistant": ASSISTANT,
                "action": "list-dns-records",
                "pin": pin["list-dns-records"],
                "input": {"zone_id": zone, "page": _literal(1), "per_page": _literal(5)},
            }
        )
    output = {"mode": "show", "step": "again", "when": None}
    return {"version": 3, "timezone": "UTC", "steps": steps, "output": output}


class ScaleJourneyTests(RoutineServiceCase):
    def test_a_120_step_routine_is_paged_run_and_shown_within_its_bounds(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def invoke(_team, _assistant, action, payload, _evidence):
            calls.append((action, dict(payload)))
            if action == "list-zones":
                return {"result": {**LOOKUP_RESULT, "zones": [{"id": ZONE, "name": "example.com"}]}}
            return {"result": {"records": [], "pagination": LOOKUP_RESULT["pagination"]}}

        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, Runtime())
            controller.assistant_lifecycle.invoke = invoke
            value = self.routine(service, plan=_scaled_plan(service), next_run_at=int(time.time()) + 3600)
            # Within its definition budget.
            self.assertLessEqual(routine_definition.definition_bytes(value), routine_plan.MAX_DEFINITION_BYTES)
            # The Brain's listing projects the plan: no step keeps its pin or any input's source, so a listing never
            # outgrows the plan bytes Team admitted, which the Brain's listing bound relies on.
            (listed,) = service._chat_routines("team_1")
            self.assertEqual((len(listed["steps"]), listed["daily_steps"]), (120, 120))
            self.assertLess(len(routine_plan.canonical(listed["steps"])), len(routine_plan.canonical(value.plan)))
            # Its plan reads page by page, every step once and in order.
            positions, offset = [], 0
            while offset is not None:
                page = service.routine_steps("team_1", value.routine_id, value.revision, offset)
                self.assertEqual(http_routine.canonical_page(page), page)
                self.assertEqual(page["total"], 120)
                positions.extend(step["position"] for step in page["steps"])
                offset = page["next"]
            self.assertEqual(positions, list(range(1, 121)))
            # A long run is never claimed while Admin already holds one; its active time grows with its steps.
            self._due(service, value)
            self.assertIsNone(service.claim_routine_run(False))
            claim = service.claim_routine_run(True)
            self.assertEqual(claim["active_seconds"], routine_plan.active_seconds(120))
            result = self.run_claim(service, claim)
            state = self.state(service)
            steps_view = self._run_steps(service, claim["run_id"])
        self.assertEqual(result["status"], "done")
        # One Action 118 times with its own inputs, then the first zone's records by position and by its name.
        self.assertEqual(
            [payload for _action, payload in calls[:118]], [{"page": page, "per_page": 5} for page in PAGES]
        )
        self.assertEqual(calls[118:], [("list-dns-records", {"zone_id": ZONE, "page": 1, "per_page": 5})] * 2)
        done = state.notices[-1]
        summary = done.detail["plan"]
        self.assertEqual(
            (done.outcome, summary["steps"], done.detail["output"]["step"], done.detail["decision"]),
            ("done", 120, 120, None),
        )
        self.assertEqual(summary["actions"], [[ASSISTANT, "list-zones", 118], [ASSISTANT, "list-dns-records", 2]])
        self.assertFalse(done.protection_lost)
        self.assertLessEqual(len(routine_store._encode(state, "team_1")), routine_store.MAX_STATE_BYTES)
        # The run's own records say what each step did with the inputs it was given, bound to its revision.
        self.assertEqual([step["status"] for step in steps_view], ["done"] * 120)
        self.assertEqual(
            steps_view[41]["inputs"],
            [
                {"member": "page", "source": "literal", "value": "42"},
                {"member": "per_page", "source": "literal", "value": "5"},
            ],
        )
        self.assertEqual(
            steps_view[119]["inputs"][2], {"member": "zone_id", "source": "step_output", "value": f'"{ZONE}"'}
        )

    def _due(self, service, value: record.Routine) -> None:
        service.routine_store.update(
            "team_1",
            lambda state: (
                record._replace_routine(
                    state,
                    dataclasses.replace(record.routine(state, value.routine_id), next_run_at=int(time.time()) - 1),
                ),
                None,
            ),
        )

    @staticmethod
    def _run_steps(service, run_id: str) -> list[dict[str, object]]:
        steps, offset, snapshot = [], 0, "latest"
        while offset is not None:
            page = service.routine_run_steps("team_1", run_id, snapshot, offset, int(time.time()))
            snapshot = page["snapshot"]
            steps.extend(page["steps"])
            offset = page["next"]
            ended = page["ended"]
        return steps if ended else []


class StepRecordEdgeTests(RoutineServiceCase):
    """A step or run record is a display record: what it cannot keep is audited and never changes the run."""

    def runtime(self, service, value: record.Routine, diagnostics_store, run_id: str = "e" * 32) -> object:
        network = service.assistant_lifecycle._network("team_1").id
        plan = routine_compiled._plan(service, "team_1", value)
        binding = routine_cursor.Binding(network, value.routine_id, value.revision, run_id)
        protections = service.routine_protections
        protections.bind(run_id)
        seal = routine_compiled._Seal("team_1", service.routine_store, diagnostics_store, protections, lambda: None)
        cursor = routine_cursor.start(plan, binding, 0, protections.boot)
        return routine_compiled.CompiledRuntime(seal, plan, cursor, "Done.")

    def test_a_record_too_large_keeps_no_inputs_and_one_that_cannot_be_kept_is_audited(self) -> None:
        kept: list[object] = []
        failing = mock.Mock(side_effect=routine_diagnostics.DiagnosticStoreError("down"))
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            value = self.routine(service)
            runtime = self.runtime(service, value, mock.Mock(record_step=lambda *args: kept.append(args)))
            runtime.record_step("done")
            self.assertEqual(kept, [])
            runtime._attempt = routine_compiled._Attempt(1, 0.0, {"page": 1, "per_page": 5}, ())
            with mock.patch.object(routine_compiled.http_routine, "MAX_STEP_VIEW_BYTES", 64):
                runtime.record_step("done")
            self.assertIsNone(kept[0][2].inputs)
            self.assertEqual(kept[0][2].position, {"phase": "replay", "step": 1})
            broken = self.runtime(service, value, mock.Mock(record_step=failing))
            broken._attempt = routine_compiled._Attempt(1, 0.0, {"page": 1, "per_page": 5}, ())
            with mock.patch.object(routine_compiled.local_audit, "record_request") as audited:
                broken.record_step("failed")
            self.assertEqual(audited.call_args.args[0], "routine-step-record")
            # A recovered step whose input can no longer be resolved withholds every preview, and has no duration.
            recovered: list[object] = []
            service.routine_diagnostics = mock.Mock(record_step=lambda *args: recovered.append(args[2]))
            cursor = runtime.cursor
            with mock.patch.object(
                routine_plan, "resolve", side_effect=routine_plan.PlanError("plan-reference-missing")
            ):
                routine_compiled.record_recovered(service, "team_1", cursor, runtime.plan, ())
            self.assertEqual((recovered[0].status, recovered[0].duration_ms), ("recovered", None))
            self.assertEqual({item["value"] for item in recovered[0].inputs}, {None})

    def test_a_terminal_record_is_kept_only_from_a_readable_snapshot_and_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            value = self.routine(service)
            runtime = self.runtime(service, value, service.routine_diagnostics)
            service.routine_store.put_cursor("team_1", runtime.cursor)
            snapshot = routine_incident.Recovery(runtime.cursor.binding, value.name, value.plan)
            recorded: list[object] = []
            service.routine_diagnostics = mock.Mock(record_run=lambda *args: recorded.append(args[2]))
            other = dataclasses.replace(snapshot, binding=dataclasses.replace(snapshot.binding, run_id="f" * 32))
            for refused in (None, other):
                routine_incident.seal_terminal(service, "team_1", "e" * 32, refused)
            with mock.patch.object(service.routine_store, "cursor", side_effect=routine_store.RoutineStoreError("x")):
                routine_incident.seal_terminal(service, "team_1", "e" * 32, snapshot)
            with mock.patch.object(service.routine_store, "cursor", return_value=None):
                routine_incident.seal_terminal(service, "team_1", "e" * 32, snapshot)
            moved = dataclasses.replace(runtime.cursor, plan="sha256:" + "0" * 64)
            with mock.patch.object(service.routine_store, "cursor", return_value=moved):
                routine_incident.seal_terminal(service, "team_1", "e" * 32, snapshot)
            self.assertEqual(recorded, [])
            routine_incident.seal_terminal(service, "team_1", "e" * 32, snapshot)
            terminal = recorded[0]
            self.assertEqual(
                (terminal.reached, terminal.dispatched, terminal.binding.total, terminal.calls, terminal.decision),
                (0, False, 1, 0, None),
            )
            service.routine_diagnostics = mock.Mock(
                record_run=mock.Mock(side_effect=routine_diagnostics.DiagnosticStoreError("down"))
            )
            with mock.patch.object(routine_incident.local_audit, "record_request") as audited:
                routine_incident.seal_terminal(service, "team_1", "e" * 32, snapshot)
            self.assertEqual(audited.call_args.args[0], "routine-run-record")
            # A recovery snapshot that cannot be read proves nothing, so none is kept.
            with mock.patch.object(service.routine_store, "recovery", return_value=b"not json"):
                self.assertIsNone(routine_manage._snapshot(service, "team_1", "e" * 32))
            self.assertIsNone(
                routine_manage._snapshot(SimpleNamespace(routine_store=service.routine_store), "team_1", "e" * 32)
            )
            self.assertIsInstance(runtime.cursor, routine_cursor.Cursor)

    def test_an_attempt_lasts_until_its_action_returns_and_a_cut_one_is_stopped(self) -> None:
        kept: list[object] = []
        operation = "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6"
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            value = self.routine(service)
            runtime = self.runtime(service, value, mock.Mock(record_step=lambda *args: kept.append(args[2])))
            # Nothing was dispatched yet: a failure names no step.
            at = routine_compiled._at(SimpleNamespace(runtime=runtime))
            self.assertEqual(at, {"position": None, "steps": None})
            request = runtime.start(None, "").actions[0]
            # The Action returned 500 ms after it started; sealing and projecting it later never counts.
            with mock.patch.object(routine_compiled, "_STEP_CLOCK", side_effect=[10.0, 10.5]):
                runtime.dispatching(request, operation)
                runtime.returned()
                runtime.returned()
                runtime.record_step("done")
            self.assertEqual((kept[-1].status, kept[-1].duration_ms), ("done", 500))
            at = routine_compiled._at(SimpleNamespace(runtime=runtime))
            self.assertEqual(at, {"position": {"phase": "replay", "step": 1}, "steps": 1})
            # Stop cutting the Action's call records it as stopped, never as failed.
            runtime.dispatching(request, operation)
            with mock.patch.object(routine_compiled.routine_diagnostics, "protected", return_value=()):
                runtime.failed(
                    request, mock.Mock(operation_id=operation), ApiProblemError(409, "x", code="chat-stopped")
                )
            self.assertEqual(kept[-1].status, "stopped")
            # A segment cut while an attempt is still in flight records that attempt as stopped too.
            claim = service.claim_routine_run()
            lease = record.lease_of(claim["lease_token"], KEY)
            run = routine_run._Run("team_1", claim["run_id"], lease, "token", "openai", value)
            # Stop raises out of the Action's call itself; its time still ends there, before the segment unwinds.
            runtime = self.runtime(service, value, mock.Mock(record_step=lambda *args: kept.append(args[2])))
            request = runtime.start(None, "").actions[0]

            def cut() -> object:
                raise chat_orchestrator.ChatStoppedError("chat turn stopped")

            with mock.patch.object(routine_compiled, "_STEP_CLOCK", side_effect=[20.0, 20.5]):
                runtime.dispatching(request, operation)
                with self.assertRaises(chat_orchestrator.ChatStoppedError):
                    chat_segment._observed(cut, runtime, request, None)
            stopped = ApiProblemError(409, "stopped", code="chat-stopped")
            segment = SimpleNamespace(runtime=runtime, batches=[])
            routine_compiled._ended(service, run, record.run(self.state(service), claim["run_id"]), segment, stopped)
            self.assertEqual((kept[-1].status, kept[-1].duration_ms), ("stopped", 500))
            self.assertIsNone(runtime._attempt)

    def test_a_run_frozen_far_into_its_plan_fails_at_that_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            value = self.routine(service, plan=_scaled_plan(service), next_run_at=int(time.time()) + 3600)
            frozen = record.Run(
                record.new_id(),
                value.routine_id,
                "frozen",
                0,
                request_kind="human",
                assistant_id=ASSISTANT,
                action="list-zones",
                position={"phase": "replay", "step": 37},
                steps=120,
            )
            service.routine_store.update("team_1", lambda state: (dataclasses.replace(state, runs=(frozen,)), None))
            routine_human._end_changed(service, "team_1", frozen)
            detail = self.state(service).notices[-1].detail
        self.assertEqual(
            (detail["code"], detail["position"], detail["steps"]),
            ("team-context-changed", {"phase": "replay", "step": 37}, 120),
        )

    def test_a_deleting_routine_shows_no_steps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            value = self.routine(service)
            service.routine_store.update(
                "team_1", lambda state: (record.begin_delete(state, value.routine_id)[0], None)
            )
            with self.assertRaises(ApiProblemError) as caught:
                service.routine_steps("team_1", value.routine_id, value.revision, 0)
        self.assertEqual(caught.exception.code, "routine-not-found")


class ProtectionLossTests(RoutineServiceCase):
    """A run keeps its protection only in the Team boot that bound it; once lost, nothing it produced is shown."""

    runtime = StepRecordEdgeTests.runtime

    def test_a_run_reopened_after_a_restart_has_lost_its_protection_for_good(self) -> None:
        kept: list[object] = []
        operation = "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6"
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            value = self.routine(service)
            claim = service.claim_routine_run()
            lease = record.lease_of(claim["lease_token"], KEY)
            routine_run._bind(service, "team_1", claim["run_id"], lease)
            run = record.run(self.state(service), claim["run_id"])
            fresh = routine_compiled.runtime(service, "team_1", run, value)
            self.assertEqual((fresh.protection().lost, fresh.cursor.protection_lost), (False, False))
            # A Team restart forgets every run's protection; the run reopened from its sealed cursor has lost it.
            service.routine_protections = routine_protection.RunProtections()
            service.routine_diagnostics = mock.Mock(record_step=lambda *args: kept.append(args[2]))
            reopened = routine_compiled.runtime(service, "team_1", run, value)
            self.assertTrue(reopened.cursor.protection_lost)
            self.assertTrue(record.run(self.state(service), claim["run_id"]).protection_lost)
            request = reopened.start(None, "").actions[0]
            reopened.dispatching(request, operation)
            turn = reopened.resume(None, {request.interrupt_id: LOOKUP_RESULT})
            # Protecting more never regains it, and the loss is sealed once.
            self.assertTrue(reopened.protect(("later",)).lost)
            sealed = service.routine_store.cursor("team_1", reopened.cursor.binding)
        self.assertEqual(turn.status, "completed")
        # The shown result is unavailable and keeps no digest, so a ``changes`` baseline never moves.
        self.assertEqual(
            reopened.cursor.shown,
            {"step": "zones", "output": routine_plan.output_state(1, "unavailable"), "digest": None},
        )
        self.assertEqual(sealed, reopened.cursor)
        self.assertEqual((kept[-1].status, kept[-1].inputs), ("done", None))

    def test_a_diagnostic_that_cannot_be_kept_is_audited_and_the_failure_goes_on(self) -> None:
        operation = "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6"
        down = routine_diagnostics.DiagnosticStoreError("down")
        store = mock.Mock(record=mock.Mock(side_effect=down), record_step=mock.Mock())
        problem = ApiProblemError(502, "failed", code="assistant-action-failed")
        problem.__cause__ = action_failure.ActionFailedError(action_failure.ActionFailure(**FAILURE))
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            value = self.routine(service)
            runtime = self.runtime(service, value, store)
            request = runtime.start(None, "").actions[0]
            runtime.dispatching(request, operation)
            with (
                mock.patch.object(routine_compiled.routine_diagnostics, "protected", return_value=()),
                mock.patch.object(routine_compiled.local_audit, "record_request") as audited,
            ):
                runtime.failed(request, mock.Mock(operation_id=operation), problem)
        diagnostic = store.record.call_args.args[2]
        self.assertEqual(
            (diagnostic.position, diagnostic.failure["http_status"]), ({"phase": "replay", "step": 1}, 404)
        )
        self.assertEqual(audited.call_args.args[0], "routine-diagnostic")
        self.assertEqual(store.record_step.call_args.args[2].status, "failed")
