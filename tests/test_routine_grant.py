"""A Routine revision keeps the evidence of the request that granted it, and shows its plan safely (ADR-0092)."""

from __future__ import annotations

import copy
import unittest

import routine_fixture

from routine import grant as routine_grant

PLAN = {
    "version": 1,
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
}


class GrantTests(unittest.TestCase):
    def test_a_complete_grant_binds_its_receipt_revision_and_plan(self) -> None:
        sources = {
            "zones": {
                "page": {"origins": [{"at": "", "from": "message", "text": "1", "region": None, "instruction": None}]}
            },
            "records": {"zone": {"instruction": "then"}, "day": {}},
        }
        partial = routine_grant.evidence("Every day, list", (0, 9), sources, {"zones": [], "records": ["token", "api"]})
        complete = routine_grant.complete(partial, "e" * 64, 2, PLAN)
        self.assertTrue(routine_grant.valid(complete, PLAN, 2))
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
            lambda value: value["sources"]["records"].update(day={"origins": [], "instruction": "x"}),
            lambda value: value["sources"]["records"].update(zone={"instruction": ""}),
            lambda value: value["sources"]["zones"].update(page={"origins": []}),
            lambda value: value["sources"]["zones"].update(page={"origins": [{"at": ""}]}),
            lambda value: value["sources"]["zones"].update(page={"origins": ["x"]}),
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

    def test_the_projection_shows_each_step_its_sources_and_stored_inputs_by_name_only(self) -> None:
        grant = routine_fixture.grant(PLAN)
        grant["stored_inputs"]["records"] = ["api"]
        self.assertEqual(
            routine_grant.steps(PLAN, grant),
            [
                {
                    "id": "zones",
                    "assistant": "dns",
                    "action": "list-zones",
                    "inputs": [{"member": "page", "source": "literal", "value": r'{"n":"a\u202eb"}'}],
                    "stored_inputs": [],
                },
                {
                    "id": "records",
                    "assistant": "dns",
                    "action": "list-records",
                    "inputs": [
                        {"member": "day", "source": "run_clock", "value": "date"},
                        {"member": "zone", "source": "step_output", "step": "zones", "pointer": "/zones/0/id"},
                    ],
                    "stored_inputs": ["api"],
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
