"""Routine runs stay consistent when their endings, replays, and the watchdog interleave (ADR-0086)."""

from __future__ import annotations

import contextlib
import functools
import tempfile
import threading
import time
from unittest import mock

from test_local_routine_service import (
    ASSISTANT,
    KEY,
    RoutineServiceCase,
    Runtime,
    acting,
)

from action import challenges as action_challenges
from local import app as local_app
from local.routine import compiled as routine_compiled
from local.routine import contracts as routine_contracts
from local.routine import human as routine_human
from local.routine import run as routine_run
from local.routine import watchdog as routine_watchdog
from routine import claim as routine_claim
from routine import record
from routine import runs as routine_runs


def held_elsewhere(lock) -> bool:
    """Whether a thread other than a fresh probe holds ``lock``: the probe's non-blocking acquire fails exactly then."""
    acquired: list[bool] = []

    def probe() -> None:
        if lock.acquire(blocking=False):
            lock.release()
            acquired.append(True)

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join()
    return not acquired


class LockOrderTests(RoutineServiceCase):
    """Teardown takes the Team lifecycle lock and then the Routine lock; every Routine writer must do the same."""

    def observe_routine_updates(self, service) -> list[bool]:
        """Record, on entry to each Routine update callback, whether the Team lifecycle lock is already held."""
        lifecycle = service._lock("team_1")
        observed: list[bool] = []
        update = service.routine_store.update

        def ordered(team_id, change):
            def observed_change(state):
                observed.append(held_elsewhere(lifecycle))
                return change(state)

            return update(team_id, observed_change)

        patch = mock.patch.object(service.routine_store, "update", side_effect=ordered)
        patch.start()
        self.addCleanup(patch.stop)
        return observed

    def test_a_claim_takes_the_team_lifecycle_lock_before_the_routine_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.routine(service)
            observed = self.observe_routine_updates(service)
            self.assertIsNotNone(service.claim_routine_run())
            self.assertEqual(observed, [True])
            self.assertFalse(held_elsewhere(service._lock("team_1")))


class FrozenCase(RoutineServiceCase):
    def frozen(self, directory: str):
        _controller, service, claim = self.asking(directory)
        self.assertEqual(self.run_claim(service, claim)["status"], "frozen")
        return service, claim


class NoticeTests(FrozenCase):
    def test_a_frozen_run_is_announced_and_its_end_updates_the_same_notice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, claim = self.frozen(directory)
            notice = self.state(service).notices[-1]
            self.assertEqual(
                (notice.notice_id, notice.run_id, notice.outcome, notice.version),
                (claim["run_id"], claim["run_id"], "frozen", 1),
            )
            self.assertEqual(
                notice.detail,
                {
                    "request_kind": "human",
                    "assistant_id": ASSISTANT,
                    "action": "list-zones",
                    "position": {"phase": "replay", "step": 1},
                    "steps": 1,
                },
            )
            service.acknowledge_routine_notices(
                {"deliveries": [{"team_id": "team_1", "notice_id": claim["run_id"], "version": 1}]}
            )
            opened = service.open_routine_challenge("team_1", claim["run_id"], "en")
            self.answer_human(service, claim["run_id"], opened["challenge_id"], "deny")
            (ended,) = self.state(service).notices
            self.assertEqual((ended.notice_id, ended.outcome, ended.version), (claim["run_id"], "denied", 2))
            self.assertEqual(self.state(service).discards, ())
            self.assertEqual(service.routine_store.continuations("team_1"), ())


