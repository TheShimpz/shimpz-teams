import sqlite3
import sys
import tempfile
import threading
from contextlib import closing
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
from local_controller_harness import LOOKUP_INPUT, LOOKUP_RESULT, LocalContractCase, chat_body

from action import execution as action_execution
from action import human as action_human
from assistant import spec as assistant_spec
from inference import client as brain_runtime_client
from local import app as local_app
from local import audit as local_audit
from tests import catalog_fixtures, human_request_fixtures

LOCAL_TEAM_RESIDUES = [
    "action_checkpoints",
    "assistant_containers",
    "brain_checkpoints",
    "chat_continuations",
    "egress_policies",
    "inference_configuration",
    "integration_credentials",
    "preparation_helpers",
    "publication_bindings",
    "routines",
    "runtime_state",
    "stored_inputs",
    "team_names",
    "team_networks",
    "team_storage",
]
LIST_ZONES = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)


class PausingRuntime:
    """A Brain that asks for one list-zones Action and refuses any resume; ``fresh`` builds a new request per turn."""

    purpose = staticmethod(lambda *_args: None)

    def __init__(self, refusal: str, *, fresh: bool = False) -> None:
        self.resume = mock.Mock(side_effect=AssertionError(refusal))
        self.fresh = fresh

    def start(self, _context, _message, *, conversation=()):
        request = LIST_ZONES
        if self.fresh:
            request = brain_runtime_client.ActionRequest(
                "action-1", "shimpz-cloudflare", "list-zones", dict(LOOKUP_INPUT)
            )
        return brain_runtime_client.RuntimeTurn("action-required", "", (request,))


def restarted_chat_service(action_state: object, chat_continuations: object) -> local_app.ChatTurnService:
    """The chat service a restarted Controller builds: fresh challenge stores over durable journal and continuations."""
    return local_app.ChatTurnService(
        local_app.ChatTurnDependencies(
            action_state=action_state,
            integration_challenges=local_app.integration_challenges.IntegrationChallengeStore(),
            human_challenges=local_app.action_challenges.HumanChallengeStore(),
            chat_continuations=chat_continuations,
        )
    )


def _chat(controller: local_app.LocalController, message: str, **fields: object) -> dict[str, object]:
    body = chat_body(message, assistant_ids=["shimpz-cloudflare"], **fields)
    return controller.chat_turn_service.chat("team_1", body, "openai", "sk-test-0123456789")


def _resume(controller: local_app.LocalController, body: dict[str, object]) -> dict[str, object]:
    return controller.chat_turn_service.resume_chat_human("team_1", body, "openai", "sk-test-0123456789")


def _suspend(controller: local_app.LocalController, request: action_human.HumanRequest) -> None:
    controller.assistant_lifecycle.invoke = lambda *_args: (_ for _ in ()).throw(
        action_human.HumanRequestSuspensionError(request)
    )


def _batch_count(controller: local_app.LocalController) -> tuple[int]:
    with closing(sqlite3.connect(controller.action_state.path)) as connection:
        return connection.execute("SELECT COUNT(*) FROM batches").fetchone()


def _identity_request(kind: str) -> action_human.HumanRequest:
    descriptor = {
        "kind": kind,
        "ordinal": 0,
        "title": "Confirm identity",
        "description": "Confirm current identity before continuing.",
    }
    return human_request_fixtures.admit(human_request_fixtures.fingerprinted(descriptor), (kind,))


