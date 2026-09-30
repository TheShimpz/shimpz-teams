"""Every Routine failure path fails closed and leaves nothing a later run could misuse (ADR-0086)."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import time
from http import HTTPStatus
from types import SimpleNamespace
from unittest import mock

from test_local_routine_service import (
    API_KEY,
    ASSISTANT,
    KEY,
    LIST,
    RoutineServiceCase,
    Runtime,
    acting,
    approval,
    completed,
)

from action import human as action_human
from inference import client as brain_runtime_client
from inference import config as inference_config
from local import app as local_app
from local import authority as local_authority
from local.routine import manage as routine_manage
from local.routine import run as routine_run
from local.routine import state as routine_state
from local.routine import store as routine_store
from local.routine import watchdog as routine_watchdog
from routine import record


def broken(*_args, **_kwargs):
    raise routine_store.RoutineStoreError("down")


class StateAccessTests(RoutineServiceCase):
    def test_every_store_failure_is_one_retryable_problem(self) -> None:
        service = SimpleNamespace(routine_store=SimpleNamespace(load=broken, update=broken))
        for call in (
            lambda: routine_state.load(service, "team_1"),
            lambda: routine_state.update(service, "team_1", lambda state: (state, None)),
            lambda: routine_state.call(broken),
        ):
            with self.subTest(call=call), self.assertRaises(local_app.ApiProblem) as caught:
                call()
            self.assertEqual((caught.exception.status, caught.exception.code), (503, "routine-state-unavailable"))
        self.assertEqual(routine_state.call(lambda: "ok"), "ok")


class RunFaultTests(RoutineServiceCase):
    def paused(self, directory: str, request: action_human.HumanRequest | None = None, *turns):
        controller, service = self.service(directory, Runtime(acting(), *turns))
        suspended = request or approval()

        def invoke(*_args):
            raise action_human.HumanRequestSuspensionError(suspended)

        controller.assistant_lifecycle.invoke = invoke
        self.routine(service)
        claim = service.claim_routine_run()
        return controller, service, claim

    def test_an_unanswerable_authentication_ends_the_run_instead_of_freezing(self) -> None:
        descriptor = {"kind": "auth:totp", "ordinal": 0, "title": "Confirm", "description": "Confirm identity."}
        descriptor["fingerprint"] = action_human._fingerprint(descriptor)
        totp = action_human.validate_request(descriptor, ("auth:totp",))
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.paused(directory, totp)
            result = self.run_claim(service, claim)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(self.state(service).notices[-1].detail, {"code": "request-unavailable", "actions": []})
            self.assertEqual(service.routine_store.continuations("team_1"), ())

    def test_a_freeze_that_stop_wins_or_that_cannot_be_recorded_keeps_no_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.paused(directory)
            with mock.patch.object(service, "_commit_chat_terminal", return_value=False):
                self.assertEqual(self.run_claim(service, claim)["status"], "stopped")
            self.assertEqual(service.routine_store.continuations("team_1"), ())
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.paused(directory)
            with mock.patch.object(record, "freeze", side_effect=record.RoutineStateError("frozen-limit")):
                self.assertEqual(self.run_claim(service, claim)["status"], "failed")
            self.assertEqual(self.state(service).notices[-1].detail["code"], "freeze-unavailable")
            self.assertEqual(service.routine_store.continuations("team_1"), ())

    def test_a_missing_integration_freezes_the_run_and_a_resume_continues_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, Runtime(acting(), completed("Connected and listed.")))
            controller.assistant_integrations.delete_assistant("team_1", ASSISTANT)
            self.routine(service)
            claim = service.claim_routine_run()
            self.assertEqual(self.run_claim(service, claim)["status"], "frozen")
            frozen = record.run(self.state(service), claim["run_id"])
            self.assertEqual(frozen.request_kind, "integrations")
            self.assertEqual(
                service.open_routine_challenge("team_1", claim["run_id"])["status"], "integrations-required"
            )
            with self.assertRaises(local_app.ApiProblem) as provider:
                service.resume_routine_integrations("team_1", claim["run_id"], "anthropic", API_KEY)
            self.assertEqual(provider.exception.code, "inference-provider-mismatch")
            # Still missing: the replay pauses again and the run freezes again.
            again = service.resume_routine_integrations("team_1", claim["run_id"], "openai", API_KEY)
            self.assertEqual(again["status"], "frozen")

    def test_a_completion_stop_wins_and_a_lease_that_ran_out_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # The same Action twice is named once.
            second = dataclasses.replace(LIST, interrupt_id="action-2")
            controller, service = self.service(directory, Runtime(acting(), acting(second), completed()))
            controller.assistant_lifecycle.invoke = lambda *_args: {"result": {"zones": []}}
            self.routine(service)
            claim = service.claim_routine_run()
            with mock.patch.object(service, "_commit_chat_terminal", return_value=False):
                self.assertEqual(self.run_claim(service, claim)["status"], "stopped")
            self.assertEqual(self.state(service).notices[-1].detail, {"actions": [[ASSISTANT, "list-zones"]]})
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime(completed()))
            self.routine(service)
            claim = service.claim_routine_run()
            with mock.patch.object(record, "finish", side_effect=record.RoutineStateError("lease-invalid")):
                self.assertEqual(self.run_claim(service, claim)["status"], "failed")
            self.assertEqual(self.state(service).notices[-1].detail["code"], "lease-expired")

    def test_a_skill_save_failure_fails_closed_and_a_lease_lost_before_binding_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            second = dataclasses.replace(LIST, interrupt_id="action-2")
            controller, service = self.service(directory, Runtime(acting(), acting(second), completed()))
            controller.assistant_lifecycle.invoke = lambda *_args: {"result": {"zones": []}}
            self.routine(service)
            claim = service.claim_routine_run()
            with (
                mock.patch.object(
                    service.inference_store, "apply_knowledge", side_effect=inference_config.InferenceConfigError("x")
                ),
                self.assertRaises(local_app.ApiProblem) as caught,
            ):
                self.run_claim(service, claim)
            self.assertEqual(caught.exception.code, "memory-store-failed")
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime(completed()))
            self.routine(service)
            claim = service.claim_routine_run()
            with (
                mock.patch.object(record, "bind_generation", side_effect=record.RoutineStateError("lease-invalid")),
                self.assertRaises(local_app.ApiProblem) as lost,
            ):
                self.run_claim(service, claim)
            self.assertEqual(lost.exception.code, "routine-lease-invalid")
            with mock.patch.object(record, "spend", side_effect=record.RoutineStateError("run-not-running")):
                routine_run._spend(service, "team_1", claim["run_id"], record.lease_of(claim["lease_token"], KEY), 1)

    def test_stopping_a_running_run_aborts_its_brain_request_and_fail_stops_its_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, Runtime())
            abort = mock.Mock()
            container = object()
            routine_run.register_routine_run(service, "team_1", "f" * 32, "token", 600)
            service._brain_aborts["token"] = abort
            service._active_action_containers["team_1"] = ("token", container)
            with mock.patch.object(controller.assistant_lifecycle, "_fail_stop_action") as fail_stop:
                self.assertTrue(routine_run.stop_routine_run(service, "team_1", "f" * 32))
            abort.abort.assert_called_once_with()
            fail_stop.assert_called_once_with(container)
            self.assertFalse(routine_run.stop_routine_run(service, "team_2", "f" * 32))
            self.assertFalse(routine_run.stop_routine_run(service, "team_1", "0" * 32))


class FrozenFaultTests(RoutineServiceCase):
    def frozen(self, directory: str, *turns):
        controller, service = self.service(directory, Runtime(acting(), *turns))
        calls: list[object] = []

        def invoke(*_args):
            calls.append(None)
            if len(calls) == 1:
                raise action_human.HumanRequestSuspensionError(approval())
            raise local_app.ApiProblem(HTTPStatus.BAD_GATEWAY, "failed", code="assistant-rpc-failed")

        controller.assistant_lifecycle.invoke = invoke
        self.routine(service)
        claim = service.claim_routine_run()
        self.assertEqual(self.run_claim(service, claim)["status"], "frozen")
        return controller, service, claim

    def test_a_failure_after_a_human_answer_is_held_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.frozen(directory)
            opened = service.open_routine_challenge("team_1", claim["run_id"])
            resumed = service.resume_routine_human(
                "team_1",
                claim["run_id"],
                {"challenge_id": opened["challenge_id"], "decision": "submit", "value": True},
                "openai",
                API_KEY,
            )
            self.assertEqual(resumed["status"], "uncertain")
            # The Supervisor who resolves it sees the Actions whose effects are unknown.
            (held,) = service.list_routines("team_1")["runs"]
            self.assertEqual((held["status"], held["actions"]), ("uncertain", [[ASSISTANT, "list-zones"]]))

    def test_deleting_a_routine_stops_its_frozen_run_and_the_watchdog_keeps_a_frozen_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.frozen(directory)
            routine_watchdog.check(service)
            self.assertEqual(service.routine_store.continuations("team_1"), (claim["run_id"],))
            self.assertTrue(service.delete_routine("team_1", claim["routine_id"])["deleted"])
            self.assertEqual(service.routine_store.continuations("team_1"), ())
            self.assertEqual(self.state(service).notices[-1].outcome, "stopped")

    def test_a_corrupt_continuation_or_a_vanished_team_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.frozen(directory)
            for blob in (
                b"not json",
                json.dumps({"kind": "human", "bindings": [], "payload": "@@"}).encode(),
                json.dumps({"kind": "human", "bindings": [], "payload": "e30="}).encode(),
            ):
                service.routine_store.put_continuation("team_1", claim["run_id"], blob)
                with self.subTest(blob=blob), self.assertRaises(local_app.ApiProblem) as caught:
                    service.open_routine_challenge("team_1", claim["run_id"])
                self.assertEqual(caught.exception.code, "routine-state-unavailable")
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.frozen(directory)
            with (
                mock.patch.object(
                    service, "_chat_setup", side_effect=local_app.ApiProblem(409, "gone", code="assistant-unavailable")
                ),
                self.assertRaises(local_app.ApiProblem) as changed,
            ):
                service.open_routine_challenge("team_1", claim["run_id"])
            self.assertEqual(changed.exception.code, "team-context-changed")

    def test_an_answer_must_match_its_own_run_and_only_a_frozen_run_takes_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.frozen(directory)
            opened = service.open_routine_challenge("team_1", claim["run_id"])
            challenge = service.routine_human_challenges.current("team_1")
            object.__setattr__(challenge, "payload", ("0" * 32, challenge.payload[1]))
            with self.assertRaises(local_app.ApiProblem) as other:
                service.resume_routine_human(
                    "team_1",
                    claim["run_id"],
                    {"challenge_id": opened["challenge_id"], "decision": "deny"},
                    "openai",
                    API_KEY,
                )
            self.assertEqual(other.exception.code, "human-request-expired")
            self.routine(service)
            leased = service.claim_routine_run()
            with self.assertRaises(local_app.ApiProblem) as not_frozen:
                service.open_routine_challenge("team_1", leased["run_id"])
            self.assertEqual(not_frozen.exception.code, "routine-run-not-frozen")


class ManageAndNoticeFaultTests(RoutineServiceCase):
    def test_deleting_a_routine_ends_each_kind_of_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, Runtime())
            value = self.routine(service)
            claim = service.claim_routine_run()
            routine_run.register_routine_run(service, "team_1", claim["run_id"], "token", 600)
            deleting = service.delete_routine("team_1", value.routine_id)
            self.assertFalse(deleting["deleted"])
            self.assertEqual(service.list_routines("team_1")["runs"][0]["status"], "leased")
            self.assertTrue(record.routine(self.state(service), value.routine_id).deleting)
            # The run's own end completes the deletion.
            service.routine_store.update(
                "team_1",
                lambda state: (record.end(state, claim["run_id"], int(time.time()), "stopped", {"actions": []}), None),
            )
            self.assertTrue(routine_manage.complete_deletion(service, "team_1", value.routine_id))
            self.assertTrue(routine_manage.complete_deletion(service, "team_1", value.routine_id))
            uncertain = self.routine(service)
            claim = service.claim_routine_run()
            network = controller.assistant_lifecycle._network("team_1").id
            lease = record.lease_of(claim["lease_token"], KEY)
            service.routine_store.update(
                "team_1",
                lambda state: (record.bind_generation(state, claim["run_id"], lease, int(time.time()), network), None),
            )
            service.routine_store.update(
                "team_1",
                lambda state: (
                    record.hold_uncertain(state, claim["run_id"], lease, int(time.time()), "d" * 64, {"actions": []}),
                    None,
                ),
            )
            # Only the Supervisor's resolution of that exact batch releases an uncertain run; deletion never does.
            with self.assertRaises(local_app.ApiProblem) as held:
                service.delete_routine("team_1", uncertain.routine_id)
            self.assertEqual(held.exception.code, "routine-run-uncertain")
            self.assertFalse(record.routine(self.state(service), uncertain.routine_id).deleting)
            service.resolve_routine_run("team_1", claim["run_id"], {"batch_fingerprint": "d" * 64})
            self.assertTrue(service.delete_routine("team_1", uncertain.routine_id)["deleted"])
            self.assertEqual(self.state(service).discards, ())

    def test_run_state_that_cannot_be_removed_stays_queued_and_holds_back_new_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, Runtime())
            self.routine(service)
            claim = service.claim_routine_run()
            network = controller.assistant_lifecycle._network("team_1").id
            lease = record.lease_of(claim["lease_token"], KEY)
            now = int(time.time())
            service.routine_store.update(
                "team_1", lambda state: (record.bind_generation(state, claim["run_id"], lease, now, network), None)
            )
            service.routine_store.update(
                "team_1",
                lambda state: (record.end(state, claim["run_id"], now, "stopped", {"actions": []}), None),
            )
            self.routine(service)
            down = brain_runtime_client.BrainRuntimeError("down")
            with mock.patch.object(service.brain_runtime, "delete_thread", side_effect=down):
                with self.assertRaises(local_app.ApiProblem) as caught:
                    routine_manage.drain(service, "team_1")
                self.assertEqual(caught.exception.code, "routine-state-unavailable")
                self.assertEqual(len(self.state(service).discards), 1)
                self.assertIsNone(service.claim_routine_run())
                # The watchdog's pass fails the same way; its loop audits it and retries later.
                with self.assertRaises(local_app.ApiProblem):
                    routine_watchdog.check(service)
            self.assertIsNotNone(service.claim_routine_run())
            self.assertEqual(self.state(service).discards, ())

    def test_notices_and_stops_refuse_unknown_runs_and_stop_a_running_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            with self.assertRaises(local_app.ApiProblem) as missing:
                service.stop_routine("team_1", "0" * 32)
            self.assertEqual(missing.exception.code, "routine-run-not-found")
            self.routine(service)
            claim = service.claim_routine_run()
            routine_run.register_routine_run(service, "team_1", claim["run_id"], "token", 600)
            self.assertTrue(service.stop_routine("team_1", claim["run_id"])["stopped"])
            self.assertIn("token", service._cancelled_chat_tokens)
            # A worker registered under another Team is never reached, and the run is left to that worker's own end.
            service._routine_runs[claim["run_id"]] = dataclasses.replace(
                service._routine_runs[claim["run_id"]], team_id="team_2"
            )
            self.assertFalse(service.stop_routine("team_1", claim["run_id"])["stopped"])


class WatchdogFaultTests(RoutineServiceCase):
    def test_a_missing_routine_key_still_recovers_expired_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.routine(service)
            service.claim_routine_run()
            with mock.patch.object(
                local_authority, "routine_key_fingerprint", side_effect=local_authority.SupervisorUnavailableError
            ):
                routine_watchdog.check(service, startup=True)
            self.assertEqual(self.state(service).runs, ())
