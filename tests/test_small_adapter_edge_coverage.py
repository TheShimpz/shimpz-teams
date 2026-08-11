"""Complete failure coverage for small Team process, token, and HTTP adapters."""

from __future__ import annotations

import importlib
import io
import json
import os
import re
import runpy
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from core.http import stdlib
from hosted import audit as hosted_audit
from hosted import token as hosted_token
from hosted.http import routes
from local import token as local_token
from local.http import dispatch as local_dispatch

with (
    mock.patch("docker.from_env", return_value=mock.Mock()),
    mock.patch.object(hosted_token, "ensure_token", return_value="test-token"),
):
    admission = importlib.import_module("hosted.http.admission")
    hosted_chat_lifecycle = importlib.import_module("hosted.chat.lifecycle")


class _Headers:
    def __init__(self, *, authorization: list[str] | None = None, length: str | None = None) -> None:
        self.authorization = list(authorization or [])
        self.length = length

    def get_all(self, _name: str, *, failobj):
        return self.authorization or failobj

    def get(self, _name: str, default=None):
        return self.length if self.length is not None else default


class _ProblemError(Exception):
    def __init__(self, status, message: str, code: str) -> None:
        self.status = status
        self.message = message
        self.code = code


class _ApiError(Exception):
    def __init__(self, status, message: str) -> None:
        self.status = status
        self.message = message


