"""Every Routine failure path fails closed and leaves nothing a later run could misuse (ADR-0086)."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import time
from http import HTTPStatus
from types import SimpleNamespace
from unittest import mock

from test_local_chat_scope import LOOKUP_RESULT
from test_local_routine_service import (
    API_KEY,
    ASSISTANT,
    KEY,
    RoutineServiceCase,
    Runtime,
    acting,
    approval,
    completed,
)

from action import human as action_human
from action import journal as action_journal
from inference import config as inference_config
from local import app as local_app
from local import authority as local_authority
from local.routine import manage as routine_manage
from local.routine import run as routine_run
from local.routine import state as routine_state
from local.routine import store as routine_store
from local.routine import watchdog as routine_watchdog
from routine import record, trace
from tests import human_request_fixtures


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


def _challenge(**params: object) -> dict[str, object]:
    """A public challenge descriptor whose title names a zone and a count, as an Action may parameterize its copy."""
    request = {
        "kind": "input:select",
        "ordinal": 0,
        "title": {"message": "m-title", "params": dict(params)},
        "description": {"message": "m-description", "params": {}},
        "label": {"message": "m-label", "params": {}},
        "options": [{"value": "keep", "label": {"message": "m-keep", "params": {}}, "description": None}],
        "required": True,
        "min_selections": 1,
        "max_selections": 1,
    }
    request["fingerprint"] = routine_run.request_fingerprint(request)
    words = " ".join(str(value) for value in params.values())
    rendered = {
        "title": f"Publish {words}".strip(),
        "description": "Choose what to keep.",
        "label": "Record",
        "options": [{"label": "Keep it", "description": None}],
    }
    return {"team_id": "team_1", "status": "human-required", "request": request, "rendered": rendered}


def choice() -> action_human.HumanRequest:
    """A valid single-choice request whose option value the Action chose at run time."""
    options = [
        {"value": "zone-1", "label": "Continue", "description": None},
        {"value": "zone-2", "label": "Confirm", "description": None},
    ]
    descriptor = {
        "kind": "input:choice",
        "ordinal": 0,
        "title": "List zones",
        "description": "Allow listing the zones.",
        "label": "Value",
        "required": True,
        "options": options,
    }
    return human_request_fixtures.admit(human_request_fixtures.fingerprinted(descriptor), ("input:choice",))


def _approval_challenge(**params: object) -> dict[str, object]:
    """The same challenge as an approval, which offers no value to choose."""
    descriptor = _challenge(**params)
    request = {key: descriptor["request"][key] for key in ("ordinal", "title", "description")}
    request = {"kind": "approval", **request}
    request["fingerprint"] = routine_run.request_fingerprint(request)
    rendered = {key: descriptor["rendered"][key] for key in ("title", "description")}
    return {**descriptor, "request": request, "rendered": rendered}


class PublicChallengeTests(RoutineServiceCase):
    """A frozen run's request is shown without what its run protects; its sealed request never changes (ADR-0101)."""

    def test_a_protected_parameter_is_withheld_and_its_copy_says_redacted(self) -> None:
        protection = trace.Protection(frozenset({"zone-secret-1"}))
        public = routine_run.public_challenge(
            _challenge(zone="zone-secret-1", count=3, other="example.com"), protection
        )
        request = public["request"]
        self.assertEqual(request["title"]["params"], {"count": 3, "other": "example.com"})
        self.assertEqual(public["rendered"]["title"], "Publish [redacted] 3 example.com")
        # The fingerprint is that of exactly what is shown, so the public descriptor stays self-consistent.
        unsigned = {key: value for key, value in request.items() if key != "fingerprint"}
        self.assertEqual(request["fingerprint"], routine_run.request_fingerprint(unsigned))
        self.assertFalse(trace.exposes(public, protection.values))

    def test_after_a_loss_every_text_parameter_is_withheld(self) -> None:
        public = routine_run.public_challenge(
            _approval_challenge(zone="example.com", count=3), trace.Protection(lost=True)
        )
        self.assertEqual(public["request"]["title"]["params"], {"count": 3})
        self.assertEqual(public["rendered"]["title"], "Publish [redacted] 3")

    def test_after_a_loss_no_option_value_or_purpose_is_ever_shown(self) -> None:
        purposed = {**_approval_challenge(), "purpose": "Publish the zone."}
        for descriptor in (_challenge(), purposed):
            with self.subTest(keys=sorted(descriptor)):
                self.assertIsNone(routine_run.public_challenge(descriptor, trace.Protection(lost=True)))
        del purposed["purpose"]
        self.assertIsNotNone(routine_run.public_challenge(purposed, trace.Protection(lost=True)))

    def test_nothing_protected_leaves_the_challenge_as_it_was(self) -> None:
        descriptor = _challenge(zone="example.com")
        self.assertEqual(routine_run.public_challenge(descriptor, trace.Protection(frozenset({"absent"}))), descriptor)

    def test_a_protected_value_outside_any_parameter_or_past_a_bound_cannot_be_shown(self) -> None:
        exposed = _challenge()
        exposed["request"]["options"][0]["value"] = "zone-secret-1"
        long = _challenge(zone="a1")
        long["rendered"]["title"] = "a1 " + "x" * 75
        for descriptor, protected in ((exposed, "zone-secret-1"), (long, "a1")):
            with self.subTest(protected=protected):
                self.assertIsNone(routine_run.public_challenge(descriptor, trace.Protection(frozenset({protected}))))


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
        totp = human_request_fixtures.admit(human_request_fixtures.fingerprinted(descriptor), ("auth:totp",))
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.paused(directory, totp)
            result = self.run_claim(service, claim)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(
                self.state(service).notices[-1].detail,
                # The run ends at the step that asked, by its position.
                {"code": "request-unavailable", "actions": [], "position": {"phase": "replay", "step": 1}, "steps": 1},
            )
            self.assertEqual(service.routine_store.continuations("team_1"), ())

    def test_a_run_that_lost_its_protection_still_freezes_and_shows_its_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.paused(directory)
            grow = service.routine_protections.grow

            def lost(run_id, values):
                grow(run_id, values)
                return trace.Protection(lost=True)

            with mock.patch.object(service.routine_protections, "grow", side_effect=lost):
                self.assertEqual(self.run_claim(service, claim)["status"], "frozen")
                opened = service.open_routine_challenge("team_1", claim["run_id"], "en")
            frozen = record.run(self.state(service), claim["run_id"])
        self.assertEqual((opened["status"], frozen.protection_lost), ("human-required", True))

    def test_after_a_loss_a_choice_request_is_never_frozen_since_its_values_cannot_be_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.paused(directory, choice())
            grow = service.routine_protections.grow

            def lost(run_id, values):
                grow(run_id, values)
                return trace.Protection(lost=True)

            with mock.patch.object(service.routine_protections, "grow", side_effect=lost):
                self.assertEqual(self.run_claim(service, claim)["status"], "failed")
            self.assertEqual(self.state(service).notices[-1].detail["code"], "request-unavailable")

    def test_a_request_whose_protected_value_cannot_be_hidden_ends_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.paused(directory)
            with mock.patch.object(routine_run, "public_challenge", return_value=None):
                self.assertEqual(self.run_claim(service, claim)["status"], "failed")
            self.assertEqual(self.state(service).notices[-1].detail["code"], "request-unavailable")

    def test_a_frozen_request_that_cannot_be_shown_when_opened_keeps_the_run_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.paused(directory)
            self.assertEqual(self.run_claim(service, claim)["status"], "frozen")
            with (
                mock.patch.object(routine_run, "public_challenge", return_value=None),
                self.assertRaises(local_app.ApiProblem) as caught,
            ):
                service.open_routine_challenge("team_1", claim["run_id"], "en")
            status = record.run(self.state(service), claim["run_id"]).status
            current = service.current_routine_challenge("team_1")
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "human-request-invalid"))
        self.assertEqual((status, current), ("frozen", None))

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
            detail = self.state(service).notices[-1].detail
            # It fails at the step that asked, by its position.
            self.assertEqual(
                (detail["code"], detail["position"], detail["steps"]),
                ("freeze-unavailable", {"phase": "replay", "step": 1}, 1),
            )
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
                service.open_routine_challenge("team_1", claim["run_id"], "en")["status"], "integrations-required"
            )
            with self.assertRaises(local_app.ApiProblem) as provider:
                service.resume_routine_integrations("team_1", claim["run_id"], "anthropic", API_KEY)
            self.assertEqual(provider.exception.code, "inference-provider-mismatch")
            # Still missing: the replay pauses again and the run freezes again.
            again = service.resume_routine_integrations("team_1", claim["run_id"], "openai", API_KEY)
            self.assertEqual(again["status"], "frozen")

    def test_a_completion_stop_wins_and_a_lease_that_ran_out_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, Runtime())
            controller.assistant_lifecycle.invoke = lambda *_args: {"result": LOOKUP_RESULT}
            self.routine(service)
            claim = service.claim_routine_run()
            with mock.patch.object(service, "_commit_chat_terminal", return_value=False):
                self.assertEqual(self.run_claim(service, claim)["status"], "stopped")
            self.assertEqual(self.state(service).notices[-1].detail, {"actions": []})
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, Runtime())
            controller.assistant_lifecycle.invoke = lambda *_args: {"result": LOOKUP_RESULT}
            self.routine(service)
            claim = service.claim_routine_run()
            with mock.patch.object(record, "finish", side_effect=record.RoutineStateError("lease-invalid")):
                self.assertEqual(self.run_claim(service, claim)["status"], "failed")
            self.assertEqual(self.state(service).notices[-1].detail["code"], "lease-expired")

    def test_a_lease_lost_before_binding_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
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

    def test_a_team_without_a_model_configuration_is_never_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.routine(service)
            missing = inference_config.InferenceConfigMissingError("none")
            with mock.patch.object(service.inference_store, "load", side_effect=missing):
                self.assertIsNone(service.claim_routine_run())
            self.assertEqual(self.state(service).runs, ())

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
    def frozen(self, directory: str):
        controller, service = self.service(directory, Runtime())
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

    def test_a_failure_after_a_human_answer_holds_the_run_as_an_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.frozen(directory)
            opened = service.open_routine_challenge("team_1", claim["run_id"], "en")
            resumed = service.resume_routine_human(
                "team_1",
                claim["run_id"],
                {"challenge_id": opened["challenge_id"], "decision": "submit", "value": True},
                "openai",
                API_KEY,
            )
            self.assertEqual(resumed["status"], "held")
            state = self.state(service)
            self.assertEqual((state.runs, [item.incident_id for item in state.incidents]), ((), [claim["run_id"]]))

    def test_deleting_a_routine_stops_its_frozen_run_and_the_watchdog_keeps_a_frozen_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.frozen(directory)
            routine_watchdog.check(service)
            self.assertEqual(service.routine_store.continuations("team_1"), (claim["run_id"],))
            self.assertTrue(service.delete_routine("team_1", claim["routine_id"])["deleted"])
            self.assertEqual(service.routine_store.continuations("team_1"), ())
            # The frozen run ends stopped, and the deletion's own notice closes the Routine's timeline after it.
            outcomes = [(item.outcome, item.run_id) for item in self.state(service).notices[-2:]]
            self.assertEqual(outcomes, [("stopped", claim["run_id"]), ("deleted", "")])

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
                    service.open_routine_challenge("team_1", claim["run_id"], "en")
                self.assertEqual(caught.exception.code, "routine-state-unavailable")
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.frozen(directory)
            # The Team no longer runs the run's Assistant: that is proof, so the run ends.
            with (
                mock.patch.object(service, "_active_chat_assistants", return_value=()),
                self.assertRaises(local_app.ApiProblem) as changed,
            ):
                service.open_routine_challenge("team_1", claim["run_id"], "en")
            self.assertEqual(changed.exception.code, "team-context-changed")
            self.assertEqual(self.state(service).runs, ())

    def test_a_team_that_cannot_be_read_keeps_the_frozen_run_for_a_retry(self) -> None:
        down = local_app.ApiProblem(503, "Docker is unavailable", code="docker-unavailable")
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.frozen(directory)
            run_id = claim["run_id"]
            unreadable = (
                mock.patch.object(service, "_active_chat_assistants", side_effect=down),
                mock.patch.object(
                    service.inference_store, "load", side_effect=inference_config.InferenceConfigError("unavailable")
                ),
                # Setup failed although everything reads as unchanged: still no proof.
                mock.patch.object(service, "_chat_setup", side_effect=down),
            )
            for patch in unreadable:
                with self.subTest(patch=patch), patch, self.assertRaises(local_app.ApiProblem) as unavailable:
                    service.open_routine_challenge("team_1", run_id, "en")
                self.assertEqual(
                    (unavailable.exception.status, unavailable.exception.code), (503, "team-context-unavailable")
                )
            opened = service.open_routine_challenge("team_1", run_id, "en")
            with (
                unreadable[0],
                self.assertRaises(local_app.ApiProblem) as replay,
            ):
                service.resume_routine_human(
                    "team_1",
                    run_id,
                    {"challenge_id": opened["challenge_id"], "decision": "submit", "value": True},
                    "openai",
                    API_KEY,
                )
            self.assertEqual(replay.exception.code, "team-context-unavailable")
            (held,) = self.state(service).runs
            self.assertEqual((held.run_id, held.status), (run_id, "frozen"))
            self.assertEqual(service.routine_store.continuations("team_1"), (run_id,))
            self.assertEqual(service.open_routine_challenge("team_1", run_id, "en")["run_id"], run_id)

    def test_an_unreadable_or_malformed_registry_keeps_the_frozen_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.frozen(directory)
            run_id = claim["run_id"]
            for damage in ("unreadable", "malformed"):
                with (
                    self.subTest(damage=damage),
                    self.broken_registry(service, directory, damage),
                    self.assertRaises(local_app.ApiProblem) as unavailable,
                ):
                    service.open_routine_challenge("team_1", run_id, "en")
                self.assertEqual(
                    (unavailable.exception.status, unavailable.exception.code), (503, "team-context-unavailable")
                )
            (held,) = self.state(service).runs
            self.assertEqual((held.run_id, held.status), (run_id, "frozen"))
            self.assertEqual(service.routine_store.continuations("team_1"), (run_id,))
            self.assertEqual(service.open_routine_challenge("team_1", run_id, "en")["run_id"], run_id)

    def test_a_removed_or_changed_model_configuration_ends_the_frozen_run(self) -> None:
        for load in (
            mock.Mock(side_effect=inference_config.InferenceConfigMissingError("unset")),
            mock.Mock(return_value=SimpleNamespace(provider="anthropic")),
        ):
            with self.subTest(load=load), tempfile.TemporaryDirectory() as directory:
                _controller, service, claim = self.frozen(directory)
                with (
                    mock.patch.object(service.inference_store, "load", load),
                    self.assertRaises(local_app.ApiProblem) as changed,
                ):
                    service.open_routine_challenge("team_1", claim["run_id"], "en")
                self.assertEqual(changed.exception.code, "team-context-changed")
                self.assertEqual(self.state(service).runs, ())

    def test_an_answer_must_match_its_own_run_and_only_a_frozen_run_takes_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim = self.frozen(directory)
            opened = service.open_routine_challenge("team_1", claim["run_id"], "en")
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
                service.open_routine_challenge("team_1", leased["run_id"], "en")
            self.assertEqual(not_frozen.exception.code, "routine-run-not-frozen")


