"""What a Supervisor sees of a Routine revision's plan: positions, summary, and pages (ADR-0092, 2026-10-05, scale)."""

from __future__ import annotations

import unittest

import routine_fixture
from test_routine_definition import PLAN

from protocol.http.v1 import routine as http_routine
from protocol.http.v1 import routine_notice as http_routine_notice
from routine import definition as routine_definition


def _many(count: int, actions: tuple[str, ...] = ("check",)) -> dict[str, object]:
    """A plan of ``count`` steps cycling through ``actions``, each step referring to the one before it."""
    steps = []
    for index in range(count):
        inputs = {"zone": {"kind": "literal", "value": f"zone-{index}.example"}}
        if index:
            inputs["after"] = {"kind": "step_output", "step": f"s{index - 1}", "pointer": "/id"}
        action = actions[index % len(actions)]
        steps.append(
            {"id": f"s{index}", "assistant": "dns", "action": action, "pin": routine_fixture.PIN, "input": inputs}
        )
    output = {"mode": "show", "step": f"s{count - 1}", "when": None}
    return {"version": 3, "timezone": "UTC", "steps": steps, "output": output}


class ProjectionTests(unittest.TestCase):
    def test_a_step_shows_its_sources_by_position_and_its_stored_inputs_by_name_only(self) -> None:
        permitted = tuple(
            {**item, "stored_inputs": ["api"]} if item["action"] == "list-records" else item
            for item in routine_fixture.permitted(PLAN)
        )
        projected = [routine_definition.step(PLAN, permitted, position) for position in (1, 2)]
        self.assertEqual(
            projected,
            [
                {
                    "position": 1,
                    "assistant": "dns",
                    "action": "list-zones",
                    "read_only": True,
                    "inputs": [{"member": "page", "source": "literal", "value": r'{"n":"a\u202eb"}'}],
                    "stored_inputs": [],
                },
                {
                    "position": 2,
                    "assistant": "dns",
                    "action": "list-records",
                    "read_only": True,
                    "inputs": [
                        {"member": "day", "source": "run_clock", "value": "date"},
                        {
                            "member": "named",
                            "source": "step_output",
                            "step": 1,
                            "pointer": "/zones",
                            "where": {"member": "name", "value_json": r'"a\u202eb.com"'},
                            "item": "/id",
                        },
                        {
                            "member": "zone",
                            "source": "step_output",
                            "step": 1,
                            "pointer": "/zones/0/id",
                            "where": None,
                            "item": None,
                        },
                    ],
                    "stored_inputs": ["api"],
                },
            ],
        )
        for position, step in enumerate(projected, start=1):
            self.assertEqual(http_routine.canonical_step(step, position), step)
        self.assertTrue(routine_definition.steps_fit(PLAN, permitted))
        self.assertEqual(routine_definition.disposition(PLAN), {"mode": "none", "step": None, "when": None})

    def test_a_step_whose_projection_outgrows_its_bound_does_not_fit(self) -> None:
        plan = _many(1)
        plan["steps"][0]["input"] = {
            f"m{index:02d}": {"kind": "step_output", "step": "s0", "pointer": "/" + "p" * 250} for index in range(64)
        }
        self.assertFalse(routine_definition.steps_fit(plan, routine_fixture.permitted(plan)))


class SummaryTests(unittest.TestCase):
    def test_runs_of_one_repeated_action_summarize_a_long_plan(self) -> None:
        plan = _many(120)
        plan["steps"][-1]["action"] = "notify"
        summary = routine_definition.summary(plan, 3)
        self.assertEqual(
            {key: summary[key] for key in ("revision", "steps", "actions", "more")},
            {"revision": 3, "steps": 120, "actions": [["dns", "check", 119], ["dns", "notify", 1]], "more": 0},
        )
        self.assertEqual(http_routine.canonical_summary(summary), summary)
        self.assertEqual(routine_definition.disposition(plan), {"mode": "show", "step": 120, "when": None})

    def test_beyond_sixteen_runs_the_rest_is_counted(self) -> None:
        summary = routine_definition.summary(_many(256, ("check", "notify")), 1)
        self.assertEqual((len(summary["actions"]), summary["more"]), (16, 240))
        self.assertLessEqual(http_routine.encoded_bytes(summary), http_routine_notice.MAX_SUMMARY_BYTES)
        self.assertEqual(http_routine.canonical_summary(summary), summary)


class PageTests(unittest.TestCase):
    def test_pages_cover_every_step_once_in_order_within_their_bounds(self) -> None:
        plan = _many(256)
        permitted = routine_fixture.permitted(plan)
        positions, offset, pages = [], 0, 0
        while offset is not None:
            page = routine_definition.page("a" * 32, 4, plan, permitted, offset)
            self.assertEqual(http_routine.canonical_page(page), page)
            self.assertEqual((page["revision"], page["total"], page["offset"]), (4, 256, offset))
            positions.extend(step["position"] for step in page["steps"])
            offset, pages = page["next"], pages + 1
        self.assertEqual((positions, pages), (list(range(1, 257)), 4))

    def test_a_page_of_large_steps_stops_at_its_byte_bound(self) -> None:
        plan = _many(20)
        for step in plan["steps"]:
            step["input"]["zone"]["value"] = "z" * 4000
            step["input"].update({f"x{index}": {"kind": "literal", "value": "y" * 4000} for index in range(40)})
        permitted = routine_fixture.permitted(plan)
        page = routine_definition.page("a" * 32, 1, plan, permitted, 0)
        self.assertEqual(http_routine.canonical_page(page), page)
        self.assertLess(len(page["steps"]), 20)
        self.assertLessEqual(http_routine.encoded_bytes(page["steps"]), http_routine.MAX_PAGE_BYTES)


if __name__ == "__main__":
    unittest.main()
