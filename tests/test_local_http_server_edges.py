import contextlib
import http.client
import unittest
from collections.abc import Iterator
from email.message import Message
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from io import BytesIO
from types import SimpleNamespace
from unittest import mock

from action import human as action_human
from core.http import strict as strict_http
from integrations import broker as integration_broker
from local import authority
from local.errors import ApiProblemError
from local.http import audit as http_audit
from local.http import chat as http_chat
from local.http import server
from local.http import stream as local_http_stream
from protocol.http.v1 import payload as http_payload
from tests import human_request_fixtures

TEST_TOKEN = "t" * 32
# SHA-256 of the fixture model key "a" * 32, written out so the test checks the fingerprint the boundary emits
# instead of recomputing it from the key.
MODEL_KEY_SHA256 = "3ba3f5f43b92602683c19aee62a20342b084dd5971ddd33808d81a328879a547"


class LocalHttpEdgeHelpers:
    @staticmethod
    def handler(
        method: str = "GET",
        path: str = "/healthz",
        controller: object | None = None,
    ) -> server.Handler:
        handler = object.__new__(server.Handler)
        handler.command = method
        handler.path = path
        handler.server = SimpleNamespace(controller=controller, token=TEST_TOKEN)
        handler.headers = Message()
        handler.rfile = BytesIO()
        handler.wfile = BytesIO()
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        return handler

    @staticmethod
    def route(operation: str, **params: str) -> strict_http.ControllerRouteMatch:
        return strict_http.ControllerRouteMatch(operation, params)