class EndingRaceTests(FrozenCase):
    def test_a_deleting_routine_never_resumes_its_frozen_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, claim = self.frozen(directory)
            opened = service.open_routine_challenge("team_1", claim["run_id"], "en")
            service.routine_store.update("team_1", lambda state: record.begin_delete(state, claim["routine_id"]))
            with self.assertRaises(local_app.ApiProblem) as caught:
                self.answer_human(service, claim["run_id"], opened["challenge_id"])
            self.assertEqual(caught.exception.code, "routine-run-not-frozen")
            self.assertEqual(record.run(self.state(service), claim["run_id"]).status, "frozen")

    def test_an_ending_decided_on_a_frozen_read_never_lands_on_a_resumed_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, claim = self.frozen(directory)
            opened = service.open_routine_challenge("team_1", claim["run_id"], "en")
            snapshot = record.run(self.state(service), claim["run_id"])
            now = int(time.time())
            service.routine_store.update("team_1", lambda state: routine_runs.thaw(state, claim["run_id"], now, 0))
            with self.assertRaises(local_app.ApiProblem) as changed:
                routine_human._end_changed(service, "team_1", snapshot)
            with (
                mock.patch.object(
                    routine_human,
                    "_frozen",
                    return_value=(snapshot, record.routine(self.state(service), claim["routine_id"])),
                ),
                self.assertRaises(local_app.ApiProblem) as denied,
            ):
                self.answer_human(service, claim["run_id"], opened["challenge_id"], "deny")
            self.assertEqual((changed.exception.code, denied.exception.code), ("routine-run-not-frozen",) * 2)
            resumed = record.run(self.state(service), claim["run_id"])
            self.assertEqual((resumed.status, resumed.generation), ("leased", snapshot.generation))
            self.assertEqual(self.state(service).discards, ())


class ExecutionBoundTests(RoutineServiceCase):
    def test_a_contract_changed_since_the_claim_never_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(acting())
            _controller, service = self.service(directory, runtime)
            self.routine(service)
            claim = service.claim_routine_run()
            with mock.patch.object(
                routine_contracts, "current_contracts", return_value={ASSISTANT: "sha256:" + "0" * 64}
            ):
                self.assertEqual(self.run_claim(service, claim)["status"], "failed")
            self.assertEqual(
                self.state(service).notices[-1].detail,
                {"code": "team-context-changed", "actions": [], "position": None, "steps": None},
            )
            self.assertEqual(runtime.contexts, [])

    def test_a_segment_out_of_active_time_is_stopped_and_ends_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.routine(service)
            claim = service.claim_routine_run()
            routine_run.register_routine_run(service, "team_1", claim["run_id"], "token", 0)
            routine_watchdog.check(service)
            self.assertIn("token", service._cancelled_chat_tokens)
            stopped = local_app.ApiProblem(409, "stopped", code="chat-stopped")
            lease = record.lease_of(claim["lease_token"], KEY)
            run = routine_run._Run("team_1", claim["run_id"], lease, "token", "openai", self.state(service).routines[0])
            # Nothing was dispatched, so a stop that ran out of active time fails the run instead of holding it.
            value = record.run(self.state(service), claim["run_id"])
            outcome = routine_compiled._ended(service, run, value, None, stopped)
            self.assertEqual(outcome, "failed")
            self.assertEqual(
                self.state(service).notices[-1].detail,
                {"code": "active-time-exceeded", "actions": [], "position": None, "steps": None},
            )


class WatchdogRaceTests(RoutineServiceCase):
    def bound(self, controller, service):
        """A claimed run whose generation is bound, and a journal read during which its worker finishes."""
        claim = service.claim_routine_run()
        network = controller.assistant_lifecycle._network("team_1").id
        lease = record.lease_of(claim["lease_token"], KEY)
        now = int(time.time())
        service.routine_store.update(
            "team_1", lambda state: (routine_claim.bind_generation(state, claim["run_id"], lease, now, network), None)
        )

        def finish(state: record.TeamRoutines) -> tuple[record.TeamRoutines, None]:
            if not any(item.run_id == claim["run_id"] for item in state.runs):
                return state, None
            return routine_runs.end(state, claim["run_id"], now, "stopped", {"actions": []}), None

        def worker_finishes(_generation):
            # The first journal read the pass makes finds the worker finishing; any later read finds it ended.
            service.routine_store.update("team_1", finish)

        return claim, worker_finishes

    def test_recovery_leaves_a_run_that_ended_since_the_pass_read_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, Runtime())
            self.routine(service)
            claim, worker_finishes = self.bound(controller, service)
            snapshot = record.run(self.state(service), claim["run_id"])
            with mock.patch.object(service.action_state, "current_batch", side_effect=worker_finishes):
                self.assertIsNone(routine_watchdog._recover(service, "team_1", snapshot))
            self.assertEqual(self.state(service).notices[-1].outcome, "stopped")

    def test_a_pass_after_a_restart_leaves_a_run_that_ended_meanwhile_and_finishes_deletions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, Runtime())
            value = self.routine(service)
            claim, worker_finishes = self.bound(controller, service)
            routine_run.register_routine_run(service, "team_1", claim["run_id"], "token", 600)
            self.assertFalse(service.delete_routine("team_1", value.routine_id)["deleted"])
            routine_run.unregister_routine_run(service, claim["run_id"])
            with mock.patch.object(service.action_state, "current_batch", side_effect=worker_finishes):
                routine_watchdog.check(service, startup=True)
            self.assertEqual(self.state(service).routines, ())
            self.assertEqual(self.state(service).discards, ())