class SmallHttpAdapterCoverageTests(unittest.TestCase):
    def test_stdlib_bearer_json_and_route_contracts(self) -> None:
        self.assertEqual(stdlib.bearer_token(object()), "")
        self.assertEqual(stdlib.bearer_token(_Headers(authorization=["one", "two"])), "")
        self.assertEqual(stdlib.bearer_token(_Headers(authorization=["Basic token"])), "")
        self.assertEqual(stdlib.bearer_token(_Headers(authorization=["Bearer token"])), "token")
        self.assertTrue(stdlib.bearer_authorized(_Headers(authorization=["Bearer token"]), "token"))
        self.assertFalse(stdlib.bearer_authorized(_Headers(), "token"))

        handler = mock.Mock()
        handler.wfile = io.BytesIO()
        stdlib.send_json(handler, 200, {"ok": True})
        self.assertEqual(json.loads(handler.wfile.getvalue()), {"ok": True})

        self.assertEqual(stdlib.read_json_body(_Headers(), io.BytesIO(), max_bytes=10), {})
        self.assertEqual(
            stdlib.read_json_body(_Headers(length="2"), io.BytesIO(b"{}"), max_bytes=10),
            {},
        )
        for headers, stream, status in (
            (_Headers(length="bad"), io.BytesIO(), 400),
            (_Headers(length="11"), io.BytesIO(), 413),
            (_Headers(length="1"), io.BytesIO(b"{"), 400),
            (_Headers(length="2"), io.BytesIO(b"[]"), 400),
        ):
            with self.subTest(status=status), self.assertRaises(stdlib.HttpError) as raised:
                stdlib.read_json_body(headers, stream, max_bytes=10)
            self.assertEqual(raised.exception.status, status)

        route = stdlib.Route("GET", re.compile(r"/teams/(?P<team_id>[a-z0-9_]+)"), "team-get")
        matched = stdlib.resolve_route([route], "GET", "/teams/team_1?view=full")
        self.assertEqual(matched.params, {"team_id": "team_1"})
        self.assertEqual(matched.query, {"view": ["full"]})
        with self.assertRaises(stdlib.HttpError) as missing:
            stdlib.resolve_route([route], "POST", "/teams/team_1")
        self.assertEqual(missing.exception.status, 404)

    def test_stdlib_dispatch_redacts_unclassified_errors(self) -> None:
        emitted = mock.Mock()
        stdlib.dispatch(lambda: None, classify=mock.Mock(), emit=emitted, unexpected_message="internal")
        emitted.assert_not_called()

        expected = stdlib.HttpFailure(400, "bad", "bad", "denied")
        stdlib.dispatch(
            lambda: (_ for _ in ()).throw(ValueError("bad")),
            classify=lambda _exc: expected,
            emit=emitted,
            unexpected_message="internal",
        )
        emitted.assert_called_with(expected)

        emitted.reset_mock()
        stdlib.dispatch(
            lambda: (_ for _ in ()).throw(ValueError("secret")),
            classify=lambda _exc: None,
            emit=emitted,
            unexpected_message="internal",
        )
        failure = emitted.call_args.args[0]
        self.assertEqual(failure.public_message, "internal")
        self.assertEqual(failure.audit_reason, "ValueError")

    def test_local_dispatch_classifies_and_projects_each_result(self) -> None:
        problem = _ProblemError(409, "conflict", "conflict")
        projected = local_dispatch.classify_failure(problem, _ProblemError, OSError)
        self.assertEqual(projected.result, "denied")
        self.assertEqual(projected.public_code, "conflict")
        server_problem = _ProblemError(503, "offline", "offline")
        self.assertEqual(local_dispatch.classify_failure(server_problem, _ProblemError, OSError).result, "error")
        self.assertEqual(
            local_dispatch.classify_failure(OSError(), _ProblemError, OSError).audit_reason,
            "docker-error",
        )
        self.assertEqual(local_dispatch.classify_failure(ValueError(), _ProblemError, OSError).status, 500)

        record = mock.Mock(return_value="trace")
        send = mock.Mock()
        local_dispatch.dispatch_route(lambda: None, record, send, _ProblemError, OSError)
        send.assert_not_called()

        local_dispatch.dispatch_route(
            lambda: (200, {"ok": True}, "team-create", "team_1", None),
            record,
            send,
            _ProblemError,
            OSError,
        )
        self.assertEqual(send.call_args.args, (200, {"ok": True, "trace_id": "trace"}))

        send.reset_mock()
        local_dispatch.dispatch_route(
            lambda: (_ for _ in ()).throw(problem),
            record,
            send,
            _ProblemError,
            OSError,
        )
        self.assertEqual(send.call_args.args[1]["code"], "conflict")

        send.reset_mock()
        local_dispatch.dispatch_route(
            lambda: (_ for _ in ()).throw(ValueError("secret")),
            record,
            send,
            _ProblemError,
            OSError,
        )
        self.assertNotIn("code", send.call_args.args[1])

    def test_hosted_routes_translate_parsing_and_domain_failures(self) -> None:
        contract_error = routes.strict.HttpContractError(400, "invalid", code="invalid")
        with (
            mock.patch.object(routes.strict, "parse_routed_request", side_effect=contract_error),
            self.assertRaises(_ApiError) as raised,
        ):
            routes.route_target(object(), "/", "GET", _ApiError)
        self.assertEqual(raised.exception.status, 400)

        target = types.SimpleNamespace(parts=("teams",), path="/teams")
        with (
            mock.patch.object(routes.strict, "parse_routed_request", return_value=target),
            mock.patch.object(routes.strict, "resolve_controller_route", return_value=None),
            self.assertRaises(_ApiError) as missing,
        ):
            routes.route_target(object(), "/teams", "GET", _ApiError)
        self.assertEqual(missing.exception.status, 404)

        route = object()
        with (
            mock.patch.object(routes.strict, "parse_routed_request", return_value=target),
            mock.patch.object(routes.strict, "resolve_controller_route", return_value=route),
        ):
            self.assertEqual(routes.route_target(object(), "/teams", "GET", _ApiError), (target, route))

        class ValidationError(Exception):
            pass

        class SpecError(Exception):
            pass

        for error, status in (
            (_ApiError(409, "conflict"), 409),
            (ValidationError("invalid"), 400),
            (SpecError("missing"), 404),
        ):
            failure = routes.classify_failure(error, _ApiError, ValidationError, SpecError)
            self.assertEqual(failure.status, status)
        self.assertIsNone(routes.classify_failure(ValueError(), _ApiError, ValidationError, SpecError))


