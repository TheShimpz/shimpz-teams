"""Security contracts for the Team Browser's CONNECT boundary (ADR-0117).

The neutral CONNECT transport this profile runs is the umbrella `.egress/` (ADR-0104), proven once by
`.egress/tests`; these tests prove what this profile owns: its admission, its audit record, and its envelope.
"""

import contextlib
import importlib.util
import json
import runpy
import socket
import ssl
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[2] / ".egress"))


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = _module("audit", ROOT / "audit.py")
with mock.patch.dict("sys.modules", {"audit": audit}):
    app = _module("browser_egress_app", ROOT / "app.py")
healthcheck = _module("browser_egress_healthcheck", ROOT / "healthcheck.py")
connect = app.connect
BROWSER = "10.231.0.10"


def _client_hello(server_name: str) -> bytes:
    """The first flight a real Python TLS client sends for `server_name`."""
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = ssl.create_default_context().wrap_bio(incoming, outgoing, server_hostname=server_name)
    with contextlib.suppress(ssl.SSLWantReadError):
        client.do_handshake()
    return outgoing.read()


def _request(target: str) -> bytes:
    """The CONNECT Chromium sends through its proxy, headers included."""
    return (
        f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nProxy-Connection: keep-alive\r\n"
        "User-Agent: Mozilla/5.0 (X11; Linux x86_64)\r\n\r\n"
    ).encode()


class BrowserEgressTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.audit_path = Path(directory.name, "audit.jsonl")
        patcher = mock.patch.object(audit, "AUDIT", audit.AuditWriter(self.audit_path, "browser-egress"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _records(self) -> list[dict]:
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]

    def _handler(self):
        handler = object.__new__(app.Handler)
        handler.client_address = (BROWSER, 40000)
        return handler

    def test_envelope_is_pinned(self) -> None:
        self.assertEqual(app.LISTEN_PORT, 3128)
        self.assertEqual(
            (app.Handler.connect_timeout, app.Handler.idle_timeout, app.Handler.request_limit), (15, 120, 4096)
        )
        self.assertEqual(
            (app.Server.max_concurrency, app.Server.max_source_concurrency, app.Server.request_queue_size),
            (40, 40, 40),
        )

    def test_admission_takes_any_public_hostname_on_443_in_canonical_form(self) -> None:
        handler = self._handler()
        for target, host in (
            ("example.com:443", "example.com"),
            ("WWW.Gov.BR:443", "www.gov.br"),
            ("pt.wikipedia.org:443", "pt.wikipedia.org"),
            ("xn--bcher-kva.example:443", "xn--bcher-kva.example"),
        ):
            with self.subTest(target=target):
                self.assertEqual(handler.admit(_request(target)), connect.Target(host, 443, {"client": BROWSER}))

    def test_admission_refuses_ports_literals_malformed_names_and_other_methods_without_naming_them(self) -> None:
        handler = self._handler()
        destination = connect.Decision("denied", 403, "destination-rejected", audit.REJECTED, {"client": BROWSER})
        for target in (
            "example.com:80",
            "example.com:8443",
            "93.184.216.34:443",
            "0x5db8d822:443",
            "0x8.0x8.0x8.0x8:443",
            "010.010.010.0x8:443",
            "0x7f.1:443",
            "1.2.3:443",
            "[2606:2800:220:1:248:1893:25c8:1946]:443",
            "localhost:443",
            "example.com.:443",
            "-bad.example:443",
            "under_score.example:443",
            f"{'a' * 64}.example:443",
            f"{'a.' * 127}com:443",
        ):
            with self.subTest(target=target):
                self.assertEqual(handler.admit(_request(target)), destination)
        for payload in (
            b"GET https://example.com/ HTTP/1.1\r\n\r\n",
            b"CONNECT example.com HTTP/1.1\r\n\r\n",
            _request("example.com:443") + b"\x16\x03\x01",
        ):
            with self.subTest(payload=payload):
                decision = handler.admit(payload)
                self.assertIsInstance(decision, connect.Decision)
                self.assertEqual((decision.reason, decision.subject), ("request-rejected", audit.REJECTED))

    def _tunnel(self, request: bytes, flight: bytes) -> tuple[bytes, bytes]:
        """Serve one real connection to a local stand-in for a site; return what the client and upstream saw."""
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        listener.settimeout(5)
        handler_socket, client_socket = socket.socketpair()
        self.addCleanup(client_socket.close)
        self.addCleanup(handler_socket.close)
        with mock.patch.object(connect, "resolve_public", return_value=((socket.AF_INET, listener.getsockname()),)):
            worker = threading.Thread(target=app.Handler, args=(handler_socket, (BROWSER, 40000), mock.Mock()))
            worker.start()
            client_socket.settimeout(5)
            client_socket.sendall(request)
            response = client_socket.recv(512)
            received = b""
            if response.startswith(b"HTTP/1.1 200"):
                upstream, _peer = listener.accept()
                self.addCleanup(upstream.close)
                upstream.settimeout(5)
                client_socket.sendall(flight)
                while len(received) < len(flight) and (chunk := upstream.recv(65536)):
                    received += chunk
            client_socket.shutdown(socket.SHUT_WR)
            worker.join(10)
        self.assertFalse(worker.is_alive())
        return response, received

    def test_an_admitted_tunnel_is_recorded_with_the_browser_address_and_the_process_generation(self) -> None:
        flight = _client_hello("example.com")
        response, received = self._tunnel(_request("example.com:443"), flight)
        self.assertTrue(response.startswith(b"HTTP/1.1 200"))
        self.assertEqual(received, flight)
        [record] = self._records()
        self.assertEqual(
            (record["result"], record["code"], record["reason"], record["subject"]),
            ("ok", 200, "allowed", "example.com:443"),
        )
        self.assertEqual((record["client"], record["generation"]), (BROWSER, audit.GENERATION))
        self.assertRegex(audit.GENERATION, r"\A[0-9a-f]{32}\Z")
        self.assertEqual(
            (record["principal_class"], record["principal_id"], record["probe"]), ("machine", "browser", False)
        )
        self.assertEqual(stat.S_IMODE(self.audit_path.stat().st_mode), 0o600)

    def test_a_fronted_server_name_is_refused_and_no_request_material_is_recorded(self) -> None:
        response, received = self._tunnel(_request("example.com:443"), _client_hello("attacker.example"))
        self.assertTrue(response.startswith(b"HTTP/1.1 200"))
        self.assertEqual(received, b"")
        self.assertEqual(
            (self._records()[-1]["reason"], self._records()[-1]["subject"]), ("sni-mismatch", "example.com:443")
        )
        response, _received = self._tunnel(_request("10.0.0.1:443"), b"")
        self.assertTrue(response.startswith(b"HTTP/1.1 403"))
        self.assertEqual(self._records()[-1]["subject"], audit.REJECTED)
        text = self.audit_path.read_text(encoding="utf-8")
        for material in ("Mozilla", "attacker.example", "10.0.0.1"):
            self.assertNotIn(material, text)

    def test_a_private_answer_is_refused_before_any_upstream_connection(self) -> None:
        handler_socket, client_socket = socket.socketpair()
        self.addCleanup(client_socket.close)
        self.addCleanup(handler_socket.close)
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.201.0.1", 443))]
        with (
            mock.patch.object(connect, "_resolve", return_value=answer),
            mock.patch.object(connect, "connect_first") as upstream,
        ):
            client_socket.sendall(_request("internal.example:443"))
            app.Handler(handler_socket, (BROWSER, 40000), mock.Mock())
        upstream.assert_not_called()
        self.assertTrue(client_socket.recv(512).startswith(b"HTTP/1.1 403"))
        self.assertEqual(
            (self._records()[-1]["reason"], self._records()[-1]["subject"]),
            ("destination-rejected", "internal.example:443"),
        )

    def test_audit_admits_only_its_bounded_schema(self) -> None:
        self.assertEqual(audit.AUDIT.service, "browser-egress")
        for decision in (
            connect.Decision("ok", 200, "allowed", "example.com:80", {"client": BROWSER}),
            connect.Decision("ok", 200, "allowed", "not-evaluated", {"client": BROWSER}),
            connect.Decision("other", 200, "allowed", "example.com:443", {"client": BROWSER}),
            connect.Decision("ok", 200, "allowed", "example.com:443", {}),
            connect.Decision("ok", 200, "allowed", "example.com:443", {"client": "browser"}),
            connect.Decision("ok", 200, "allowed", "example.com:443", {"client": BROWSER, "path": "/x"}),
        ):
            with self.subTest(decision=decision), self.assertRaises(audit.AuditError):
                audit.record(decision)
        self.assertEqual(self._records(), [])

    def test_main_fails_before_bind_without_audit_custody_and_closes_the_server(self) -> None:
        with (
            mock.patch.object(audit.AUDIT, "ensure_custody", side_effect=audit.AuditError("unsafe")),
            mock.patch.object(app, "Server") as server,
        ):
            self.assertEqual(app.main(), 1)
        server.assert_not_called()

        for side_effect in (None, KeyboardInterrupt):
            server = mock.Mock()
            server.serve_forever.side_effect = side_effect
            with (
                self.subTest(side_effect=side_effect),
                mock.patch.object(audit.AUDIT, "ensure_custody"),
                mock.patch.object(app, "Server", return_value=server),
            ):
                self.assertEqual(app.main(), 0)
            server.server_close.assert_called_once_with()

        with (
            mock.patch.object(audit.AUDIT, "ensure_custody"),
            mock.patch.object(app, "Server", side_effect=OSError("bind")),
        ):
            self.assertEqual(app.main(), 1)

        with (
            mock.patch.dict(sys.modules, {"audit": audit}),
            mock.patch.object(audit.AUDIT, "ensure_custody", side_effect=audit.AuditError("closed")),
            self.assertRaises(SystemExit) as raised,
        ):
            runpy.run_path(str(ROOT / "app.py"), run_name="__main__")
        self.assertEqual(raised.exception.code, 1)

    def test_healthcheck_reports_listener_state(self) -> None:
        connection = mock.MagicMock()
        with mock.patch.object(healthcheck.socket, "create_connection", return_value=connection) as create:
            self.assertEqual(healthcheck.main(), 0)
        create.assert_called_once_with(("127.0.0.1", 3128), timeout=2)
        with mock.patch.object(healthcheck.socket, "create_connection", side_effect=OSError("closed")):
            self.assertEqual(healthcheck.main(), 1)
        with (
            mock.patch.object(socket, "create_connection", side_effect=OSError("closed")),
            self.assertRaises(SystemExit) as raised,
        ):
            runpy.run_path(str(ROOT / "healthcheck.py"), run_name="__main__")
        self.assertEqual(raised.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
