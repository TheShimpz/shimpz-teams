from __future__ import annotations

import hashlib
import unittest
from email.message import Message
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from types import SimpleNamespace
from unittest import mock

from core.http import strict as strict_http
from integrations import broker as integration_broker
from local import authority
from local.errors import ApiProblemError
from local.http import server

TEST_TOKEN = "t" * 32


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
    def test_setup_authorization_and_response_writers(self) -> None:
        handler = self.handler()
        handler.connection = SimpleNamespace(settimeout=mock.Mock())
        with mock.patch.object(BaseHTTPRequestHandler, "setup"):
            handler.setup()
        handler.connection.settimeout.assert_called_once_with(server.REQUEST_TIMEOUT_SECONDS)

        handler.headers["Authorization"] = f"Bearer {TEST_TOKEN}"
        self.assertTrue(handler._authorized())
        self.assertIsNone(handler.log_message("ignored"))

        handler._send(HTTPStatus.UNAUTHORIZED, {"error": "denied"})
        handler.send_header.assert_any_call("WWW-Authenticate", 'Bearer realm="shimpz-local"')
        self.assertIn(b"denied", handler.wfile.getvalue())

        head = self.handler("HEAD")
        with mock.patch.object(server, "MAX_API_RESPONSE_BYTES", 1):
            head._send(HTTPStatus.OK, {"large": "value"})
        head.send_response.assert_called_once_with(HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertEqual(head.wfile.getvalue(), b"")

        icon = self.handler()
        icon._send_icon(b"png")
        self.assertEqual(icon.wfile.getvalue(), b"png")
        icon.command = "HEAD"
        icon.wfile = BytesIO()
        icon._send_icon(b"png")
        self.assertEqual(icon.wfile.getvalue(), b"")

    def test_captured_body_and_file_contract_fail_closed(self) -> None:
        handler = self.handler()
        with self.assertRaises(ApiProblemError):
            handler._body()
        handler._captured_json_raw = b"{}"
        handler._captured_json_body = {}
        self.assertEqual(handler._body(), {})
        with self.assertRaises(ApiProblemError):
            handler._body(max_bytes=1)

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
            handler._capture_body("chat")
        self.assertEqual(caught.exception.code, "content-length")

    def test_body_shapes_and_request_target_validation(self) -> None:
        handler = self.handler()
        handler._body = mock.Mock(return_value={})
        with self.assertRaises(ApiProblemError):
            handler._team_create_body()
        handler._body.return_value = {"team_name": "Team"}
        self.assertEqual(handler._team_create_body(), "Team")

        for body in ({}, {"assistant_id": 1, "source_digest": "sha256:" + "a" * 64}):
            handler._body.return_value = body
            with self.subTest(body=body), self.assertRaises(ApiProblemError):
                handler._install_body()
        digest = "sha256:" + "a" * 64
        handler._body.return_value = {"assistant_id": "assistant", "source_digest": digest}
        self.assertEqual(handler._install_body(), ("assistant", digest))

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
            handler._model_binding("assistant-action-labels"),
            {"provider": "openai", "key_sha256": hashlib.sha256(api_key.encode("ascii")).hexdigest()},
        )
        self.assertIsNone(handler._model_binding("assistant-list"))


