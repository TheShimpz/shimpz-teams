"""A Routine definition on the wire and in its Team's budgets: units, state, scope, and detail (ADR-0101)."""

from __future__ import annotations

import dataclasses
import unittest

import routine_fixture

from protocol.http.v1 import routine_notice as http_routine_notice
from routine import definition as routine_definition
from routine import plan as routine_plan
from routine import record

PLAN = {
    "version": 3,
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
                "named": {
                    "kind": "step_output",
                    "step": "zones",
                    "pointer": "/zones",
                    "where": {"name": "a‮b.com"},
                    "item": "/id",
                },
                "day": {"kind": "run_clock", "format": "date"},
            },
        },
    ],
    "output": {"mode": "none", "step": None, "when": None},
}
MODEL = {"provider": "openai", "model": "gpt-6-luna", "effort": "low"}


def _defined(**changes: object) -> record.Routine:
    value = routine_fixture.confirmed(
        record.Routine(
            routine_id="a" * 32,
            name="DNS",
            schedule={"kind": "continuous", "gap": 864, "cap": 100},
            timezone="UTC",
            assistants=(),
            plan=routine_fixture.plan_document(),
            anchor=0,
            next_run_at=0,
        )
    )
    return dataclasses.replace(value, **changes)


class DefinitionTests(unittest.TestCase):
    def test_units_are_replay_steps_and_the_decision_allowance(self) -> None:
        self.assertEqual(routine_definition.run_units(_defined()), 1)
        decide = {**routine_fixture.plan_document(), "output": {"mode": "decide", "step": None, "when": "always"}}
        self.assertEqual(routine_definition.run_units(_defined(plan=decide, allowance=16, model=MODEL)), 17)
        # Every start may use every unit, as often as the cap allows.
        self.assertEqual(routine_definition.daily_steps(_defined(plan=decide, allowance=16)), 100 * 17)
        self.assertEqual(routine_definition.capacity([_defined()]), routine_plan.MAX_DAILY_STEPS - 100)

    def test_a_routine_is_active_or_paused(self) -> None:
        self.assertEqual(routine_definition.state(_defined()), "active")
        self.assertEqual(routine_definition.state(_defined(paused=True)), "paused")

    def test_views_and_notices_carry_the_standing_scope_as_a_summary(self) -> None:
        changing = (
            *routine_fixture.permitted(PLAN),
            {**routine_fixture.permitted(PLAN)[0], "action": "delete-record", "read_only": False},
        )
        self.assertEqual(routine_definition.permitted_summary(changing), {"total": 3, "changes": 1})
        value = _defined()
        self.assertEqual(
            routine_definition.scope(value),
            {"state": "active", "permitted": {"total": 1, "changes": 0}, "model": None, "allowance": 0},
        )
        detail = routine_definition.detail(value)
        self.assertEqual(detail["output"], {"mode": "show", "step": 1, "when": None})
        self.assertEqual(http_routine_notice.canonical_notice_detail("created", detail), detail)
        decide = {**routine_fixture.plan_document(), "output": {"mode": "decide", "step": None, "when": "changes"}}
        decided = routine_definition.scope(_defined(plan=decide, model=MODEL, allowance=8))
        self.assertEqual((decided["model"], decided["allowance"]), (MODEL, 8))

    def test_definition_bytes_count_the_plan_and_every_standing_part(self) -> None:
        value = _defined()
        plain = routine_definition.definition_bytes(value)
        self.assertGreater(plain, len(routine_plan.canonical(value.plan)))
        self.assertGreater(routine_definition.definition_bytes(dataclasses.replace(value, model=MODEL)), plain)

    def test_a_baseline_never_changes_a_counted_definition(self) -> None:
        # Its room is reserved at its largest, so a full Team can still record a baseline.
        value = _defined()
        plain = routine_definition.definition_bytes(value)
        baseline = {"id": "b" * 32, "digest": "c" * 64}
        self.assertEqual(routine_definition.definition_bytes(dataclasses.replace(value, baseline=baseline)), plain)

    def test_a_budget_names_the_units_or_the_bytes_it_outgrows(self) -> None:
        busy = _defined(routine_id="b" * 32, schedule={"kind": "continuous", "gap": 5, "cap": 17_280})
        every_thirty = {"kind": "continuous", "gap": 30, "cap": 2880}
        self.assertEqual(routine_definition.over_budget([busy], _defined(schedule=every_thirty)), "routine-step-budget")
        self.assertIsNone(routine_definition.over_budget([busy], _defined()))
        heavy = {
            **routine_fixture.plan_document(),
            "steps": [{**routine_fixture.plan_document()["steps"][0], "id": f"s{index}"} for index in range(201)],
        }
        heavy["output"] = {"mode": "none", "step": None, "when": None}
        self.assertEqual(routine_definition.over_budget([], _defined(plan=heavy)), "routine-step-budget")
        self.assertIsNone(routine_definition.over_budget([], _defined()))


if __name__ == "__main__":
    unittest.main()
