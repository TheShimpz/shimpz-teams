"""Sanitized handled Action failures: admission and Team's independent re-redaction (ADR-0092 section 8)."""

from __future__ import annotations

import base64
import copy
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

from action import execution as action_execution
from action import failure as action_failure
from action import human as action_human

VECTORS = Path(__file__).resolve().parents[1] / "protocol" / "assistant" / "v1" / "failure-vectors.json"
TOKEN = "oauth-Access-Token-7f3a9c"
STORED = "stored/input+value=42"
ANSWER = "human-password-answer"
CAPABILITY = "egresscapability0123456789abcdef"


def _envelope(**members: object) -> dict[str, object]:
    failure = {
        "error_type": "httpx.HTTPStatusError",
        "message": "Client error '404 Not Found'",
        "provider": "api.example.com",
        "http_status": 404,
        "response_excerpt": '{"errors":[]}',
        "redacted": False,
        "truncated": False,
    }
    return {"type": "failure", "failure": {**failure, **members}}


def _admit(envelope: dict[str, object], *secrets: str) -> action_failure.ActionFailure:
    return action_failure.admit(envelope, secrets)


class FailureAdmissionTests(unittest.TestCase):
    def test_team_admission_matches_every_published_failure_vector(self) -> None:
        vectors = json.loads(VECTORS.read_bytes())
        self.assertEqual(vectors["version"], 1)
        for case in vectors["cases"]:
            try:
                admitted = _admit(copy.deepcopy(case["response"]))
            except action_failure.FailureEnvelopeError:
                valid = False
            else:
                valid = True
                self.assertEqual(set(admitted.document()), set(case["response"]["failure"]), case["name"])
            self.assertEqual(valid, case["valid"], case["name"])

    def test_a_clean_diagnostic_is_kept_exactly(self) -> None:
        envelope = _envelope(redacted=True, truncated=True)
        self.assertEqual(_admit(envelope, TOKEN).document(), envelope["failure"])
        self.assertTrue(action_failure.is_failure(envelope))
        self.assertFalse(action_failure.is_failure({"type": "result", "result": {}}))
        self.assertFalse(action_failure.is_failure(["failure"]))

    def test_every_member_is_re_redacted_with_each_injected_value(self) -> None:
        envelope = _envelope(
            error_type=f"Leak{TOKEN}Error",
            message=f"stored value {STORED} and answer {ANSWER.upper()} were refused",
            provider=f"{CAPABILITY}.example.com",
            response_excerpt=f'{{"token":"{TOKEN}"}}',
        )
        failure = _admit(envelope, TOKEN, STORED, ANSWER, CAPABILITY)
        rendered = json.dumps(failure.document())
        for secret in (TOKEN, STORED, ANSWER, ANSWER.upper(), CAPABILITY):
            self.assertNotIn(secret, rendered)
        self.assertEqual(failure.error_type, "Leak[REDACTED]Error")
        self.assertIsNone(failure.provider)
        self.assertEqual(failure.http_status, 404)
        self.assertTrue(failure.redacted)
        self.assertFalse(failure.truncated)

    def test_every_common_encoding_of_an_injected_value_is_replaced(self) -> None:
        raw = STORED.encode()
        encodings = {
            "json": json.dumps(STORED)[1:-1],
            "json slash": json.dumps(STORED)[1:-1].replace("/", "\\/"),
            "percent": quote(STORED, safe=""),
            "lower percent": quote(STORED, safe="").lower(),
            "hex": raw.hex(),
            "upper hex": raw.hex().upper(),
            "base64": base64.b64encode(raw).decode(),
            "url-safe base64": base64.urlsafe_b64encode(raw).decode().rstrip("="),
            "basic credential": "Authorization " + base64.b64encode(b"user:" + raw).decode(),
            "offset base64": base64.b64encode(b"ab" + raw + b"tail").decode(),
        }
        for name, encoded in encodings.items():
            with self.subTest(encoding=name):
                failure = _admit(_envelope(message=f"rejected {encoded} here"), STORED)
                self.assertTrue(failure.redacted)
                self.assertIn("[REDACTED]", failure.message)
                self.assertNotIn(encoded, failure.message)
        unicode_secret = "pässwörd-ünïcode"
        failure = _admit(_envelope(message=json.dumps({"v": unicode_secret})), unicode_secret)
        self.assertNotIn("u00e4", failure.message)

    def test_secret_shaped_text_is_consumed_whole_through_the_end_of_its_value(self) -> None:
        cases = {
            "Authorization: Bearer abc.def-ghi/jkl": "Authorization: [REDACTED]",
            "token=abc def": "token=[REDACTED] def",
            '{"password": "a b \\" c"} after': '{"password": [REDACTED]} after',
            "client_secret='unterminated value": "client_secret=[REDACTED]",
            "api_key: sk-proj-ABCDEFGHIJKLmnop_qrst-uvwx and more": "api_key: [REDACTED] and more",
            "key sk-proj-ABCDEFGHIJKLmnop_qrst-uvwx.extra": "key [REDACTED]",
            "jwt eyJhbGciOi.eyJzdWIiOjF9.c2lnbmF0dXJl end": "jwt [REDACTED] end",
            "pem -----BEGIN RSA PRIVATE KEY-----\nMIIabc\n": "pem [REDACTED]",
            "pem -----BEGIN PRIVATE KEY-----x-----END PRIVATE KEY----- ok": "pem [REDACTED] ok",
            "proxy https://user:pass@proxy.local:3128 failed": "proxy https://[REDACTED]@proxy.local:3128 failed",
            "token=[REDACTED] already": "token=[REDACTED] already",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(_admit(_envelope(message=text)).message, expected)
        self.assertFalse(_admit(_envelope(message="token=[REDACTED] already")).redacted)

    def test_a_clipped_trailing_secret_is_withheld_rather_than_shown_cut(self) -> None:
        for clipped in (TOKEN[:4], TOKEN[:-1], TOKEN[:12].upper(), quote(STORED, safe="")[:9]):
            with self.subTest(clipped=clipped):
                failure = _admit(_envelope(message=f"request failed for {clipped}", truncated=True), TOKEN, STORED)
                self.assertEqual(failure.message, "request failed for [REDACTED]")
                self.assertTrue(failure.redacted and failure.truncated)
        # Three characters are too short to identify a value; a value elsewhere in the text is not a clipped tail.
        self.assertEqual(_admit(_envelope(message="ends oau"), TOKEN).message, "ends oau")
        self.assertEqual(_admit(_envelope(message=f"{TOKEN[:8]} first"), TOKEN).message, f"{TOKEN[:8]} first")

    def test_redaction_runs_before_the_bound_and_marks_the_cut(self) -> None:
        secret = "s3"
        failure = _admit(_envelope(error_type="E" + "s3" * 63, message="é" * 1023 + "s3"), secret)
        self.assertEqual(len(failure.error_type), action_failure.MAX_ERROR_TYPE)
        self.assertTrue(failure.error_type.startswith("E[REDACTED]"))
        self.assertLessEqual(len(failure.message.encode()), action_failure.MAX_TEXT_BYTES)
        self.assertNotIn("s3", failure.error_type + failure.message)
        self.assertTrue(failure.redacted and failure.truncated)
        self.assertIsNone(
            action_failure.failure_validator.failure_error({"type": "failure", "failure": failure.document()})
        )

    def test_an_empty_value_or_a_lone_surrogate_never_passes(self) -> None:
        self.assertEqual(_admit(_envelope(message="nothing to hide"), "").message, "nothing to hide")
        with self.assertRaises(action_failure.FailureEnvelopeError):
            _admit(_envelope(message="lone \ud800 surrogate"))

    def test_short_derived_encodings_never_erase_ordinary_text(self) -> None:
        failure = _admit(_envelope(message="QQ is 51 in hex", response_excerpt=None), "Q")
        self.assertEqual(failure.message, "[REDACTED][REDACTED] is 51 in hex")
        self.assertIsNone(failure.response_excerpt)

    def test_adversarial_text_stays_cheap_to_sanitize(self) -> None:
        secrets = ["a" * (16_000 - index) + "b" for index in range(14)]
        started = time.monotonic()
        failure = _admit(_envelope(message="a" * 2047 + "c", response_excerpt="a" * 2048), *secrets)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(failure.message, "a" * 2047 + "c")
        self.assertEqual(failure.response_excerpt, "[REDACTED]")

    def test_a_failure_survives_as_the_cause_of_a_controller_problem(self) -> None:
        failure = _admit(_envelope())
        try:
            try:
                raise action_failure.ActionFailedError(failure)
            except action_failure.ActionFailedError as exc:
                raise RuntimeError("problem") from exc
        except RuntimeError as problem:
            self.assertIs(action_failure.failure_of(problem), failure)
        self.assertIsNone(action_failure.failure_of(RuntimeError("other")))
        self.assertIsNone(action_failure.failure_of(None))
        deep = RuntimeError("deep")
        current = deep
        for _ in range(8):
            current.__cause__ = RuntimeError("link")
            current = current.__cause__
        current.__cause__ = action_failure.ActionFailedError(failure)
        self.assertIsNone(action_failure.failure_of(deep))

    def test_the_egress_capability_is_read_from_the_workload_proxy_environment(self) -> None:
        container = SimpleNamespace(
            attrs={
                "Config": {
                    "Env": [
                        f"HTTPS_PROXY=http://{CAPABILITY}@assistant-egress:3128",
                        f"https_proxy=http://{CAPABILITY}@assistant-egress:3128",
                        "HTTP_PROXY=http://ignored@proxy:1",
                        "NO_PROXY=localhost",
                        "HTTPS_PROXY",
                        7,
                    ]
                }
            }
        )
        self.assertEqual(action_failure.capability_values(container), (CAPABILITY,))
        for missing in (SimpleNamespace(), SimpleNamespace(attrs=[]), SimpleNamespace(attrs={"Config": []})):
            self.assertEqual(action_failure.capability_values(missing), ())
        self.assertEqual(action_failure.capability_values(SimpleNamespace(attrs={"Config": {"Env": "x"}})), ())


class FailureProjectionTests(unittest.TestCase):
    def _project(self, raw: object, **policy: object) -> object:
        transcript = SimpleNamespace(protected_values=lambda: {"human-response-0": ANSWER})
        return action_execution.project_rpc_result(
            raw,
            {"cloudflare": {"type": "oauth2-bearer", "access_token": TOKEN}},
            lambda value: value,
            action_execution.RpcResultPolicy(
                protected_values=transcript.protected_values(),
                stored_inputs_by_id={"api-key": STORED},
                capabilities=(CAPABILITY,),
                **policy,
            ),
        )

    def test_a_handled_failure_is_re_redacted_instead_of_refused_as_an_echo(self) -> None:
        envelope = _envelope(message=f"{TOKEN} {STORED} {ANSWER} {CAPABILITY}")
        with self.assertRaises(action_failure.ActionFailedError) as caught:
            self._project(envelope)
        self.assertEqual(caught.exception.failure.message, " ".join(["[REDACTED]"] * 4))
        self.assertTrue(caught.exception.failure.redacted)

    def test_a_malformed_failure_frame_is_an_invalid_result(self) -> None:
        for invalid in (
            {"type": "failure"},
            {"type": "failure", "failure": {}, "extra": True},
            _envelope(stack="Traceback"),
            _envelope(message="red\x1b[31m"),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(action_execution.RpcInvalidResultError) as caught:
                self._project(invalid)
            self.assertIsInstance(caught.exception.__cause__, action_failure.FailureEnvelopeError)

    def test_every_other_branch_still_refuses_an_echoed_secret(self) -> None:
        for raw in (
            {"type": "result", "result": {"value": TOKEN}},
            {"type": "result", "result": {"value": STORED}},
            {"type": "result", "result": {"value": ANSWER}},
            {"type": "stored_input_rejected", "stored_input": STORED},
        ):
            with self.subTest(raw=raw), self.assertRaises(action_execution.RpcSecretExposureError):
                self._project(raw)
        self.assertEqual(self._project({"type": "result", "result": {"value": "public"}}), {"value": "public"})

    def test_an_echoed_egress_capability_is_refused_outside_the_failure_branch(self) -> None:
        request = {
            "kind": "approval",
            "ordinal": 0,
            "fingerprint": "a" * 64,
            "title": {"message": CAPABILITY, "params": {}},
            "description": {"message": "b" * 64, "params": {}},
        }
        for raw in (
            {"type": "result", "result": {"proxy": f"http://{CAPABILITY}@assistant-egress:3128"}},
            {"type": "result", "result": {CAPABILITY: True}},
            {"type": "request", "request": request},
        ):
            with self.subTest(raw=raw), self.assertRaises(action_execution.RpcSecretExposureError):
                self._project(raw, human_requests=("approval",))
        with self.assertRaises(action_failure.ActionFailedError) as caught:
            self._project(_envelope(message=f"proxy {CAPABILITY} refused"))
        self.assertEqual(caught.exception.failure.message, "proxy [REDACTED] refused")

    def test_transport_faults_record_only_the_actual_safe_condition(self) -> None:
        stream = SimpleNamespace(_sock=SimpleNamespace(shutdown=lambda _how: None))
        frame = json.dumps(_envelope()).encode()
        for details, stderr, expected in (
            ({"ExitCode": 1}, b"", "exit-status:1"),
            ({"ExitCode": 0}, b"Traceback: secret", "stderr-output"),
            ({"ExitCode": 3}, b"noise", "exit-status:3"),
            ({"ExitCode": None}, b"", "exit-unavailable"),
        ):
            api = mock.Mock()
            api.exec_create.return_value = {"Id": "exec"}
            api.exec_start.return_value = stream
            api.exec_inspect.return_value = details
            strategy = action_execution.RpcExchangeStrategy(
                api=api,
                user="10001:10001",
                workdir="/workdir",
                timeout=1,
                maximum=1024,
                transport_errors=(RuntimeError,),
                fail_stop=mock.Mock(),
                cancelled=mock.Mock(),
                close_stream=mock.Mock(),
            )
            with (
                self.subTest(expected=expected),
                mock.patch.object(action_execution, "_write_all"),
                mock.patch.object(action_execution, "read_rpc_frames", return_value=(frame, stderr)),
                self.assertRaises(action_execution.RpcExchangeError) as caught,
            ):
                action_execution.rpc_exchange("container", ["command"], b"request", strategy)
            self.assertEqual(caught.exception.condition, expected)
            self.assertNotIn("secret", str(caught.exception))
            # Every ambiguous outcome fail-stops its workload before anything may verify it (ADR-0092).
            strategy.fail_stop.assert_called_once_with()
        for raw in (b"{", b"[]"):
            with self.subTest(raw=raw), self.assertRaises(action_execution.RpcExchangeError) as decoded:
                action_execution.decode_rpc_response(raw)
            self.assertEqual((decoded.exception.kind, decoded.exception.condition), ("invalid-result", "frame-invalid"))
        self.assertEqual(action_execution.RpcExchangeError("timeout").condition, "timeout")
        self.assertIsInstance(action_human.ActionTranscript("interrupt").protected_values(), dict)


if __name__ == "__main__":
    unittest.main()
