"""A Routine definition on the wire and in its Team's budgets: units, state, scope, and detail (ADR-0101)."""

import dataclasses
import unittest

import routine_fixture

from routine import definition as routine_definition
from routine import plan as routine_plan
from routine import record

PLAN = {
    "version": 4,
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
    "output": {"mode": "none", "step": None},
}


def _defined(**changes: object) -> record.Routine:
    value = routine_fixture.confirmed(
        record.Routine(
            routine_id="a" * 32,
            name="DNS",
            schedule={"kind": "continuous", "gap": 864},
            assistants=(),
            plan=routine_fixture.plan_document(),
            anchor=0,
            next_run_at=0,
        )
    )
    return dataclasses.replace(value, **changes)


class DefinitionTests(unittest.TestCase):
    def test_units_are_replay_steps(self) -> None:
        self.assertEqual(routine_definition.run_units(_defined()), 1)
        many = {
            **routine_fixture.plan_document(),
            "steps": [{**routine_fixture.plan_document()["steps"][0], "id": f"s{index}"} for index in range(17)],
        }
        self.assertEqual(routine_definition.run_units(_defined(plan=many)), 17)
        # Every start may use every unit, as often as the cap allows.
        self.assertEqual(routine_definition.daily_steps(_defined(plan=many)), 100 * 17)
        self.assertEqual(routine_definition.capacity([_defined()]), routine_plan.MAX_DAILY_STEPS - 100)

    def test_a_routine_is_active_or_paused(self) -> None:
        self.assertEqual(routine_definition.state(_defined()), "active")
        self.assertEqual(routine_definition.state(_defined(paused=True)), "paused")

    def test_definition_bytes_count_the_plan_and_every_standing_part(self) -> None:
        value = _defined()
        plain = routine_definition.definition_bytes(value)
        self.assertGreater(plain, len(routine_plan.canonical(value.plan)))
        wider = dataclasses.replace(value, permitted=(*value.permitted, {**value.permitted[0], "action": "other"}))
        self.assertGreater(routine_definition.definition_bytes(wider), plain)

    def test_a_budget_names_the_units_or_the_bytes_it_outgrows(self) -> None:
        busy = _defined(routine_id="b" * 32, schedule={"kind": "continuous", "gap": 5})
        every_thirty = {"kind": "continuous", "gap": 30}
        self.assertEqual(routine_definition.over_budget([busy], _defined(schedule=every_thirty)), "routine-step-budget")
        self.assertIsNone(routine_definition.over_budget([busy], _defined()))
        heavy = {
            **routine_fixture.plan_document(),
            "steps": [{**routine_fixture.plan_document()["steps"][0], "id": f"s{index}"} for index in range(201)],
        }
        heavy["output"] = {"mode": "none", "step": None}
        self.assertEqual(routine_definition.over_budget([], _defined(plan=heavy)), "routine-step-budget")
        self.assertIsNone(routine_definition.over_budget([], _defined()))


if __name__ == "__main__":
    unittest.main()
