from __future__ import annotations

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
from local_controller_harness import LocalContractCase

from action import execution as action_execution
from action import human as action_human
from assistant import spec as assistant_spec
from inference import client as brain_runtime_client
from local import app as local_app
from local import audit as local_audit
from tests import human_request_fixtures

LOOKUP_INPUT = {"page": 1, "per_page": 25}
LOOKUP_RESULT = {
    "zones": [],
    "pagination": {"page": 1, "per_page": 25, "count": 0, "total_count": 0, "total_pages": 0},
}
TEST_ACCOUNT_ACCESS_TOKEN = "-".join(("oauth", "access", "test", "token", "123456789"))
TEST_ACCOUNT_REFRESH_TOKEN = "-".join(("oauth", "refresh", "test", "token", "123456789"))
LOCAL_TEAM_RESIDUES = [
    "action_checkpoints",
    "assistant_containers",
    "brain_checkpoints",
    "chat_continuations",
    "egress_policies",
    "inference_configuration",
    "integration_credentials",
    "publication_bindings",
    "routines",
    "runtime_state",
    "stored_inputs",
    "team_names",
    "team_networks",
    "team_storage",
]


class LocalTurnLifecycleTests(LocalContractCase):
    @staticmethod
    def _approval_request() -> action_human.HumanRequest:
        descriptor = {
            "kind": "approval",
            "ordinal": 0,
            "title": "List zones",
            "description": "Allow this Action to list the reviewed Cloudflare zones.",
        }
        return human_request_fixtures.admit(human_request_fixtures.fingerprinted(descriptor), ("approval",))

    def test_local_snapshot_persists_an_integration_pause(self) -> None:
        request = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

        class Runtime:
            def start(self, _context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def resume(self, _context, _results):
                raise AssertionError("the missing Integration must pause before Brain resume")

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            controller.assistant_integrations.delete_assistant("team_1", "shimpz-cloudflare")
            paused = controller.chat_turn_service.chat(
                "team_1",
                {
                    "message": "List zones",
                    "files": [],
                    "assistant_ids": ["shimpz-cloudflare"],
                    "conversation": [],
                    "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                    "timezone": None,
                    "locale": None,
                },
                "openai",
                "sk-test-0123456789",
            )
            stored = controller.chat_continuations.current("team_1")
            state_exists = controller.chat_continuations.state_path.is_file()
            key_exists = controller.chat_continuations.key_path.is_file()

        self.assertEqual(paused["status"], "integrations-required")
        self.assertEqual(paused["challenge_id"], stored.challenge_id)
        self.assertTrue(state_exists)
        self.assertTrue(key_exists)

    def test_local_human_approval_replays_the_same_action_before_brain_resume(self) -> None:
        request = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

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
            admitted = self._approval_request()
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
            paused = controller.chat_turn_service.chat(
                "team_1",
                {
                    "message": "List zones",
                    "files": [],
                    "assistant_ids": ["shimpz-cloudflare"],
                    "conversation": [],
                    "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                    "timezone": None,
                    "locale": "pt",
                },
                "openai",
                "sk-test-0123456789",
            )
            self.assertEqual(paused["status"], "human-required")
            self.assertEqual(paused["purpose"], "To list your zones, I need to read them in Cloudflare.")
            self.assertNotIn("help_url", paused)
            # The canonical request keeps its references; the copy is rendered in the turn's language (ADR-0091).
            self.assertEqual(paused["request"], admitted.payload())
            self.assertEqual((paused["locale"], paused["rendered"]["title"]), ("pt", "PT List zones"))
            self.assertEqual(paused["pack_digest"], controller.registry["shimpz-cloudflare"].pack_digest)
            self.assertEqual(runtime.locales, ["pt"])
            self.assertEqual(runtime.purposes, [(request, "Shimpz Cloudflare", runtime.purposes[0][2])])
            self.assertEqual(runtime.resumes, 0)

            completed = controller.chat_turn_service.resume_chat_human(
                "team_1",
                {"challenge_id": paused["challenge_id"], "decision": "submit", "value": True},
                "openai",
                "sk-test-0123456789",
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
                stored_inputs={"exa-api-key": assistant_spec.StoredInputSpec("password", "Exa API key", "Key")},
            )
            supplied: list[tuple[str, list[str]]] = []

            def rpc(_container, _action, payload):
                supplied.append((payload["input"]["query"], sorted(payload["stored_inputs"])))
                if not payload["stored_inputs"] and not payload.get("responses"):
                    return {"type": "request", "request": request}
                return {"type": "result", "result": {"query": payload["input"]["query"] + page_text}}

            controller.assistant_lifecycle._rpc = rpc
            with mock.patch.object(local_audit, "record_request", return_value="a" * 32):
                paused = controller.chat_turn_service.chat(
                    "team_1",
                    {
                        "message": "Search",
                        "files": [],
                        "assistant_ids": ["shimpz-cloudflare"],
                        "conversation": [],
                        "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                        "timezone": None,
                        "locale": None,
                    },
                    "openai",
                    "sk-test-0123456789",
                )
                completed = controller.chat_turn_service.resume_chat_human(
                    "team_1",
                    {"challenge_id": paused["challenge_id"], "decision": "submit", "value": "exa-key-0123456789"},
                    "openai",
                    "sk-test-0123456789",
                )

        self.assertEqual(paused["status"], "human-required")
        self.assertEqual(completed["reply"], "Searched")
        self.assertEqual(supplied[-1], ("brazil", ["exa-api-key"]))

    def test_denied_human_request_purges_the_action_batch_without_brain_resume(self) -> None:
        request = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

        class Runtime:
            purpose = staticmethod(lambda *_args: None)

            def start(self, _context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def resume(self, _context, _results):
                raise AssertionError("a denied Action must not resume the Brain")

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            controller.assistant_lifecycle.invoke = lambda *_args: (_ for _ in ()).throw(
                action_human.HumanRequestSuspensionError(self._approval_request())
            )
            paused = controller.chat_turn_service.chat(
                "team_1",
                {
                    "message": "List zones",
                    "files": [],
                    "assistant_ids": ["shimpz-cloudflare"],
                    "conversation": [],
                    "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                    "timezone": None,
                    "locale": None,
                },
                "openai",
                "sk-test-0123456789",
            )
            denied = controller.chat_turn_service.resume_chat_human(
                "team_1",
                {"challenge_id": paused["challenge_id"], "decision": "deny"},
                "openai",
                "sk-test-0123456789",
            )
            with closing(sqlite3.connect(controller.action_state.path)) as connection:
                batches = connection.execute("SELECT COUNT(*) FROM batches").fetchone()

        self.assertEqual(denied["status"], "human-denied")
        self.assertEqual(batches, (0,))

    def test_restart_purges_an_expired_human_continuation_and_unblocks_the_generation(self) -> None:
        request = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

        class Runtime:
            purpose = staticmethod(lambda *_args: None)

            def start(self, _context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def resume(self, _context, _results):
                raise AssertionError("an expired Action must not resume the Brain")

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            admitted = self._approval_request()
            controller.assistant_lifecycle.invoke = lambda *_args: (_ for _ in ()).throw(
                action_human.HumanRequestSuspensionError(admitted)
            )
            paused = controller.chat_turn_service.chat(
                "team_1",
                {
                    "message": "List zones",
                    "files": [],
                    "assistant_ids": ["shimpz-cloudflare"],
                    "conversation": [],
                    "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                    "timezone": None,
                    "locale": None,
                },
                "openai",
                "sk-test-0123456789",
            )
            with closing(sqlite3.connect(controller.action_state.path)) as connection:
                before = connection.execute("SELECT COUNT(*) FROM batches").fetchone()

            reopened = local_app.local_chat_continuation_store.EncryptedContinuationStore(
                controller.chat_continuations.state_path,
                controller.chat_continuations.key_path,
                now=lambda: 2_200_000_000,
            )
            restarted = local_app.ChatTurnService(
                local_app.ChatTurnDependencies(
                    action_state=controller.action_state,
                    integration_challenges=local_app.integration_challenges.IntegrationChallengeStore(),
                    human_challenges=local_app.action_challenges.HumanChallengeStore(),
                    chat_continuations=reopened,
                )
            )

            restarted._restore_all_chat_continuations()

            with closing(sqlite3.connect(controller.action_state.path)) as connection:
                after = connection.execute("SELECT COUNT(*) FROM batches").fetchone()
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

    def test_running_controller_purges_an_expired_human_challenge(self) -> None:
        request = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

        class Runtime:
            purpose = staticmethod(lambda *_args: None)

            def start(self, _context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def resume(self, _context, _results):
                raise AssertionError("an expired Action must not resume the Brain")

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            admitted = self._approval_request()
            controller.assistant_lifecycle.invoke = lambda *_args: (_ for _ in ()).throw(
                action_human.HumanRequestSuspensionError(admitted)
            )
            controller.chat_turn_service.chat(
                "team_1",
                {
                    "message": "List zones",
                    "files": [],
                    "assistant_ids": ["shimpz-cloudflare"],
                    "conversation": [],
                    "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                    "timezone": None,
                    "locale": None,
                },
                "openai",
                "sk-test-0123456789",
            )
            challenge = controller.chat_turn_service.human_challenges.current("team_1")
            self.assertIsNotNone(challenge)

            controller.chat_turn_service.human_challenges._clock = lambda: challenge.expires_at
            controller.chat_turn_service._expire_human_challenges()

            with closing(sqlite3.connect(controller.action_state.path)) as connection:
                batches = connection.execute("SELECT COUNT(*) FROM batches").fetchone()
            next_batch = controller.action_state.prepare_batch(
                "a" * 64,
                "next-thread",
                (local_app.action_journal.Operation("action-2", "b" * 64),),
            )

        self.assertEqual(batches, (0,))
        self.assertIsNone(controller.chat_continuations.current("team_1"))
        self.assertEqual(next_batch.generation, "a" * 64)

    def test_unavailable_strong_local_auth_assurance_auto_blocks_without_a_fake_prompt(self) -> None:
        request = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

        class Runtime:
            purpose = staticmethod(lambda *_args: None)

            def start(self, _context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def resume(self, _context, _results):
                raise AssertionError("unavailable authentication must stop the turn")

        for kind in sorted(action_human.AUTH_KINDS - {"auth:password"}):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                descriptor = {
                    "kind": kind,
                    "ordinal": 0,
                    "title": "Confirm identity",
                    "description": "Confirm current identity before continuing.",
                }
                admitted = human_request_fixtures.admit(human_request_fixtures.fingerprinted(descriptor), (kind,))
                controller = self._chat_controller(directory, Runtime())
                controller.assistant_lifecycle.invoke = lambda *_args, request=admitted: (_ for _ in ()).throw(
                    action_human.HumanRequestSuspensionError(request)
                )

                response = controller.chat_turn_service.chat(
                    "team_1",
                    {
                        "message": "List zones",
                        "files": [],
                        "assistant_ids": ["shimpz-cloudflare"],
                        "conversation": [],
                        "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                        "timezone": None,
                        "locale": None,
                    },
                    "openai",
                    "sk-test-0123456789",
                )
                with closing(sqlite3.connect(controller.action_state.path)) as connection:
                    batches = connection.execute("SELECT COUNT(*) FROM batches").fetchone()

                self.assertEqual(response["status"], "human-denied")
                self.assertEqual(response["reason"], "authentication-unavailable")
                self.assertEqual(batches, (0,))
                self.assertIsNone(controller.chat_turn_service.human_challenges.current("team_1"))
                self.assertIsNone(controller.chat_turn_service.chat_continuations.current("team_1"))

    def test_local_reauthentication_pauses_for_supervisor_assurance(self) -> None:
        request = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

        class Runtime:
            purpose = staticmethod(lambda *_args: None)

            def start(self, _context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def resume(self, _context, _results):
                raise AssertionError("reauthentication must pause before Action replay")

        descriptor = {
            "kind": "auth:password",
            "ordinal": 0,
            "title": "Confirm identity",
            "description": "Confirm current identity before continuing.",
        }
        admitted = human_request_fixtures.admit(human_request_fixtures.fingerprinted(descriptor), ("auth:password",))

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            controller.assistant_lifecycle.invoke = lambda *_args: (_ for _ in ()).throw(
                action_human.HumanRequestSuspensionError(admitted)
            )
            response = controller.chat_turn_service.chat(
                "team_1",
                {
                    "message": "List zones",
                    "files": [],
                    "assistant_ids": ["shimpz-cloudflare"],
                    "conversation": [],
                    "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                    "timezone": None,
                    "locale": None,
                },
                "openai",
                "sk-test-0123456789",
            )

            self.assertEqual(response["status"], "human-required")
            self.assertEqual(response["request"]["kind"], "auth:password")
            self.assertIsNotNone(controller.chat_turn_service.human_challenges.current("team_1"))
            self.assertIsNotNone(controller.chat_turn_service.chat_continuations.current("team_1"))

    def test_failed_reauthentication_resume_requires_a_fresh_request_without_wedging_team(self) -> None:
        request = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

        class Runtime:
            purpose = staticmethod(lambda *_args: None)

            def start(self, _context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def resume(self, _context, results):
                if results != {"action-1": LOOKUP_RESULT}:
                    raise AssertionError("reauthenticated result changed")
                return brain_runtime_client.RuntimeTurn("completed", "Recovered", ())

        descriptor = {
            "kind": "auth:password",
            "ordinal": 0,
            "title": "Confirm identity",
            "description": "Confirm current identity before continuing.",
        }
        admitted = human_request_fixtures.admit(human_request_fixtures.fingerprinted(descriptor), ("auth:password",))

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
            first_pause = controller.chat_turn_service.chat(
                "team_1",
                {
                    "message": "List zones",
                    "files": [],
                    "assistant_ids": ["shimpz-cloudflare"],
                    "conversation": [],
                    "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                    "timezone": None,
                    "locale": None,
                },
                "openai",
                "sk-test-0123456789",
            )
            with self.assertRaises(local_app.ApiProblem) as failed:
                controller.chat_turn_service.resume_chat_human(
                    "team_1",
                    {"challenge_id": first_pause["challenge_id"], "decision": "submit", "value": True},
                    "openai",
                    "sk-test-0123456789",
                )
            second_pause = controller.chat_turn_service.chat(
                "team_1",
                {
                    "message": "List zones",
                    "files": [],
                    "assistant_ids": ["shimpz-cloudflare"],
                    "conversation": [],
                    "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                    "timezone": None,
                    "locale": None,
                },
                "openai",
                "sk-test-0123456789",
            )
            completed = controller.chat_turn_service.resume_chat_human(
                "team_1",
                {"challenge_id": second_pause["challenge_id"], "decision": "submit", "value": True},
                "openai",
                "sk-test-0123456789",
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
        service = local_app.ChatTurnService(
            local_app.ChatTurnDependencies(
                integration_challenges=SimpleNamespace(cancel_team=lambda _team_id: False),
                oauth_pkce=SimpleNamespace(cancel_team=lambda _team_id: None),
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
                controller.chat_turn_service.chat(
                    "team_1",
                    {
                        "message": "Hello",
                        "files": [],
                        "assistant_ids": ["shimpz-cloudflare"],
                        "conversation": [],
                        "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                        "timezone": None,
                        "locale": None,
                    },
                    "openai",
                    "sk-test-0123456789",
                )

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
            response = controller.chat_turn_service.chat(
                "team_1",
                {
                    "message": "Greet me",
                    "files": [],
                    "assistant_ids": ["shimpz-cloudflare"],
                    "conversation": [],
                    "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                    "timezone": None,
                    "locale": None,
                },
                "openai",
                "sk-test-0123456789",
            )

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
        request = brain_runtime_client.ActionRequest(
            interrupt_id="action-1",
            assistant_id="shimpz-cloudflare",
            action="list-zones",
            input=LOOKUP_INPUT,
        )

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
                controller.chat_turn_service.chat(
                    "team_1",
                    {
                        "message": "Greet me",
                        "files": [],
                        "assistant_ids": ["shimpz-cloudflare"],
                        "conversation": [],
                        "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                        "timezone": None,
                        "locale": None,
                    },
                    "openai",
                    "sk-test-0123456789",
                )

            response = controller.chat_turn_service.chat(
                "team_1",
                {
                    "message": "Greet me",
                    "files": [],
                    "assistant_ids": ["shimpz-cloudflare"],
                    "conversation": [],
                    "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                    "timezone": None,
                    "locale": None,
                },
                "openai",
                "sk-test-0123456789",
            )
            with closing(sqlite3.connect(controller.action_state.path)) as connection:
                pending = connection.execute("SELECT COUNT(*) FROM batches").fetchone()

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

            response = controller.chat_turn_service.chat(
                "team_1",
                {
                    "message": "Start a fresh turn",
                    "files": [],
                    "assistant_ids": ["shimpz-cloudflare"],
                    "conversation": [],
                    "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                    "timezone": None,
                    "locale": None,
                },
                "openai",
                "sk-test-0123456789",
            )
            with closing(sqlite3.connect(controller.action_state.path)) as connection:
                batches = connection.execute("SELECT state FROM batches").fetchall()

        self.assertEqual(response["reply"], "Recovered")
        self.assertEqual(batches, [("ended",)])

    def test_terminal_rpc_failure_does_not_wedge_the_next_independent_turn(self) -> None:
        request = brain_runtime_client.ActionRequest(
            interrupt_id="action-1",
            assistant_id="shimpz-cloudflare",
            action="list-zones",
            input=LOOKUP_INPUT,
        )

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
                controller.chat_turn_service.chat(
                    "team_1",
                    {
                        "message": "Greet me",
                        "files": [],
                        "assistant_ids": ["shimpz-cloudflare"],
                        "conversation": [],
                        "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                        "timezone": None,
                        "locale": None,
                    },
                    "openai",
                    "sk-test-0123456789",
                )
            self.assertEqual(invocations, ["rpc"])
            retry = controller.chat_turn_service.chat(
                "team_1",
                {
                    "message": "Greet me",
                    "files": [],
                    "assistant_ids": ["shimpz-cloudflare"],
                    "conversation": [],
                    "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                    "timezone": None,
                    "locale": None,
                },
                "openai",
                "sk-test-0123456789",
            )

        self.assertEqual(first.exception.code, "assistant-rpc-failed")
        self.assertNotIn("private Assistant failure", str(retry))
        self.assertEqual(retry["reply"], "Recovered")
        self.assertEqual(invocations, ["rpc", "rpc"])

    def test_crash_uncertain_batch_remains_blocked_across_identical_local_retries(self) -> None:
        request = brain_runtime_client.ActionRequest(
            interrupt_id="action-1",
            assistant_id="shimpz-cloudflare",
            action="list-zones",
            input=LOOKUP_INPUT,
        )

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
                    controller.chat_turn_service.chat(
                        "team_1",
                        {
                            "message": "Greet me",
                            "files": [],
                            "assistant_ids": ["shimpz-cloudflare"],
                            "conversation": [],
                            "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                            "timezone": None,
                            "locale": None,
                        },
                        "openai",
                        "sk-test-0123456789",
                    )
                self.assertEqual(failed.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
                self.assertEqual(failed.exception.code, "action-state-unavailable")

            with self.assertRaises(local_app.action_journal.ActionJournalUncertainError):
                controller.action_state.begin(batch, operation)