class StopBeforeRegistrationTests(FrozenCase):
    def test_stop_and_deletion_end_a_claimed_run_before_its_worker_starts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(acting())
            _controller, service = self.service(directory, runtime)
            self.routine(service)
            claim = service.claim_routine_run()
            self.assertTrue(service.stop_routine("team_1", claim["run_id"])["stopped"])
            with self.assertRaises(local_app.ApiProblem) as late:
                self.run_claim(service, claim)
            self.assertEqual(late.exception.code, "routine-lease-invalid")
            value = self.routine(service)
            claim = service.claim_routine_run()
            self.assertTrue(service.delete_routine("team_1", value.routine_id)["deleted"])
            self.assertEqual(runtime.contexts, [])
            self.assertEqual(self.state(service).discards, ())

    def test_a_worker_registering_while_stop_ends_its_run_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(acting())
            _controller, service = self.service(directory, runtime)
            self.routine(service)
            claim = service.claim_routine_run()
            service._routine_halting.add(claim["run_id"])
            with self.assertRaises(local_app.ApiProblem) as fenced:
                self.run_claim(service, claim)
            self.assertEqual(fenced.exception.code, "chat-stopped")
            self.assertEqual(runtime.contexts, [])
            self.assertEqual(record.run(self.state(service), claim["run_id"]).generation, "")

    def test_halting_a_run_that_is_no_longer_leased_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, claim = self.frozen(directory)
            self.assertFalse(routine_run.halt_routine_run(service, "team_1", claim["run_id"]))
            self.assertEqual(record.run(self.state(service), claim["run_id"]).status, "frozen")
            self.assertEqual(service._routine_halting, set())

    def test_a_stop_while_a_replay_is_admitted_ends_the_frozen_run_before_it_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, claim = self.frozen(directory)
            opened = service.open_routine_challenge("team_1", claim["run_id"], "en")
            admit = routine_human._current_context

            def stopped_meanwhile(*args):
                admit(*args)
                self.assertTrue(service.stop_routine("team_1", claim["run_id"])["stopped"])

            with (
                mock.patch.object(routine_human, "_current_context", side_effect=stopped_meanwhile),
                self.assertRaises(local_app.ApiProblem) as caught,
            ):
                self.answer_human(service, claim["run_id"], opened["challenge_id"])
            # Stop withdrew the challenge with the run, so the answer finds nothing to consume and nothing resumes.
            self.assertEqual(caught.exception.code, "human-request-expired")
            self.assertEqual(self.state(service).runs, ())
            self.assertEqual(self.state(service).notices[-1].outcome, "stopped")


