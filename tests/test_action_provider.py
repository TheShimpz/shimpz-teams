"""Team-made provider calls: admission, credential placement, real TLS through a CONNECT proxy, and response checks.

The transport scenarios run the real broker against a loopback CONNECT proxy and a TLS origin whose certificate only
the test trusts, so every byte the provider sees is exactly what Team sent (ADR-0106).
"""

import base64
import contextlib
import datetime
import hashlib
import hmac
import http.server
import json
import socket
import socketserver
import ssl
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from action import human as action_human
from action import provider

PROTOCOL = Path(__file__).resolve().parents[1] / "protocol" / "assistant" / "v1"
HOST = "api.example.com"
TOKEN = "test-token"
SECRET = "test-secret"
PROXY_TOKEN = "f" * 32


def _spec(**placements: dict[str, str]) -> SimpleNamespace:
    stored = {
        stored_id: SimpleNamespace(
            **{"host": HOST, "header": None, "query": None, "scheme": None, "hmac": None, **placement}
        )
        for stored_id, placement in placements.items()
    }
    return SimpleNamespace(stored_inputs=stored, integrations={})


def _scope(credentials=(), *, missing=frozenset(), authorized=True, audit=None, stopped=lambda: False):
    return provider.CallScope(
        team_id="team_1",
        assistant_id="meta-ads",
        action_id="list-campaigns",
        operation_id="6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6",
        credentials=tuple(credentials),
        missing=missing,
        authorized=authorized,
        route=lambda: (PROXY_TOKEN, frozenset({HOST})),
        audit=audit if audit is not None else (lambda _fields: None),
        stopped=stopped,
    )


def _frame(**fields: object) -> dict[str, object]:
    return {"type": "fetch", "method": "GET", "url": f"https://{HOST}/v1/items?limit=2", "headers": [], **fields}


