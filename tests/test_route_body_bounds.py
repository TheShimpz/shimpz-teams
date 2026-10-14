"""The largest capability plan and intent route Team admits fit the body bounds Team and Admin both apply."""

import json
import unittest

from local.chat import capabilities
from local.http import chat as local_http_chat
from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import supervisor as supervisor_contract
from protocol.http.v1 import turn as http_turn

# A code point an admitted text may hold whose JSON is longest in Admin's compact UTF-8 encoding.
WIDE = "\U0001f600"


def _text(length: int) -> str:
    return WIDE * length


def _serialized(body: dict[str, object]) -> int:
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def _assistant_id(index: int) -> str:
    return f"{chr(ord('a') + index)}" * http_payload.MAX_ASSISTANT_ID_CHARS


class RouteBodyBoundTests(unittest.TestCase):
    def test_the_largest_admitted_capability_plan_fits_its_bound(self) -> None:
        body = {
            "objective": _text(http_turn.MAX_OBJECTIVE_CHARS),
            "candidates": [
                {
                    "id": _assistant_id(index),
                    "name": _text(http_turn.MAX_CAPABILITY_NAME_CHARS),
                    "summary": _text(http_turn.MAX_CAPABILITY_SUMMARY_CHARS),
                    "actions": sorted(
                        f"{action:03d}".rjust(http_payload.MAX_ACTION_ID_CHARS, "a")
                        for action in range(http_turn.MAX_CAPABILITY_ACTIONS)
                    ),
                    "integrations": [
                        {"id": f"{item:02d}".rjust(64, "i"), "provider": f"{item:02d}".rjust(64, "p")}
                        for item in range(http_turn.MAX_CAPABILITY_INTEGRATIONS)
                    ],
                }
                for index in range(http_turn.MAX_CAPABILITY_CANDIDATES)
            ],
        }
        capabilities._capability_plan_input(body)
        self.assertLessEqual(_serialized(body), local_http_chat.MAX_CAPABILITY_PLAN_BODY_BYTES)
        self.assertEqual(local_http_chat.MAX_CAPABILITY_PLAN_BODY_BYTES, http_turn.MAX_CAPABILITY_PLAN_BODY_BYTES)

    def test_the_largest_admitted_intent_route_fits_its_bound(self) -> None:
        entries = http_payload.MAX_CONVERSATION_ENTRIES
        per_entry = min(http_payload.MAX_CONVERSATION_TEXT_CHARS, http_payload.MAX_CONVERSATION_CHARS // entries)
        objective = _text(http_turn.MAX_OBJECTIVE_CHARS)
        # Classification carries the conversation, the last lifecycle Assistant, and the installable Assistants;
        # selection carries candidates.
        classification = {
            "objective": objective,
            "expected_intent": None,
            "candidates": [],
            "lifecycle_reference": {"id": _assistant_id(9), "name": _text(http_turn.MAX_INTENT_ROUTE_NAME_CHARS)},
            "conversation": [
                {"role": "user" if index % 2 == 0 else "assistant", "text": _text(per_entry), "truncated": True}
                for index in range(entries)
            ],
            "locale": "pt",
            "installable": [
                {
                    "name": _text(http_turn.MAX_CAPABILITY_NAME_CHARS),
                    "summary": _text(http_turn.MAX_CAPABILITY_SUMMARY_CHARS),
                }
            ]
            * http_turn.MAX_CAPABILITY_CANDIDATES,
        }
        selection = {
            "objective": objective,
            "expected_intent": "assistant-install",
            "candidates": [
                {
                    "id": _assistant_id(index),
                    "name": _text(http_turn.MAX_INTENT_ROUTE_NAME_CHARS),
                    "summary": _text(http_turn.MAX_INTENT_ROUTE_SUMMARY_CHARS),
                }
                for index in range(http_turn.MAX_INTENT_ROUTE_CANDIDATES)
            ],
            "lifecycle_reference": None,
            "conversation": [],
            "locale": "pt",
        }
        for body in (classification, selection):
            with self.subTest(expected_intent=body["expected_intent"]):
                capabilities._intent_route_input(body)
                self.assertLessEqual(_serialized(body), local_http_chat.MAX_INTENT_ROUTE_BODY_BYTES)
        self.assertEqual(local_http_chat.MAX_INTENT_ROUTE_BODY_BYTES, http_turn.MAX_INTENT_ROUTE_BODY_BYTES)

    def test_an_assertion_binds_every_body_an_operation_admits(self) -> None:
        self.assertLessEqual(max(local_http_chat.BODY_LIMITS.values()), supervisor_contract.MAX_JSON_BODY_BYTES)
        self.assertEqual(supervisor_contract.MAX_JSON_BODY_BYTES, http_turn.MAX_CAPABILITY_PLAN_BODY_BYTES)


if __name__ == "__main__":
    unittest.main()
