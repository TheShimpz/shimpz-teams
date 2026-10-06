from __future__ import annotations

import concurrent.futures
import contextlib
import sys
import tempfile
import threading
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
from local_controller_harness import LocalContractCase, chat_body

from action import dispatch as action_dispatch
from action import execution as action_execution
from action import human as action_human
from chat import orchestrator as chat_orchestrator
from inference import client as brain_runtime_client
from local import app as local_app
from local import labels as local_labels
from local.chat.types import ActiveAssistant
from local.validation import MAX_CHAT_ASSISTANTS

LOOKUP_INPUT = {"page": 1, "per_page": 25}
LOOKUP_RESULT = {
    "zones": [],
    "pagination": {"page": 1, "per_page": 25, "count": 0, "total_count": 0, "total_pages": 0},
}
TEST_ACCOUNT_ACCESS_TOKEN = "-".join(("oauth", "access", "test", "token", "123456789"))
TEST_ACCOUNT_REFRESH_TOKEN = "-".join(("oauth", "refresh", "test", "token", "123456789"))
CURRENT_ASSISTANT_IMAGE = "ghcr.io/theshimpz/shimpz-assistant@sha256:" + "b" * 64
OUTDATED_ASSISTANT_IMAGE = "ghcr.io/theshimpz/shimpz-assistant@sha256:" + "a" * 64