class ManageAndNoticeFaultTests(RoutineServiceCase):
    def test_deleting_a_routine_ends_each_kind_of_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
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
            # Its diagnostic bodies go with it; a body store that cannot remove them keeps the deletion retryable.
            with (
                mock.patch.object(
                    service.routine_diagnostics,
                    "delete_routine",
                    side_effect=routine_manage.routine_diagnostics.DiagnosticStoreError("down"),
                ),
                self.assertRaises(local_app.ApiProblem) as unavailable,
            ):
                routine_manage.complete_deletion(service, "team_1", value.routine_id)
            self.assertEqual(unavailable.exception.code, "routine-state-unavailable")
            self.assertTrue(routine_manage.complete_deletion(service, "team_1", value.routine_id))
            self.assertTrue(routine_manage.complete_deletion(service, "team_1", value.routine_id))

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
            down = action_journal.ActionJournalError("down")
            with (
                mock.patch.object(service.action_state, "discard", side_effect=down),
                mock.patch.object(service.action_state, "purge", side_effect=down),
            ):
                with self.assertRaises(local_app.ApiProblem) as caught:
                    routine_manage.drain(service, "team_1")
                self.assertEqual(caught.exception.code, "routine-state-unavailable")
                self.assertEqual(len(self.state(service).discards), 1)
                self.assertIsNone(service.claim_routine_run())
                # The watchdog's periodic pass audits the Team's failure and retries it later; startup stays fatal.
                routine_watchdog.check(service)
                self.assertEqual(len(self.state(service).discards), 1)
                with self.assertRaises(local_app.ApiProblem):
                    routine_watchdog.check(service, startup=True)
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


