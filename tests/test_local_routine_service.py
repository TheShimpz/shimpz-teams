"""Local Routine runs end to end on the Local controller: claim, run, freeze, resume, stop (ADR-0086)."""

from __future__ import annotations

import dataclasses
import tempfile
import threading
import time
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import routine_fixture
from local_controller_harness import LocalContractCase
from test_local_chat_scope import LOOKUP_INPUT, LOOKUP_RESULT

from action import human as action_human
from inference import client as brain_runtime_client
from install.bindings import DynamicAssistantStore
from local import app as local_app
from local import audit as local_audit
from local import authority as local_authority
from local.install.registry import AssistantRegistry
from local.labels import ASSISTANT_LABEL
from local.routine import human as routine_human
from local.routine import run as routine_run
from local.routine import store as routine_store
from local.routine import turn as routine_turn
from local.routine import watchdog as routine_watchdog
from routine import pin as routine_pin
from routine import record
from tests import human_request_fixtures

KEY = "e" * 64
API_KEY = "sk-test-0123456789"
# SHA-256 of API_KEY, written out so tests check the fingerprint the boundary binds instead of recomputing it.
API_KEY_SHA256 = "0d3b560722915d2f931a4c4100a00ecbce063d121e577e6b93bbbe7c05f23ad6"
ASSISTANT = "shimpz-cloudflare"
CHANGE = {
    "op": "propose",
    "quote": "Every day at 9, list my zones",
    "schedule": {"kind": "daily", "time": "09:00"},
    "timezone": None,
    "routine_id": None,
}
LIST = brain_runtime_client.ActionRequest("action-1", ASSISTANT, "list-zones", LOOKUP_INPUT)


class Runtime:
    """A scripted Brain: each start or resume returns the next turn."""

    def __init__(self, *turns: brain_runtime_client.RuntimeTurn) -> None:
        self.turns = list(turns)
        self.contexts: list[brain_runtime_client.RuntimeContext] = []

    def start(self, context, _message, *, conversation=()):
        self.contexts.append(context)
        return self.turns.pop(0)

    def resume(self, context, _results):
        self.contexts.append(context)
        return self.turns.pop(0)

    def delete_thread(self, _thread_id):
        return None

    def purpose(self, _context, _request, _assistant_name, _summary):
        return None

    @staticmethod
    def routine_recovery(_payload, _provider, _model):
        # A held run's automatic recovery asks the person unless a test scripts another decision.
        return {"decision": "ask"}


def completed(reply: str = "Your zones are listed.") -> brain_runtime_client.RuntimeTurn:
    return brain_runtime_client.RuntimeTurn("completed", reply, ())


def acting(*requests) -> brain_runtime_client.RuntimeTurn:
    return brain_runtime_client.RuntimeTurn("action-required", "", tuple(requests) or (LIST,))


def approval() -> action_human.HumanRequest:
    descriptor = {"kind": "approval", "ordinal": 0, "title": "List zones", "description": "Allow listing the zones."}
    return human_request_fixtures.admit(human_request_fixtures.fingerprinted(descriptor), ("approval",))


