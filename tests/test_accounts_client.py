"""Fail-closed transport contracts for request-bound Account authority."""

from __future__ import annotations

import hashlib
import json
import socket
import socketserver
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))

from hosted import authority as accounts_client

ACCOUNT_ID = "a" * 32
OWNER_ID = "b" * 32
CAPABILITY = "c" * 64
NONE_BODY = {
    "kind": "none",
    "length": 0,
    "sha256": hashlib.sha256(b"").hexdigest(),
}


def _binding(operation: str = "team-list", *, owner: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "method": "POST" if operation == "team-create" else "GET",
        "operation": operation,
        "params": {"team_id": "team_1"} if operation == "team-create" else {},
        "query": {},
        "body": NONE_BODY,
    }
    if owner is not None:
        value["owner_account_id"] = owner
    return value


class _AccountsHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, object]]] = []

    def log_message(self, *_args) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        self.__class__.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "request": request,
            }
        )
        session = request["session_token"]
        digest = accounts_client.binding_digest(request["binding"])
        response: dict[str, object] = {
            "version": 1,
            "account_id": ACCOUNT_ID,
            "supervisor": session == "supervisor",
            "binding_digest": digest,
        }
        if request["binding"]["operation"] == "team-create":
            response["owner_account_id"] = request["binding"].get("owner_account_id", ACCOUNT_ID)
        status = HTTPStatus.OK
        if session == "denied":
            status = HTTPStatus.FORBIDDEN
            response = {"error": "denied"}
        elif session == "bad-request":
            status = HTTPStatus.BAD_REQUEST
            response = {"error": "invalid binding"}
        elif session == "missing-owner":
            status = HTTPStatus.NOT_FOUND
            response = {"error": "Owner Account is unavailable"}
        elif session == "rate-limited":
            status = HTTPStatus.TOO_MANY_REQUESTS
            response = {"error": "authority is busy"}
        elif session == "mismatch":
            response["binding_digest"] = "d" * 64
        elif session == "extra":
            response["token"] = "protected"
        body = json.dumps(response, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _GarbageStatusHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.recv(4096)
        self.request.sendall(b"this is not HTTP\r\n")


class _TimeoutHandler(socketserver.BaseRequestHandler):
    release = threading.Event()

    def handle(self) -> None:
        self.request.recv(4096)
        self.release.wait(timeout=1)


@contextmanager
def _server(server_type, handler_type):
    server = server_type(("127.0.0.1", 0), handler_type)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        if handler_type is _TimeoutHandler:
            handler_type.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if handler_type is _TimeoutHandler:
            handler_type.release.clear()


class AccountsClientTests(unittest.TestCase):
    def setUp(self) -> None:
        _AccountsHandler.requests = []
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.capability = Path(self.temporary.name) / "authority.token"
        self.capability.write_text(CAPABILITY, encoding="ascii")
        self.capability.chmod(0o440)
        self.capability_patch = mock.patch.object(accounts_client, "CAPABILITY_FILE", self.capability)
        self.capability_patch.start()
        self.addCleanup(self.capability_patch.stop)

    def test_exact_evidence_is_uncached_and_bound_to_the_request(self) -> None:
        binding = _binding()
        with (
            _server(ThreadingHTTPServer, _AccountsHandler) as port,
            mock.patch.object(accounts_client, "ACCOUNT_URL", f"http://127.0.0.1:{port}"),
        ):
            first = accounts_client.evaluate("ordinary", binding)
            second = accounts_client.evaluate("ordinary", binding)

        self.assertEqual(first, accounts_client.Evaluation(ACCOUNT_ID, False, first.binding_digest, None))
        self.assertEqual(second, first)
        self.assertEqual(len(_AccountsHandler.requests), 2)
        request = _AccountsHandler.requests[0]
        self.assertEqual(request["path"], accounts_client.EVALUATE_PATH)
        self.assertEqual(request["authorization"], f"Bearer {CAPABILITY}")
        self.assertEqual(
            request["request"],
            {"version": 1, "session_token": "ordinary", "binding": binding},
        )

    def test_supervisor_owner_assignment_must_match_exact_account_evidence(self) -> None:
        binding = _binding("team-create", owner=OWNER_ID)
        with (
            _server(ThreadingHTTPServer, _AccountsHandler) as port,
            mock.patch.object(accounts_client, "ACCOUNT_URL", f"http://127.0.0.1:{port}"),
        ):
            evaluation = accounts_client.evaluate("supervisor", binding)

        self.assertEqual(evaluation.principal, ("supervisor", ACCOUNT_ID))
        self.assertEqual(evaluation.owner_account_id, OWNER_ID)

        with (
            _server(ThreadingHTTPServer, _AccountsHandler) as port,
            mock.patch.object(accounts_client, "ACCOUNT_URL", f"http://127.0.0.1:{port}"),
            self.assertRaises(accounts_client.AuthorityUnavailableError),
        ):
            accounts_client.evaluate("ordinary", binding)

        self_owned = _binding("team-create", owner=ACCOUNT_ID)
        with (
            _server(ThreadingHTTPServer, _AccountsHandler) as port,
            mock.patch.object(accounts_client, "ACCOUNT_URL", f"http://127.0.0.1:{port}"),
        ):
            evaluation = accounts_client.evaluate("ordinary", self_owned)
        self.assertEqual(evaluation.owner_account_id, ACCOUNT_ID)

    def test_denial_mismatch_extra_fields_and_bad_session_fail_closed(self) -> None:
        with (
            _server(ThreadingHTTPServer, _AccountsHandler) as port,
            mock.patch.object(accounts_client, "ACCOUNT_URL", f"http://127.0.0.1:{port}"),
        ):
            with self.assertRaises(accounts_client.AuthorityDeniedError):
                accounts_client.evaluate("denied", _binding())
            for session in ("mismatch", "extra"):
                with self.subTest(session=session), self.assertRaises(accounts_client.AuthorityUnavailableError):
                    accounts_client.evaluate(session, _binding())
            before = len(_AccountsHandler.requests)
            for session in ("", "line\ninjection", None, "x" * 2049):
                with self.subTest(session=session), self.assertRaises(accounts_client.AuthorityDeniedError):
                    accounts_client.evaluate(session, _binding())
            self.assertEqual(len(_AccountsHandler.requests), before)

    def test_bad_endpoint_capability_and_transport_fail_closed(self) -> None:
        with (
            mock.patch.object(accounts_client, "ACCOUNT_URL", "http://accounts:not-a-port"),
            self.assertRaises(accounts_client.AuthorityUnavailableError),
        ):
            accounts_client.evaluate("ordinary", _binding())

        self.capability.chmod(0o640)
        with (
            mock.patch.object(accounts_client.http.client, "HTTPConnection") as connection,
            self.assertRaises(accounts_client.AuthorityUnavailableError),
        ):
            accounts_client.evaluate("ordinary", _binding())
        connection.return_value.request.assert_not_called()

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.capability.chmod(0o440)
        with (
            mock.patch.object(accounts_client, "ACCOUNT_URL", f"http://127.0.0.1:{port}"),
            self.assertRaises(accounts_client.AuthorityUnavailableError),
        ):
            accounts_client.evaluate("ordinary", _binding())

    def test_bad_status_line_and_timeout_fail_closed(self) -> None:
        for server_type, handler_type in (
            (socketserver.TCPServer, _GarbageStatusHandler),
            (socketserver.TCPServer, _TimeoutHandler),
        ):
            with (
                self.subTest(handler=handler_type.__name__),
                _server(server_type, handler_type) as port,
                mock.patch.object(accounts_client, "ACCOUNT_URL", f"http://127.0.0.1:{port}"),
                mock.patch.object(accounts_client, "EVALUATION_TIMEOUT_SECONDS", 0.05),
                self.assertRaises(accounts_client.AuthorityUnavailableError),
            ):
                accounts_client.evaluate("ordinary", _binding())

    def test_only_semantic_account_denials_project_as_denied_authority(self) -> None:
        with (
            _server(ThreadingHTTPServer, _AccountsHandler) as port,
            mock.patch.object(accounts_client, "ACCOUNT_URL", f"http://127.0.0.1:{port}"),
        ):
            with self.assertRaises(accounts_client.AuthorityDeniedError):
                accounts_client.evaluate("missing-owner", _binding())
            for session in ("bad-request", "rate-limited"):
                with self.subTest(session=session), self.assertRaises(accounts_client.AuthorityUnavailableError):
                    accounts_client.evaluate(session, _binding())

    def test_consumer_digest_matches_every_producer_vector(self) -> None:
        vectors = json.loads((accounts_client._PROTOCOL / "vectors.json").read_bytes())
        for vector in vectors["vectors"]:
            with self.subTest(vector=vector["name"]):
                self.assertEqual(accounts_client.binding_digest(vector["binding"]), vector["binding_digest"])


if __name__ == "__main__":
    unittest.main()