class CredentialPlacementTests(unittest.TestCase):
    def test_places_a_bearer_header_and_an_hmac_proof_of_the_token(self) -> None:
        spec = _spec(
            **{
                "meta-access-token": {"header": "Authorization", "scheme": "Bearer"},
                "meta-app-secret": {"query": "appsecret_proof", "hmac": "meta-access-token"},
            }
        )
        action = SimpleNamespace(stored_inputs=("meta-access-token", "meta-app-secret"), integrations=())
        credentials, missing = provider.action_credentials(
            action, spec.stored_inputs, {"meta-access-token": TOKEN, "meta-app-secret": SECRET}, {}, {}
        )
        proof = hmac.new(SECRET.encode(), TOKEN.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(missing, frozenset())
        placed = {credential.id: (credential.header, credential.query, credential.value) for credential in credentials}
        self.assertEqual(placed["stored-input:meta-access-token"], ("authorization", None, f"Bearer {TOKEN}"))
        self.assertEqual(placed["stored-input:meta-app-secret"], (None, "appsecret_proof", proof))
        call = provider._inject(provider._parse(_frame(), frozenset({HOST})), credentials)
        self.assertIn(("authorization", f"Bearer {TOKEN}"), call.headers)
        self.assertEqual(call.target, f"/v1/items?limit=2&appsecret_proof={proof}")

    def test_an_unheld_credential_or_its_unheld_proof_key_marks_its_host_missing(self) -> None:
        spec = _spec(
            **{
                "meta-access-token": {"header": "Authorization", "scheme": "Bearer"},
                "meta-app-secret": {"query": "appsecret_proof", "hmac": "meta-access-token"},
            }
        )
        action = SimpleNamespace(stored_inputs=("meta-access-token", "meta-app-secret"), integrations=())
        for held in ({"meta-access-token": TOKEN}, {"meta-app-secret": SECRET}, {}):
            with self.subTest(held=sorted(held)):
                _credentials, missing = provider.action_credentials(action, spec.stored_inputs, held, {}, {})
                self.assertEqual(missing, frozenset({HOST}))

    def test_an_integration_bearer_goes_only_to_its_provider_api_hosts(self) -> None:
        action = SimpleNamespace(stored_inputs=(), integrations=("cloudflare",))
        credentials, _missing = provider.action_credentials(
            action,
            {},
            {},
            {"cloudflare": {"type": "oauth2-bearer", "access_token": TOKEN}},
            {"cloudflare": "cloudflare"},
        )
        self.assertEqual(
            [(item.host, item.header, item.value) for item in credentials],
            [("api.cloudflare.com", "authorization", f"Bearer {TOKEN}")],
        )

    def test_the_authorization_gate_holds_until_the_transcript_has_the_declared_authorization(self) -> None:
        action = SimpleNamespace(stored_inputs=(), integrations=(), human_requests=("auth:password",))
        spec = SimpleNamespace(stored_inputs={}, integrations={})
        approval = action_human.HumanResponse("auth:password", 0, "a" * 64, True)
        attempt = provider.Attempt("team_1", "meta-ads", "set-status", "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6")
        for responses, authorized in (((), False), ((approval,), True)):
            private = SimpleNamespace(
                stored_inputs={}, integrations={}, transcript=action_human.ActionTranscript("i", responses)
            )
            scope = provider.call_scope(attempt, spec, action, private, lambda: ("", frozenset()), lambda _fields: None)
            self.assertIs(scope.authorized, authorized)


class AdmissionTests(unittest.TestCase):
    def test_the_published_call_vectors_match_frame_admission(self) -> None:
        vectors = json.loads((PROTOCOL / "vectors" / "fetch.json").read_bytes())
        for case in vectors["cases"]:
            with self.subTest(case=case["name"]):
                try:
                    provider._parse(case["frame"], frozenset({HOST}))
                except provider.CallRefusedError:
                    admitted = False
                else:
                    admitted = True
                self.assertIs(admitted, case["valid"])

    def test_refuses_every_undeclared_destination_and_team_owned_field(self) -> None:
        refused = (
            _frame(url="https://collector.example.org/steal"),
            _frame(url=f"https://{HOST}:8443/v1"),
            _frame(url=f"https://user@{HOST}/v1"),
            _frame(url="https://API.EXAMPLE.COM/v1"),
            _frame(url=f"https://{HOST}\\@collector.example.org/v1"),
            _frame(headers=[["Host", "collector.example.org"]]),
            _frame(headers=[["Accept-Encoding", "gzip"]]),
            _frame(headers=[["x-a", "1"], ["X-A", "2"]]),
            _frame(headers=[["x-a", "a\nb"]]),
        )
        for frame in refused:
            with self.subTest(frame=frame), self.assertRaises(provider.CallRefusedError):
                provider._parse(frame, frozenset({HOST}))

    def test_refuses_a_header_or_parameter_a_placement_owns_and_a_missing_or_unauthorized_call(self) -> None:
        credential = provider.Credential("stored-input:k", HOST, "x-api-key", None, "v", ("v",))
        proof = provider.Credential("stored-input:p", HOST, None, "appsecret_proof", "p", ("p",))
        cases = (
            (_scope((credential,)), _frame(headers=[["X-Api-Key", "mine"]]), "refused"),
            (_scope((proof,)), _frame(url=f"https://{HOST}/v1?appsecret_proof=forged"), "refused"),
            (_scope((proof,)), _frame(url=f"https://{HOST}/v1?appsecret%5Fproof=forged"), "refused"),
            (_scope(missing=frozenset({HOST})), _frame(), "credential-missing"),
            (_scope(authorized=False), _frame(), "refused"),
        )
        for scope, frame, code in cases:
            with self.subTest(frame=frame):
                audits = []
                broker = provider.Broker(provider.CallScope(**{**_fields(scope), "audit": audits.append}))
                reply = json.loads(broker(frame, time.monotonic() + 5))
                self.assertEqual(reply, {"error": code})
                self.assertEqual([item["phase"] for item in audits], ["refused"])

    def test_the_egress_route_is_read_once_on_the_first_call_and_its_absence_refuses(self) -> None:
        reads = []

        def unavailable() -> tuple[str, frozenset[str]]:
            reads.append(1)
            raise provider.CallRefusedError("unavailable", "egress-policy")

        broker = provider.Broker(provider.CallScope(**{**_fields(_scope()), "route": unavailable}))
        self.assertEqual(reads, [])
        self.assertEqual(json.loads(broker(_frame(), time.monotonic() + 5)), {"error": "unavailable"})
        missing = provider.Broker(provider.CallScope(**{**_fields(_scope()), "route": lambda: ("", frozenset())}))
        self.assertEqual(json.loads(missing(_frame(), time.monotonic() + 5)), {"error": "refused"})

    def test_refuses_the_seventeenth_call_of_one_attempt(self) -> None:
        broker = provider.Broker(_scope(missing=frozenset({HOST})))
        replies = [json.loads(broker(_frame(), time.monotonic() + 5)) for _ in range(provider.MAX_CALLS + 1)]
        self.assertEqual(replies[-1], {"error": "refused"})
        self.assertEqual(broker.calls, provider.MAX_CALLS + 1)


def _fields(scope: provider.CallScope) -> dict[str, object]:
    return {name: getattr(scope, name) for name in provider.CallScope.__dataclass_fields__}


class _Origin(http.server.BaseHTTPRequestHandler):
    """The provider: records every request and answers with the scripted response."""

    script: tuple[int, list[tuple[str, str]], bytes] = (200, [("Content-Type", "application/json")], b"{}")
    seen: ClassVar[list[tuple[str, str, dict[str, str], bytes]]] = []

    def do_GET(self) -> None:
        self._answer()

    def do_POST(self) -> None:
        self._answer()

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        type(self).seen.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        status, headers, payload = type(self).script
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        pass


def _certificate(directory: Path) -> tuple[Path, Path]:
    """A self-signed certificate for the test origin host, trusted only by the test's TLS context."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    certificate_path = directory / "origin.pem"
    key_path = directory / "origin.key"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return certificate_path, key_path


class _Proxy(socketserver.ThreadingTCPServer):
    """A CONNECT proxy that admits only the expected policy token and tunnels every host to the test origin."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, origin_port: int) -> None:
        super().__init__(("127.0.0.1", 0), _Tunnel)
        self.origin_port = origin_port
        self.requests: list[tuple[str, str | None]] = []


class _Tunnel(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        lines = []
        while (line := self.rfile.readline(65536)) not in {b"\r\n", b""}:
            lines.append(line.decode("latin-1").strip())
        target = lines[0].split(" ")[1]
        authorization = next(
            (line.split(":", 1)[1].strip() for line in lines[1:] if line.lower().startswith("proxy-authorization:")),
            None,
        )
        self.server.requests.append((target, authorization))
        expected = "Basic " + base64.b64encode(f"{PROXY_TOKEN}:".encode()).decode()
        if authorization != expected:
            self.wfile.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
            return
        with socket.create_connection(("127.0.0.1", self.server.origin_port)) as upstream:
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.wfile.flush()
            pipes = [
                threading.Thread(target=_pipe, args=(self.connection, upstream), daemon=True),
                threading.Thread(target=_pipe, args=(upstream, self.connection), daemon=True),
            ]
            for pipe in pipes:
                pipe.start()
            for pipe in pipes:
                pipe.join(10)


def _pipe(source: socket.socket, destination: socket.socket) -> None:
    with contextlib.suppress(OSError):
        while chunk := source.recv(65536):
            destination.sendall(chunk)
    with contextlib.suppress(OSError):
        destination.shutdown(socket.SHUT_WR)


class TransportTests(unittest.TestCase):
    """The real broker over real TLS through a CONNECT proxy, with the credential placed by Team alone."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        certificate, key = _certificate(Path(cls.directory.name))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(certificate, key)
        cls.origin = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
        cls.origin.socket = server_context.wrap_socket(cls.origin.socket, server_side=True)
        cls.proxy = _Proxy(cls.origin.server_address[1])
        cls.threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (cls.origin, cls.proxy)]
        for thread in cls.threads:
            thread.start()
        client_context = ssl.create_default_context(cafile=str(certificate))
        cls.patches = [
            mock.patch.object(provider, "PROXY_HOST", "127.0.0.1"),
            mock.patch.object(provider, "PROXY_PORT", cls.proxy.server_address[1]),
            mock.patch.object(provider, "_TLS", client_context),
        ]
        for patch in cls.patches:
            patch.start()

    @classmethod
    def tearDownClass(cls) -> None:
        for patch in cls.patches:
            patch.stop()
        for server in (cls.origin, cls.proxy):
            server.shutdown()
            server.server_close()
        cls.directory.cleanup()

    def setUp(self) -> None:
        _Origin.seen = []
        _Origin.script = (200, [("Content-Type", "application/json")], b'{"data":[{"id":"1"}]}')

    def _call(self, frame: dict[str, object], credentials=(), audit=None) -> dict[str, object]:
        broker = provider.Broker(_scope(credentials, audit=audit))
        try:
            return json.loads(broker(frame, time.monotonic() + 5))
        finally:
            broker.release()

    def test_team_places_the_credential_and_the_action_receives_only_the_response(self) -> None:
        bearer = provider.Credential("stored-input:t", HOST, "authorization", None, f"Bearer {TOKEN}", (TOKEN,))
        proof = provider.Credential("stored-input:p", HOST, None, "appsecret_proof", "abc123", (SECRET, "abc123"))
        audits: list[dict[str, object]] = []
        body = base64.b64encode(b"status=PAUSED").decode()
        reply = self._call(
            _frame(method="POST", headers=[["content-type", "application/x-www-form-urlencoded"]], body=body),
            (bearer, proof),
            audits.append,
        )
        self.assertEqual(reply["status"], 200)
        self.assertEqual(base64.b64decode(reply["body"]), b'{"data":[{"id":"1"}]}')
        method, path, headers, sent = _Origin.seen[-1]
        self.assertEqual((method, path, sent), ("POST", "/v1/items?limit=2&appsecret_proof=abc123", b"status=PAUSED"))
        self.assertEqual(headers["authorization"], f"Bearer {TOKEN}")
        self.assertEqual(headers["accept-encoding"], "identity")
        self.assertEqual(self.proxy.requests[-1][0], f"{HOST}:443")
        self.assertEqual([item["phase"] for item in audits], ["dispatch", "outcome"])
        self.assertEqual(audits[0]["credentials"], ["stored-input:p", "stored-input:t"])
        self.assertNotIn(TOKEN, json.dumps(audits))
        self.assertNotIn("appsecret_proof", json.dumps(audits))

    def test_refuses_a_response_that_echoes_a_credential_in_any_common_form(self) -> None:
        bearer = provider.Credential("stored-input:t", HOST, "authorization", None, f"Bearer {TOKEN}", (TOKEN,))
        escaped = "".join(f"\\u{ord(character):04x}" for character in TOKEN)
        for payload in (
            TOKEN.encode(),
            base64.b64encode(TOKEN.encode()),
            json.dumps({"echo": TOKEN}).encode(),
            b'{"echo": "' + escaped.encode() + b'"}',
            b'{"' + escaped.encode() + b'": 1}',
        ):
            with self.subTest(payload=payload[:24]):
                _Origin.script = (200, [("Content-Type", "application/json")], payload)
                self.assertEqual(self._call(_frame(), (bearer,)), {"error": "failed"})
        _Origin.script = (200, [("X-Echo", TOKEN)], b"{}")
        self.assertEqual(self._call(_frame(), (bearer,)), {"error": "failed"})

    def test_refuses_echoes_in_header_names_escaped_or_duplicated_json_and_unicode_text(self) -> None:
        unicode_token = "tök€n-" + TOKEN
        bearer = provider.Credential("stored-input:t", HOST, "authorization", None, f"Bearer {TOKEN}", (TOKEN,))
        accented = provider.Credential("stored-input:u", HOST, None, "key", unicode_token, (unicode_token,))
        mixed = "".join(f"\\u{ord(character):04X}" for character in TOKEN)
        for headers, payload in (
            ([(f"X-{TOKEN}", "1")], b"{}"),
            ([], b'{"a":"' + mixed.encode() + b'"}'),
            ([], b'{"a":"safe","a":"' + mixed.encode() + b'"}'),
            ([], b'not json "' + mixed.encode() + b'"'),
            ([], f"plain {unicode_token}".encode()),
        ):
            with self.subTest(payload=payload[:24]):
                _Origin.script = (200, headers, payload)
                self.assertEqual(self._call(_frame(), (bearer, accented)), {"error": "failed"})

    def test_a_header_value_http_cannot_carry_is_refused_before_dispatch(self) -> None:
        euro = provider.Credential("stored-input:e", HOST, "x-key", None, "k€y", ("k€y",))
        self.assertEqual(self._call(_frame(), (euro,)), {"error": "refused"})
        self.assertEqual(self._call(_frame(headers=[["x-note", "€"]])), {"error": "refused"})
        self.assertEqual(_Origin.seen, [])

    def test_a_response_cut_before_its_declared_length_is_refused(self) -> None:
        def truncated(handler: _Origin) -> None:
            handler.send_response(200)
            handler.send_header("Content-Length", "10")
            handler.end_headers()
            handler.wfile.write(b"{}")
            handler.close_connection = True

        with mock.patch.object(_Origin, "_answer", truncated):
            self.assertEqual(self._call(_frame()), {"error": "failed"})

    def test_a_delivered_reply_holds_its_call_capacity_until_released(self) -> None:
        free = provider._CAPACITY._value
        broker = provider.Broker(_scope())
        self.assertEqual(json.loads(broker(_frame(), time.monotonic() + 5))["status"], 200)
        self.assertEqual(provider._CAPACITY._value, free - 1)
        broker.release()
        broker.release()
        self.assertEqual(provider._CAPACITY._value, free)

    def test_refuses_compressed_and_oversized_responses(self) -> None:
        _Origin.script = (200, [("Content-Encoding", "gzip")], b"\x1f\x8b")
        self.assertEqual(self._call(_frame()), {"error": "failed"})
        _Origin.script = (200, [], b"a" * (provider.MAX_RESPONSE_BYTES + 1))
        self.assertEqual(self._call(_frame()), {"error": "failed"})

    def test_a_proxy_refusal_is_unavailable_and_nothing_reaches_the_provider(self) -> None:
        broker = provider.Broker(
            provider.CallScope(**{**_fields(_scope()), "route": lambda: ("0" * 32, frozenset({HOST}))})
        )
        self.assertEqual(json.loads(broker(_frame(), time.monotonic() + 5)), {"error": "unavailable"})
        self.assertEqual(_Origin.seen, [])

    def test_an_origin_certificate_the_system_does_not_trust_is_unavailable(self) -> None:
        with mock.patch.object(provider, "_TLS", ssl.create_default_context()):
            self.assertEqual(self._call(_frame()), {"error": "unavailable"})
        self.assertEqual(_Origin.seen, [])

    def test_stop_ends_a_call_in_flight_and_refuses_the_next(self) -> None:
        stopped = threading.Event()

        def slow(*_args: object) -> None:
            stopped.set()
            time.sleep(2)

        with mock.patch.object(_Origin, "_answer", slow):
            broker = provider.Broker(_scope(stopped=stopped.is_set))
            started = time.monotonic()
            self.assertEqual(json.loads(broker(_frame(), time.monotonic() + 5)), {"error": "failed"})
            self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(json.loads(broker(_frame(), time.monotonic() + 5)), {"error": "refused"})

    def test_a_stop_while_the_dispatch_record_is_written_sends_nothing(self) -> None:
        stopped = threading.Event()
        audits: list[dict[str, object]] = []

        def audit(fields: dict[str, object]) -> None:
            audits.append(fields)
            if fields["phase"] == "dispatch":
                stopped.set()

        broker = provider.Broker(_scope(audit=audit, stopped=stopped.is_set))
        self.assertEqual(json.loads(broker(_frame(), time.monotonic() + 5)), {"error": "refused"})
        self.assertEqual(_Origin.seen, [])
        self.assertEqual(
            [(item["phase"], item.get("reason")) for item in audits], [("dispatch", None), ("outcome", "stopped")]
        )

    def test_an_audit_failure_before_dispatch_sends_nothing(self) -> None:
        def failing(_fields: object) -> None:
            raise RuntimeError("the audit journal is unavailable")

        broker = provider.Broker(_scope(audit=failing))
        with self.assertRaises(RuntimeError):
            broker(_frame(), time.monotonic() + 5)
        self.assertEqual(_Origin.seen, [])


if __name__ == "__main__":
    unittest.main()
