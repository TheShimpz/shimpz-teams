"""Routine runs stay consistent when their endings, replays, and the watchdog interleave (ADR-0086)."""

from __future__ import annotations

import tempfile
import time
from unittest import mock

from test_local_routine_service import (
    API_KEY,
    ASSISTANT,
    KEY,
    RoutineServiceCase,
    Runtime,
    acting,
    approval,
)

from action import human as action_human
from local import app as local_app
from local.chat.segment import RoutineSegment
from local.routine import human as routine_human
from local.routine import run as routine_run
from local.routine import turn as routine_turn
from local.routine import watchdog as routine_watchdog
from routine import record


class FrozenCase(RoutineServiceCase):
    def frozen(self, directory: str):
        controller, service = self.service(directory, Runtime(acting()))

        def invoke(*_args):
            raise action_human.HumanRequestSuspensionError(approval())

        controller.assistant_lifecycle.invoke = invoke
        self.routine(service)
        claim = service.claim_routine_run()
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
                notice.detail, {"request_kind": "human", "assistant_id": ASSISTANT, "action": "list-zones"}
            )
            service.acknowledge_routine_notices(
                {"deliveries": [{"team_id": "team_1", "notice_id": claim["run_id"], "version": 1}]}
            )
            opened = service.open_routine_challenge("team_1", claim["run_id"])
            service.resume_routine_human(
                "team_1",
                claim["run_id"],
                {"challenge_id": opened["challenge_id"], "decision": "deny"},
                "openai",
                API_KEY,
            )
            (ended,) = self.state(service).notices
            self.assertEqual((ended.notice_id, ended.outcome, ended.version), (claim["run_id"], "denied", 2))
            self.assertEqual(self.state(service).discards, ())
            self.assertEqual(service.routine_store.continuations("team_1"), ())


class EndingRaceTests(FrozenCase):
    def test_a_deleting_routine_never_resumes_its_frozen_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, claim = self.frozen(directory)
            opened = service.open_routine_challenge("team_1", claim["run_id"])
            service.routine_store.update("team_1", lambda state: record.begin_delete(state, claim["routine_id"]))
            with self.assertRaises(local_app.ApiProblem) as caught:
                service.resume_routine_human(
                    "team_1",
                    claim["run_id"],
                    {"challenge_id": opened["challenge_id"], "decision": "submit", "value": True},
                    "openai",
                    API_KEY,
                )
            self.assertEqual(caught.exception.code, "routine-run-not-frozen")
            self.assertEqual(record.run(self.state(service), claim["run_id"]).status, "frozen")

    def test_an_ending_decided_on_a_frozen_read_never_lands_on_a_resumed_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, claim = self.frozen(directory)
            snapshot = record.run(self.state(service), claim["run_id"])
            now = int(time.time())
            service.routine_store.update("team_1", lambda state: record.thaw(state, claim["run_id"], now))
            with self.assertRaises(local_app.ApiProblem) as caught:
                routine_human._end_changed(service, "team_1", snapshot, "denied", "denied")
            self.assertEqual(caught.exception.code, "routine-run-not-frozen")
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
            with mock.patch.object(routine_turn, "current_contracts", return_value={ASSISTANT: "sha256:" + "0" * 64}):
                self.assertEqual(self.run_claim(service, claim)["status"], "failed")
            self.assertEqual(self.state(service).notices[-1].detail, {"code": "team-context-changed", "actions": []})
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
            outcome = routine_run._failed(
                service, "team_1", claim["run_id"], RoutineSegment(claim["run_id"], ""), stopped
            )
            self.assertEqual(outcome, "failed")
            self.assertEqual(self.state(service).notices[-1].detail, {"code": "active-time-exceeded", "actions": []})


class WatchdogRaceTests(RoutineServiceCase):
    def bound(self, controller, service):
        """A claimed run whose generation is bound, and a journal read during which its worker finishes."""
        claim = service.claim_routine_run()
        network = controller.assistant_lifecycle._network("team_1").id
        lease = record.lease_of(claim["lease_token"], KEY)
        now = int(time.time())
        service.routine_store.update(
            "team_1", lambda state: (record.bind_generation(state, claim["run_id"], lease, now, network), None)
        )

        def worker_finishes(_generation):
            service.routine_store.update(
                "team_1", lambda state: (record.end(state, claim["run_id"], now, "stopped", {"actions": []}), None)
            )

        return claim, worker_finishes

    def test_recovery_leaves_a_run_that_ended_since_the_pass_read_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, Runtime())
            self.routine(service)
            claim, worker_finishes = self.bound(controller, service)
            snapshot = record.run(self.state(service), claim["run_id"])
            with mock.patch.object(service.action_state, "uncertain_fingerprint", side_effect=worker_finishes):
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
            with mock.patch.object(service.action_state, "uncertain_fingerprint", side_effect=worker_finishes):
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
            opened = service.open_routine_challenge("team_1", claim["run_id"])
            admit = routine_human._current_context

            def stopped_meanwhile(*args):
                admit(*args)
                self.assertTrue(service.stop_routine("team_1", claim["run_id"])["stopped"])

            with (
                mock.patch.object(routine_human, "_current_context", side_effect=stopped_meanwhile),
                self.assertRaises(local_app.ApiProblem) as caught,
            ):
                service.resume_routine_human(
                    "team_1",
                    claim["run_id"],
                    {"challenge_id": opened["challenge_id"], "decision": "submit", "value": True},
                    "openai",
                    API_KEY,
                )
            self.assertEqual(caught.exception.code, "routine-run-not-frozen")
            self.assertEqual(self.state(service).runs, ())
            self.assertEqual(self.state(service).notices[-1].outcome, "stopped")