class HandlerRouteEdgeTests(LocalHttpEdgeHelpers, unittest.TestCase):
    @staticmethod
    def controller() -> SimpleNamespace:
        service = SimpleNamespace(
            action_labels=mock.Mock(return_value={"actions": []}),
            complete_cloudflare_oauth_callback=mock.Mock(return_value={"connected": True}),
            pending_chat_human=mock.Mock(return_value={"pending": "human"}),
            pending_chat_integrations=mock.Mock(return_value={"pending": "integration"}),
            resume_chat_human=mock.Mock(return_value={"status": "ok"}),
            resume_chat_integrations=mock.Mock(return_value={"status": "ok"}),
            stop_chat=mock.Mock(return_value={"stopped": True}),
            list_assistant_integrations=mock.Mock(return_value={"integrations": []}),
            start_assistant_integration_authorization=mock.Mock(return_value={"url": "https://example.com"}),
            cancel_assistant_integration_authorization=mock.Mock(return_value={"cancelled": True}),
            disconnect_assistant_integration=mock.Mock(return_value={"disconnected": True}),
        )
        return SimpleNamespace(
            chat_turn_service=service,
            list_registry=mock.Mock(return_value={"assistants": []}),
            reset_space=mock.Mock(return_value={"reset": True}),
            list_files=mock.Mock(return_value={"files": []}),
            put_file=mock.Mock(return_value={"file": {}}),
            delete_file=mock.Mock(return_value={"deleted": True}),
            inference_status=mock.Mock(return_value={"configured": True}),
            configure_inference=mock.Mock(return_value={"configured": True}),
            create_team=mock.Mock(return_value={"created": True}),
            destroy_team=mock.Mock(return_value={"deleted": True}),
            list_assistants=mock.Mock(return_value={"assistants": []}),
            install_publication=mock.Mock(return_value={"installed": True}),
            assistant_lifecycle=SimpleNamespace(uninstall_assistant=mock.Mock(return_value={"uninstalled": True})),
            invoke=mock.Mock(return_value={"result": "ok"}),
        )

    def test_fixed_file_and_inference_routes_cover_every_outcome(self) -> None:
        controller = self.controller()
        handler = self.handler(controller=controller)
        self.assertEqual(handler._fixed_route(["v1", "assistants"])[2], "registry-list")
        handler.command = "DELETE"
        self.assertIsNone(handler._fixed_route(["v1", "space", "unknown"]))
        with mock.patch.object(authority, "require_supervisor_absent"):
            self.assertEqual(handler._fixed_route(["v1", "space", "bootstrap"])[2], "space-bootstrap-reset")
        self.assertEqual(handler._fixed_route(["v1", "space"])[2], "space-reset")
        handler.command = "POST"
        handler._body = mock.Mock(return_value={})
        with self.assertRaises(ApiProblemError):
            handler._fixed_route(["v1", "oauth", "cloudflare", "callback"])
        handler._body.return_value = {"state": "s", "claim": "c", "session_binding": "b"}
        self.assertEqual(handler._fixed_route(["v1", "oauth", "cloudflare", "callback"])[1], {"connected": True})
        self.assertIsNone(handler._fixed_route(["other"]))

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
                handler._fixed_route(["v1", "space", "bootstrap"])
            self.assertEqual(caught.exception.code, code)
        controller.reset_space.assert_not_called()

        self.assertIsNone(handler._file_route(["other"]))
        handler.command = "GET"
        self.assertEqual(handler._file_route(["v1", "teams", "team_1", "files"])[2], "file-list")
        handler.command = "POST"
        handler._file_body = mock.Mock(return_value=("a.txt", b"x", "text/plain"))
        slots = SimpleNamespace(acquire=mock.Mock(return_value=False), release=mock.Mock())
        with mock.patch.object(server, "_FILE_UPLOAD_SLOTS", slots), self.assertRaises(ApiProblemError):
            handler._file_route(["v1", "teams", "team_1", "files"])
        slots.acquire.return_value = True
        with mock.patch.object(server, "_FILE_UPLOAD_SLOTS", slots):
            self.assertEqual(handler._file_route(["v1", "teams", "team_1", "files"])[2], "file-upload")
        slots.release.assert_called_once_with()
        handler.command = "DELETE"
        self.assertEqual(handler._file_route(["v1", "teams", "team_1", "files", "a" * 32])[2], "file-delete")
        handler.command = "PATCH"
        self.assertIsNone(handler._file_route(["v1", "teams", "team_1", "files"]))

        self.assertIsNone(handler._inference_route(["other"]))
        handler.command = "GET"
        self.assertEqual(handler._inference_route(["v1", "teams", "team_1", "inference"])[2], "inference-status")
        handler.command = "PUT"
        handler._body = mock.Mock(return_value={"provider": "openai"})
        self.assertEqual(handler._inference_route(["v1", "teams", "team_1", "inference"])[2], "inference-configure")
        handler.command = "PATCH"
        self.assertIsNone(handler._inference_route(["v1", "teams", "team_1", "inference"]))

    def test_chat_route_variants_and_validation(self) -> None:
        handler = self.handler(controller=self.controller())
        handler._model_credential_headers = mock.Mock(return_value=("openai", "key"))
        handler._body = mock.Mock(return_value={})
        self.assertIsNone(handler._chat_pending("team_1", "unknown"))
        self.assertEqual(handler._chat_pending("team_1", "integrations")[2], "chat-integration-pending")
        self.assertIsNone(handler._chat_submit("team_1", "unknown"))
        self.assertEqual(handler._chat_submit("team_1", "integrations")[2], "chat-integration-submit")
        handler._body.return_value = {"unexpected": True}
        with self.assertRaises(ApiProblemError):
            handler._chat_stop("team_1")
        handler._body.return_value = {}
        self.assertEqual(handler._chat_stop("team_1")[2], "chat-stop")

        self.assertIsNone(handler._chat_route(["other"]))
        handler.command = "GET"
        self.assertIsNone(handler._chat_route(["v1", "teams", "team_1", "chat"]))
        self.assertIsNone(handler._chat_route(["v1", "teams", "team_1", "chat", "unknown"]))
        handler.command = "PATCH"
        self.assertIsNone(handler._chat_route(["v1", "teams", "team_1", "chat", "human"]))
        handler.command = "POST"
        self.assertEqual(handler._chat_route(["v1", "teams", "team_1", "chat", "stop"])[2], "chat-stop")

    def test_integration_and_team_routes_cover_exact_shapes(self) -> None:
        handler = self.handler(controller=self.controller())
        handler._body = mock.Mock(return_value={})
        self.assertIsNone(handler._assistant_integration_route(["other"]))
        handler.command = "GET"
        path = ["v1", "teams", "team_1", "assistant-integrations"]
        self.assertEqual(handler._assistant_integration_route(path)[2], "assistant-integration-list")
        authorize = ["v1", "teams", "team_1", "assistant-integrations", "challenges", "a" * 32, "authorize"]
        handler.command = "POST"
        with self.assertRaises(ApiProblemError):
            handler._assistant_integration_route(authorize)
        handler._body.return_value = {
            "assistant_id": "assistant",
            "integration_id": "cloudflare",
            "callback_mode": "invalid",
            "session_binding": "b",
        }
        with self.assertRaises(ApiProblemError):
            handler._assistant_integration_route(authorize)
        callback_mode = next(iter(integration_broker.CALLBACK_MODES))
        handler._body.return_value = {
            "assistant_id": "assistant",
            "integration_id": "cloudflare",
            "callback_mode": callback_mode,
            "session_binding": "b",
        }
        self.assertEqual(handler._assistant_integration_route(authorize)[2], "assistant-integration-authorize")
        handler.command = "DELETE"
        handler._body.return_value = {}
        with self.assertRaises(ApiProblemError):
            handler._assistant_integration_route(authorize)
        handler._body.return_value = {"session_binding": "b"}
        self.assertEqual(handler._assistant_integration_route(authorize)[2], "assistant-integration-cancel")
        disconnect = ["v1", "teams", "team_1", "assistant-integrations", "assistant", "cloudflare"]
        self.assertEqual(handler._assistant_integration_route(disconnect)[2], "assistant-integration-disconnect")
        handler.command = "PATCH"
        self.assertIsNone(handler._assistant_integration_route(path))

        create = ["v1", "teams", "team_1", "create"]
        self.assertIsNone(handler._team_route(create))
        handler.command = "POST"
        handler._team_create_body = mock.Mock(return_value="Team")
        self.assertEqual(handler._team_route(create)[2], "team-create")
        handler.command = "DELETE"
        self.assertEqual(handler._team_route(["v1", "teams", "team_1"])[2], "team-destroy")
        self.assertIsNone(handler._team_route(["other"]))

    def test_generic_route_dispatches_all_assistant_operations(self) -> None:
        controller = self.controller()
        handler = self.handler(controller=controller)
        handler._install_body = mock.Mock(return_value=("assistant", "sha256:" + "a" * 64))
        handler._body = mock.Mock(return_value={"input": "ok"})
        handler._model_credential_headers = mock.Mock(return_value=("openai", "private-model-key"))
        cases = (
            ("assistant-list", {"team_id": "team_1"}),
            ("assistant-install", {"team_id": "team_1"}),
            ("assistant-uninstall", {"team_id": "team_1", "assistant_id": "assistant"}),
            (
                "assistant-invoke",
                {"team_id": "team_1", "assistant_id": "assistant", "action_id": "action"},
            ),
        )
        for operation, params in cases:
            with self.subTest(operation=operation):
                result = handler._route([], self.route(operation, **params))
                self.assertEqual(result[2], operation)

        exact_body = {"language_exemplar": "Quero listar minhas zonas DNS"}
        handler._body.return_value = exact_body
        result = handler._route(
            [],
            self.route("assistant-action-labels", team_id="team_1", assistant_id="assistant"),
        )
        self.assertEqual(result[2], "assistant-action-labels")
        controller.chat_turn_service.action_labels.assert_called_once_with(
            "team_1",
            "assistant",
            exact_body,
            "openai",
            "private-model-key",
        )

        with self.assertRaises(AssertionError):
            handler._route([], self.route("unknown", team_id="team_1", assistant_id="assistant"))
        handler._fixed_route = mock.Mock(return_value=None)
        with self.assertRaises(AssertionError):
            handler._route([], self.route("health"))