class RoutineServiceCase(LocalContractCase):
    def setUp(self) -> None:
        super().setUp()
        for patch in (
            mock.patch.object(local_authority, "routine_key_fingerprint", return_value=KEY),
            mock.patch.object(local_audit, "record_request", return_value="a" * 32),
            mock.patch.object(local_audit, "record", return_value="a" * 32),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def service(self, directory: str, runtime: Runtime):
        controller = self._chat_controller(directory, runtime)
        service = controller.chat_turn_service
        controller.brain_runtime = service.brain_runtime = runtime
        return controller, service

    @staticmethod
    def plan(service, *steps: tuple[str, str, dict[str, object]]) -> dict[str, object]:
        """A compiled plan of literal-input steps, each pinned to the Team's current Action contract."""
        _name, _network, active = service._team_assistants("team_1")
        contracts = routine_turn.contracts(tuple(active.values()), routine_pin.SCOPE_LOCALE)
        return {
            "version": 1,
            "timezone": "UTC",
            "steps": [
                {
                    "id": step_id,
                    "assistant": ASSISTANT,
                    "action": action,
                    "pin": contracts[(ASSISTANT, action)].pin,
                    "input": {name: {"kind": "literal", "value": value} for name, value in inputs.items()},
                }
                for step_id, action, inputs in steps or (("zones", "list-zones", LOOKUP_INPUT),)
            ],
        }

    def routine(self, service, *, next_run_at: int | None = None, plan: dict | None = None) -> record.Routine:
        """Add one daily compiled Routine pinned to the Team's current contracts, due now unless told otherwise."""
        contracts = routine_turn.current_contracts(service, "team_1", (ASSISTANT,))
        value = routine_fixture.granted(
            record.Routine(
                routine_id=record.new_id(),
                name="Daily zones",
                quote=CHANGE["quote"],
                plan=plan or self.plan(service),
                schedule=dict(CHANGE["schedule"]),
                timezone="UTC",
                assistants=tuple(sorted(contracts.items())),
                anchor=int(time.time()) - 3 * 86_400,
                next_run_at=0,
            )
        )
        value = dataclasses.replace(value, next_run_at=record.next_after(value, value.anchor))
        service.routine_store.update("team_1", lambda state: (record.add_routine(state, value), None))
        due = int(time.time()) - 60 if next_run_at is None else next_run_at
        service.routine_store.update(
            "team_1",
            lambda state: (
                record._replace_routine(
                    state, dataclasses.replace(record.routine(state, value.routine_id), next_run_at=due)
                ),
                None,
            ),
        )
        return value

    def run_claim(self, service, claim: dict[str, object]) -> dict[str, object]:
        evidence = local_authority.RoutineEvidence(KEY, record.lease_sha256(claim["lease_token"]), "a" * 32, 0)
        return service.run_routine("team_1", claim["run_id"], evidence, "openai", API_KEY)

    def state(self, service) -> record.TeamRoutines:
        return service.routine_store.load("team_1")

    @contextmanager
    def broken_registry(self, service, directory: str, damage: str):
        """Read the Team's running Assistant through the real registry, whose store is unreadable or malformed."""
        path = Path(directory) / f"registry-{damage}" / "bindings.json"
        path.parent.mkdir()
        if damage.startswith("unreadable"):
            path.mkdir()
        else:
            path.write_bytes(b"not json")
        container = SimpleNamespace(id="assistant-container", status="running", labels={ASSISTANT_LABEL: ASSISTANT})
        docker = SimpleNamespace(containers=SimpleNamespace(list=lambda **_filters: [container]))
        # The real scan, not the harness's fixed answer, so the registry itself is read.
        scan = type(service)._active_chat_assistants.__get__(service)
        with (
            mock.patch.object(service, "_active_chat_assistants", scan),
            mock.patch.object(service, "registry", AssistantRegistry(DynamicAssistantStore(path))),
            mock.patch.object(service.assistant_lifecycle, "client", docker, create=True),
        ):
            yield


class RunTests(RoutineServiceCase):
    def test_a_due_routine_is_claimed_run_and_delivered_as_done(self) -> None:
        runtime = Runtime(acting(), completed())
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, runtime)
            controller.assistant_lifecycle.invoke = lambda *_args: {"result": LOOKUP_RESULT}
            self.routine(service)
            claim = service.claim_routine_run(("anthropic", "openai"))
            self.assertIsNone(service.claim_routine_run(("anthropic", "openai")))
            result = self.run_claim(service, claim)
            state = self.state(service)
        self.assertEqual(result["status"], "done")
        self.assertEqual(state.runs, ())
        self.assertEqual([(item.outcome, item.detail) for item in state.notices], [("done", {"reply": "Daily zones"})])
        # A healthy compiled run never asks the Brain anything.
        self.assertEqual(runtime.contexts, [])

    def test_nothing_is_claimed_without_a_routine_key_or_while_chat_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.routine(service)
            with mock.patch.object(
                local_authority, "routine_key_fingerprint", side_effect=local_authority.SupervisorUnavailableError
            ):
                self.assertIsNone(service.claim_routine_run(("anthropic", "openai")))
            lock = service._chat_lock("team_1")
            lock.acquire()
            try:
                self.assertIsNone(service.claim_routine_run(("anthropic", "openai")))
            finally:
                lock.release()
            self.assertIsNotNone(service.claim_routine_run(("anthropic", "openai")))

    def test_a_changed_assistant_contract_marks_the_routine_for_reconfirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            value = self.routine(service)
            with mock.patch.object(routine_turn, "current_contracts", return_value={ASSISTANT: "sha256:" + "0" * 64}):
                self.assertIsNone(service.claim_routine_run(("anthropic", "openai")))
            state = self.state(service)
        self.assertTrue(record.routine(state, value.routine_id).needs_reconfirm)
        self.assertEqual(
            [(item.outcome, item.detail) for item in state.notices], [("scope-changed", {"assistants": [ASSISTANT]})]
        )

    def test_unreadable_contracts_never_mark_the_routine_changed(self) -> None:
        down = local_app.ApiProblem(503, "Docker is unavailable", code="docker-unavailable")
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            value = self.routine(service)
            with mock.patch.object(service, "_active_chat_assistants", side_effect=down):
                self.assertIsNone(service.claim_routine_run(("anthropic", "openai")))
            state = self.state(service)
            self.assertEqual((record.routine(state, value.routine_id).needs_reconfirm, state.notices), (False, ()))
            # The claim reads the contracts; the second read, in the run's slot, fails.
            claim = service.claim_routine_run(("anthropic", "openai"))
            with mock.patch.object(service, "_active_chat_assistants", side_effect=down):
                self.assertEqual(self.run_claim(service, claim)["status"], "failed")
            state = self.state(service)
        self.assertFalse(record.routine(state, value.routine_id).needs_reconfirm)
        self.assertEqual(
            [(item.outcome, item.detail) for item in state.notices],
            [("failed", {"code": "team-context-unavailable", "actions": []})],
        )

    def test_an_unreadable_or_malformed_registry_never_marks_the_routine_changed_or_strands_a_run(self) -> None:
        for damage in ("unreadable", "malformed"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as directory:
                _controller, service = self.service(directory, Runtime())
                value = self.routine(service)
                with self.broken_registry(service, directory, damage):
                    self.assertIsNone(service.claim_routine_run(("anthropic", "openai")))
                state = self.state(service)
                self.assertEqual((record.routine(state, value.routine_id).needs_reconfirm, state.notices), (False, ()))
                claim = service.claim_routine_run(("anthropic", "openai"))
                with self.broken_registry(service, directory, f"{damage}-run"):
                    self.assertEqual(self.run_claim(service, claim)["status"], "failed")
                state = self.state(service)
                self.assertEqual(state.runs, ())
                self.assertFalse(record.routine(state, value.routine_id).needs_reconfirm)
                self.assertEqual(state.notices[-1].detail, {"code": "team-context-unavailable", "actions": []})

    def test_an_assistant_the_team_no_longer_runs_marks_the_routine_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            value = self.routine(service)
            with mock.patch.object(service, "_active_chat_assistants", return_value=()):
                self.assertIsNone(service.claim_routine_run(("anthropic", "openai")))
            state = self.state(service)
        self.assertTrue(record.routine(state, value.routine_id).needs_reconfirm)
        self.assertEqual(
            [(item.outcome, item.detail) for item in state.notices], [("scope-changed", {"assistants": [ASSISTANT]})]
        )

    def test_a_failing_action_holds_the_run_as_an_incident_that_holds_its_routine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, Runtime())

            def failing(*_args):
                raise local_app.ApiProblem(HTTPStatus.BAD_GATEWAY, "failed", code="assistant-rpc-failed")

            controller.assistant_lifecycle.invoke = failing
            value = self.routine(service)
            claim = service.claim_routine_run(("anthropic", "openai"))
            self.assertEqual(self.run_claim(service, claim)["status"], "held")
            state = self.state(service)
            (incident,) = state.incidents
            self.assertEqual((state.runs, incident.incident_id, incident.status), ((), claim["run_id"], "unresolved"))
            self.assertIn(value.routine_id, record.held_routines(state))
            # Its cursor and recovery snapshot survive for verification; nothing is claimed while it holds.
            self.assertEqual(service.routine_store.cursors("team_1"), (claim["run_id"],))
            self.assertIsNone(service.claim_routine_run(("anthropic", "openai")))
            self.assertTrue(service.delete_routine("team_1", value.routine_id)["deleted"])
            self.assertEqual(self.state(service).incidents[0].status, "unresolved")

    def test_deleting_an_unknown_or_malformed_routine_is_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            for routine_id in ("0" * 32, "Not-An-Id", None):
                with self.subTest(routine_id=routine_id), self.assertRaises(local_app.ApiProblem) as caught:
                    service.delete_routine("team_1", routine_id)
                self.assertEqual((caught.exception.status, caught.exception.code), (404, "routine-not-found"))

    def test_other_failures_and_a_dead_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.routine(service)
            claim = service.claim_routine_run(("anthropic", "openai"))
            with mock.patch.object(
                service, "_run_chat_segment", side_effect=local_app.ApiProblem(409, "stopped", code="chat-stopped")
            ):
                self.assertEqual(self.run_claim(service, claim)["status"], "stopped")
            self.routine(service)
            claim = service.claim_routine_run(("anthropic", "openai"))
            with mock.patch.object(
                service, "_run_chat_segment", side_effect=local_app.ApiProblem(409, "x", code="team-context-changed")
            ):
                self.assertEqual(self.run_claim(service, claim)["status"], "failed")
            with self.assertRaises(local_app.ApiProblem) as dead:
                self.run_claim(service, claim)
            self.assertEqual(dead.exception.code, "routine-lease-invalid")


