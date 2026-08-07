"""Transport, admission, and authorization edge coverage for Hosted HTTP."""

from __future__ import annotations

import io
import sys
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hosted_assistant_fixture as harness

server = harness.hosted_controller
resources = harness.hosted_resources
state = harness.runtime_state

TEAM_ID = "team_1"
ACCOUNT_ID = "a" * 32


def _handler() -> server.Handler:
    handler = object.__new__(server.Handler)
    handler.path = "/v1/teams"
    handler.headers = mock.Mock()
    handler.rfile = io.BytesIO()
    handler.wfile = io.BytesIO()
    handler.send_response = mock.Mock()
    handler.send_header = mock.Mock()
    handler.end_headers = mock.Mock()
    return handler


def _route(operation: str, params: dict[str, str] | None = None):
    return SimpleNamespace(operation=operation, params=params or {})


class HostedHttpTransportEdgeTests(unittest.TestCase):
    def test_bounded_server_sets_timeout_releases_slots_and_contains_spawn_failure(self) -> None:
        with mock.patch.object(server.ThreadingHTTPServer, "__init__", return_value=None):
            bounded = server._BoundedThreadingHTTPServer(("127.0.0.1", 0), server.Handler, max_concurrency=1)
        socket = mock.Mock()
        with mock.patch.object(server.ThreadingHTTPServer, "get_request", return_value=(socket, ("client", 1))):
            self.assertEqual(bounded.get_request(), (socket, ("client", 1)))
        socket.settimeout.assert_called_once_with(state.HTTP_CONNECTION_TIMEOUT_SECONDS)

        with mock.patch.object(server.ThreadingHTTPServer, "process_request", return_value=None):
            bounded.process_request(socket, ("client", 1))
        self.assertFalse(bounded._request_slots.acquire(blocking=False))
        bounded._request_slots.release()

        with (
            mock.patch.object(server.ThreadingHTTPServer, "process_request", side_effect=RuntimeError("spawn")),
            self.assertRaises(RuntimeError),
        ):
            bounded.process_request(socket, ("client", 1))
        self.assertTrue(bounded._request_slots.acquire(blocking=False))
        bounded._request_slots.release()

        bounded._request_slots.acquire()
        with mock.patch.object(server.ThreadingHTTPServer, "process_request_thread"):
            bounded.process_request_thread(socket, ("client", 1))
        self.assertTrue(bounded._request_slots.acquire(blocking=False))
        bounded._request_slots.release()

    def test_response_helpers_emit_closed_json_and_icon_headers(self) -> None:
        handler = _handler()
        handler._send_json(HTTPStatus.OK, {"ok": True}, no_store=True)
        self.assertIn(b'"ok": true', handler.wfile.getvalue())
        handler.send_header.assert_any_call("Cache-Control", "no-store")

        handler.wfile = io.BytesIO()
        handler._send_icon(b"png")
        self.assertEqual(handler.wfile.getvalue(), b"png")
        handler.send_header.assert_any_call("X-Content-Type-Options", "nosniff")

    def test_captured_body_file_and_closed_team_shape_validation(self) -> None:
        handler = _handler()
        handler._captured_json_raw = b"{}"
        handler._captured_json_body = {}
        self.assertEqual(handler._read_body(), {})
        handler._captured_json_raw = b"x" * 4
        with self.assertRaises(state.ApiError):
            handler._read_body(max_bytes=3)

        with self.assertRaises(state.ApiError):
            handler._read_file_body()
        metadata = server.strict_http.FileUploadMetadata(1, "file.txt", "text/plain")
        handler._captured_file_metadata = metadata
        with mock.patch.object(server.strict_http, "read_file_content", return_value=b"x"):
            self.assertEqual(handler._read_file_body(), ("file.txt", b"x", "text/plain"))
        contract_error = server.strict_http.HttpContractError(400, "invalid", code="invalid")
        with (
            mock.patch.object(server.strict_http, "read_file_content", side_effect=contract_error),
            self.assertRaises(state.ApiError),
        ):
            handler._read_file_body()

        handler.headers = mock.Mock()
        with mock.patch.object(server.strict_http, "file_upload_metadata", return_value=metadata):
            self.assertEqual(handler._capture_body("file-upload")["kind"], "file")
        with mock.patch.object(server.strict_http, "read_json_document", return_value=(b"{}", {})):
            captured = handler._capture_body("chat")
        self.assertEqual(captured["kind"], "json")
        with mock.patch.object(server.strict_http, "reject_body"):
            self.assertEqual(handler._capture_body("team-status")["kind"], "none")
        with (
            mock.patch.object(server.strict_http, "reject_body", side_effect=contract_error),
            self.assertRaises(state.ApiError),
        ):
            handler._capture_body("team-status")

        handler._captured_json_raw = b'{"a":1}'
        handler._captured_json_body = {"a": 1}
        self.assertEqual(handler._read_team_body({"a"}), {"a": 1})
        with self.assertRaises(state.ApiError):
            handler._read_team_body({"b"})

    def test_http_verbs_dispatch_and_developers_boundary_short_circuit(self) -> None:
        handler = _handler()
        handler._dispatch = mock.Mock()
        handler.do_GET()
        handler.do_POST()
        handler.do_PUT()
        handler.do_DELETE()
        self.assertEqual([call.args[0] for call in handler._dispatch.call_args_list], ["GET", "POST", "PUT", "DELETE"])

        handler = _handler()
        with (
            mock.patch.object(server.developers_http, "is_path", return_value=True),
            mock.patch.object(server.developers_http, "dispatch") as dispatch,
        ):
            handler._dispatch("GET")
        dispatch.assert_called_once()

    def test_params_and_query_validation_reject_noncanonical_or_unbounded_values(self) -> None:
        handler = _handler()
        with (
            mock.patch.object(server.validate, "validate_team_id", return_value="canonical"),
            self.assertRaises(state.ApiError),
        ):
            handler._validated_params(_route("status", {"team_id": "other"}))
        with self.assertRaises(state.ApiError):
            handler._validated_params(_route("oauth", {"challenge_id": "bad"}))
        with self.assertRaises(state.ApiError):
            handler._validated_params(_route("file", {"file_id": "bad"}))
        with mock.patch.object(server.validate, "validate_team_id", return_value=TEAM_ID):
            params = handler._validated_params(
                _route("status", {"team_id": TEAM_ID, "assistant_id": "assistant", "integration_id": "integration"})
            )
        self.assertEqual(params["assistant_id"], "assistant")

        with self.assertRaises(state.ApiError):
            handler._validated_query("status", {"x": "1"})
        self.assertEqual(handler._validated_query("status", {}), {})
        with self.assertRaises(state.ApiError):
            handler._validated_query("team-logs", {"x": "1"})
        for lines in ("0", "1001", "１", "a"):
            with self.subTest(lines=lines), self.assertRaises(state.ApiError):
                handler._validated_query("team-logs", {"lines": lines})
        self.assertEqual(handler._validated_query("team-logs", {"lines": "10"}), {"lines": "10"})

    def test_owner_target_and_account_session_validate_closed_evidence(self) -> None:
        handler = _handler()
        self.assertIsNone(handler._owner_target("status"))
        handler._read_body = mock.Mock(return_value={"extra": True})
        with self.assertRaises(state.ApiError):
            handler._owner_target("team-create")
        for owner in (1, "bad"):
            handler._read_body = mock.Mock(return_value={"owner_account_id": owner})
            with self.assertRaises(state.ApiError):
                handler._owner_target("team-create")
        handler._read_body = mock.Mock(return_value={"owner_account_id": ACCOUNT_ID})
        self.assertEqual(handler._owner_target("team-create"), ACCOUNT_ID)

        handler.headers.get_all.return_value = []
        with self.assertRaises(state.ApiError):
            handler._account_session()
        handler.headers.get_all.return_value = ["invalid"]
        with (
            mock.patch.object(
                server.account_authority, "session_token", side_effect=server.account_authority.AuthorityDeniedError()
            ),
            self.assertRaises(state.ApiError),
        ):
            handler._account_session()
        handler.headers.get_all.return_value = ["session"]
        with mock.patch.object(server.account_authority, "session_token", return_value="session"):
            self.assertEqual(handler._account_session(), "session")

    def test_human_authority_audits_denied_unavailable_and_accepted_evidence(self) -> None:
        handler = _handler()
        handler._owner_target = mock.Mock(return_value=ACCOUNT_ID)
        request = SimpleNamespace(
            method="POST",
            route=_route("team-create"),
            params={},
            query={},
            body={"kind": "json"},
            assurance={"kind": "password"},
            assurance_handle="handle",
        )
        for error, status in (
            (server.account_authority.AuthorityDeniedError(), HTTPStatus.FORBIDDEN),
            (server.account_authority.AuthorityUnavailableError(), HTTPStatus.SERVICE_UNAVAILABLE),
        ):
            with (
                mock.patch.object(server.account_authority, "binding_digest", return_value="digest"),
                mock.patch.object(server.account_authority, "evaluate", side_effect=error),
                mock.patch.object(server.audit, "log", return_value="trace"),
                self.assertRaises(state.ApiError) as caught,
            ):
                handler._human_authority("session", request)
            self.assertEqual(caught.exception.status, status)

        evaluation = SimpleNamespace(
            account_id=ACCOUNT_ID,
            supervisor=False,
            binding_digest="digest",
            principal=("account", ACCOUNT_ID),
        )
        with (
            mock.patch.object(server.account_authority, "binding_digest", return_value="digest"),
            mock.patch.object(server.account_authority, "evaluate", return_value=evaluation),
            mock.patch.object(server.audit, "log", return_value="trace"),
        ):
            self.assertIs(handler._human_authority("session", request), evaluation)
        self.assertEqual(handler._audit_account_id, ACCOUNT_ID)

    def test_publication_dependencies_failure_emission_and_audit_principal_shapes(self) -> None:
        handler = _handler()
        with mock.patch.object(state, "_developers_client", None), self.assertRaises(state.ApiError):
            handler._publication_dependencies()
        with (
            mock.patch.object(state, "_developers_client", "client"),
            mock.patch.object(state, "_artifact_trust", "trust"),
        ):
            self.assertEqual(handler._publication_dependencies(), ("client", "trust"))

        handler._audit_security = mock.Mock()
        handler._send_json = mock.Mock()
        failure = SimpleNamespace(result="denied", audit_reason="reason", status=403, public_message="no")
        handler._emit_failure("GET", failure)
        handler._send_json.assert_called_once_with(403, {"error": "no"})

        handler = _handler()
        with mock.patch.object(server.audit, "log", return_value="trace") as audit:
            self.assertEqual(handler._audit_security("get", "target", result="ok"), "trace")
            handler._audit_machine_principal = "admin"
            handler._audit_security("get", "target", result="ok")
            del handler._audit_machine_principal
            handler._audit_account_id = ACCOUNT_ID
            handler._audit_supervisor = False
            handler._audit_credential_state = "credential_present"
            handler._audit_security("get", "target", result="ok")
        self.assertEqual(audit.call_count, 3)

    def test_route_dispatches_global_preauthorized_and_owner_bound_operations(self) -> None:
        handler = _handler()
        evaluation = SimpleNamespace(
            principal=("account", ACCOUNT_ID),
            owner_account_id=ACCOUNT_ID,
            account_id=ACCOUNT_ID,
            assurance=None,
        )
        global_handler = mock.Mock()
        with mock.patch.dict(server._GLOBAL_ROUTES, {"global": global_handler}):
            handler._route(_route("global"), {}, {}, evaluation)
        global_handler.assert_called_once()

        preauthorized = mock.Mock()
        with mock.patch.dict(server._PREAUTHORIZED_ROUTES, {"pre": preauthorized}):
            handler._route(_route("pre"), {"team_id": TEAM_ID}, {}, evaluation)
        preauthorized.assert_called_once()

        lease = SimpleNamespace(owner=ACCOUNT_ID)
        authorized = mock.Mock()
        with (
            mock.patch.object(resources, "_authorize", return_value=lease),
            mock.patch.dict(server._AUTHORIZED_ROUTES, {"status": authorized}),
        ):
            handler._route(_route("status"), {"team_id": TEAM_ID}, {}, evaluation)
        authorized.assert_called_once()

        owner_mismatch = SimpleNamespace(**(evaluation.__dict__ | {"account_id": "b" * 32}))
        with mock.patch.object(resources, "_authorize", return_value=lease), self.assertRaises(state.ApiError):
            handler._route(_route("chat-human-submit"), {"team_id": TEAM_ID}, {}, owner_mismatch)


if __name__ == "__main__":
    unittest.main()
