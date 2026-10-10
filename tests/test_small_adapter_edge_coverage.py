"""Complete failure coverage for small Team process, token, and HTTP adapters."""

import io
import json
import os
import re
import stat
import tempfile
import types
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock

from core.http import stdlib
from core.http import strict as strict_http
from local import token as local_token
from local.http import dispatch as local_dispatch


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


class TokenAndProcessCoverageTests(unittest.TestCase):
    def test_a_token_from_a_previous_start_is_refused_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tokens" / "token"
            group = types.SimpleNamespace(gr_gid=os.getgid())
            with mock.patch.object(local_token.grp, "getgrnam", return_value=group):
                earlier = local_token.issue_token(path)
                current = local_token.issue_token(path)
            self.assertNotEqual(earlier, current)
            self.assertEqual(path.read_text(encoding="ascii"), current)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o440)
            self.assertEqual([entry.name for entry in path.parent.iterdir()], ["token"])
            for token, admitted in ((earlier, False), (current, True)):
                headers = Message()
                headers["Authorization"] = f"Bearer {token}"
                self.assertIs(strict_http.bearer_matches(headers, current), admitted)

    def test_local_token_metadata_and_content_failures_are_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("a" * 64, encoding="ascii")
            path.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "unsafe metadata"):
                local_token._read_checked(path, os.getgid())
            path.write_text("z" * 64, encoding="ascii")
            path.chmod(0o440)
            with self.assertRaisesRegex(RuntimeError, "token is invalid"):
                local_token._read_checked(path, os.getgid())
            group = types.SimpleNamespace(gr_gid=os.getgid())
            with (
                mock.patch.object(local_token.grp, "getgrnam", return_value=group),
                mock.patch.object(local_token, "_read_checked", return_value="b" * 64),
                self.assertRaisesRegex(RuntimeError, "changed while it was issued"),
            ):
                local_token.issue_token(Path(directory) / "issued" / "token")

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
                local_token.issue_token(new_path)
            self.assertFalse(any(new_path.parent.iterdir()))


if __name__ == "__main__":
    unittest.main()