class TokenAndProcessCoverageTests(unittest.TestCase):
    def test_hosted_audit_writes_and_rotates_bounded_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            with (
                mock.patch.object(hosted_audit, "AUDIT_PATH", path),
                mock.patch.object(hosted_audit, "MAX_BYTES", 1),
                mock.patch.object(hosted_audit, "uuid") as uuid_module,
            ):
                uuid_module.uuid4.return_value.hex = "generated"
                self.assertEqual(hosted_audit.log("create", "team_1", result="ok"), "generated")
                path.with_name("audit.jsonl.1").write_text("previous", encoding="utf-8")
                hosted_audit.log(
                    "delete",
                    "team_1",
                    result="failed",
                    trace_id="provided",
                    level="error",
                    reason="test",
                )
            self.assertTrue(path.with_name("audit.jsonl.1").exists())
            self.assertTrue(path.with_name("audit.jsonl.2").exists())
            event = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual((event["trace_id"], event["level"]), ("provided", "error"))

    def test_hosted_token_creation_repair_and_empty_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            group = types.SimpleNamespace(gr_gid=os.getgid())
            with (
                mock.patch.object(hosted_token, "TOKEN_PATH", path),
                mock.patch.object(hosted_token.grp, "getgrnam", return_value=group),
                mock.patch.object(hosted_token.os, "chown") as chown,
            ):
                created = hosted_token.ensure_token()
                self.assertEqual(len(created), 64)
                self.assertEqual(hosted_token.ensure_token(), created)
                path.chmod(0o600)
                path.write_text("", encoding="utf-8")
                replacement = hosted_token.ensure_token()
            self.assertEqual(len(replacement), 64)
            self.assertGreaterEqual(chown.call_count, 3)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o440)

    def test_local_token_creation_and_metadata_failures_are_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tokens" / "token"
            group = types.SimpleNamespace(gr_gid=os.getgid())
            with mock.patch.object(local_token.grp, "getgrnam", return_value=group):
                token = local_token.ensure_token(path)
                self.assertEqual(local_token.ensure_token(path), token)
                path.chmod(0o600)
                with self.assertRaisesRegex(RuntimeError, "unsafe metadata"):
                    local_token.ensure_token(path)

            path.chmod(0o600)
            path.write_text("z" * 64, encoding="ascii")
            path.chmod(0o440)
            with (
                mock.patch.object(local_token.grp, "getgrnam", return_value=group),
                self.assertRaisesRegex(RuntimeError, "token is invalid"),
            ):
                local_token.ensure_token(path)

    def test_local_token_detects_changed_read_and_creation_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("a" * 64, encoding="ascii")
            path.chmod(0o440)
            with (
                mock.patch.object(Path, "read_text", return_value="short"),
                self.assertRaisesRegex(RuntimeError, "token is invalid"),
            ):
                local_token._read_checked(path, os.getgid())

            new_path = Path(directory) / "other" / "token"
            wrong_group = types.SimpleNamespace(gr_gid=os.getgid() + 1)
            with (
                mock.patch.object(local_token.grp, "getgrnam", return_value=wrong_group),
                self.assertRaisesRegex(RuntimeError, "unsafe ownership"),
            ):
                local_token.ensure_token(new_path)
            self.assertFalse(any(new_path.parent.iterdir()))

    def test_hosted_entrypoint_delegates_to_the_server_main(self) -> None:
        with mock.patch("hosted.http.server.main") as main:
            runpy.run_path(str(Path(__file__).resolve().parents[1] / "hosted" / "app.py"), run_name="__main__")
        main.assert_called_once_with()

    def test_hosted_chat_cleanup_maps_journal_failure(self) -> None:
        journal = mock.Mock()
        journal.purge_replayable.side_effect = hosted_chat_lifecycle.action_journal.ActionJournalError("offline")
        with (
            mock.patch.object(hosted_chat_lifecycle.runtime_state._human_challenges, "cancel_team", return_value=True),
            mock.patch.object(hosted_chat_lifecycle.runtime_state, "_action_execution_journal", return_value=journal),
            self.assertRaises(hosted_chat_lifecycle.runtime_state.ApiError) as raised,
        ):
            hosted_chat_lifecycle.cancel_replayable_human("team_1", "generation")
        self.assertEqual(raised.exception.status, 503)


class HostedAdmissionCoverageTests(unittest.TestCase):
    @staticmethod
    def _challenge(kind: str):
        request = types.SimpleNamespace(kind=kind)
        requirement = types.SimpleNamespace(request=request)
        return types.SimpleNamespace(id="a" * 32, requirement=requirement)

    def test_missing_non_auth_and_invalid_auth_challenges(self) -> None:
        with (
            mock.patch.object(admission.runtime_state._human_challenges, "get", side_effect=KeyError),
            mock.patch.object(admission.hosted_chat_human, "_expire_challenges") as expire,
        ):
            self.assertEqual(
                admission.action_assurance(
                    "chat-human-submit",
                    {"team_id": "team_1"},
                    {"decision": "submit", "challenge_id": "a" * 32},
                ),
                (None, None),
            )
        expire.assert_called_once_with()

        with mock.patch.object(
            admission.runtime_state._human_challenges,
            "get",
            return_value=self._challenge("approval"),
        ):
            self.assertEqual(
                admission.action_assurance(
                    "chat-human-submit",
                    {"team_id": "team_1"},
                    {"decision": "submit", "challenge_id": "a" * 32},
                ),
                (None, None),
            )

        with (
            mock.patch.object(
                admission.runtime_state._human_challenges,
                "get",
                return_value=self._challenge("auth:reauth"),
            ),
            self.assertRaises(admission.runtime_state.ApiError) as raised,
        ):
            admission.action_assurance(
                "chat-human-submit",
                {"team_id": "team_1"},
                {"decision": "submit", "challenge_id": "a" * 32, "value": "invalid"},
            )
        self.assertEqual(raised.exception.status, 422)


if __name__ == "__main__":
    unittest.main()