class LocalTurnLifecycleTests(LocalContractCase):
    def test_local_snapshot_persists_an_integration_pause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = PausingRuntime("the missing Integration must pause before Brain resume")
            controller = self._chat_controller(directory, runtime)
            controller.assistant_integrations.delete_assistant("team_1", "shimpz-cloudflare")
            paused = _chat(controller, "List zones")
            stored = controller.chat_continuations.current("team_1")
            state_exists = controller.chat_continuations.state_path.is_file()
            key_exists = controller.chat_continuations.key_path.is_file()

        self.assertEqual(paused["status"], "integrations-required")
        self.assertEqual(paused["challenge_id"], stored.challenge_id)
        self.assertTrue(state_exists)
        self.assertTrue(key_exists)
        runtime.resume.assert_not_called()

    def test_local_human_approval_replays_the_same_action_before_brain_resume(self) -> None:
        request = LIST_ZONES

        class Runtime:
            resumes = 0

            def __init__(self) -> None:
                self.locales: list[str | None] = []
                self.purposes: list[tuple[object, ...]] = []

            def start(self, context, _message, *, conversation=()):
                self.locales.append(context.locale)
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def purpose(self, *args):
                self.purposes.append(args[1:])
                return "To list your zones, I need to read them in Cloudflare."

            def resume(self, _context, results):
                self.resumes += 1
                if results != {"action-1": LOOKUP_RESULT}:
                    raise AssertionError("approved result changed")
                return brain_runtime_client.RuntimeTurn("completed", "Approved", ())

        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime()
            controller = self._chat_controller(directory, runtime)
            admitted = human_request_fixtures.list_zones_approval()
            invocations: list[tuple[object, ...]] = []

            def invoke(*args):
                invocations.append(args)
                if len(invocations) == 1:
                    raise action_human.HumanRequestSuspensionError(admitted)
                self.assertEqual(
                    args[4].transcript.payloads(),
                    (action_human.admit_response(admitted, True).payload(),),
                )
                return {"result": LOOKUP_RESULT}

            controller.assistant_lifecycle.invoke = invoke
            paused = _chat(controller, "List zones", locale="pt")
            self.assertEqual(paused["status"], "human-required")
            self.assertEqual(paused["purpose"], "To list your zones, I need to read them in Cloudflare.")
            self.assertNotIn("help_url", paused)
            self.assertNotIn("help", paused)
            # The canonical request keeps its references; the copy is rendered in the turn's language (ADR-0091).
            self.assertEqual(paused["request"], admitted.payload())
            self.assertEqual((paused["locale"], paused["rendered"]["title"]), ("pt", "PT List zones"))
            self.assertEqual(paused["pack_digest"], controller.registry["shimpz-cloudflare"].pack_digest)
            self.assertEqual(runtime.locales, ["pt"])
            self.assertEqual(runtime.purposes, [(request, "Shimpz Cloudflare", runtime.purposes[0][2])])
            self.assertEqual(runtime.resumes, 0)

            completed = _resume(
                controller, {"challenge_id": paused["challenge_id"], "decision": "submit", "value": True}
            )

        self.assertEqual(completed["reply"], "Approved")
        self.assertEqual(runtime.resumes, 1)
        self.assertEqual(len(invocations), 2)

    def test_a_stored_input_supplied_by_one_batched_action_serves_its_siblings(self) -> None:
        # Page text makes each result larger than 32 KiB of UTF-8, as a real Exa search with include_text does.
        page_text = " ação" * 8_000
        schema = {"type": "object", "additionalProperties": False, "properties": {"query": {"type": "string"}}}
        request = {
            "kind": "input:password",
            "ordinal": 0,
            "title": "Exa API key",
            "description": "Provide the key once.",
            "label": "Exa API key",
            "required": True,
            "placeholder": None,
            "min_length": 8,
            "max_length": 256,
            "stored_input": "exa-api-key",
        }
        request = human_request_fixtures.fingerprinted(request)
        batch = (
            brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "search-web", {"query": "news"}),
            brain_runtime_client.ActionRequest("action-2", "shimpz-cloudflare", "search-web", {"query": "brazil"}),
        )

        class Runtime:
            purpose = staticmethod(lambda *_args: None)

            def start(self, _context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn("action-required", "", batch)

            def resume(self, _context, results):
                if results != {"action-1": {"query": "news" + page_text}, "action-2": {"query": "brazil" + page_text}}:
                    raise AssertionError("the batched results changed")
                return brain_runtime_client.RuntimeTurn("completed", "Searched", ())

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            spec = controller.registry["shimpz-cloudflare"]
            controller.registry["shimpz-cloudflare"] = replace(
                spec,
                actions={
                    **spec.actions,
                    "search-web": assistant_spec.ActionSpec(
                        "Search the web", schema, schema, (), ("exa-api-key",), ("input:password",)
                    ),
                },
                stored_inputs={
                    "exa-api-key": assistant_spec.StoredInputSpec(
                        "password", "Exa API key", catalog_fixtures.STORED_INPUT_HELP, catalog_fixtures.HELP_URL
                    )
                },
            )
            supplied: list[tuple[str, list[str]]] = []

            def rpc(_container, _action, payload, _broker=None):
                supplied.append((payload["input"]["query"], sorted(payload["stored_inputs"])))
                if not payload["stored_inputs"] and not payload.get("responses"):
                    return {"type": "request", "request": request}
                return {"type": "result", "result": {"query": payload["input"]["query"] + page_text}}

            controller.assistant_lifecycle._rpc = rpc
            with mock.patch.object(local_audit, "record_request", return_value="a" * 32):
                paused = _chat(controller, "Search")
                completed = _resume(
                    controller,
                    {"challenge_id": paused["challenge_id"], "decision": "submit", "value": "exa-key-0123456789"},
                )

        self.assertEqual(paused["status"], "human-required")
        # A Stored Input request shows its binding's help text and help link, never an Action-supplied one.
        self.assertEqual(
            (paused["help"], paused["help_url"]), (catalog_fixtures.STORED_INPUT_HELP, catalog_fixtures.HELP_URL)
        )
        self.assertEqual(completed["reply"], "Searched")
        self.assertEqual(supplied[-1], ("brazil", ["exa-api-key"]))

    def test_two_missing_stored_inputs_are_each_asked_once_sealed_and_then_both_delivered(self) -> None:
        schema = {"type": "object", "additionalProperties": False, "properties": {"query": {"type": "string"}}}

        def slot_request(stored_input: str) -> dict[str, object]:
            return human_request_fixtures.fingerprinted(
                {
                    "kind": "input:password",
                    "ordinal": 0,
                    "title": "Exa API key",
                    "description": "Provide the key once.",
                    "label": "Exa API key",
                    "required": True,
                    "placeholder": None,
                    "min_length": 8,
                    "max_length": 256,
                    "stored_input": stored_input,
                }
            )

        batch = (brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "search-web", {"query": "news"}),)

        class Runtime:
            purpose = staticmethod(lambda *_args: None)

            def start(self, _context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn("action-required", "", batch)

            def resume(self, _context, results):
                if results != {"action-1": {"query": "news"}}:
                    raise AssertionError("the result changed")
                return brain_runtime_client.RuntimeTurn("completed", "Searched", ())

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            spec = controller.registry["shimpz-cloudflare"]
            declaration = assistant_spec.StoredInputSpec(
                "password", "Exa API key", catalog_fixtures.STORED_INPUT_HELP, catalog_fixtures.HELP_URL
            )
            controller.registry["shimpz-cloudflare"] = replace(
                spec,
                actions={
                    **spec.actions,
                    "search-web": assistant_spec.ActionSpec(
                        "Search the web", schema, schema, (), ("exa-account", "exa-api-key"), ("input:password",)
                    ),
                },
                stored_inputs={"exa-account": declaration, "exa-api-key": declaration},
            )
            supplied: list[dict[str, object]] = []

            def rpc(_container, _action, payload, _broker=None):
                supplied.append(dict(payload))
                for slot in ("exa-api-key", "exa-account"):
                    if slot not in payload["stored_inputs"]:
                        return {"type": "request", "request": slot_request(slot)}
                return {"type": "result", "result": {"query": payload["input"]["query"]}}

            controller.assistant_lifecycle._rpc = rpc
            with mock.patch.object(local_audit, "record_request", return_value="a" * 32) as audit:
                first = _chat(controller, "Search")
                second = _resume(
                    controller,
                    {"challenge_id": first["challenge_id"], "decision": "submit", "value": "test-exa-key"},
                )
                completed = _resume(
                    controller,
                    {"challenge_id": second["challenge_id"], "decision": "submit", "value": "test-account"},
                )
            store = controller.assistant_stored_inputs
            sealed = {
                slot: store.resolve("team_1", "shimpz-cloudflare", slot, "password").value
                for slot in ("exa-account", "exa-api-key")
            }

        self.assertEqual((first["status"], second["status"]), ("human-required", "human-required"))
        self.assertEqual(completed["reply"], "Searched")
        self.assertEqual(sealed, {"exa-account": "test-account", "exa-api-key": "test-exa-key"})
        self.assertEqual(
            [sorted(payload["stored_inputs"]) for payload in supplied],
            [[], ["exa-api-key"], ["exa-account", "exa-api-key"]],
        )
        # Neither value ever travels as a replay response; each slot is listed as held once sealed.
        self.assertTrue(all("responses" not in payload for payload in supplied))
        details = [call.kwargs.get("detail", "") for call in audit.call_args_list]
        self.assertIn("stored-input-sealed:search-web:exa-api-key", details)
        self.assertIn("stored-input-sealed:search-web:exa-account", details)
        for value in sealed.values():
            self.assertNotIn(value, repr(audit.call_args_list))
            # The Action is told which slots Team holds, never a value (ADR-0106).
            self.assertNotIn(value, repr(supplied))

    def test_denied_human_request_purges_the_action_batch_without_brain_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = PausingRuntime("a denied Action must not resume the Brain")
            controller = self._chat_controller(directory, runtime)
            _suspend(controller, human_request_fixtures.list_zones_approval())
            paused = _chat(controller, "List zones")
            denied = _resume(controller, {"challenge_id": paused["challenge_id"], "decision": "deny"})
            batches = _batch_count(controller)

        self.assertEqual(denied["status"], "human-denied")
        self.assertEqual(batches, (0,))
        runtime.resume.assert_not_called()

    def test_restart_purges_an_expired_human_continuation_and_unblocks_the_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = PausingRuntime("an expired Action must not resume the Brain")
            controller = self._chat_controller(directory, runtime)
            admitted = human_request_fixtures.list_zones_approval()
            _suspend(controller, admitted)
            paused = _chat(controller, "List zones")
            before = _batch_count(controller)

            reopened = local_app.local_chat_continuation_store.EncryptedContinuationStore(
                controller.chat_continuations.state_path,
                controller.chat_continuations.key_path,
                now=lambda: 2_200_000_000,
            )
            restarted = restarted_chat_service(controller.action_state, reopened)

            restarted._restore_all_chat_continuations()

            after = _batch_count(controller)
            next_batch = controller.action_state.prepare_batch(
                "a" * 64,
                "next-thread",
                (local_app.action_journal.Operation("action-2", "b" * 64),),
            )

        self.assertEqual(paused["status"], "human-required")
        self.assertEqual(before, (1,))
        self.assertEqual(after, (0,))
        self.assertIsNone(reopened.current("team_1"))
        self.assertEqual(next_batch.generation, "a" * 64)
        runtime.resume.assert_not_called()

    def test_running_controller_purges_an_expired_human_challenge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = PausingRuntime("an expired Action must not resume the Brain")
            controller = self._chat_controller(directory, runtime)
            admitted = human_request_fixtures.list_zones_approval()
            _suspend(controller, admitted)
            _chat(controller, "List zones")
            challenge = controller.chat_turn_service.human_challenges.current("team_1")
            self.assertIsNotNone(challenge)

            controller.chat_turn_service.human_challenges._clock = lambda: challenge.expires_at
            controller.chat_turn_service._expire_human_challenges()

            batches = _batch_count(controller)
            next_batch = controller.action_state.prepare_batch(
                "a" * 64,
                "next-thread",
                (local_app.action_journal.Operation("action-2", "b" * 64),),
            )

        self.assertEqual(batches, (0,))
        self.assertIsNone(controller.chat_continuations.current("team_1"))
        self.assertEqual(next_batch.generation, "a" * 64)
        runtime.resume.assert_not_called()

    def test_unavailable_strong_local_auth_assurance_auto_blocks_without_a_fake_prompt(self) -> None:
        for kind in sorted(action_human.AUTH_KINDS - {"auth:password"}):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                runtime = PausingRuntime("unavailable authentication must stop the turn")
                controller = self._chat_controller(directory, runtime)
                _suspend(controller, _identity_request(kind))

                response = _chat(controller, "List zones")
                batches = _batch_count(controller)

                self.assertEqual(response["status"], "human-denied")
                self.assertEqual(response["reason"], "authentication-unavailable")
                self.assertEqual(batches, (0,))
                self.assertIsNone(controller.chat_turn_service.human_challenges.current("team_1"))
                self.assertIsNone(controller.chat_turn_service.chat_continuations.current("team_1"))
                runtime.resume.assert_not_called()

    def test_local_reauthentication_pauses_for_supervisor_assurance(self) -> None:
        admitted = _identity_request("auth:password")

        with tempfile.TemporaryDirectory() as directory:
            runtime = PausingRuntime("reauthentication must pause before Action replay")
            controller = self._chat_controller(directory, runtime)
            _suspend(controller, admitted)
            response = _chat(controller, "List zones")

            self.assertEqual(response["status"], "human-required")
            self.assertEqual(response["request"]["kind"], "auth:password")
            self.assertIsNotNone(controller.chat_turn_service.human_challenges.current("team_1"))
            self.assertIsNotNone(controller.chat_turn_service.chat_continuations.current("team_1"))
            runtime.resume.assert_not_called()

    def test_failed_reauthentication_resume_requires_a_fresh_request_without_wedging_team(self) -> None:
        request = LIST_ZONES

        class Runtime:
            purpose = staticmethod(lambda *_args: None)

            def start(self, _context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def resume(self, _context, results):
                if results != {"action-1": LOOKUP_RESULT}:
                    raise AssertionError("reauthenticated result changed")
                return brain_runtime_client.RuntimeTurn("completed", "Recovered", ())

        admitted = _identity_request("auth:password")

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            invocations: list[tuple[object, ...]] = []

            def invoke(*args):
                invocations.append(args)
                if len(invocations) in {1, 3}:
                    raise action_human.HumanRequestSuspensionError(admitted)
                if len(invocations) == 2:
                    raise local_app.ApiProblem(
                        HTTPStatus.BAD_GATEWAY,
                        "private Assistant failure",
                        code="assistant-rpc-failed",
                    )
                return {"result": LOOKUP_RESULT}

            controller.assistant_lifecycle.invoke = invoke
            first_pause = _chat(controller, "List zones")
            with self.assertRaises(local_app.ApiProblem) as failed:
                _resume(controller, {"challenge_id": first_pause["challenge_id"], "decision": "submit", "value": True})
            second_pause = _chat(controller, "List zones")
            completed = _resume(
                controller, {"challenge_id": second_pause["challenge_id"], "decision": "submit", "value": True}
            )

        self.assertEqual(failed.exception.code, "assistant-rpc-failed")
        self.assertEqual(first_pause["status"], "human-required")
        self.assertEqual(second_pause["status"], "human-required")
        self.assertNotEqual(first_pause["challenge_id"], second_pause["challenge_id"])
        self.assertEqual(completed["reply"], "Recovered")
        self.assertEqual(len(invocations), 4)

    def test_chat_stop_does_not_hold_the_global_guard_during_action_termination(self) -> None:
        token = "turn-token"
        container = object()
        stop_started = threading.Event()
        release_stop = threading.Event()
        result: list[dict[str, object]] = []
        team_lock = threading.RLock()
        service = local_app.ChatTurnService(
            local_app.ChatTurnDependencies(
                integration_challenges=SimpleNamespace(withdraw_team=lambda _team_id: None),
                oauth_pkce=SimpleNamespace(cancel_team=lambda _team_id: None),
                lock_for=lambda _team_id: team_lock,
            )
        )
        service._delete_chat_continuation = lambda _team_id: False
        service._active_chat_tokens["team_1"] = token
        service._active_action_containers["team_1"] = (token, container)

        def fail_stop_action(actual_container: object) -> None:
            self.assertIs(actual_container, container)
            self.assertIn(token, service._cancelled_chat_tokens)
            stop_started.set()
            release_stop.wait(timeout=2)

        service.assistant_lifecycle = SimpleNamespace(
            _network=lambda _team_id: None,
            _fail_stop_action=fail_stop_action,
        )
        worker = threading.Thread(target=lambda: result.append(service.stop_chat("team_1")), daemon=True)
        worker.start()
        self.assertTrue(stop_started.wait(timeout=1))
        self.assertTrue(service._active_chat_guard.acquire(timeout=0.1))
        service._active_chat_guard.release()
        release_stop.set()
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertIs(result[0]["confirmed"], True)

    def test_team_identity_drift_stops_before_the_provider_call(self) -> None:
        class Runtime:
            def start(self, _context, _message, *, conversation=()):
                raise AssertionError("a changed Team must not reach the provider")

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            names = iter(("Marketing", "Renamed"))
            controller.assistant_lifecycle._validate_network = lambda _network, _team_id, **_kwargs: next(names)

            with self.assertRaises(local_app.ApiProblem) as caught:
                _chat(controller, "Hello")

        self.assertEqual(caught.exception.code, "team-context-changed")

    def test_chat_executes_only_a_controller_owned_declared_action(self) -> None:
        class Runtime:
            def start(self, _context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn(
                    status="action-required",
                    reply="",
                    actions=(
                        brain_runtime_client.ActionRequest(
                            interrupt_id="action-1",
                            assistant_id="shimpz-cloudflare",
                            action="list-zones",
                            input=LOOKUP_INPUT,
                        ),
                    ),
                )

            def resume(self, _context, results):
                if results != {"action-1": LOOKUP_RESULT}:
                    raise AssertionError("Action result did not return through the Controller")
                return brain_runtime_client.RuntimeTurn(status="completed", reply="Done", actions=())

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            invoked: list[tuple[str, str, object]] = []
            controller.invoke = lambda team_id, assistant, action, payload, _evidence: (
                invoked.append((team_id, assistant, payload))
                or {"assistant": assistant, "action": action, "result": LOOKUP_RESULT}
            )
            controller.assistant_lifecycle.invoke = controller.invoke
            response = _chat(controller, "Greet me")

        self.assertEqual(invoked, [("team_1", "shimpz-cloudflare", LOOKUP_INPUT)])
        self.assertEqual(
            response,
            {
                "team_id": "team_1",
                "team_name": "Marketing",
                "reply": "Done",
                "clarification": None,
            },
        )

    def test_chat_reuses_a_completed_action_after_resume_failure_then_delivers(self) -> None:
        request = LIST_ZONES

        class Runtime:
            resumes = 0

            def start(self, _context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn(status="action-required", reply="", actions=(request,))

            def resume(self, _context, results):
                self.resumes += 1
                if results != {"action-1": LOOKUP_RESULT}:
                    raise AssertionError("cached Action result changed")
                if self.resumes == 1:
                    raise brain_runtime_client.BrainRuntimeError("private-resume-failure")
                return brain_runtime_client.RuntimeTurn(status="completed", reply="Done", actions=())

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            invocations: list[object] = []
            controller.invoke = lambda _team_id, assistant, action, payload, _evidence: (
                invocations.append(payload) or {"assistant": assistant, "action": action, "result": LOOKUP_RESULT}
            )
            controller.assistant_lifecycle.invoke = controller.invoke
            with self.assertRaises(local_app.ApiProblem) as first:
                _chat(controller, "Greet me")

            response = _chat(controller, "Greet me")
            pending = _batch_count(controller)

        self.assertEqual(first.exception.code, "brain-runtime-failed")
        self.assertNotIn("private-resume-failure", str(first.exception))
        self.assertEqual(invocations, [LOOKUP_INPUT])
        self.assertEqual(response["reply"], "Done")
        self.assertEqual(pending, (0,))

    def test_chat_ends_an_orphaned_paused_batch_by_network_generation(self) -> None:
        class Runtime:
            @staticmethod
            def start(_context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn(status="completed", reply="Recovered", actions=())

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            first = local_app.action_journal.Operation("action-1", "b" * 64)
            second = local_app.action_journal.Operation("action-2", "c" * 64)
            orphan = controller.action_state.prepare_batch("a" * 64, "orphan-thread", (first, second))
            controller.action_state.begin(orphan, first)
            controller.action_state.complete(orphan, first, {"ok": True})
            controller.action_state.begin(orphan, second)
            controller.action_state.suspend(orphan, second)

            response = _chat(controller, "Start a fresh turn")
            with closing(sqlite3.connect(controller.action_state.path)) as connection:
                batches = connection.execute("SELECT state FROM batches").fetchall()

        self.assertEqual(response["reply"], "Recovered")
        self.assertEqual(batches, [("ended",)])

    def test_terminal_rpc_failure_does_not_wedge_the_next_independent_turn(self) -> None:
        request = LIST_ZONES

        class Runtime:
            def start(self, _context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn(status="action-required", reply="", actions=(request,))

            def resume(self, _context, results):
                if results != {"action-1": LOOKUP_RESULT}:
                    raise AssertionError("the independent Action result changed")
                return brain_runtime_client.RuntimeTurn(status="completed", reply="Recovered", actions=())

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            invocations: list[object] = []

            def fail_rpc(*_args):
                invocations.append("rpc")
                if len(invocations) == 1:
                    raise local_app.ApiProblem(
                        HTTPStatus.BAD_GATEWAY,
                        "private Assistant failure",
                        code="assistant-rpc-failed",
                    )
                return {"result": LOOKUP_RESULT}

            controller.invoke = fail_rpc
            controller.assistant_lifecycle.invoke = controller.invoke
            with self.assertRaises(local_app.ApiProblem) as first:
                _chat(controller, "Greet me")
            self.assertEqual(invocations, ["rpc"])
            retry = _chat(controller, "Greet me")

        self.assertEqual(first.exception.code, "assistant-rpc-failed")
        self.assertNotIn("private Assistant failure", str(retry))
        self.assertEqual(retry["reply"], "Recovered")
        self.assertEqual(invocations, ["rpc", "rpc"])

    def test_crash_uncertain_batch_remains_blocked_across_identical_local_retries(self) -> None:
        request = LIST_ZONES

        class Runtime:
            @staticmethod
            def start(_context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn(status="action-required", reply="", actions=(request,))

            @staticmethod
            def resume(_context, _results):
                raise AssertionError("an uncertain Action must not reach Brain resume")

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            assistant = controller.registry["shimpz-cloudflare"]
            operation = action_execution.action_operation(
                request,
                "assistant-container",
                assistant.image,
                integration_generations=(("cloudflare", 1),),
            )
            generation = "a" * 64
            batch = controller.action_state.prepare_batch(
                generation,
                local_app._brain_thread_id("local-space", "team_1", generation),
                (operation,),
            )
            controller.action_state.begin(batch, operation)
            controller.assistant_lifecycle.invoke = lambda *_args: (_ for _ in ()).throw(
                AssertionError("an uncertain Action must not execute")
            )

            for attempt in range(2):
                with self.subTest(attempt=attempt), self.assertRaises(local_app.ApiProblem) as failed:
                    _chat(controller, "Greet me")
                self.assertEqual(failed.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
                self.assertEqual(failed.exception.code, "action-state-unavailable")

            with self.assertRaises(local_app.action_journal.ActionJournalUncertainError):
                controller.action_state.begin(batch, operation)