class TeamIsolationTests(RoutineServiceCase):
    """One identified Team's unreadable Routine state never holds back the healthy Teams (ADR-0086)."""

    @staticmethod
    def break_team(service, team_id: str = "team_2") -> None:
        """Write a real state file that names its Team but whose body fails the store's contract."""
        path = service.routine_store._team_dir(team_id) / "state.json"
        routine_store._PRIVATE.atomic_write(path, json.dumps({"team_id": team_id}).encode(), "Routine state")

    def expire(self, service, run_id: str) -> None:
        past = int(time.time()) - 60
        service.routine_store.update(
            "team_1",
            lambda state: (
                dataclasses.replace(
                    state,
                    runs=tuple(
                        dataclasses.replace(item, lease_expires_at=past) if item.run_id == run_id else item
                        for item in state.runs
                    ),
                ),
                None,
            ),
        )

    def test_a_periodic_pass_recovers_healthy_teams_and_audits_the_unreadable_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.routine(service)
            claim = service.claim_routine_run()
            self.expire(service, claim["run_id"])
            self.break_team(service)
            self.assertEqual(set(service.routine_store.teams()), {"team_1", "team_2"})

            routine_watchdog.check(service)

            state = self.state(service)
            self.assertEqual(
                (state.runs, state.notices[-1].detail),
                ((), {"code": "interrupted", "actions": [], "position": None, "steps": None}),
            )
            local_app.local_audit.record.assert_any_call(
                "routine-watchdog",
                result="error",
                principal=routine_watchdog._PRINCIPAL,
                team_id="team_2",
                detail="team-check-failed",
            )
            # At startup the same Team stays a hard failure.
            with self.assertRaises(routine_store.RoutineStoreError):
                routine_watchdog.check(service, startup=True)

    def test_an_unidentifiable_team_directory_still_fails_the_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.routine(service)
            path = service.routine_store._team_dir("team_2") / "state.json"
            routine_store._PRIVATE.atomic_write(path, b'{"team_id":"Team 2"}', "Routine state")
            with self.assertRaises(routine_store.RoutineStoreError):
                routine_watchdog.check(service)

    def test_notices_skip_an_unreadable_team_and_deliver_the_healthy_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, Runtime(acting(), completed()))
            controller.assistant_lifecycle.invoke = lambda *_args: {"result": LOOKUP_RESULT}
            self.routine(service)
            self.run_claim(service, service.claim_routine_run())
            self.break_team(service)
            damaged = service.routine_store._team_dir("team_2") / "state.json"
            before = damaged.read_bytes()

            batch = service.routine_notices()

            self.assertEqual(
                ([(item["team_id"], item["outcome"]) for item in batch["notices"]], batch["more"]),
                ([("team_1", "done")], False),
            )
            local_app.local_audit.record_request.assert_any_call(
                "routine-notices", result="error", team_id="team_2", detail="routine-state-unavailable"
            )
            service.acknowledge_routine_notices(
                {"deliveries": [{"team_id": "team_1", "notice_id": batch["notices"][0]["notice_id"], "version": 1}]}
            )
            self.assertEqual(service.routine_notices()["notices"], [])
            # The damaged Team is never acknowledged: a delivery naming it fails closed and its state stays as written.
            with self.assertRaises(local_app.ApiProblem) as refused:
                service.acknowledge_routine_notices(
                    {"deliveries": [{"team_id": "team_2", "notice_id": "0" * 32, "version": 1}]}
                )
            self.assertEqual((refused.exception.status, refused.exception.code), (503, "routine-state-unavailable"))
            self.assertEqual(damaged.read_bytes(), before)

    def test_notices_fail_closed_when_teams_cannot_be_enumerated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, Runtime(acting(), completed()))
            controller.assistant_lifecycle.invoke = lambda *_args: {"result": LOOKUP_RESULT}
            self.routine(service)
            self.run_claim(service, service.claim_routine_run())
            path = service.routine_store._team_dir("team_2") / "state.json"
            routine_store._PRIVATE.atomic_write(path, b'{"team_id":"Team 2"}', "Routine state")
            with self.assertRaises(local_app.ApiProblem) as unavailable:
                service.routine_notices()
            self.assertEqual(unavailable.exception.code, "routine-state-unavailable")

    def test_claims_skip_an_unreadable_team_and_serve_the_healthy_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.routine(service)
            self.break_team(service)

            claim = service.claim_routine_run()

            self.assertEqual(claim["team_id"], "team_1")
            local_app.local_audit.record_request.assert_any_call(
                "routine-claim", result="error", team_id="team_2", detail="routine-state-unavailable"
            )

    def test_a_claim_whose_team_state_cannot_change_is_audited_and_passed_over(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.routine(service)
            with mock.patch.object(service.routine_store, "update", side_effect=routine_store.RoutineStoreError("x")):
                self.assertIsNone(service.claim_routine_run())
            local_app.local_audit.record_request.assert_any_call(
                "routine-claim", result="error", team_id="team_1", detail="routine-state-unavailable"
            )
            self.assertIsNotNone(service.claim_routine_run())

    def test_a_team_whose_due_run_cannot_transition_is_audited_and_passed_over(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.routine(service)
            # The state reads, but the record rules refuse its claim transition (e.g. an inconsistent notice).
            refused = routine_run.record.RoutineStateError("notice-invalid")
            with mock.patch.object(routine_run, "_claim", side_effect=refused):
                self.assertIsNone(service.claim_routine_run())
            local_app.local_audit.record_request.assert_any_call(
                "routine-claim", result="error", team_id="team_1", detail="routine-state-unavailable"
            )
            self.assertIsNotNone(service.claim_routine_run())

    def test_one_overdue_run_that_cannot_be_stopped_never_keeps_another_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            routine_run.register_routine_run(service, "team_1", "1" * 32, "token-1", 0)
            routine_run.register_routine_run(service, "team_2", "2" * 32, "token-2", 0)
            # The first run's Action cannot be proved stopped; the second run must still be stopped.
            service._active_action_containers["team_1"] = ("token-1", object())
            blocked = local_app.ApiProblem(HTTPStatus.SERVICE_UNAVAILABLE, "blocked", code="assistant-action-blocked")
            with mock.patch.object(service.assistant_lifecycle, "_fail_stop_action", side_effect=blocked):
                routine_watchdog.check(service)

            self.assertLessEqual({"token-1", "token-2"}, service._cancelled_chat_tokens)
            self.assertTrue(service._routine_runs["1" * 32].overdue)
            local_app.local_audit.record.assert_any_call(
                "routine-stop",
                result="error",
                principal=routine_watchdog._PRINCIPAL,
                team_id="team_1",
                detail="1" * 32,
            )
