"""Local HTTP enforcement of request-bound Supervisor authority."""

from __future__ import annotations

import hashlib
import unittest
from email.message import Message
from http import HTTPStatus
from io import BytesIO
from types import SimpleNamespace
from unittest import mock

from local import authority
from local.errors import ApiProblemError
from local.http import server
from protocol.http.v1 import supervisor as contract


class LocalSupervisorHttpTests(unittest.TestCase):
    @staticmethod
    def _handler(
        method: str,
        path: str,
        controller: object,
        *,
        body: bytes = b"",
        headers: tuple[tuple[str, str], ...] = (),
    ) -> server.Handler:
        handler = object.__new__(server.Handler)
        handler.command = method
        handler.path = path
        handler.server = SimpleNamespace(controller=controller)
        handler.headers = Message()
        for name, value in headers:
            handler.headers.add_header(name, value)
        handler.rfile = BytesIO(body)
        return handler

    @staticmethod
    def _evidence() -> authority.Evidence:
        return authority.Evidence(
            supervisor_id="a" * 32,
            session_digest="b" * 64,
            assertion_id="c" * 32,
            expires_at=2_200_000_015,
        )

    def test_health_is_the_only_bearer_only_fixed_read(self) -> None:
        controller = SimpleNamespace(health=lambda: {"status": "ok"})
        handler = self._handler("GET", "/healthz", controller)
        request_audit = server._RequestAudit()

        with (
            mock.patch.object(authority, "verify") as verify,
            mock.patch.object(server.local_audit, "record", return_value="d" * 32) as record,
        ):
            result = handler._authorized_route(request_audit)

        self.assertEqual(result[:3], (HTTPStatus.OK, {"status": "ok"}, "health"))
        self.assertEqual(request_audit.principal_class, "machine")
        self.assertEqual(request_audit.principal_id, "admin")
        self.assertEqual(record.call_args.kwargs["principal"].principal_class, "machine")
        verify.assert_not_called()

    def test_human_route_denies_missing_assertion_before_execution(self) -> None:
        list_teams = mock.Mock(return_value={"teams": []})
        handler = self._handler("GET", "/v1/teams", SimpleNamespace(list_teams=list_teams))
        request_audit = server._RequestAudit()

        with (
            mock.patch.object(server.local_audit, "record", return_value="d" * 32) as record,
            self.assertRaises(ApiProblemError) as caught,
        ):
            handler._authorized_route(request_audit)

        self.assertEqual(caught.exception.status, HTTPStatus.FORBIDDEN)
        self.assertEqual(caught.exception.code, "invalid-supervisor")
        list_teams.assert_not_called()
        self.assertEqual(request_audit.principal_class, "absent")
        self.assertEqual(request_audit.credential_state, "assertion_absent_or_malformed")
        self.assertEqual(record.call_args.kwargs["principal"].principal_class, "absent")

    def test_exact_evidence_attributes_the_operation_without_persisting_its_nonce(self) -> None:
        list_teams = mock.Mock(return_value={"teams": []})
        handler = self._handler(
            "GET",
            "/v1/teams",
            SimpleNamespace(list_teams=list_teams),
            headers=((contract.ASSERTION_HEADER, "Bearer assertion"),),
        )
        request_audit = server._RequestAudit()

        with (
            mock.patch.object(authority, "verify", return_value=self._evidence()) as verify,
            mock.patch.object(server.local_audit, "record", return_value="d" * 32) as record,
        ):
            result = handler._authorized_route(request_audit)
            request_audit.record(result[2], result="ok")

        self.assertEqual(result[:3], (HTTPStatus.OK, {"teams": []}, "team-list"))
        verify.assert_called_once_with(
            handler.headers,
            method="GET",
            path="/v1/teams",
            body={"kind": "none", "length": 0, "sha256": contract.EMPTY_SHA256},
            model=None,
        )
        self.assertEqual(request_audit.principal_id, "a" * 32)
        self.assertEqual(request_audit.principal_class, "human")
        self.assertEqual(record.call_count, 2)
        self.assertEqual(
            {call.kwargs["principal"].trace_id for call in record.call_args_list},
            {None, "d" * 32},
        )
        self.assertNotIn("c" * 32, {call.kwargs["principal"].trace_id for call in record.call_args_list})
        list_teams.assert_called_once_with()

    def test_machine_route_binds_its_attributable_audit_principal(self) -> None:
        handler = self._handler(
            "POST",
            "/v1/oauth/cloudflare/callback",
            SimpleNamespace(),
        )
        handler._capture_body = mock.Mock()

        def route(_parts, resolved):
            server.local_audit.record_request(
                resolved.operation,
                result="ok",
            )
            return HTTPStatus.OK, {"connected": True}, resolved.operation, None, None

        handler._route = route
        request_audit = server._RequestAudit()

        with mock.patch.object(
            server.local_audit,
            "record",
            return_value="e" * 32,
        ) as record:
            result = handler._authorized_route(request_audit)

        self.assertEqual(result[2], "assistant-integration-complete")
        self.assertEqual(record.call_count, 2)
        principals = [call.kwargs["principal"] for call in record.call_args_list]
        self.assertEqual({principal.principal_class for principal in principals}, {"machine"})
        self.assertEqual({principal.principal_id for principal in principals}, {"admin"})
        self.assertEqual([principal.trace_id for principal in principals], [None, "e" * 32])

    def test_chat_assertion_binds_raw_json_and_model_credential_digest(self) -> None:
        raw = b'{"message":"hello","files":[],"assistant_ids":[]}'
        chat = mock.Mock(return_value={"team_id": "team_1", "reply": "done"})
        controller = SimpleNamespace(chat_turn_service=SimpleNamespace(chat=chat))
        api_key = "sk-test-0123456789"
        handler = self._handler(
            "POST",
            "/v1/teams/team_1/chat",
            controller,
            body=raw,
            headers=(
                (contract.ASSERTION_HEADER, "Bearer assertion"),
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(raw))),
                ("X-Shimpz-Model-Provider", "openai"),
                ("X-Shimpz-Model-Api-Key", api_key),
            ),
        )

        with (
            mock.patch.object(authority, "verify", return_value=self._evidence()) as verify,
            mock.patch.object(server.local_audit, "record", return_value="c" * 32),
        ):
            handler._authorized_route(server._RequestAudit())

        self.assertEqual(
            verify.call_args.kwargs["body"],
            {
                "kind": "json",
                "length": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
        )
        self.assertEqual(
            verify.call_args.kwargs["model"],
            {
                "provider": "openai",
                "key_sha256": hashlib.sha256(api_key.encode("ascii")).hexdigest(),
            },
        )
        chat.assert_called_once()

    def test_file_content_is_not_read_before_supervisor_verification(self) -> None:
        body = b"protected file"
        put_file = mock.Mock()
        handler = self._handler(
            "POST",
            "/v1/teams/team_1/files",
            SimpleNamespace(put_file=put_file),
            body=body,
            headers=(
                (contract.ASSERTION_HEADER, "Bearer assertion"),
                ("Content-Type", "text/plain"),
                ("Content-Length", str(len(body))),
                ("X-Shimpz-Filename", "brief.txt"),
            ),
        )

        with (
            mock.patch.object(
                authority,
                "verify",
                side_effect=authority.SupervisorDeniedError("rejected"),
            ),
            mock.patch.object(server.local_audit, "record", return_value="d" * 32),
            self.assertRaises(ApiProblemError),
        ):
            handler._authorized_route(server._RequestAudit())

        self.assertEqual(handler.rfile.tell(), 0)
        put_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