class FreezeTests(RoutineServiceCase):
    def paused(self, directory: str, *turns):
        runtime = Runtime(acting(), *turns)
        controller, service = self.service(directory, runtime)
        calls: list[object] = []

        def invoke(*args):
            calls.append(args)
            if len(calls) == 1:
                raise action_human.HumanRequestSuspensionError(approval())
            return {"result": LOOKUP_RESULT}

        controller.assistant_lifecycle.invoke = invoke
        self.routine(service)
        claim = service.claim_routine_run(("anthropic", "openai"))
        return controller, service, claim, self.run_claim(service, claim)

    def test_an_approval_freezes_the_run_and_a_person_resumes_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim, frozen = self.paused(directory, completed("Approved and listed."))
            self.assertEqual(frozen["status"], "frozen")
            run = record.run(self.state(service), claim["run_id"])
            self.assertEqual((run.status, run.request_kind, run.action), ("frozen", "human", "list-zones"))
            # A frozen run never blocks chat: no chat challenge exists.
            self.assertIsNone(service.human_challenges.current("team_1"))
            opened = service.open_routine_challenge("team_1", claim["run_id"], "en")
            self.assertEqual(opened["run_id"], claim["run_id"])
            resumed = service.resume_routine_human(
                "team_1",
                claim["run_id"],
                {"challenge_id": opened["challenge_id"], "decision": "submit", "value": True},
                "openai",
                API_KEY,
            )
            state = self.state(service)
        self.assertEqual(resumed["status"], "done")
        self.assertEqual((state.runs, state.notices[-1].detail), ((), {"reply": "Daily zones"}))

    def test_a_rename_never_ends_a_frozen_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service, claim, _frozen = self.paused(directory, completed("Approved and listed."))
            # Only the display name changes; the run's Team context keeps the immutable creation label.
            controller.team_names.save("team_1", "a" * 64, "Growth")
            opened = service.open_routine_challenge("team_1", claim["run_id"], "en")
            resumed = service.resume_routine_human(
                "team_1",
                claim["run_id"],
                {"challenge_id": opened["challenge_id"], "decision": "submit", "value": True},
                "openai",
                API_KEY,
            )
        self.assertEqual(resumed["status"], "done")

    def test_a_denied_or_stopped_frozen_run_ends_and_an_expired_challenge_leaves_it_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim, _frozen = self.paused(directory)
            opened = service.open_routine_challenge("team_1", claim["run_id"], "en")
            with self.assertRaises(local_app.ApiProblem) as expired:
                service.resume_routine_human(
                    "team_1", claim["run_id"], {"challenge_id": "0" * 32, "decision": "deny"}, "openai", API_KEY
                )
            self.assertEqual(expired.exception.code, "human-request-expired")
            self.assertEqual(record.run(self.state(service), claim["run_id"]).status, "frozen")
            denied = service.resume_routine_human(
                "team_1",
                claim["run_id"],
                {"challenge_id": opened["challenge_id"], "decision": "deny"},
                "openai",
                API_KEY,
            )
            self.assertEqual((denied["status"], self.state(service).notices[-1].outcome), ("denied", "denied"))
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim, _frozen = self.paused(directory)
            service.open_routine_challenge("team_1", claim["run_id"], "en")
            self.assertTrue(service.stop_routine("team_1", claim["run_id"])["stopped"])
            self.assertIsNone(service.current_routine_challenge("team_1"))
            self.assertEqual(self.state(service).notices[-1].outcome, "stopped")

    def test_invalid_answers_and_runs_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim, _frozen = self.paused(directory)
            opened = service.open_routine_challenge("team_1", claim["run_id"], "en")
            cases = (
                ({"decision": "submit"}, "invalid-body"),
                (
                    {"challenge_id": opened["challenge_id"], "decision": "submit", "value": "yes"},
                    "invalid-human-response",
                ),
            )
            for body, code in cases:
                with self.subTest(code=code), self.assertRaises(local_app.ApiProblem) as refused:
                    service.resume_routine_human("team_1", claim["run_id"], body, "openai", API_KEY)
                self.assertEqual(refused.exception.code, code)
            with self.assertRaises(local_app.ApiProblem) as provider:
                service.resume_routine_human(
                    "team_1",
                    claim["run_id"],
                    {"challenge_id": opened["challenge_id"], "decision": "submit", "value": True},
                    "anthropic",
                    API_KEY,
                )
            self.assertEqual(provider.exception.code, "inference-provider-mismatch")
            for run_id, code in (("0" * 32, "routine-run-not-found"), (7, "routine-run-not-found")):
                with self.subTest(run_id=run_id), self.assertRaises(local_app.ApiProblem) as missing:
                    service.open_routine_challenge("team_1", run_id, "en")
                self.assertEqual(missing.exception.code, code)
            with self.assertRaises(local_app.ApiProblem) as integration:
                service.resume_routine_integrations("team_1", claim["run_id"], "openai", API_KEY)
            self.assertEqual(integration.exception.code, "routine-run-not-frozen")

    def test_a_changed_team_ends_the_frozen_run_instead_of_replaying(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim, _frozen = self.paused(directory)
            with (
                mock.patch.object(service, "_chat_identity", return_value=("changed",)),
                self.assertRaises(local_app.ApiProblem) as changed,
            ):
                service.open_routine_challenge("team_1", claim["run_id"], "en")
            self.assertEqual(changed.exception.code, "team-context-changed")
            self.assertEqual(self.state(service).notices[-1].detail["code"], "team-context-changed")

    def test_each_opening_renders_a_fresh_challenge_in_the_admin_language(self) -> None:
        """A frozen run's request copy follows the language of whoever opens it (ADR-0091)."""
        with tempfile.TemporaryDirectory() as directory:
            _controller, service, claim, _frozen = self.paused(directory)
            japanese = service.open_routine_challenge("team_1", claim["run_id"], "ja")
            portuguese = service.open_routine_challenge("team_1", claim["run_id"], "pt")

            self.assertNotEqual(japanese["challenge_id"], portuguese["challenge_id"])
            self.assertEqual(japanese["request"], portuguese["request"])
            self.assertEqual(japanese["pack_digest"], portuguese["pack_digest"])
            self.assertEqual((japanese["locale"], portuguese["locale"]), ("ja", "pt"))
            self.assertTrue(japanese["rendered"]["title"].startswith("JA "))
            self.assertTrue(portuguese["rendered"]["title"].startswith("PT "))
            self.assertNotIn("purpose", portuguese)
            # Another language replaced the earlier challenge: only the newest one can be answered.
            with self.assertRaises(local_app.ApiProblem) as stale:
                service.resume_routine_human(
                    "team_1",
                    claim["run_id"],
                    {"challenge_id": japanese["challenge_id"], "decision": "submit", "value": True},
                    "openai",
                    "sk-test-0123456789",
                )
            self.assertEqual(stale.exception.code, "human-request-expired")

            with (
                mock.patch.object(
                    routine_human.action_challenges,
                    "relocalize",
                    side_effect=routine_human.action_challenges.HumanChallengeError("render"),
                ),
                self.assertRaises(local_app.ApiProblem) as refused,
            ):
                service.open_routine_challenge("team_1", claim["run_id"], "de")
            self.assertEqual(refused.exception.code, "human-request-invalid")
            self.assertEqual(record.run(self.state(service), claim["run_id"]).status, "frozen")

    def test_a_binding_with_another_pack_ends_the_frozen_run_on_open_or_answer(self) -> None:
        """The frozen request's copy must still come from the binding's catalog and pack (ADR-0091)."""
        for stage in ("open", "answer"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                controller, service, claim, _frozen = self.paused(directory)
                opened = service.open_routine_challenge("team_1", claim["run_id"], "en") if stage == "answer" else None
                spec = controller.registry[ASSISTANT]
                controller.registry[ASSISTANT] = dataclasses.replace(spec, pack_digest=f"sha256:{'9' * 64}")
                with self.assertRaises(local_app.ApiProblem) as changed:
                    if opened is None:
                        service.open_routine_challenge("team_1", claim["run_id"], "en")
                    else:
                        service.resume_routine_human(
                            "team_1",
                            claim["run_id"],
                            {"challenge_id": opened["challenge_id"], "decision": "submit", "value": True},
                            "openai",
                            "sk-test-0123456789",
                        )
                self.assertEqual(changed.exception.code, "team-context-changed")
                self.assertEqual(self.state(service).notices[-1].detail["code"], "team-context-changed")


class NoticeAndWatchdogTests(RoutineServiceCase):
    def test_notices_are_listed_for_every_team_and_acknowledged_by_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, Runtime())
            controller.assistant_lifecycle.invoke = lambda *_args: {"result": LOOKUP_RESULT}
            self.routine(service)
            self.run_claim(service, service.claim_routine_run(("anthropic", "openai")))
            notices = service.routine_notices()["notices"]
            self.assertEqual(
                [(item["team_id"], item["outcome"], item["version"]) for item in notices], [("team_1", "done", 1)]
            )
            for body in ({}, {"deliveries": []}, {"deliveries": [{"team_id": "team_1"}]}):
                with self.subTest(body=body), self.assertRaises(local_app.ApiProblem):
                    service.acknowledge_routine_notices(body)
            service.acknowledge_routine_notices(
                {"deliveries": [{"team_id": "team_1", "notice_id": notices[0]["notice_id"], "version": 2}]}
            )
            self.assertEqual(len(service.routine_notices()["notices"]), 1)
            service.acknowledge_routine_notices(
                {"deliveries": [{"team_id": "team_1", "notice_id": notices[0]["notice_id"], "version": 1}]}
            )
            self.assertEqual(service.routine_notices()["notices"], [])

    def test_the_watchdog_recovers_orphaned_runs_and_stops_expired_ones(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.routine(service)
            orphan = service.claim_routine_run(("anthropic", "openai"))
            service.routine_store.put_continuation("team_1", "f" * 32, b"orphaned continuation")
            routine_watchdog.check(service, startup=True)
            state = self.state(service)
            self.assertEqual((state.runs, state.notices[-1].detail), ((), {"code": "interrupted", "actions": []}))
            self.assertEqual(service.routine_store.continuations("team_1"), ())
            self.routine(service)
            running = service.claim_routine_run(("anthropic", "openai"))
            routine_run.register_routine_run(service, "team_1", running["run_id"], "token", 600)
            service._active_chat_tokens["team_1"] = "token"
            with mock.patch.object(
                record, "expired", return_value=(record.run(self.state(service), running["run_id"]),)
            ):
                routine_watchdog.check(service)
            self.assertIn("token", service._cancelled_chat_tokens)
        self.assertIsNotNone(orphan)

    def test_a_recovered_run_whose_batch_may_have_acted_is_held_as_an_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.service(directory, Runtime())
            self.routine(service)
            claim = service.claim_routine_run(("anthropic", "openai"))
            network = controller.assistant_lifecycle._network("team_1").id
            lease = record.lease_of(claim["lease_token"], KEY)
            service.routine_store.update(
                "team_1",
                lambda state: (record.bind_generation(state, claim["run_id"], lease, int(time.time()), network), None),
            )
            generation = record.generation_for(network, claim["run_id"])
            operation = local_app.action_journal.Operation("action-1", "b" * 64)
            batch = controller.action_state.prepare_batch(generation, "thread", (operation,), archivable=True)
            controller.action_state.begin(batch, operation)
            routine_watchdog.check(service, startup=True)
            state = self.state(service)
            archived = controller.action_state.current_batch(generation)
        # Never the legacy uncertain state: the run's incident keeps the evidence and its batch is archived.
        self.assertEqual(state.runs, ())
        self.assertEqual([item.incident_id for item in state.incidents], [claim["run_id"]])
        self.assertEqual(archived, (batch.fingerprint, "archived"))

    def test_the_watchdog_survives_a_failed_audit_and_runs_its_next_pass(self) -> None:
        second_pass = threading.Event()
        passes = []

        def teams():
            passes.append(1)
            if len(passes) >= 2:
                second_pass.set()
            raise routine_store.RoutineStoreError("down")

        service = SimpleNamespace(
            _active_chat_guard=threading.Lock(),
            _routine_runs={},
            routine_store=SimpleNamespace(teams=teams),
        )
        local_audit.record.side_effect = RuntimeError("the local audit journal could not be synchronized")
        watchdog = routine_watchdog.RoutineWatchdog(service, interval=0.01)
        with self.assertLogs(routine_watchdog.log, "ERROR") as logs:
            watchdog.start()
            self.assertTrue(second_pass.wait(5))
            self.assertTrue(watchdog._thread.is_alive())
            watchdog.close()
        self.assertIn("could not audit routine-watchdog/check-failed (RuntimeError)", logs.output[0])
        self.assertNotIn("synchronized", "".join(logs.output))

    def test_a_failed_recovery_audit_never_stops_the_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.routine(service)
            service.claim_routine_run(("anthropic", "openai"))
            service.routine_store.put_continuation("team_1", "f" * 32, b"orphaned continuation")
            local_audit.record.side_effect = RuntimeError("the local audit journal could not be written")
            with self.assertLogs(routine_watchdog.log, "ERROR") as logs:
                routine_watchdog.check(service, startup=True)
            self.assertEqual(self.state(service).runs, ())
            self.assertEqual(service.routine_store.continuations("team_1"), ())
        self.assertIn("could not audit routine-recover/failed (RuntimeError)", logs.output[0])

    def test_a_failed_watchdog_pass_is_audited_and_retried(self) -> None:
        service = SimpleNamespace(
            _active_chat_guard=threading.Lock(),
            _routine_runs={},
            routine_store=SimpleNamespace(teams=mock.Mock(side_effect=routine_store.RoutineStoreError("down"))),
        )
        watchdog = routine_watchdog.RoutineWatchdog(service, interval=0.01)
        watchdog.start()
        time.sleep(0.05)
        watchdog.close()
        self.assertGreaterEqual(service.routine_store.teams.call_count, 1)
        local_audit.record.assert_any_call(
            "routine-watchdog",
            result="error",
            principal=routine_watchdog._PRINCIPAL,
            team_id=None,
            detail="check-failed",
        )
        self.assertIs(routine_run._problem(409, "x", "y").code, "y")
