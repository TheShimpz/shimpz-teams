"""A Local chat request carries one identity that binds any Routine change it makes (ADR-0092)."""

from __future__ import annotations

import tempfile
import time
import unittest

from local_controller_harness import LocalContractCase

from inference import client as brain_runtime_client
from local import app as local_app
from local import audit as local_audit
from local.chat import segment as local_chat_segment
from protocol.http.v1 import payload as http_payload
from routine.request import Request

PRINCIPAL = "a" * 32
NONCE = "b" * 32


def _body(**changes: object) -> dict[str, object]:
    body = {
        "message": "Every Monday at 9:00, list my DNS zones",
        "files": [],
        "assistant_ids": [],
        "conversation": [],
        "locale": "pt",
        "request": {"issued_at": int(time.time()), "nonce": NONCE},
        "timezone": "America/Sao_Paulo",
    }
    return {**body, **changes}


class Runtime:
    def __init__(self) -> None:
        self.contexts: list[brain_runtime_client.RuntimeContext] = []

    def start(self, context, _message, *, conversation=()):
        self.contexts.append(context)
        return brain_runtime_client.RuntimeTurn(status="completed", reply="Done.", actions=())


class RequestIdentityTests(unittest.TestCase):
    def test_an_identity_is_fresh_only_inside_its_window(self) -> None:
        request = Request(PRINCIPAL, "hello", 1_000_000, NONCE)
        self.assertTrue(request.fresh(1_000_000))
        # Exclusive at its end: fresh through 899 s, expired from the second its receipt stops being live.
        self.assertTrue(request.fresh(1_000_899))
        self.assertFalse(request.fresh(1_000_900))
        self.assertFalse(request.fresh(1_000_901))
        self.assertTrue(request.fresh(1_000_000 - http_payload.REQUEST_IDENTITY_SKEW_SECONDS))
        self.assertFalse(request.fresh(999_999 - http_payload.REQUEST_IDENTITY_SKEW_SECONDS))
        self.assertEqual(request.expires_at, 1_000_000 + http_payload.REQUEST_IDENTITY_SECONDS)

    def test_a_receipt_binds_principal_incarnation_message_and_nonce_but_not_the_issue_instant(self) -> None:
        request = Request(PRINCIPAL, "hello", 1_000_000, NONCE)
        receipt = request.receipt("c" * 64)
        self.assertRegex(receipt, r"\A[0-9a-f]{64}\Z")
        self.assertEqual(Request(PRINCIPAL, "hello", 1_000_500, NONCE, "UTC", "pt").receipt("c" * 64), receipt)
        for other in (
            Request("d" * 32, "hello", 1_000_000, NONCE).receipt("c" * 64),
            Request(PRINCIPAL, "hello!", 1_000_000, NONCE).receipt("c" * 64),
            Request(PRINCIPAL, "hello", 1_000_000, "e" * 32).receipt("c" * 64),
            request.receipt("f" * 64),
        ):
            self.assertNotEqual(other, receipt)


class LocalChatRequestTests(LocalContractCase):
    def chat(self, body: dict[str, object], *, principal: str | None = PRINCIPAL) -> Runtime:
        runtime = Runtime()
        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, runtime)
            if principal is None:
                controller.chat_turn_service.chat("team_1", body, "openai", "sk-test-0123456789")
            else:
                with local_audit.bind_request_principal(local_audit.AuditPrincipal(principal, "human")):
                    controller.chat_turn_service.chat("team_1", body, "openai", "sk-test-0123456789")
        return runtime

    def test_a_fresh_human_request_without_files_sees_the_teams_routines(self) -> None:
        self.assertEqual(self.chat(_body()).contexts[0].routines, ())

    def test_a_stale_machine_or_future_request_withholds_the_routine_tool(self) -> None:
        now = int(time.time())
        stale = {"issued_at": now - http_payload.REQUEST_IDENTITY_SECONDS - 5, "nonce": NONCE}
        future = {"issued_at": now + http_payload.REQUEST_IDENTITY_SKEW_SECONDS + 30, "nonce": NONCE}
        self.assertIsNone(self.chat(_body(request=stale)).contexts[0].routines)
        self.assertIsNone(self.chat(_body(request=future)).contexts[0].routines)
        self.assertIsNone(self.chat(_body(), principal=None).contexts[0].routines)
        with local_audit.bind_request_principal(local_audit.AuditPrincipal("admin", "machine")):
            self.assertIsNone(local_audit.human_principal())

    def test_an_invalid_identity_body_or_timezone_is_refused_before_any_turn(self) -> None:
        for body, code in (
            ({key: value for key, value in _body().items() if key != "timezone"}, "invalid-body"),
            (_body(request={"issued_at": 1, "nonce": "B" * 32}), "invalid-request"),
            (_body(request=None), "invalid-request"),
            (_body(request={"issued_at": 0, "nonce": NONCE}), "invalid-request"),
            (_body(timezone="Mars/Olympus_Mons"), "invalid-timezone"),
            (_body(timezone="../etc/passwd"), "invalid-timezone"),
        ):
            with self.subTest(code=code, body=body), self.assertRaises(local_app.ApiProblem) as caught:
                self.chat(body)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.status, 422)

    def test_a_null_timezone_is_admitted(self) -> None:
        self.assertEqual(self.chat(_body(timezone=None)).contexts[0].routines, ())

    def test_a_turn_with_files_or_a_routine_run_never_changes_a_routine(self) -> None:
        request = Request(PRINCIPAL, "hello", int(time.time()), NONCE)

        def segment(**changes: object) -> local_chat_segment.SegmentRequest:
            fields = {
                "team_id": "team_1",
                "file_ids": [],
                "assistant_ids": (),
                "provider": "openai",
                "api_key": "sk-test-0123456789",
                "token": "t",
                "routine_request": request,
            }
            return local_chat_segment.SegmentRequest(**{**fields, **changes})

        self.assertTrue(local_chat_segment._routine_mutable(segment()))
        self.assertFalse(local_chat_segment._routine_mutable(segment(file_ids=["0" * 32])))
        self.assertFalse(local_chat_segment._routine_mutable(segment(routine_request=None)))
        routine = local_chat_segment.RoutineSegment("f" * 32, "g", object())
        self.assertFalse(local_chat_segment._routine_mutable(segment(routine=routine)))