class BoundedServerEdgeTests(unittest.TestCase):
    def test_initialization_and_slot_lifecycle_are_bounded(self) -> None:
        with mock.patch.object(ThreadingHTTPServer, "__init__", return_value=None):
            bounded = server.BoundedServer(("127.0.0.1", 0), server.Handler, object(), TEST_TOKEN)
        self.assertEqual(bounded.token, TEST_TOKEN)

        request = SimpleNamespace(close=mock.Mock())
        bounded._slots = SimpleNamespace(acquire=mock.Mock(return_value=False), release=mock.Mock())
        bounded.process_request(request, ("127.0.0.1", 1))
        request.close.assert_called_once_with()

        bounded._slots.acquire.return_value = True
        with mock.patch.object(ThreadingHTTPServer, "process_request") as process:
            bounded.process_request(request, ("127.0.0.1", 1))
        process.assert_called_once()

        with (
            mock.patch.object(ThreadingHTTPServer, "process_request", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            bounded.process_request(request, ("127.0.0.1", 1))
        bounded._slots.release.assert_called_once_with()

        with mock.patch.object(ThreadingHTTPServer, "process_request_thread"):
            bounded.process_request_thread(request, ("127.0.0.1", 1))
        self.assertEqual(bounded._slots.release.call_count, 2)


class HandlerPrimitiveEdgeTests(LocalHttpEdgeHelpers, unittest.TestCase):
    def test_json_body_limits_cover_every_local_body_route(self) -> None:
        body_operations = {
            route.operation for route in strict_http.CONTROLLER_ROUTES if route.method in {"POST", "PUT", "PATCH"}
        }
        body_operations.remove("file-upload")
        # A Local deletion carries the Team name it confirms (ADR-0088).
        body_operations.update(("assistant-integration-cancel", "team-destroy"))

        self.assertEqual(set(server._JSON_BODY_LIMITS), body_operations)

    def test_setup_authorization_and_response_writers(self) -> None:
        handler = self.handler()
        handler.headers["Authorization"] = f"Bearer {TEST_TOKEN}"
        self.assertTrue(handler._authorized())
        self.assertIsNone(handler.log_message("ignored"))

        handler._send(HTTPStatus.UNAUTHORIZED, {"error": "denied"})
        handler.send_header.assert_any_call("WWW-Authenticate", 'Bearer realm="shimpz-local"')
        self.assertIn(b"denied", handler.wfile.getvalue())

        head = self.handler("HEAD")
        head._response_limit = 1
        head._send(HTTPStatus.OK, {"large": "value"})
        head.send_response.assert_called_once_with(HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertEqual(head.wfile.getvalue(), b"")

        icon = self.handler()
        icon._send_image(b"png", "image/png")
        self.assertEqual(icon.wfile.getvalue(), b"png")
        icon.command = "HEAD"
        icon.wfile = BytesIO()
        icon._send_image(b"png", "image/png")
        self.assertEqual(icon.wfile.getvalue(), b"")

    def test_captured_body_and_file_contract_fail_closed(self) -> None:
        handler = self.handler()
        with self.assertRaises(ApiProblemError):
            handler.body()
        handler._captured_json_raw = b"{}"
        handler._captured_json_body = {}
        self.assertEqual(handler.body(), {})

        with self.assertRaises(ApiProblemError):
            handler._file_body()
        handler._captured_file_metadata = strict_http.FileUploadMetadata(1, "a.txt", "text/plain")
        with (
            mock.patch.object(
                strict_http,
                "read_file_content",
                side_effect=strict_http.HttpContractError(
                    HTTPStatus.BAD_REQUEST,
                    "short file",
                    code="invalid-file",
                ),
            ),
            self.assertRaises(ApiProblemError) as caught,
        ):
            handler._file_body()
        self.assertEqual(caught.exception.code, "invalid-file")

        handler.headers["Content-Length"] = "invalid"
        with self.assertRaises(ApiProblemError) as caught:
            handler.capture_body("chat")
        self.assertEqual(caught.exception.code, "content-length")

    def test_body_shapes_and_request_target_validation(self) -> None:
        handler = self.handler()
        handler.body = mock.Mock(return_value={})
        with self.assertRaises(ApiProblemError):
            handler._team_name_body()
        handler.body.return_value = {"team_name": "Team"}
        self.assertEqual(handler._team_name_body(), "Team")

        for body in ({}, {"assistant_id": 1, "source_digest": "sha256:" + "a" * 64}):
            handler.body.return_value = body
            with self.subTest(body=body), self.assertRaises(ApiProblemError):
                handler._install_body()
        digest = "sha256:" + "a" * 64
        handler.body.return_value = {"assistant_id": "assistant", "source_digest": digest}
        self.assertEqual(handler._install_body(), ("assistant", digest))
        for body in ({}, {"image_id": 1}, {"image_id": digest, "extra": True}):
            handler.body.return_value = body
            with self.subTest(body=body), self.assertRaises(ApiProblemError):
                handler._local_install_body()
        handler.body.return_value = {"image_id": digest}
        self.assertEqual(handler._local_install_body(), digest)

        handler.path = "/v1//teams"
        with self.assertRaises(ApiProblemError) as caught:
            handler._resolved_route()
        self.assertEqual(caught.exception.code, "invalid-path")
        handler.path = "/unknown"
        with self.assertRaises(ApiProblemError) as caught:
            handler._resolved_route()
        self.assertEqual(caught.exception.code, "route-not-found")
        handler.path = "/healthz?query=forbidden"
        with self.assertRaises(ApiProblemError):
            handler._resolved_route()

        api_key = "a" * 32
        handler.headers.add_header("X-Shimpz-Model-Provider", "openai")
        handler.headers.add_header("X-Shimpz-Model-Api-Key", api_key)
        self.assertEqual(
            handler._model_binding("chat-capability-plan"),
            {"provider": "openai", "key_sha256": MODEL_KEY_SHA256},
        )
        self.assertEqual(
            handler._model_binding("chat-intent-route"),
            {"provider": "openai", "key_sha256": MODEL_KEY_SHA256},
        )
        self.assertIsNone(handler._model_binding("assistant-list"))


class HandlerRouteEdgeTests(LocalHttpEdgeHelpers, unittest.TestCase):
    @staticmethod
    def controller() -> SimpleNamespace:
        service = SimpleNamespace(
            capability_plan=mock.Mock(return_value={"status": "sufficient", "assistant_ids": []}),
            intent_route=mock.Mock(
                return_value={
                    "intent": "assistant-uninstall",
                    "query": "",
                    "assistant_ids": ["shimpz-cloudflare"],
                }
            ),
            complete_cloudflare_oauth_callback=mock.Mock(return_value={"connected": True}),
            pending_chat_human=mock.Mock(return_value={"pending": "human"}),
            pending_chat_integrations=mock.Mock(return_value={"pending": "integration"}),
            resume_chat_human=mock.Mock(return_value={"status": "ok"}),
            resume_chat_integrations=mock.Mock(return_value={"status": "ok"}),
            stop_chat=mock.Mock(return_value={"stopped": True}),
            list_assistant_integrations=mock.Mock(return_value={"integrations": []}),
            list_assistant_stored_inputs=mock.Mock(return_value={"stored_inputs": []}),
            clear_assistant_stored_input=mock.Mock(return_value={"cleared": True}),
            start_assistant_integration_authorization=mock.Mock(return_value={"url": "https://example.com"}),
            cancel_assistant_integration_authorization=mock.Mock(return_value={"cancelled": True}),
        )
        return SimpleNamespace(
            chat_turn_service=service,
            list_registry=mock.Mock(return_value={"assistants": []}),
            list_local_snapshots=mock.Mock(return_value={"assistants": []}),
            local_snapshot_icon=mock.Mock(return_value=b"local-png"),
            reset_space=mock.Mock(return_value={"reset": True}),
            list_files=mock.Mock(return_value={"files": []}),
            put_file=mock.Mock(return_value={"file": {}}),
            inference_status=mock.Mock(return_value={"configured": True}),
            configure_inference=mock.Mock(return_value={"configured": True}),
            create_team=mock.Mock(return_value={"created": True}),
            destroy_team=mock.Mock(return_value={"deleted": True}),
            rename_team=mock.Mock(return_value={"team_id": "team_1", "team_name": "Team"}),
            list_assistants=mock.Mock(return_value={"assistants": []}),
            team_registry=mock.Mock(return_value={"assistants": []}),
            install_publication=mock.Mock(return_value={"installed": True}),
            install_local_snapshot=mock.Mock(return_value={"assistant": "assistant", "installed": True}),
            install_fresh_local_snapshot=mock.Mock(return_value={"assistant": "assistant", "installed": True}),
            assistant_lifecycle=SimpleNamespace(uninstall_assistant=mock.Mock(return_value={"uninstalled": True})),
            invoke=mock.Mock(return_value={"result": "ok"}),
        )

    def test_fixed_file_and_inference_routes_cover_every_outcome(self) -> None:
        controller = self.controller()
        handler = self.handler(controller=controller)
        self.assertEqual(handler._route(self.route("registry-list"))[2], "registry-list")
        self.assertEqual(handler._route(self.route("local-assistant-list"))[2], "local-assistant-list")
        handler.command = "POST"
        handler.body = mock.Mock(return_value={"image_id": "sha256:" + ("a" * 64)})
        self.assertEqual(
            handler._route(self.route("local-assistant-fresh-install", team_id="team_1")),
            (
                HTTPStatus.OK,
                {"assistant": "assistant", "installed": True},
                "local-assistant-fresh-install",
                "team_1",
                "assistant",
            ),
        )
        controller.install_fresh_local_snapshot.assert_called_once()
        handler.command = "DELETE"
        with mock.patch.object(authority, "require_supervisor_absent"):
            self.assertEqual(handler._route(self.route("space-bootstrap-reset"))[2], "space-bootstrap-reset")
        self.assertEqual(handler._route(self.route("space-reset"))[2], "space-reset")
        handler.command = "POST"
        handler.body = mock.Mock(return_value={})
        callback = self.route("assistant-integration-complete")
        with self.assertRaises(ApiProblemError):
            handler._route(callback)
        handler.body.return_value = {"state": "s", "claim": "c", "session_binding": "b"}
        self.assertEqual(handler._route(callback)[1], {"connected": True})

    def test_fresh_local_install_resolves_and_dispatches_through_the_public_route(self) -> None:
        controller = self.controller()
        path = "/v1/teams/team_1/assistants/local/fresh"
        handler = self.handler("POST", path, controller)
        image_id = "sha256:" + ("a" * 64)
        handler._local_install_body = mock.Mock(return_value=image_id)

        parts, route = handler._resolved_route()
        self.assertEqual(route.operation, "local-assistant-fresh-install")
        result = handler._route(route)

        self.assertEqual(parts, ["v1", "teams", "team_1", "assistants", "local", "fresh"])
        self.assertEqual(result[2], "local-assistant-fresh-install")
        controller.install_fresh_local_snapshot.assert_called_once_with("team_1", image_id)

    def test_bootstrap_reset_maps_supervisor_state_before_cleanup(self) -> None:
        controller = self.controller()
        handler = self.handler(method="DELETE", controller=controller)
        for error, code in (
            (authority.SupervisorEstablishedError("configured"), "supervisor-established"),
            (authority.SupervisorUnavailableError("unsafe"), "supervisor-unavailable"),
        ):
            with (
                self.subTest(code=code),
                mock.patch.object(authority, "require_supervisor_absent", side_effect=error),
                self.assertRaises(ApiProblemError) as caught,
            ):
                handler._route(self.route("space-bootstrap-reset"))
            self.assertEqual(caught.exception.code, code)
        controller.reset_space.assert_not_called()

        self.assertEqual(handler._route(self.route("file-list", team_id="team_1"))[2], "file-list")
        handler._file_body = mock.Mock(return_value=("a.txt", b"x", "text/plain"))
        slots = SimpleNamespace(acquire=mock.Mock(return_value=False), release=mock.Mock())
        upload = self.route("file-upload", team_id="team_1")
        with mock.patch.object(server, "_FILE_UPLOAD_SLOTS", slots), self.assertRaises(ApiProblemError):
            handler._route(upload)
        slots.acquire.return_value = True
        with mock.patch.object(server, "_FILE_UPLOAD_SLOTS", slots):
            self.assertEqual(handler._route(upload)[2], "file-upload")
        slots.release.assert_called_once_with()

        self.assertEqual(handler._route(self.route("inference-status", team_id="team_1"))[2], "inference-status")
        handler.body = mock.Mock(return_value={"provider": "openai"})
        self.assertEqual(handler._route(self.route("inference-configure", team_id="team_1"))[2], "inference-configure")
        controller.configure_inference.assert_called_once_with("team_1", {"provider": "openai"})

    def test_chat_route_variants_and_validation(self) -> None:
        handler = self.handler(controller=self.controller())
        handler.model_credential_headers = mock.Mock(return_value=("openai", "key"))
        handler.body = mock.Mock(return_value={})
        for operation, pending in (
            ("chat-integration-pending", {"pending": "integration"}),
            ("chat-human-pending", {"pending": "human"}),
        ):
            with self.subTest(operation=operation):
                self.assertEqual(
                    handler._route(self.route(operation, team_id="team_1")),
                    (HTTPStatus.OK, pending, operation, "team_1", None),
                )
        self.assertEqual(
            http_chat._submit(handler, "chat-integration-submit", "team_1", None), (HTTPStatus.OK, {"status": "ok"})
        )
        stop = self.route("chat-stop", team_id="team_1")
        handler.body.return_value = {"unexpected": True}
        with self.assertRaises(ApiProblemError):
            handler._route(stop)
        handler.body.return_value = {}
        self.assertEqual(handler._route(stop), (HTTPStatus.OK, {"stopped": True}, "chat-stop", "team_1", None))

    def test_integration_and_team_routes_cover_exact_shapes(self) -> None:
        handler = self.handler(controller=self.controller())
        handler.body = mock.Mock(return_value={})
        listed = self.route("assistant-integration-list", team_id="team_1")
        self.assertEqual(handler._route(listed)[2], "assistant-integration-list")
        authorize = self.route("assistant-integration-authorize", team_id="team_1", challenge_id="a" * 32)
        with self.assertRaises(ApiProblemError):
            handler._route(authorize)
        handler.body.return_value = {
            "assistant_id": "assistant",
            "integration_id": "cloudflare",
            "callback_mode": "invalid",
            "session_binding": "b",
        }
        with self.assertRaises(ApiProblemError):
            handler._route(authorize)
        callback_mode = next(iter(integration_broker.CALLBACK_MODES))
        handler.body.return_value = {
            "assistant_id": "assistant",
            "integration_id": "cloudflare",
            "callback_mode": callback_mode,
            "session_binding": "b",
        }
        self.assertEqual(handler._route(authorize)[2], "assistant-integration-authorize")
        handler.server.controller.chat_turn_service.start_assistant_integration_authorization.assert_called_once_with(
            "team_1", "a" * 32, "assistant", "cloudflare", "b", callback_mode
        )
        cancel = self.route("assistant-integration-cancel", team_id="team_1", challenge_id="a" * 32)
        handler.body.return_value = {}
        with self.assertRaises(ApiProblemError):
            handler._route(cancel)
        handler.body.return_value = {"session_binding": "b"}
        self.assertEqual(handler._route(cancel)[2], "assistant-integration-cancel")

        stored_inputs = self.route("assistant-stored-input-list", team_id="team_1")
        self.assertEqual(handler._route(stored_inputs)[2], "assistant-stored-input-list")
        clear = self.route(
            "assistant-stored-input-clear", team_id="team_1", assistant_id="whatsapp", stored_input_id="whatsapp-token"
        )
        self.assertEqual(
            handler._route(clear),
            (HTTPStatus.OK, {"cleared": True}, "assistant-stored-input-clear", "team_1", "whatsapp"),
        )
        handler.server.controller.chat_turn_service.clear_assistant_stored_input.assert_called_once_with(
            "team_1", "whatsapp", "whatsapp-token"
        )

    def test_team_routes_create_confirm_deletion_and_rename_by_team_name(self) -> None:
        handler = self.handler(controller=self.controller())
        handler._team_name_body = mock.Mock(return_value="Team")
        self.assertEqual(handler._route(self.route("team-create", team_id="team_1"))[2], "team-create")
        handler.server.controller.create_team.assert_called_with("team_1", "Team")
        self.assertEqual(handler._route(self.route("team-destroy", team_id="team_1"))[2], "team-destroy")
        handler.server.controller.destroy_team.assert_called_with("team_1", "Team")
        self.assertEqual(handler._route(self.route("team-rename", team_id="team_1"))[2], "team-rename")
        handler.server.controller.rename_team.assert_called_with("team_1", "Team")

    def test_generic_route_dispatches_all_assistant_operations(self) -> None:
        controller = self.controller()
        handler = self.handler(controller=controller)
        handler._install_body = mock.Mock(return_value=("assistant", "sha256:" + "a" * 64))
        handler._local_install_body = mock.Mock(return_value="sha256:" + "b" * 64)
        handler.body = mock.Mock(return_value={"input": "ok"})
        handler.model_credential_headers = mock.Mock(return_value=("openai", "private-model-key"))
        cases = (
            ("assistant-list", {"team_id": "team_1"}),
            ("assistant-registry", {"team_id": "team_1"}),
            ("assistant-install", {"team_id": "team_1"}),
            ("assistant-uninstall", {"team_id": "team_1", "assistant_id": "assistant"}),
            (
                "assistant-invoke",
                {"team_id": "team_1", "assistant_id": "assistant", "action_id": "action"},
            ),
        )
        for operation, params in cases:
            with self.subTest(operation=operation):
                result = handler._route(self.route(operation, **params))
                self.assertEqual(result[2], operation)

        controller.assistant_summary = mock.Mock(return_value={"locale": "pt", "summary": "Resumo."})
        result = handler._route(
            self.route("assistant-summary", team_id="team_1", assistant_id="assistant", locale="pt")
        )
        self.assertEqual(
            result, (200, {"locale": "pt", "summary": "Resumo."}, "assistant-summary", "team_1", "assistant")
        )
        controller.assistant_summary.assert_called_once_with("team_1", "assistant", "pt")
        controller.assistant_details = mock.Mock(return_value={"locale": "pt", "name": "Assistente"})
        result = handler._route(
            self.route("assistant-details", team_id="team_1", assistant_id="assistant", locale="pt")
        )
        self.assertEqual(
            result, (200, {"locale": "pt", "name": "Assistente"}, "assistant-details", "team_1", "assistant")
        )
        controller.assistant_details.assert_called_once_with("team_1", "assistant", "pt")

        handler.command = "POST"
        result = handler._route(self.route("local-assistant-install", team_id="team_1"))
        self.assertEqual(result[2], "local-assistant-install")

    def test_every_route_has_exactly_one_dispatcher(self) -> None:
        # Streamed, binary, and leased-run operations are sent from the session route; every other one is dispatched
        # through the operation table, so the route table and the dispatch cannot disagree.
        sent = {
            "assistant-icon",
            "local-assistant-icon",
            "local-assistant-summary",
            "local-assistant-details",
            "file-preview",
            "file-page-preview",
            "file-content",
            server.local_http_routine.RUN_OPERATION,
            *http_chat.STREAMED_OPERATIONS,
            *server.local_http_routine.STREAMED_OPERATIONS,
        }
        routed = {route.operation for route in strict_http.CONTROLLER_ROUTES}
        self.assertEqual(set(server._OPERATIONS), routed - sent)
        self.assertTrue(sent < routed)

    def test_direct_action_human_request_is_a_stable_audited_refusal(self) -> None:
        controller = self.controller()
        suspension = action_human.HumanRequestSuspensionError(human_request_fixtures.request("approval"))
        controller.invoke = mock.Mock(side_effect=suspension)
        handler = self.handler(controller=controller)
        handler.body = mock.Mock(return_value={"name": "World"})
        invoke = self.route("assistant-invoke", team_id="team_1", assistant_id="assistant", action_id="hello")

        with (
            mock.patch.object(server.local_audit, "record_request") as record,
            self.assertRaises(ApiProblemError) as refused,
        ):
            handler._route(invoke)

        self.assertEqual(
            (refused.exception.status, refused.exception.code),
            (HTTPStatus.CONFLICT, "action-human-request-required"),
        )
        self.assertIs(refused.exception.__cause__, suspension)
        controller.invoke.assert_called_once_with("team_1", "assistant", "hello", {"name": "World"})
        record.assert_called_once_with(
            "assistant-action",
            result="denied",
            team_id="team_1",
            assistant="assistant",
            detail="human-request-required:hello",
        )

    def test_chat_capability_plan_uses_the_exact_model_bound_body(self) -> None:
        controller = self.controller()
        handler = self.handler(controller=controller)
        exact_body = {"objective": "Configure DNS.", "candidates": []}
        handler.body = mock.Mock(return_value=exact_body)
        handler.model_credential_headers = mock.Mock(return_value=("openai", "private-model-key"))
        handler.command = "POST"

        result = handler._route(self.route("chat-capability-plan", team_id="team_1"))

        self.assertEqual(result[2], "chat-capability-plan")
        controller.chat_turn_service.capability_plan.assert_called_once_with(
            "team_1",
            exact_body,
            "openai",
            "private-model-key",
        )

    def test_chat_intent_route_uses_the_exact_model_bound_body(self) -> None:
        controller = self.controller()
        handler = self.handler(controller=controller)
        exact_body = {
            "objective": "Desinstale o Cloudflare.",
            "expected_intent": "assistant-uninstall",
            "candidates": [{"id": "shimpz-cloudflare", "name": "Shimpz Cloudflare", "summary": ""}],
            "lifecycle_reference": None,
            "conversation": [],
            "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
            "timezone": None,
            "locale": "en",
        }
        handler.body = mock.Mock(return_value=exact_body)
        handler.model_credential_headers = mock.Mock(return_value=("openai", "private-model-key"))
        handler.command = "POST"

        result = handler._route(self.route("chat-intent-route", team_id="team_1"))

        self.assertEqual(result[2], "chat-intent-route")
        controller.chat_turn_service.intent_route.assert_called_once_with(
            "team_1",
            exact_body,
            "openai",
            "private-model-key",
            None,
        )


class HandlerStreamAndAuthorityEdgeTests(LocalHttpEdgeHelpers, unittest.TestCase):
    @contextlib.contextmanager
    def _assertion_admitted(self, trace: str) -> Iterator[None]:
        """Admit the Supervisor assertion and record the request audit under ``trace``."""
        with (
            mock.patch.object(authority, "credential_state", return_value="assertion_present"),
            mock.patch.object(authority, "verify", return_value=self.evidence()),
            mock.patch.object(http_audit.local_audit, "record", return_value=trace),
        ):
            yield

    @staticmethod
    def evidence() -> authority.Evidence:
        return authority.Evidence("a" * 32)

    def test_stream_terminal_records_success_and_failure(self) -> None:
        handler = self.handler(controller=SimpleNamespace())
        submitted = (HTTPStatus.OK, {"reply": "ok"}, "op", "team_1", None)
        request_audit = SimpleNamespace(record=mock.Mock(return_value="d" * 32))
        with (
            mock.patch.object(local_http_stream, "_write_record") as write,
            mock.patch.object(http_chat, "_submit", return_value=submitted),
            mock.patch.object(
                http_chat, "_start", side_effect=ApiProblemError(HTTPStatus.BAD_REQUEST, "bad", code="bad")
            ),
        ):
            route = self.route("chat-human-submit", team_id="team_1")
            http_chat.stream(handler, route, request_audit)
            self.assertEqual(write.call_args_list[-1].args[1]["type"], "terminal")
            http_chat.stream(handler, self.route("chat", team_id="team_1"), request_audit)
            self.assertEqual(write.call_args.args[1]["body"]["code"], "bad")

            def unexpected(_progress):
                raise RuntimeError("private detail")

            local_http_stream.respond(handler, "chat", "team_1", request_audit, unexpected)
            terminal = write.call_args.args[1]
            self.assertEqual((terminal["status"], terminal["body"].get("code")), (500, None))

    def test_stream_write_failures_are_contained(self) -> None:
        handler = self.handler(controller=SimpleNamespace())
        request_audit = SimpleNamespace(record=mock.Mock(return_value="d" * 32))

        def chat(progress):
            with progress.span("model"):
                pass
            return HTTPStatus.OK, {"reply": "ok"}

        with mock.patch.object(local_http_stream, "_write_record", side_effect=OSError("closed")) as write:
            local_http_stream.respond(handler, "chat", "team_1", request_audit, chat)
            self.assertEqual(write.call_count, 1)
            write.reset_mock(side_effect=True)
            write.side_effect = OSError("terminal closed")
            local_http_stream.respond(handler, "chat", "team_1", request_audit, lambda _progress: (HTTPStatus.OK, {}))
            write.assert_called_once()
        with mock.patch.object(local_http_stream, "_write", side_effect=RuntimeError("failed")):
            handler.close_connection = False
            local_http_stream.respond(handler, "chat", "team_1", request_audit, chat)
        self.assertTrue(handler.close_connection)

    def test_a_teams_largest_registry_is_answered_within_its_route_allowance(self) -> None:
        # Every Assistant a Team may hold, each with the longest identity (40), name, summary, and Actions (64) its
        # contract admits, the text in four-byte characters: more than the API cap, answered whole on its own route.
        text = "\U0001f600" * 80
        largest = {
            "assistants": [
                {
                    "id": f"assistant-{index:02d}{'a' * 28}",
                    "title": text,
                    "summary": text,
                    "actions": [f"action-{action:03d}{'a' * 54}" for action in range(128)],
                }
                for index in range(http_payload.MAX_TEAM_ASSISTANTS)
            ]
        }
        controller = SimpleNamespace(team_registry=mock.Mock(return_value=largest))
        handler = self.handler(controller=controller)
        handler._resolved_route = mock.Mock(return_value=([], self.route("assistant-registry", team_id="team_1")))
        handler.capture_body = mock.Mock(return_value={})
        handler._model_binding = mock.Mock(return_value=None)
        handler._expected_human_assurance = mock.Mock(return_value=None)
        with self._assertion_admitted("d" * 32):
            status, payload, operation, team_id, _assistant = handler._authorized_route(http_audit.RequestAudit())
        self.assertEqual((status, operation, team_id), (HTTPStatus.OK, "assistant-registry", "team_1"))
        controller.team_registry.assert_called_once_with("team_1")
        handler._send(status, payload)
        handler.send_response.assert_called_once_with(HTTPStatus.OK)
        self.assertGreater(len(handler.wfile.getvalue()), server.MAX_API_RESPONSE_BYTES)

    def test_authority_unavailable_icon_and_rejected_assertion_paths(self) -> None:
        controller = SimpleNamespace(
            assistant_icon=mock.Mock(return_value=b"png"),
            local_snapshot_icon=mock.Mock(return_value=b"local-png"),
            local_snapshot_summary=mock.Mock(return_value={"locale": "pt", "summary": "Resumo."}),
        )
        handler = self.handler(controller=controller)
        handler._resolved_route = mock.Mock(return_value=([], self.route("team-list")))
        handler.capture_body = mock.Mock(return_value={})
        handler._model_binding = mock.Mock(return_value=None)
        handler._expected_human_assurance = mock.Mock(return_value=None)
        audit = http_audit.RequestAudit()
        with (
            mock.patch.object(authority, "credential_state", return_value="assertion_present"),
            mock.patch.object(authority, "verify", side_effect=authority.SupervisorDeniedError("denied")),
            mock.patch.object(http_audit.local_audit, "record", return_value="d" * 32),
            self.assertRaises(ApiProblemError),
        ):
            handler._authorized_route(audit)
        self.assertEqual(audit.credential_state, "assertion_rejected")

        with (
            mock.patch.object(authority, "credential_state", return_value="assertion_absent_or_malformed"),
            mock.patch.object(authority, "verify", side_effect=authority.SupervisorUnavailableError("missing")),
            mock.patch.object(http_audit.local_audit, "record", return_value="d" * 32),
            self.assertRaises(ApiProblemError) as caught,
        ):
            handler._authorized_route(http_audit.RequestAudit())
        self.assertEqual(caught.exception.code, "supervisor-unavailable")

        icon_route = self.route("assistant-icon", team_id="team_1", assistant_id="assistant")
        handler._resolved_route.return_value = ([], icon_route)
        handler._send_image = mock.Mock()
        with self._assertion_admitted("d" * 32):
            self.assertIsNone(handler._authorized_route(http_audit.RequestAudit()))
        handler._send_image.assert_called_once_with(b"png", "image/png")

        local_icon_route = self.route(
            "local-assistant-icon",
            image_hash="a" * 64,
        )
        handler._resolved_route.return_value = ([], local_icon_route)
        handler._send_image.reset_mock()
        with self._assertion_admitted("d" * 32):
            self.assertIsNone(handler._authorized_route(http_audit.RequestAudit()))
        controller.local_snapshot_icon.assert_called_once_with("sha256:" + "a" * 64)
        handler._send_image.assert_called_once_with(b"local-png", "image/png")

        controller.local_snapshot_icon.side_effect = ApiProblemError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Local Assistant preview capacity is busy",
            code="local-assistant-preview-busy",
        )
        handler._send = mock.Mock()
        with self._assertion_admitted("e" * 32):
            self.assertIsNone(handler._authorized_route(http_audit.RequestAudit()))
        handler._send.assert_called_once_with(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {
                "error": "Local Assistant preview capacity is busy",
                "code": "local-assistant-preview-busy",
                "retry_after_ms": 250,
                "trace_id": "e" * 32,
            },
        )

        controller.local_snapshot_icon.side_effect = ApiProblemError(
            HTTPStatus.NOT_FOUND,
            "Local Assistant preview is unavailable",
            code="local-assistant-preview-unavailable",
        )
        with self._assertion_admitted("f" * 32), self.assertRaises(ApiProblemError) as caught:
            handler._authorized_route(http_audit.RequestAudit())
        self.assertEqual(caught.exception.code, "local-assistant-preview-unavailable")

        # A staged snapshot's localized summary shares the bounded preview, including its busy retry hint.
        handler._resolved_route.return_value = (
            [],
            self.route("local-assistant-summary", image_hash="a" * 64, locale="pt"),
        )
        handler._send.reset_mock()
        with self._assertion_admitted("a" * 32):
            self.assertIsNone(handler._authorized_route(http_audit.RequestAudit()))
        controller.local_snapshot_summary.assert_called_once_with("sha256:" + "a" * 64, "pt")
        handler._send.assert_called_once_with(
            HTTPStatus.OK,
            {"locale": "pt", "summary": "Resumo.", "trace_id": "a" * 32},
        )
        controller.local_snapshot_summary.side_effect = ApiProblemError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Local Assistant preview capacity is busy",
            code="local-assistant-preview-busy",
        )
        handler._send.reset_mock()
        with self._assertion_admitted("b" * 32):
            self.assertIsNone(handler._authorized_route(http_audit.RequestAudit()))
        self.assertEqual(handler._send.call_args.args[1]["retry_after_ms"], 250)

    def test_a_staged_snapshots_page_shares_the_bounded_preview_and_its_busy_hint(self) -> None:
        busy = ApiProblemError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Local Assistant preview capacity is busy",
            code="local-assistant-preview-busy",
        )
        controller = SimpleNamespace(
            local_snapshot_details=mock.Mock(side_effect=({"locale": "pt", "summary": "Resumo."}, busy))
        )
        handler = self.handler(controller=controller)
        handler._resolved_route = mock.Mock(
            return_value=([], self.route("local-assistant-details", image_hash="a" * 64, locale="pt"))
        )
        handler.capture_body = mock.Mock(return_value={})
        handler._model_binding = mock.Mock(return_value=None)
        handler._expected_human_assurance = mock.Mock(return_value=None)
        handler._send = mock.Mock()
        with self._assertion_admitted("c" * 32):
            self.assertIsNone(handler._authorized_route(http_audit.RequestAudit()))
        controller.local_snapshot_details.assert_called_with("sha256:" + "a" * 64, "pt")
        handler._send.assert_called_once_with(
            HTTPStatus.OK,
            {"locale": "pt", "summary": "Resumo.", "trace_id": "c" * 32},
        )
        handler._send.reset_mock()
        with self._assertion_admitted("d" * 32):
            self.assertIsNone(handler._authorized_route(http_audit.RequestAudit()))
        self.assertEqual(handler._send.call_args.args[0], HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(handler._send.call_args.args[1]["code"], "local-assistant-preview-busy")
        self.assertEqual(handler._send.call_args.args[1]["retry_after_ms"], 250)

    def test_supervisor_key_rotation_route_maps_every_outcome(self) -> None:
        controller = HandlerRouteEdgeTests.controller()
        handler = self.handler(method="POST", controller=controller)
        handler.body = mock.Mock(return_value={"public_key": "A" * 43})
        rotate_route = self.route("supervisor-key-rotate")
        rotated = {"rotated": True, "key_sha256": "a" * 64}
        with (
            mock.patch.object(authority, "rotate_supervisor_key", return_value=rotated) as rotate,
            mock.patch.object(http_audit.local_audit, "record_request") as audit,
        ):
            self.assertEqual(handler._route(rotate_route)[1:3], (rotated, "supervisor-key-rotate"))
            handler._supervisor_evidence = SimpleNamespace(key_sha256="b" * 64)
            handler._route(rotate_route)
        self.assertEqual([call.args[1] for call in rotate.call_args_list], ["", "b" * 64])
        audit.assert_called_with("supervisor-key-rotate", result="ok", detail="a" * 16)
        for error, status, code in (
            (authority.SupervisorDeniedError("stale"), HTTPStatus.CONFLICT, "supervisor-key-rotation-refused"),
            (authority.SupervisorUnavailableError("io"), HTTPStatus.SERVICE_UNAVAILABLE, "supervisor-unavailable"),
        ):
            with (
                self.subTest(code=code),
                mock.patch.object(authority, "rotate_supervisor_key", side_effect=error),
                self.assertRaises(ApiProblemError) as caught,
            ):
                handler._route(rotate_route)
            self.assertEqual((caught.exception.status, caught.exception.code), (status, code))
        match = strict_http.resolve_controller_route("POST", ("v1", "space", "supervisor-key"))
        self.assertEqual(match.operation, "supervisor-key-rotate")

    def test_a_bootstrap_reset_removes_teams_supervisor_key_pin(self) -> None:
        controller = HandlerRouteEdgeTests.controller()
        handler = self.handler(method="DELETE", controller=controller)
        with (
            mock.patch.object(authority, "require_supervisor_absent"),
            mock.patch.object(authority, "forget_supervisor_key") as forget,
        ):
            handler._route(self.route("space-bootstrap-reset"))
        forget.assert_called_once_with()
        with (
            mock.patch.object(authority, "require_supervisor_absent"),
            mock.patch.object(
                authority, "forget_supervisor_key", side_effect=authority.SupervisorUnavailableError("io")
            ),
            self.assertRaises(ApiProblemError) as caught,
        ):
            handler._route(self.route("space-bootstrap-reset"))
        self.assertEqual(caught.exception.code, "supervisor-unavailable")

    def test_bootstrap_reset_uses_machine_authority_without_human_assertion(self) -> None:
        controller = HandlerRouteEdgeTests.controller()
        handler = self.handler(method="DELETE", path="/v1/space/bootstrap", controller=controller)
        handler._resolved_route = mock.Mock(
            return_value=(
                ["v1", "space", "bootstrap"],
                self.route("space-bootstrap-reset"),
            )
        )
        handler.capture_body = mock.Mock(return_value={"kind": "none"})
        with (
            mock.patch.object(authority, "require_supervisor_absent"),
            mock.patch.object(authority, "verify") as verify,
            mock.patch.object(http_audit.local_audit, "record", return_value="d" * 32),
        ):
            result = handler._authorized_route(http_audit.RequestAudit())

        self.assertEqual(result[2], "space-bootstrap-reset")
        verify.assert_not_called()
        controller.reset_space.assert_called_once_with()

    def test_human_assurance_rejects_every_mismatch(self) -> None:
        handler = self.handler(controller=SimpleNamespace())
        handler.body = mock.Mock(return_value={})
        self.assertIsNone(handler._expected_human_assurance("other", {}))
        self.assertIsNone(handler._expected_human_assurance("chat-human-submit", {"team_id": "team_1"}))

        challenge = SimpleNamespace(
            id="a" * 32,
            requirement=SimpleNamespace(request=SimpleNamespace(kind="auth:password")),
        )
        service = SimpleNamespace(
            expire_human_challenges=mock.Mock(),
            human_challenges=SimpleNamespace(current=mock.Mock(return_value=None)),
        )
        handler.server.controller = SimpleNamespace(chat_turn_service=service)
        handler.body.return_value = {"challenge_id": "a" * 32, "decision": "submit", "value": True}
        self.assertIsNone(handler._expected_human_assurance("chat-human-submit", {"team_id": "team_1"}))
        service.human_challenges.current.return_value = challenge
        handler.body.return_value["challenge_id"] = "b" * 32
        self.assertIsNone(handler._expected_human_assurance("chat-human-submit", {"team_id": "team_1"}))
        handler.body.return_value["challenge_id"] = "a" * 32
        challenge.requirement.request.kind = "approval"
        self.assertIsNone(handler._expected_human_assurance("chat-human-submit", {"team_id": "team_1"}))

    def test_non_ascii_bearer_is_refused_as_unauthenticated(self) -> None:
        handler = self.handler()
        # The stdlib parser decodes raw header bytes as Latin-1, so UTF-8 bytes arrive as non-ASCII text.
        handler.headers = http.client.parse_headers(BytesIO(b"Authorization: Bearer \xc3\xa9\r\n\r\n"))
        self.assertFalse(handler.headers["Authorization"].isascii())
        self.assertFalse(handler._authorized())
        with mock.patch.object(http_audit.local_audit, "record", return_value="d" * 32):
            handler._handle()
        handler.send_response.assert_called_once_with(HTTPStatus.UNAUTHORIZED)
        self.assertIn(b"authentication required", handler.wfile.getvalue())

    def test_handle_and_method_entrypoints_delegate_once(self) -> None:
        handler = self.handler()
        handler._authorized = mock.Mock(return_value=False)
        handler._send = mock.Mock()
        with mock.patch.object(http_audit.local_audit, "record", return_value="d" * 32):
            handler._handle()
        handler._send.assert_called_once()

        handler._authorized.return_value = True
        with mock.patch.object(server.local, "dispatch_route") as dispatch:
            handler._handle()
        dispatch.assert_called_once()

        handler._handle = mock.Mock()
        for method in (
            handler.do_GET,
            handler.do_POST,
            handler.do_DELETE,
            handler.do_HEAD,
            handler.do_OPTIONS,
            handler.do_PATCH,
            handler.do_PUT,
        ):
            method()
        self.assertEqual(handler._handle.call_count, 7)


if __name__ == "__main__":
    unittest.main()