class ChallengeEndingRaceTests(FrozenCase):
    """Opening a frozen run's challenge and ending that run serialize, and cancelling never reaches another run's."""

    @contextlib.contextmanager
    def ending_meanwhile(self, service, end):
        """Run ``end`` in a second thread started right after an opening's continuation read, then join it.

        The read returns only once that thread finished or is about to wait on the Team lifecycle lock, so the opening
        either sees the ending done or holds the lock it waits on. Yields the ending's results.
        """
        paused = threading.Event()
        results: list[object] = []

        def ending() -> None:
            try:
                results.append(end())
            finally:
                paused.set()

        thread = threading.Thread(target=ending)
        lifecycle = service._lock

        @contextlib.contextmanager
        def lock(team_id):
            if threading.current_thread() is thread:
                paused.set()
            with lifecycle(team_id):
                yield

        decoded = routine_human._decoded

        def read(*args):
            value = decoded(*args)
            thread.start()
            self.assertTrue(paused.wait(10))
            return value

        with mock.patch.object(service, "_lock", lock), mock.patch.object(routine_human, "_decoded", read):
            yield results
        thread.join(10)

    def test_a_run_ended_while_its_challenge_opens_never_keeps_a_challenge(self) -> None:
        endings = {
            "stop": lambda service, claim: service.stop_routine("team_1", claim["run_id"])["stopped"],
            "delete": lambda service, claim: service.delete_routine("team_1", claim["routine_id"])["deleted"],
        }
        for name, end in endings.items():
            with self.subTest(ending=name), tempfile.TemporaryDirectory() as directory:
                service, claim = self.frozen(directory)
                with (
                    self.ending_meanwhile(service, functools.partial(end, service, claim)) as results,
                    contextlib.suppress(local_app.ApiProblem),
                ):
                    service.open_routine_challenge("team_1", claim["run_id"], "en")
                self.assertEqual(results, [True])
                self.assertIsNone(service.current_routine_challenge("team_1"))
                # The run's own notice says stopped; a deletion's notice closes the timeline after it.
                ended = next(item for item in self.state(service).notices if item.run_id == claim["run_id"])
                self.assertEqual(ended.outcome, "stopped")
                self.assertEqual(self.state(service).runs, ())

    def test_cancelling_a_run_challenge_never_cancels_its_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, claim = self.frozen(directory)
            service.open_routine_challenge("team_1", claim["run_id"], "en")
            store = service.routine_human_challenges
            current = store.current
            replacements: list[action_challenges.PendingHumanChallenge] = []

            def replaced(team_id):
                observed = current(team_id)
                # Another run's opening replaces the observed challenge before the cancellation reaches the store.
                store.cancel_team(team_id)
                replacements.append(store.create(team_id, observed.requirement, ("other", observed.payload[1])))
                return observed

            with mock.patch.object(store, "current", side_effect=replaced):
                routine_human.cancel_routine_challenge(service, "team_1", claim["run_id"])
            self.assertEqual(store.current("team_1"), replacements[0])


class FreezeRaceTests(RoutineServiceCase):
    """A run reaching its pause as a Stop or a deletion reaches it is never left frozen behind either."""

    def committing(self, service, during, after):
        """Run ``during`` inside the freeze's terminal commit, before what it commits, and ``after`` once it returns."""
        commit = service._commit_chat_terminal

        def committed(team_id, token, before_commit=lambda: None):
            def before() -> None:
                during()
                before_commit()

            done = commit(team_id, token, before)
            after()
            return done

        return mock.patch.object(service, "_commit_chat_terminal", committed)

    def test_a_stop_reaching_a_run_as_it_freezes_ends_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.asking(directory)
            stops: list[dict[str, object]] = []
            with self.committing(
                service, lambda: None, lambda: stops.append(service.stop_routine("team_1", claim["run_id"]))
            ):
                self.run_claim(service, claim)
            self.assertTrue(stops[0]["stopped"])
            self.assertEqual(self.state(service).runs, ())
            self.assertEqual(self.state(service).notices[-1].outcome, "stopped")
            self.assertEqual(service.routine_store.continuations("team_1"), ())

    def test_a_routine_deleted_as_its_run_freezes_ends_the_run_and_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.asking(directory)

            def deleting() -> None:
                service.routine_store.update("team_1", lambda state: record.begin_delete(state, claim["routine_id"]))

            with self.committing(service, deleting, lambda: None):
                self.assertEqual(self.run_claim(service, claim)["status"], "stopped")
            self.assertEqual((self.state(service).runs, self.state(service).routines), ((), ()))
            self.assertEqual(service.routine_store.continuations("team_1"), ())

    def test_a_run_freezing_while_stop_halts_it_is_ended_by_that_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.asking(directory)
            commit, halt = service._commit_chat_terminal, routine_run.halt_routine_run
            committed: list[bool] = []
            stops: list[dict[str, object]] = []

            def stopping(team_id, token, before_commit=lambda: None):
                # Stop reads the run leased; the freeze commits just before Stop's cancellation reaches the segment.
                def halt_after_freeze(*args):
                    committed.append(commit(team_id, token, before_commit))
                    return halt(*args)

                with mock.patch.object(routine_run, "halt_routine_run", side_effect=halt_after_freeze):
                    stops.append(service.stop_routine("team_1", claim["run_id"]))
                return committed[0]

            with mock.patch.object(service, "_commit_chat_terminal", stopping):
                self.run_claim(service, claim)
            self.assertEqual((committed, stops[0]["stopped"]), ([True], True))
            self.assertEqual(self.state(service).runs, ())
            self.assertEqual(self.state(service).notices[-1].outcome, "stopped")
            self.assertEqual(service.routine_store.continuations("team_1"), ())