class HandlerStreamAndAuthorityEdgeTests(LocalHttpEdgeHelpers, unittest.TestCase):
    @staticmethod
    def evidence() -> authority.Evidence:
        return authority.Evidence("a" * 32, "session", "b" * 64, "c" * 32, 2_200_000_000)

    def test_stream_submit_failure_and_non_stream_guard(self) -> None:
        handler = self.handler(controller=SimpleNamespace())
        handler._chat_submit = mock.Mock(return_value=(HTTPStatus.OK, {"reply": "ok"}, "op", "team_1", None))
        handler._write_stream_record = mock.Mock()
        request_audit = SimpleNamespace(record=mock.Mock(return_value="d" * 32))
        route = self.route("chat-human-submit", team_id="team_1")
        handler._write_chat_stream(["v1", "teams", "team_1", "chat", "human"], route, request_audit)
        self.assertEqual(handler._write_stream_record.call_args_list[-1].args[0]["type"], "terminal")

        handler._write_stream_record.reset_mock()
        route = self.route("unexpected", team_id="team_1")
        handler._write_chat_stream(["v1", "teams", "team_1", "chat"], route, request_audit)
        terminal = handler._write_stream_record.call_args.args[0]
        self.assertEqual(terminal["status"], HTTPStatus.INTERNAL_SERVER_ERROR)

        handler._chat_start = mock.Mock(side_effect=ApiProblemError(HTTPStatus.BAD_REQUEST, "bad", code="bad"))
        route = self.route("chat", team_id="team_1")
        handler._write_chat_stream(["v1", "teams", "team_1", "chat"], route, request_audit)
        terminal = handler._write_stream_record.call_args.args[0]
        self.assertEqual(terminal["body"]["code"], "bad")

    def test_stream_write_failures_are_contained(self) -> None:
        handler = self.handler(controller=SimpleNamespace())
        request_audit = SimpleNamespace(record=mock.Mock(return_value="d" * 32))
        route = self.route("chat", team_id="team_1")

        def chat(_team_id, progress):
            with progress.span("model"):
                pass
            return HTTPStatus.OK, {"reply": "ok"}, "chat", "team_1", None

        handler._chat_start = mock.Mock(side_effect=chat)
        handler._write_stream_record = mock.Mock(side_effect=OSError("closed"))
        handler._write_chat_stream(["v1", "teams", "team_1", "chat"], route, request_audit)
        self.assertEqual(handler._write_stream_record.call_count, 1)

        handler._chat_start = mock.Mock(return_value=(HTTPStatus.OK, {"reply": "ok"}, "chat", "team_1", None))
        handler._write_stream_record.reset_mock(side_effect=True)
        handler._write_stream_record.side_effect = OSError("terminal closed")
        handler._write_chat_stream(["v1", "teams", "team_1", "chat"], route, request_audit)
        handler._write_stream_record.assert_called_once()

        handler._write_chat_stream = mock.Mock(side_effect=RuntimeError("failed"))
        handler.close_connection = False
        handler._stream_chat_route([], route, request_audit)
        self.assertTrue(handler.close_connection)

    def test_authority_unavailable_icon_and_rejected_assertion_paths(self) -> None:
        controller = SimpleNamespace(assistant_icon=mock.Mock(return_value=b"png"))
        handler = self.handler(controller=controller)
        handler._resolved_route = mock.Mock(return_value=([], self.route("team-list")))
        handler._capture_body = mock.Mock(return_value={})
        handler._model_binding = mock.Mock(return_value=None)
        handler._expected_human_assurance = mock.Mock(return_value=None)
        audit = server._RequestAudit()
        with (
            mock.patch.object(authority, "credential_state", return_value="assertion_present"),
            mock.patch.object(authority, "verify", side_effect=authority.SupervisorDeniedError("denied")),
            mock.patch.object(server.local_audit, "record", return_value="d" * 32),
            self.assertRaises(ApiProblemError),
        ):
            handler._authorized_route(audit)
        self.assertEqual(audit.credential_state, "assertion_rejected")

        with (
            mock.patch.object(authority, "credential_state", return_value="assertion_absent_or_malformed"),
            mock.patch.object(authority, "verify", side_effect=authority.SupervisorUnavailableError("missing")),
            mock.patch.object(server.local_audit, "record", return_value="d" * 32),
            self.assertRaises(ApiProblemError) as caught,
        ):
            handler._authorized_route(server._RequestAudit())
        self.assertEqual(caught.exception.code, "supervisor-unavailable")

        icon_route = self.route("assistant-icon", team_id="team_1", assistant_id="assistant")
        handler._resolved_route.return_value = ([], icon_route)
        handler._send_icon = mock.Mock()
        with (
            mock.patch.object(authority, "credential_state", return_value="assertion_present"),
            mock.patch.object(authority, "verify", return_value=self.evidence()),
            mock.patch.object(server.local_audit, "record", return_value="d" * 32),
        ):
            self.assertIsNone(handler._authorized_route(server._RequestAudit()))
        handler._send_icon.assert_called_once_with(b"png")

    def test_bootstrap_reset_uses_machine_authority_without_human_assertion(self) -> None:
        controller = HandlerRouteEdgeTests.controller()
        handler = self.handler(method="DELETE", path="/v1/space/bootstrap", controller=controller)
        handler._resolved_route = mock.Mock(
            return_value=(
                ["v1", "space", "bootstrap"],
                self.route("space-bootstrap-reset"),
            )
        )
        handler._capture_body = mock.Mock(return_value={"kind": "none"})
        with (
            mock.patch.object(authority, "require_supervisor_absent"),
            mock.patch.object(authority, "verify") as verify,
            mock.patch.object(server.local_audit, "record", return_value="d" * 32),
        ):
            result = handler._authorized_route(server._RequestAudit())

        self.assertEqual(result[2], "space-bootstrap-reset")
        verify.assert_not_called()
        controller.reset_space.assert_called_once_with()

    def test_human_assurance_rejects_every_mismatch(self) -> None:
        handler = self.handler(controller=SimpleNamespace())
        handler._body = mock.Mock(return_value={})
        self.assertIsNone(handler._expected_human_assurance("other", {}))
        self.assertIsNone(handler._expected_human_assurance("chat-human-submit", {"team_id": "team_1"}))

        challenge = SimpleNamespace(
            id="a" * 32,
            requirement=SimpleNamespace(request=SimpleNamespace(kind="auth:password")),
        )
        service = SimpleNamespace(
            _expire_human_challenges=mock.Mock(),
            human_challenges=SimpleNamespace(current=mock.Mock(return_value=None)),
        )
        handler.server.controller = SimpleNamespace(chat_turn_service=service)
        handler._body.return_value = {"challenge_id": "a" * 32, "decision": "submit", "value": True}
        self.assertIsNone(handler._expected_human_assurance("chat-human-submit", {"team_id": "team_1"}))
        service.human_challenges.current.return_value = challenge
        handler._body.return_value["challenge_id"] = "b" * 32
        self.assertIsNone(handler._expected_human_assurance("chat-human-submit", {"team_id": "team_1"}))
        handler._body.return_value["challenge_id"] = "a" * 32
        challenge.requirement.request.kind = "approval"
        self.assertIsNone(handler._expected_human_assurance("chat-human-submit", {"team_id": "team_1"}))

    def test_handle_and_method_entrypoints_delegate_once(self) -> None:
        handler = self.handler()
        handler._authorized = mock.Mock(return_value=False)
        handler._send = mock.Mock()
        with mock.patch.object(server.local_audit, "record", return_value="d" * 32):
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
