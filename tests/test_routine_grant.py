"""A Routine revision keeps the evidence of the request that granted it (ADR-0092)."""

from __future__ import annotations

import copy
import unittest

import routine_fixture

from routine import grant as routine_grant
from routine import request as routine_request

PLAN = {
    "version": 2,
    "timezone": "UTC",
    "steps": [
        {
            "id": "zones",
            "assistant": "dns",
            "action": "list-zones",
            "pin": routine_fixture.PIN,
            "input": {"page": {"kind": "literal", "value": {"n": "a‮b"}}},
        },
        {
            "id": "records",
            "assistant": "dns",
            "action": "list-records",
            "pin": routine_fixture.PIN,
            "input": {
                "zone": {"kind": "step_output", "step": "zones", "pointer": "/zones/0/id"},
                "day": {"kind": "run_clock", "format": "date"},
            },
        },
    ],
    "output": {"mode": "chain", "step": None},
}


class GrantTests(unittest.TestCase):
    def test_a_complete_grant_binds_its_receipt_revision_and_plan(self) -> None:
        first = {"message": "a" * 64, "receipt": "b" * 64, "revision": 1, "selected": "Hoje"}
        sources = {
            "zones": {
                "page": {
                    "proof": {
                        "origins": [
                            {"at": "", "from": "message", "span": [0, 1]},
                            {"at": "/a", "from": "quote", "region": 0, "span": [2, 3], "instruction": [4, 5]},
                            {"at": "/b", "from": "answer"},
                        ]
                    },
                    "by": None,
                }
            },
            # A reference proved now, and a token kept from the first revision with what granted it.
            "records": {"zone": {"proof": {"instruction": [0, 4]}, "by": None}, "day": {"proof": {}, "by": first}},
        }
        # The evidence commits to the Routine's words as the structured request committed them, never one joined text.
        commitment = routine_request.commitment((("said", "Every day, list"),))
        output = {"proof": {"instruction": [0, 4]}, "by": None}
        partial = routine_grant.evidence(
            commitment, (0, 9), sources, {"zones": [], "records": ["token", "api"]}, output
        )
        self.assertEqual(partial["message"], commitment)
        self.assertIsNone(output["by"])
        partial["selected"] = {"field": ["input", "zones", "page"], "label": "Página 1"}
        complete = routine_grant.complete(partial, "e" * 64, 2, PLAN)
        self.assertTrue(routine_grant.valid(complete, PLAN, 2))
        message = complete["message"]
        self.assertEqual(
            complete["sources"]["zones"]["page"]["by"],
            {"message": message, "receipt": "e" * 64, "revision": 2, "selected": "Página 1"},
        )
        self.assertEqual(complete["sources"]["records"]["zone"]["by"]["selected"], None)
        self.assertEqual(complete["sources"]["records"]["day"]["by"], first)
        # The words that chose the output disposition are bound like a proved input, never as a selected answer.
        self.assertEqual(
            complete["output"],
            {"proof": {"instruction": [0, 4]}, "by": {**complete["sources"]["records"]["zone"]["by"]}},
        )
        self.assertEqual(routine_grant.complete({**partial, "sources": []}, "e" * 64, 1, PLAN), {})
        self.assertEqual(complete["stored_inputs"]["records"], ["api", "token"])
        self.assertEqual(routine_grant.complete({"message": "x"}, "e" * 64, 1, PLAN), {})
        self.assertEqual(routine_grant.complete(None, "e" * 64, 1, PLAN), {})
        tampered = (
            lambda value: value.update(extra=1),
            lambda value: value.update(receipt="E" * 64),
            lambda value: value.update(receipt=None),
            lambda value: value.update(revision=3),
            lambda value: value.update(plan="sha256:" + "0" * 64),
            lambda value: value.update(message=None),
            lambda value: value.update(message="x"),
            lambda value: value.update(quote=[3, 3]),
            lambda value: value.update(quote=[0]),
            lambda value: value.update(quote="0,9"),
            lambda value: value.update(quote=[0, "9"]),
            lambda value: value.update(selected={"field": ["schedule"]}),
            lambda value: value.update(selected={"field": [], "label": "x"}),
            lambda value: value.update(selected={"field": "schedule", "label": "x"}),
            lambda value: value.update(selected={"field": [""], "label": "x"}),
            lambda value: value.update(selected={"field": ["schedule"], "label": " x"}),
            lambda value: value.update(selected=[]),
            lambda value: value.update(sources=[]),
            lambda value: value["sources"].pop("records"),
            lambda value: value["sources"].update(records=[]),
            lambda value: value["sources"]["records"].pop("day"),
            lambda value: value["sources"]["records"].update(day=[]),
            lambda value: value.pop("output"),
            lambda value: value.update(output={"proof": {}, "by": first}),
            lambda value: value.update(output={"proof": {"instruction": [0, 4]}, "by": None}),
            lambda value: value.update(output={"proof": {"instruction": [4, 0]}, "by": first}),
            lambda value: value["sources"]["records"].update(day={"origins": [], "instruction": "x"}),
            lambda value: value["sources"]["records"]["zone"].update(proof={"instruction": "then share it"}),
            lambda value: value["sources"]["zones"]["page"].update(proof={"origins": []}),
            lambda value: value["sources"]["zones"]["page"].update(proof={"origins": [{"at": ""}]}),
            lambda value: value["sources"]["zones"]["page"].update(proof={"origins": ["x"]}),
            lambda value: value["sources"]["zones"]["page"].update(proof={"origins": [{"at": 1, "from": "answer"}]}),
            # A schema default never grants a value (ADR-0092 amendment, 2026-10-05, scale).
            lambda value: value["sources"]["zones"]["page"].update(proof={"origins": [{"at": "", "from": "default"}]}),
            lambda value: value["sources"]["zones"]["page"].update(
                proof={"origins": [{"at": "", "from": "message", "span": [0, 1], "text": "API_KEY=x"}]}
            ),
            lambda value: value["sources"]["zones"]["page"].update(
                proof={"origins": [{"at": "", "from": "message", "span": [3, 3]}]}
            ),
            lambda value: value["sources"]["zones"]["page"].update(
                proof={"origins": [{"at": "", "from": "quote", "region": -1, "span": [0, 1], "instruction": [0, 1]}]}
            ),
            lambda value: value["sources"]["zones"]["page"].update(
                proof={"origins": [{"at": "", "from": "quote", "region": 0, "span": [0, 1], "instruction": "x"}]}
            ),
            lambda value: value["sources"]["zones"]["page"].update(
                proof={"origins": [{"at": "", "from": "answer", "x": 1}]}
            ),
            lambda value: value["sources"]["zones"]["page"].update(
                proof={"origins": [{"at": "", "from": "elsewhere"}]}
            ),
            lambda value: value["sources"]["zones"]["page"].update(proof=[]),
            lambda value: value["sources"]["records"]["day"].update(by=None),
            lambda value: value["sources"]["records"]["day"].update(extra=1),
            lambda value: value["sources"]["records"]["day"]["by"].update(extra=1),
            lambda value: value["sources"]["records"]["day"]["by"].update(message="x"),
            lambda value: value["sources"]["records"]["day"]["by"].update(receipt=None),
            lambda value: value["sources"]["records"]["day"]["by"].update(revision=3),
            lambda value: value["sources"]["records"]["day"]["by"].update(revision=0),
            lambda value: value["sources"]["records"]["day"]["by"].update(selected=" padded"),
            # Whatever this revision proved is bound to exactly its own message and receipt.
            lambda value: value["sources"]["zones"]["page"]["by"].update(message="c" * 64),
            lambda value: value["sources"]["zones"]["page"]["by"].update(receipt="c" * 64),
            lambda value: value.update(stored_inputs=[]),
            lambda value: value["stored_inputs"].pop("zones"),
            lambda value: value["stored_inputs"].update(zones="api"),
            lambda value: value["stored_inputs"].update(zones=["Bad"]),
            lambda value: value["stored_inputs"].update(zones=[1]),
            lambda value: value["stored_inputs"].update(zones=["b", "a"]),
            lambda value: value["stored_inputs"].update(zones=["a"] * 9),
        )
        for mutate in tampered:
            changed = copy.deepcopy(complete)
            mutate(changed)
            with self.subTest(mutate=mutate):
                self.assertFalse(routine_grant.valid(changed, PLAN, 2))
        selected = {**complete, "selected": {"field": ["input", "zones", "page"], "label": "Página 1"}}
        self.assertTrue(routine_grant.valid(selected, PLAN, 2))
        self.assertFalse(routine_grant.valid(None, PLAN, 2))


if __name__ == "__main__":
    unittest.main()