class LocalChatScopeTests(LocalContractCase):
    def test_blocking_action_rpc_does_not_hold_a_colliding_team_stripe(self) -> None:
        started = threading.Event()
        release = threading.Event()

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, object())
            first_team = "team_1"
            token = "turn-token"
            frozen_container_id = controller.assistant_lifecycle._assistant_container(
                first_team, "shimpz-cloudflare"
            ).id
            controller.chat_turn_service._active_chat_tokens[first_team] = token
            colliding_team = next(
                f"team_{index}"
                for index in range(2, 10_000)
                if controller._lock(f"team_{index}") is controller._lock(first_team)
            )

            def rpc(*_args):
                started.set()
                release.wait(timeout=2)
                return {"type": "result", "result": LOOKUP_RESULT}

            controller.assistant_lifecycle._rpc = rpc
            with (
                mock.patch.object(local_app.local_audit, "record_request", return_value="trace"),
                concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor,
            ):
                future = executor.submit(
                    controller.chat_turn_service._invoke_chat_action,
                    first_team,
                    token,
                    brain_runtime_client.ActionRequest(
                        interrupt_id="interrupt-1",
                        assistant_id="shimpz-cloudflare",
                        action="list-zones",
                        input=LOOKUP_INPUT,
                    ),
                    frozen_container_id,
                    local_app.action_execution.ActionInvocationEvidence(
                        local_app.action_execution.RpcPrivateInputs({}, {}),
                        action_human.ActionTranscript(""),
                        "a" * 64,
                        "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6",
                    ),
                )
                try:
                    self.assertTrue(started.wait(timeout=1))
                    stripe = controller._lock(colliding_team)
                    self.assertTrue(stripe.acquire(blocking=False))
                    stripe.release()
                finally:
                    release.set()
                result = future.result(timeout=2)

        self.assertEqual(result, LOOKUP_RESULT)

    def test_chat_setup_validates_the_network_from_one_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, object())
            labels = controller.assistant_lifecycle._base_labels("team_1", "team")
            labels[local_labels.TEAM_NAME_LABEL] = "Marketing"
            network = SimpleNamespace(
                id="a" * 64,
                name=controller.assistant_lifecycle._network_name("team_1"),
                attrs={
                    "Name": controller.assistant_lifecycle._network_name("team_1"),
                    "Driver": "bridge",
                    "Internal": True,
                    "Attachable": False,
                    "Labels": labels,
                },
                reload=mock.Mock(),
            )
            controller.client = SimpleNamespace(networks=SimpleNamespace(get=lambda _name: network))
            controller.assistant_lifecycle.client = controller.client
            controller.assistant_lifecycle._network = local_app.AssistantLifecycle._network.__get__(
                controller.assistant_lifecycle
            )
            controller.assistant_lifecycle._validate_network = local_app.AssistantLifecycle._validate_network.__get__(
                controller.assistant_lifecycle
            )

            setup = controller.chat_turn_service._chat_setup("team_1", [], "openai", ())

        self.assertEqual(setup[0], "Marketing")
        network.reload.assert_not_called()

    def test_chat_reuses_one_selected_file_connection_across_revalidation(self) -> None:
        contexts = []

        class Runtime:
            @staticmethod
            def start(context, _message, *, conversation=()):
                contexts.append(context)
                return brain_runtime_client.RuntimeTurn(status="completed", reply="Done.", actions=())

        file_id = "a" * 32
        connection = object()
        opened = 0
        metadata_connections = []
        references = []

        @contextlib.contextmanager
        def metadata_connection(_team_id, _file_ids):
            nonlocal opened
            opened += 1
            yield connection

        def metadata(_team_id, _file_ids, current_connection=None):
            metadata_connections.append(current_connection)
            return [{"id": file_id, "name": "brief.txt", "media_type": "text/plain", "size": 5, "sha256": "e" * 64}]

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            controller.storage = SimpleNamespace(
                metadata=metadata,
                metadata_connection=metadata_connection,
                reference=lambda team_id, file_ids: references.append(("reference", team_id, list(file_ids))) or (),
                settle=lambda team_id, file_ids: references.append(("settle", team_id, list(file_ids))),
                get=lambda _team_id, _file_id: ({"sha256": "e" * 64, "size": 5}, b"brief"),
            )
            controller.chat_turn_service.storage = controller.storage

            response = controller.chat_turn_service.chat(
                "team_1",
                chat_body("Summarize", files=[file_id]),
                "openai",
                "sk-test-0123456789",
            )

        self.assertEqual(response["reply"], "Done.")
        # The selected text file reaches the Brain as request-local content of this message (ADR-0093).
        self.assertEqual(
            [item["content"] for item in contexts[0].attachments],
            [{"type": "text", "text": "brief", "pdf": False}],
        )
        self.assertEqual(opened, 1)
        # The turn references its file before the Brain start and leaves only it referenced once it completes.
        self.assertEqual(references, [("reference", "team_1", [file_id]), ("settle", "team_1", [file_id])])
        self.assertGreaterEqual(len(metadata_connections), 2)
        self.assertTrue(all(current is connection for current in metadata_connections))

    def test_chat_exposes_every_active_assistant_to_the_team_brain(self) -> None:
        class Runtime:
            context = None

            def start(self, context, _message, *, conversation=()):
                self.context = context
                return brain_runtime_client.RuntimeTurn(status="completed", reply="Integrated.", actions=())

        runtime = Runtime()
        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, runtime)
            hello = controller.registry["shimpz-cloudflare"]
            account_helper = replace(
                hello,
                assistant_id="account-helper",
                image=hello.image.replace("a" * 64, "b" * 64),
                actions={"lookup": hello.actions["list-zones"]},
            )
            controller.registry[account_helper.assistant_id] = account_helper
            controller.chat_turn_service._active_chat_assistants = lambda _team_id, _network: (
                ActiveAssistant(hello, "hello-container"),
                ActiveAssistant(account_helper, "account-helper-container"),
            )

            response = controller.chat_turn_service.chat(
                "team_1",
                chat_body("Check the accounts", assistant_ids=["account-helper", "shimpz-cloudflare"]),
                "openai",
                "sk-test-0123456789",
            )

        self.assertEqual(
            [assistant.id for assistant in runtime.context.assistants], ["account-helper", "shimpz-cloudflare"]
        )
        self.assertEqual(
            [assistant.genesis for assistant in runtime.context.assistants],
            ["Use only the declared Cloudflare Actions.", "Use only the declared Cloudflare Actions."],
        )
        self.assertEqual(
            runtime.context.thread_id,
            f"local:local-space:team_1:{'a' * 64}:default",
        )
        self.assertEqual(response["team_name"], "Marketing")

    def test_a_renamed_team_reaches_brain_and_the_terminal_by_its_current_name(self) -> None:
        class Runtime:
            context = None

            def start(self, context, _message, *, conversation=()):
                self.context = context
                return brain_runtime_client.RuntimeTurn(status="completed", reply="Done.", actions=())

        runtime = Runtime()
        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, runtime)
            # The creation label stays "Marketing"; the display name is the incarnation's record (ADR-0088).
            controller.team_names.save("team_1", "a" * 64, "Growth")
            response = controller.chat_turn_service.chat(
                "team_1",
                chat_body("Hello"),
                "openai",
                "sk-test-0123456789",
            )
        self.assertEqual((runtime.context.team_name, response["team_name"]), ("Growth", "Growth"))

    def test_chat_empty_scope_is_brain_only_and_scans_installed_workloads_once(self) -> None:
        class Runtime:
            context = None

            def start(self, context, _message, *, conversation=()):
                self.context = context
                return brain_runtime_client.RuntimeTurn(status="completed", reply="Brain only.", actions=())

        runtime = Runtime()
        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, runtime)
            scanner = controller.chat_turn_service._active_chat_assistants
            calls: list[str] = []
            controller.chat_turn_service._active_chat_assistants = lambda team_id, network: (
                calls.append(f"{team_id}:{network}") or scanner(team_id, network)
            )

            response = controller.chat_turn_service.chat(
                "team_1",
                chat_body("Hello"),
                "openai",
                "sk-test-0123456789",
            )

        self.assertEqual(runtime.context.assistants, ())
        self.assertEqual(len(calls), 1)
        self.assertEqual(response["reply"], "Brain only.")

    def test_chat_forwards_the_committed_conversation_only_to_the_turn_start(self) -> None:
        class Runtime:
            conversation = None

            def start(self, _context, _message, *, conversation=()):
                type(self).conversation = conversation
                return brain_runtime_client.RuntimeTurn(status="completed", reply="Hello.", actions=())

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            window = [{"role": "user", "text": "List my DNS zones", "truncated": False}]
            controller.chat_turn_service.chat(
                "team_1",
                chat_body("Hello", conversation=window),
                "openai",
                "sk-test-0123456789",
            )
            self.assertEqual(
                Runtime.conversation,
                (brain_runtime_client.RuntimeConversationEntry("user", "List my DNS zones", False),),
            )
            with self.assertRaises(local_app.ApiProblem) as caught:
                controller.chat_turn_service.chat(
                    "team_1",
                    chat_body("Hello", conversation=[{"role": [], "text": "x", "truncated": False}]),
                    "openai",
                    "sk-test-0123456789",
                )
            self.assertEqual(caught.exception.code, "invalid-conversation")
            for locale in ("pt-BR", "", 1):
                with self.subTest(locale=locale), self.assertRaises(local_app.ApiProblem) as refused:
                    controller.chat_turn_service.chat(
                        "team_1",
                        chat_body("Hello", locale=locale),
                        "openai",
                        "sk-test-0123456789",
                    )
                self.assertEqual(refused.exception.code, "invalid-locale")

    def test_chat_rejects_invalid_or_unavailable_assistant_scope_before_runtime(self) -> None:
        class Runtime:
            def start(self, _context, _message, *, conversation=()):
                raise AssertionError("an invalid Assistant scope must not reach the Brain")

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            invalid = (
                ["shimpz-cloudflare", "shimpz-cloudflare"],
                ["bad_assistant"],
                ["a" * 41],
                [f"helper-{index}" for index in range(MAX_CHAT_ASSISTANTS + 1)],
            )
            for assistant_ids in invalid:
                with self.subTest(assistant_ids=assistant_ids), self.assertRaises(local_app.ApiProblem) as caught:
                    controller.chat_turn_service.chat(
                        "team_1",
                        chat_body("Hello", assistant_ids=assistant_ids),
                        "openai",
                        "sk-test-0123456789",
                    )
                self.assertEqual(caught.exception.code, "invalid-assistants")

            with self.assertRaises(local_app.ApiProblem) as unavailable:
                controller.chat_turn_service.chat(
                    "team_1",
                    chat_body("Hello", assistant_ids=["account-helper"]),
                    "openai",
                    "sk-test-0123456789",
                )

        self.assertEqual(unavailable.exception.status, HTTPStatus.CONFLICT)
        self.assertEqual(unavailable.exception.code, "assistant-unavailable")
        self.assertEqual(unavailable.exception.message, "a selected Assistant is unavailable")

    def test_chat_revalidates_the_selected_assistant_generation_before_provider_use(self) -> None:
        class Runtime:
            def start(self, _context, _message, *, conversation=()):
                raise AssertionError("Assistant generation drift must not reach the Brain")

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            spec = controller.registry["shimpz-cloudflare"]
            generations = iter(("assistant-v1", "assistant-v2"))
            scans: list[str] = []

            def scan(_team_id: str, _network: str) -> tuple[ActiveAssistant, ...]:
                scans.append(_team_id)
                return (ActiveAssistant(spec, next(generations)),)

            controller.chat_turn_service._active_chat_assistants = scan

            with self.assertRaises(local_app.ApiProblem) as caught:
                controller.chat_turn_service.chat(
                    "team_1",
                    chat_body("Hello", assistant_ids=["shimpz-cloudflare"]),
                    "openai",
                    "sk-test-0123456789",
                )

        self.assertEqual(caught.exception.code, "team-context-changed")
        self.assertEqual(len(scans), 2)

    def test_chat_action_rejects_a_container_replaced_between_selection_and_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, object())
            frozen = SimpleNamespace(id="assistant-v1", status="running", reload=lambda: None)
            replacement = SimpleNamespace(id="assistant-v2", status="running", reload=lambda: None)
            discovered = iter((frozen, replacement))
            lookups: list[str] = []

            def assistant_container(_team_id: str, _assistant_id: str):
                container = next(discovered)
                lookups.append(container.id)
                return container

            controller.assistant_lifecycle._assistant_container = assistant_container
            controller.assistant_lifecycle._rpc = lambda *_args: self.fail(
                "a replacement Assistant container executed the Action"
            )
            controller.chat_turn_service._active_chat_tokens["team_1"] = "turn-token"

            with self.assertRaises(local_app.ApiProblem) as caught:
                controller.chat_turn_service._invoke_chat_action(
                    "team_1",
                    "turn-token",
                    brain_runtime_client.ActionRequest(
                        interrupt_id="interrupt-1",
                        assistant_id="shimpz-cloudflare",
                        action="list-zones",
                        input=LOOKUP_INPUT,
                    ),
                    frozen.id,
                    local_app.action_execution.ActionInvocationEvidence(
                        local_app.action_execution.RpcPrivateInputs({}, {}),
                        action_human.ActionTranscript(""),
                        "a" * 64,
                        "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6",
                    ),
                )

        self.assertEqual(lookups, [frozen.id, replacement.id])
        self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)
        self.assertEqual(caught.exception.code, "team-context-changed")
        self.assertEqual(controller.chat_turn_service._active_action_containers, {})

    def test_a_stopped_turn_keeps_only_a_pre_dispatch_refusal_chained(self) -> None:
        request = brain_runtime_client.ActionRequest(
            interrupt_id="interrupt-1", assistant_id="shimpz-cloudflare", action="list-zones", input=LOOKUP_INPUT
        )
        evidence = local_app.action_execution.ActionInvocationEvidence(
            local_app.action_execution.RpcPrivateInputs({}, {}),
            action_human.ActionTranscript(""),
            "a" * 64,
            "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6",
        )

        def refused(*_args):
            try:
                raise action_execution.RpcExchangeError(
                    "timeout", "deadline-expired-before-dispatch"
                ) from action_dispatch.DispatchRefusedError("the turn was stopped before its Docker call could run")
            except action_execution.RpcExchangeError as exc:
                raise local_app.ApiProblem(HTTPStatus.GATEWAY_TIMEOUT, "timed out", code="assistant-timeout") from exc

        def ran(*_args):
            raise local_app.ApiProblem(
                HTTPStatus.GATEWAY_TIMEOUT, "timed out", code="assistant-timeout"
            ) from action_execution.RpcExchangeError("timeout")

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, object())
            service = controller.chat_turn_service
            frozen = controller.assistant_lifecycle._assistant_container("team_1", "shimpz-cloudflare").id
            service._active_chat_tokens["team_1"] = "turn-token"
            service._cancelled_chat_tokens.add("turn-token")
            controller.assistant_lifecycle._rpc = lambda *_args: self.fail("a stopped turn dispatched its Action")
            # Stopped before the RPC: nothing was dispatched, and the stop says so.
            with self.assertRaises(chat_orchestrator.ChatStoppedError) as before:
                service._invoke_chat_action("team_1", "turn-token", request, frozen, evidence)
            self.assertTrue(action_dispatch.never_dispatched(before.exception))
            service._cancelled_chat_tokens.clear()
            outcomes = []
            for rpc in (refused, ran):

                def stopping_rpc(*args, rpc=rpc):
                    service._cancelled_chat_tokens.add("turn-token")
                    return rpc(*args)

                controller.assistant_lifecycle._rpc = stopping_rpc
                with (
                    mock.patch.object(local_app.local_audit, "record_request", return_value="trace"),
                    self.assertRaises(chat_orchestrator.ChatStoppedError) as stopped,
                ):
                    service._invoke_chat_action("team_1", "turn-token", request, frozen, evidence)
                service._cancelled_chat_tokens.clear()
                outcomes.append(action_dispatch.never_dispatched(stopped.exception))
        # A refusal before dispatch is kept; an RPC that may have run never reads as never dispatched.
        self.assertEqual(outcomes, [True, False])

    def test_chat_never_exposes_or_executes_an_unselected_assistant(self) -> None:
        class Runtime:
            def start(self, context, _message, *, conversation=()):
                self.context = context
                return brain_runtime_client.RuntimeTurn(
                    status="action-required",
                    reply="",
                    actions=(
                        brain_runtime_client.ActionRequest(
                            interrupt_id="action-1",
                            assistant_id="account-helper",
                            action="lookup",
                            input=LOOKUP_INPUT,
                        ),
                    ),
                )

        runtime = Runtime()
        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, runtime)
            hello = controller.registry["shimpz-cloudflare"]
            account_helper = replace(
                hello,
                assistant_id="account-helper",
                image=hello.image.replace("a" * 64, "b" * 64),
                actions={"lookup": hello.actions["list-zones"]},
            )
            controller.registry[account_helper.assistant_id] = account_helper
            controller.chat_turn_service._active_chat_assistants = lambda _team_id, _network: (
                ActiveAssistant(hello, "hello-container"),
                ActiveAssistant(account_helper, "account-helper-container"),
            )
            controller.invoke = lambda *_args: self.fail("an unselected Assistant Action executed")
            controller.assistant_lifecycle.invoke = controller.invoke

            with self.assertRaises(local_app.ApiProblem) as caught:
                controller.chat_turn_service.chat(
                    "team_1",
                    chat_body("Accounts", assistant_ids=["shimpz-cloudflare"]),
                    "openai",
                    "sk-test-0123456789",
                )

        self.assertEqual([assistant.id for assistant in runtime.context.assistants], ["shimpz-cloudflare"])
        self.assertEqual(caught.exception.code, "brain-runtime-failed")
