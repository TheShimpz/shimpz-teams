"""Routine routes over the real Local HTTP server: each is reachable only under its own authority (ADR-0086)."""

from __future__ import annotations

import dataclasses
import hashlib
import http.client
import json
import os
import tempfile
import threading
import time
import types
from pathlib import Path
from unittest import mock

import routine_fixture
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from test_local_authority import _claims, _segment
from test_local_chat_scope import LOOKUP_RESULT
from test_local_routine_service import (
    API_KEY,
    API_KEY_SHA256,
    RoutineServiceCase,
    Runtime,
    acting,
    approval,
)

from action import human as action_human
from local import authority as local_authority
from local.http import server
from local.routine import diagnostics as routine_diagnostics
from protocol.http.v1 import progress as progress_contract
from protocol.http.v1 import routine as http_routine
from protocol.http.v1 import supervisor as contract
from routine import grant as routine_grant
from routine import record
from tests import human_request_fixtures

TOKEN = "t" * 43
EMPTY = b"{}"
CLAIM = b'{"providers":["anthropic","openai"]}'


class RoutineHttpCase(RoutineServiceCase):
    def setUp(self) -> None:
        super().setUp()
        keys = tempfile.TemporaryDirectory()
        self.addCleanup(keys.cleanup)
        self.routine_key = Ed25519PrivateKey.generate()
        public = self.routine_key.public_key()
        path = Path(keys.name) / "routine.pem"
        path.write_bytes(public.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
        path.chmod(0o440)
        self.fingerprint = fingerprint = hashlib.sha256(public.public_bytes(Encoding.Raw, PublicFormat.Raw)).hexdigest()
        self.session = local_authority.Evidence("a" * 32, "session", "b" * 64, "c" * 32, 2_200_000_000)
        for patch in (
            mock.patch.object(local_authority, "ROUTINE_PUBLIC_KEY_FILE", path),
            mock.patch.object(local_authority.grp, "getgrnam", return_value=types.SimpleNamespace(gr_gid=os.getgid())),
            mock.patch.object(local_authority, "routine_key_fingerprint", return_value=fingerprint),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_claim(self, service, claim: dict[str, object]) -> dict[str, object]:
        lease = hashlib.sha256(claim["lease_token"].encode("ascii")).hexdigest()
        evidence = local_authority.RoutineEvidence(self.fingerprint, lease, "a" * 32, 0)
        return service.run_routine(
            "team_1", claim["run_id"], evidence, (claim["revision"], claim["plan_digest"]), ("openai", API_KEY)
        )

    def serve(self, directory: str, runtime: Runtime):
        controller, service = self.service(directory, runtime)
        self.server = server.BoundedServer(("127.0.0.1", 0), server.Handler, controller, TOKEN)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        return controller, service

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            sent = {"Authorization": f"Bearer {TOKEN}", **(headers or {})}
            if body is not None:
                sent |= {"Content-Type": "application/json", "Content-Length": str(len(body))}
            connection.request(method, path, body=body, headers=sent)
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type"), response.read()
        finally:
            connection.close()

    @staticmethod
    def terminal(raw: bytes) -> dict[str, object]:
        records = [progress_contract.decode_line(line) for line in raw.splitlines(keepends=True) if line.strip()]
        return records[-1]

    def model(self) -> dict[str, str]:
        return {"X-Shimpz-Model-Provider": "openai", "X-Shimpz-Model-Api-Key": API_KEY}

    def routine_headers(
        self, path: str, lease_token: str, *, key: Ed25519PrivateKey | None = None, body: bytes = EMPTY
    ) -> dict[str, str]:
        now = int(time.time())
        claims = _claims(
            aud=contract.ROUTINE_AUDIENCE,
            authority=contract.ROUTINE_AUTHORITY,
            authority_sha256=hashlib.sha256(lease_token.encode("ascii")).hexdigest(),
            jti=os.urandom(16).hex(),
            iat=now,
            exp=now + contract.ASSERTION_MAX_TTL_SECONDS,
            method="POST",
            path=path,
            body={"kind": "json", "length": len(body), "sha256": hashlib.sha256(body).hexdigest()},
            model={"provider": "openai", "key_sha256": API_KEY_SHA256},
        )
        jwt = _segment(contract.canonical_json(contract.ROUTINE_JWT_HEADER))
        payload = _segment(contract.claims_json(claims, audience=contract.ROUTINE_AUDIENCE))
        signature = _segment((key or self.routine_key).sign(f"{jwt}.{payload}".encode("ascii")))
        return {contract.ROUTINE_ASSERTION_HEADER: f"Bearer {jwt}.{payload}.{signature}", **self.model()}


class SchedulerRouteTests(RoutineHttpCase):
    def test_the_scheduler_claims_and_delivers_under_the_team_bearer_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.serve(directory, Runtime())
            value = self.routine(service)
            status, _type, _raw = self.request("POST", "/v1/routines/claim", CLAIM, {"Authorization": "Bearer x"})
            self.assertEqual(status, 401)
            status, _type, raw = self.request("POST", "/v1/routines/claim", b'{"any":1}')
            self.assertEqual((status, json.loads(raw)["code"]), (422, "invalid-body"))
            for invalid in (
                EMPTY,
                b'{"providers":[]}',
                b'{"providers":["openai","anthropic"]}',
                b'{"providers":["other"]}',
            ):
                status, _type, raw = self.request("POST", "/v1/routines/claim", invalid)
                self.assertEqual((status, json.loads(raw)["code"]), (422, "invalid-body"))
            # A Team whose model provider Admin holds no key for is never claimed.
            status, _type, raw = self.request("POST", "/v1/routines/claim", b'{"providers":["anthropic"]}')
            self.assertEqual(json.loads(raw)["run"], None)
            # Nor does its Routine wake Admin: the hint names only Routines Admin can run.
            self.assertIsNone(json.loads(raw)["next_due_at"])
            status, _type, raw = self.request("POST", "/v1/routines/claim", CLAIM)
            claimed = json.loads(raw)
            claim = claimed["run"]
            self.assertEqual((claim["provider"], claimed["next_due_at"]), ("openai", None))
            self.assertEqual((claim["revision"], claim["plan_digest"]), (1, routine_grant.plan_digest(value.plan)))
            self.assertEqual((status, claim["team_id"]), (200, "team_1"))
            status, _type, raw = self.request("POST", "/v1/routines/claim", CLAIM)
            # Nothing to claim: the hint is the Routine's next firing, which the claim moved past now.
            idle = json.loads(raw)
            next_run_at = record.routine(self.state(service), value.routine_id).next_run_at
            self.assertEqual((idle["run"], idle["next_due_at"]), (None, None))
            service.routine_store.update("team_1", lambda state: (dataclasses.replace(state, runs=()), None))
            status, _type, raw = self.request("POST", "/v1/routines/claim", CLAIM)
            self.assertEqual(
                json.loads(raw), {"run": None, "next_due_at": next_run_at, "trace_id": json.loads(raw)["trace_id"]}
            )
            status, _type, raw = self.request("GET", "/v1/routines/notices")
            self.assertEqual((status, json.loads(raw)["notices"]), (200, []))
            status, _type, raw = self.request("POST", "/v1/routines/notices/ack", b'{"deliveries":[]}')
            self.assertEqual((status, json.loads(raw)["code"]), (422, "invalid-body"))
            # Hosted never serves a Local Routine route.
            self.assertIsNone(
                server.strict_http.resolve_controller_route("hosted", "POST", ("v1", "routines", "claim"))
            )


class RunRouteTests(RoutineHttpCase):
    def test_a_leased_run_runs_only_under_its_own_routine_assertion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime()
            controller, service = self.serve(directory, runtime)
            controller.assistant_lifecycle.invoke = lambda *_args: {"result": LOOKUP_RESULT}
            self.routine(service)
            claim = service.claim_routine_run(("anthropic", "openai"))
            path = f"/v1/teams/team_1/routines/runs/{claim['run_id']}/segment"
            segment = json.dumps({"revision": claim["revision"], "plan_digest": claim["plan_digest"]}).encode()
            status, _type, raw = self.request("POST", path, segment, self.model())
            self.assertEqual((status, json.loads(raw)["code"]), (403, "invalid-routine"))
            forged = self.routine_headers(path, claim["lease_token"], key=Ed25519PrivateKey.generate(), body=segment)
            status, _type, raw = self.request("POST", path, segment, forged)
            self.assertEqual((status, json.loads(raw)["code"]), (403, "invalid-routine"))
            other_lease = self.routine_headers(path, "another-lease", body=segment)
            status, _type, raw = self.request("POST", path, segment, other_lease)
            self.assertEqual(self.terminal(raw)["body"]["code"], "routine-lease-invalid")
            # A segment that names another revision or plan than its claim is refused before anything runs.
            for stale in (
                {"revision": claim["revision"] + 1, "plan_digest": claim["plan_digest"]},
                {"revision": claim["revision"], "plan_digest": "sha256:" + "0" * 64},
            ):
                body = json.dumps(stale).encode()
                status, _type, raw = self.request(
                    "POST", path, body, self.routine_headers(path, claim["lease_token"], body=body)
                )
                self.assertEqual(self.terminal(raw)["body"]["code"], "routine-revision-stale")
            self.assertEqual(record.run(self.state(service), claim["run_id"]).status, "leased")
            status, _type, raw = self.request("POST", path, EMPTY, self.routine_headers(path, claim["lease_token"]))
            self.assertEqual((status, json.loads(raw)["code"]), (422, "invalid-body"))
            headers = self.routine_headers(path, claim["lease_token"], body=segment)
            status, content_type, raw = self.request("POST", path, segment, headers)
            self.assertEqual((status, content_type), (200, "application/x-ndjson"))
            self.assertEqual(self.terminal(raw)["body"]["status"], "done")
            status, _type, raw = self.request("POST", path, segment, headers)
            self.assertEqual((status, json.loads(raw)["code"]), (403, "invalid-routine"))
            # The compiled run never reached the Brain.
            self.assertEqual(runtime.contexts, [])

    def test_a_routine_key_that_cannot_be_read_is_unavailable_not_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.serve(directory, Runtime())
            path = "/v1/teams/team_1/routines/runs/" + "f" * 32 + "/segment"
            with mock.patch.object(local_authority, "ROUTINE_PUBLIC_KEY_FILE", Path(directory) / "absent.pem"):
                status, _type, raw = self.request("POST", path, EMPTY, self.routine_headers(path, "lease"))
            self.assertEqual((status, json.loads(raw)["code"]), (503, "routine-unavailable"))
            self.assertIsNotNone(service)

    def test_a_signed_run_with_a_body_or_a_malformed_run_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.serve(directory, Runtime())
            path = "/v1/teams/team_1/routines/runs/not-a-run/segment"
            status, _type, raw = self.request("POST", path, EMPTY, self.routine_headers(path, "lease"))
            self.assertEqual((status, json.loads(raw)["code"]), (404, "routine-run-not-found"))
            path = "/v1/teams/team_1/routines/runs/" + "f" * 32 + "/segment"
            headers = self.routine_headers(path, "lease")
            with mock.patch.object(local_authority, "verify_routine", return_value=None):
                status, _type, raw = self.request("POST", path, b'{"x":1}', headers)
            self.assertEqual((status, json.loads(raw)["code"]), (422, "invalid-body"))


class SessionRouteTests(RoutineHttpCase):
    def test_a_supervisor_session_manages_routines_and_decides_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.serve(directory, Runtime(acting()))

            def invoke(*_args):
                raise action_human.HumanRequestSuspensionError(approval())

            controller.assistant_lifecycle.invoke = invoke
            opened_title = next(
                item["msgid"]
                for item in approval().messages()
                if item["id"] == approval().payload()["title"]["message"]
            )
            value = self.routine(service)
            claim = service.claim_routine_run(("anthropic", "openai"))
            self.run_claim(service, claim)
            run = f"/v1/teams/team_1/routines/runs/{claim['run_id']}"
            with mock.patch.object(local_authority, "verify", return_value=self.session) as verify:
                status, _type, raw = self.request("GET", "/v1/teams/team_1/routines")
                self.assertEqual(json.loads(raw)["runs"][0]["status"], "frozen")
                self.assertEqual(verify.call_args.kwargs["request"].authority_kinds, frozenset({"session"}))
                # Opening names exactly the Admin interface language its copy renders in (ADR-0091).
                for invalid in (b'{"x":1}', EMPTY, b'{"locale":null}', b'{"locale":"pt-BR"}', b'{"locale":"pt","x":1}'):
                    with self.subTest(body=invalid):
                        status, _type, raw = self.request("POST", run + "/challenge", invalid)
                        self.assertEqual((status, json.loads(raw)["code"]), (422, "invalid-body"))
                status, _type, raw = self.request("POST", run + "/stop", b'{"x":1}')
                self.assertEqual((status, json.loads(raw)["code"]), (422, "invalid-body"))
                status, _type, raw = self.request("POST", run + "/challenge", b'{"locale":"pt"}')
                opened = json.loads(raw)
                self.assertEqual((opened["locale"], opened["rendered"]["title"]), ("pt", f"PT {opened_title}"))
                challenge_id = opened["challenge_id"]
                # The retired release of an uncertain run stays absent.
                status, _type, raw = self.request("POST", run + "/resolve", b'{"batch_fingerprint":"x"}')
                self.assertEqual((status, json.loads(raw)["code"]), (404, "route-not-found"))
                answer = json.dumps({"challenge_id": challenge_id, "decision": "deny"}).encode()
                status, _type, raw = self.request("POST", run + "/human", answer, self.model())
                self.assertEqual(self.terminal(raw)["body"]["status"], "denied")
                status, _type, raw = self.request("POST", "/v1/teams/team_1/routines/runs/bad/stop", EMPTY)
                self.assertEqual((status, json.loads(raw)["code"]), (404, "routine-run-not-found"))
                status, _type, raw = self.request("POST", run + "/stop", EMPTY)
                self.assertEqual((status, json.loads(raw)["code"]), (404, "routine-run-not-found"))
                status, _type, raw = self.request("POST", run + "/integrations", EMPTY, self.model())
                self.assertEqual(self.terminal(raw)["body"]["code"], "routine-run-not-found")
                status, _type, raw = self.request("DELETE", f"/v1/teams/team_1/routines/{value.routine_id}")
                self.assertEqual((status, json.loads(raw)["deleted"]), (200, True))
                # The retired confirmation and preview routes stay absent: a Routine is created only from a chat.
                preview = "/v1/teams/team_1/routines/proposals/" + "0" * 32 + "/preview"
                status, _type, raw = self.request("POST", preview, b'{"timezone":"UTC"}')
                self.assertEqual((status, json.loads(raw)["code"]), (404, "route-not-found"))
                confirm = json.dumps({"proposal_id": "0" * 32, "timezone": "UTC"}).encode()
                status, _type, raw = self.request("POST", "/v1/teams/team_1/routines", confirm)
                self.assertEqual((status, json.loads(raw)["code"]), (404, "route-not-found"))
            with mock.patch.object(local_authority, "verify", side_effect=local_authority.SupervisorDeniedError):
                status, _type, raw = self.request("GET", "/v1/teams/team_1/routines")
            self.assertEqual((status, json.loads(raw)["code"]), (403, "invalid-supervisor"))

    def test_a_supervisor_reads_a_runs_diagnostics_only_in_the_teams_current_incarnation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, _service = self.serve(directory, Runtime(acting()))
            incarnation = controller.assistant_lifecycle._network("team_1").id
            run_id = "d" * 32
            diagnostic = routine_diagnostics.Diagnostic(
                routine_id="c" * 32,
                run_id=run_id,
                operation_id="6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6",
                attempt=1,
                assistant_id="shimpz-cloudflare",
                action="list-zones",
                recorded_at=int(time.time()),
                condition="stderr-output",
            )
            controller.routine_diagnostics.record("team_1", incarnation, diagnostic, ())
            path = f"/v1/teams/team_1/routines/runs/{run_id}/diagnostics"
            with mock.patch.object(local_authority, "verify", return_value=self.session) as verify:
                status, _type, raw = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(verify.call_args.kwargs["request"].authority_kinds, frozenset({"session"}))
                body = json.loads(raw)
                # The Local API adds its trace id to every response; Admin strips it before admitting the view.
                self.assertRegex(body.pop("trace_id"), r"\A[0-9a-f]{32}\Z")
                view = http_routine.canonical_diagnostics(body)
                self.assertEqual(view["diagnostics"], [diagnostic.view()])
                status, _type, raw = self.request("GET", "/v1/teams/team_1/routines/runs/bad/diagnostics")
                self.assertEqual((status, json.loads(raw)["code"]), (404, "routine-run-not-found"))
                with mock.patch.object(
                    controller.assistant_lifecycle, "_network", return_value=types.SimpleNamespace(id="e" * 64)
                ):
                    status, _type, raw = self.request("GET", path)
                self.assertEqual((status, json.loads(raw)["diagnostics"]), (200, []))
                # A corrupted body of the current incarnation is never silently left out.
                [sealed] = controller.routine_diagnostics._team_dir("team_1").iterdir()
                envelope = json.loads(sealed.read_bytes())
                envelope["ciphertext"] = ("B" if envelope["ciphertext"][0] == "A" else "A") + envelope["ciphertext"][1:]
                sealed.write_text(json.dumps(envelope))
                status, _type, raw = self.request("GET", path)
                self.assertEqual((status, json.loads(raw)["code"]), (503, "routine-state-unavailable"))
            with mock.patch.object(local_authority, "verify", side_effect=local_authority.SupervisorDeniedError):
                status, _type, raw = self.request("GET", path)
            self.assertEqual((status, json.loads(raw)["code"]), (403, "invalid-supervisor"))

    def test_approving_a_frozen_authentication_request_binds_its_assurance(self) -> None:
        descriptor = {"kind": "auth:password", "ordinal": 0, "title": "Sign in", "description": "Enter the password."}
        password = human_request_fixtures.admit(human_request_fixtures.fingerprinted(descriptor), ("auth:password",))
        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.serve(directory, Runtime(acting()))

            def invoke(*_args):
                raise action_human.HumanRequestSuspensionError(password)

            controller.assistant_lifecycle.invoke = invoke
            self.routine(service)
            claim = service.claim_routine_run(("anthropic", "openai"))
            self.run_claim(service, claim)
            run = f"/v1/teams/team_1/routines/runs/{claim['run_id']}"
            opened = service.open_routine_challenge("team_1", claim["run_id"], "en")
            answer = json.dumps({"challenge_id": opened["challenge_id"], "decision": "submit", "value": True}).encode()
            with mock.patch.object(
                local_authority, "verify", side_effect=local_authority.SupervisorDeniedError
            ) as verify:
                self.request("POST", run + "/human", answer, self.model())
                self.assertEqual(
                    verify.call_args.kwargs["request"].assurance,
                    {"kind": "auth:password", "challenge_id": opened["challenge_id"]},
                )
                for other in (
                    {"challenge_id": "0" * 32, "decision": "submit", "value": True},
                    {"challenge_id": opened["challenge_id"], "decision": "deny"},
                ):
                    self.request("POST", run + "/human", json.dumps(other).encode(), self.model())
                    self.assertIsNone(verify.call_args.kwargs["request"].assurance)


class RecoveryRouteTests(RoutineHttpCase):
    def test_a_session_opens_and_answers_recovery_cards_and_resumes_a_routine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.serve(directory, Runtime())
            value = self.routine(service)
            base = "/v1/teams/team_1/routines"
            incident = f"{base}/incidents/{'a' * 32}"
            nonce = '{"nonce":"' + "b" * 32 + '","choice":"skip"}'
            with mock.patch.object(local_authority, "verify", return_value=self.session):
                status, _type, raw = self.request("GET", base)
                listed = json.loads(raw)
                self.assertEqual((listed["incidents"], listed["routines"][0]["paused"]), ([], False))
                self.assertIsNotNone(http_routine.canonical_routine_view(listed["routines"][0]))
                cases = (
                    (f"{base}/incidents/bad/card", EMPTY, 404, "routine-incident-unavailable"),
                    (incident + "/card", b'{"x":1}', 422, "invalid-body"),
                    (incident + "/card", EMPTY, 404, "routine-incident-unavailable"),
                    (incident + "/answer", b'{"nonce":"x","choice":"skip"}', 422, "invalid-body"),
                    (incident + "/answer", b'{"nonce":"' + b"b" * 32 + b'","choice":"other"}', 422, "invalid-body"),
                    (incident + "/answer", nonce.encode(), 409, "routine-card-expired"),
                    (f"{base}/{'f' * 32}/resume", EMPTY, 404, "routine-not-found"),
                    (f"{base}/bad/resume", EMPTY, 404, "routine-not-found"),
                    (f"{base}/{value.routine_id}/resume", b'{"x":1}', 422, "invalid-body"),
                )
                for path, body, code, problem in cases:
                    with self.subTest(path=path, body=body):
                        status, _type, raw = self.request("POST", path, body)
                        self.assertEqual((status, json.loads(raw)["code"]), (code, problem))
                service.routine_store.update(
                    "team_1", lambda state: (record.set_paused(state, value.routine_id, True), None)
                )
                status, _type, raw = self.request("POST", f"{base}/{value.routine_id}/resume", EMPTY)
                resumed = {key: item for key, item in json.loads(raw).items() if key != "trace_id"}
                self.assertEqual(resumed, {"team_id": "team_1", "routine_id": value.routine_id, "paused": False})
                self.assertFalse(record.routine(self.state(service), value.routine_id).paused)
                # An opened card and its answer travel exactly as Team's recovery card produced them.
                card = {"incident_id": "a" * 32}
                with mock.patch.object(service, "open_routine_card", return_value=card) as opened:
                    status, _type, raw = self.request("POST", incident + "/card", EMPTY)
                self.assertEqual((status, json.loads(raw)["incident_id"]), (200, "a" * 32))
                self.assertEqual(opened.call_args.args, ("team_1", "a" * 32))
                with mock.patch.object(service, "answer_routine_card", return_value=card) as answered:
                    status, _type, raw = self.request("POST", incident + "/answer", nonce.encode())
                self.assertEqual((status, answered.call_args.args[2]), (200, json.loads(nonce)))


class NoticeBacklogTests(RoutineHttpCase):
    def test_a_backlog_of_maximum_notices_drains_in_bounded_batches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.serve(directory, Runtime())
            value = self.routine(service)
            large = routine_fixture.large_definition()
            notices = tuple(
                record.Notice(f"{index:032x}", value.routine_id, "", "created", int(time.time()), large, 1, value.quote)
                for index in range(9)
            )
            service.routine_store.update("team_1", lambda state: (dataclasses.replace(state, notices=notices), None))
            delivered = 0
            more = True
            while more:
                status, _type, raw = self.request("GET", "/v1/routines/notices")
                self.assertEqual(status, 200)
                self.assertLess(len(raw), server.MAX_API_RESPONSE_BYTES)
                batch = json.loads(raw)
                self.assertGreater(len(batch["notices"]), 0)
                deliveries = [
                    {"team_id": item["team_id"], "notice_id": item["notice_id"], "version": item["version"]}
                    for item in batch["notices"]
                ]
                status, _type, _raw = self.request(
                    "POST", "/v1/routines/notices/ack", json.dumps({"deliveries": deliveries}).encode()
                )
                self.assertEqual(status, 200)
                delivered += len(deliveries)
                more = batch["more"]
            self.assertEqual((delivered, self.state(service).notices), (9, ()))


class ProtocolViewTests(RoutineHttpCase):
    def test_every_routine_response_is_in_its_canonical_protocol_view(self) -> None:
        def body(raw: bytes) -> dict[str, object]:
            value = json.loads(raw)
            value.pop("trace_id")
            return value

        with tempfile.TemporaryDirectory() as directory:
            controller, service = self.serve(directory, Runtime(acting()))

            def invoke(*_args):
                raise action_human.HumanRequestSuspensionError(approval())

            controller.assistant_lifecycle.invoke = invoke
            self.routine(service)
            _status, _type, raw = self.request("POST", "/v1/routines/claim", CLAIM)
            claim = body(raw)
            self.assertEqual(http_routine.canonical_claim(claim), claim)
            self.run_claim(service, claim["run"])
            _status, _type, raw = self.request("GET", "/v1/routines/notices")
            notices = body(raw)
            self.assertEqual(http_routine.canonical_notice_batch(notices), notices)
            self.assertEqual(notices["notices"][0]["outcome"], "frozen")
            with mock.patch.object(local_authority, "verify", return_value=self.session):
                _status, _type, raw = self.request("GET", "/v1/teams/team_1/routines")
                listed = body(raw)
            self.assertEqual(set(listed), {"team_id", "routines", "runs", "incidents"})
            for item in listed["routines"]:
                self.assertEqual(http_routine.canonical_routine_view(item), item)
            (run,) = listed["runs"]
            self.assertEqual((http_routine.canonical_run_view(run), run["status"]), (run, "frozen"))
