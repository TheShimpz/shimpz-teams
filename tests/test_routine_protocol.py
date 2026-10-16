"""The Recorded Routine wire forms Admin admits from Team, at their edges (ADR-0101)."""

import json
import unittest
from pathlib import Path

from protocol.http.v1 import payload, routine, routine_context, routine_notice, routine_proposal, routine_run

VECTORS = json.loads((Path(__file__).resolve().parents[1] / "protocol/http/v1/vectors.json").read_bytes())
CARD = VECTORS["routine_proposal"]["valid"][0]
LISTED = VECTORS["routine_listing"]["valid"][1][0]


class RoutineProtocolEdgeTests(unittest.TestCase):
    def test_schedules_name_their_cap_and_active_time(self) -> None:
        self.assertEqual(routine.daily_cap({"kind": "continuous", "gap": 25200}), 4)
        self.assertEqual(routine_run.active_seconds(8), routine_run.SHORT_ACTIVE_SECONDS)
        self.assertEqual(routine_run.active_seconds(320), routine_run.MAX_ACTIVE_SECONDS)

    def test_closed_forms_refuse_anything_but_their_own_shape(self) -> None:
        self.assertIsNone(routine.canonical_output([]))
        self.assertIsNone(routine_notice.canonical_notice_detail(None, {}))
        self.assertIsNone(routine_notice.canonical_notice_detail("deleted", []))
        self.assertFalse(routine_proposal._card_input([], 1))
        self.assertFalse(routine_proposal._card_input({**CARD["steps"][1]["inputs"][0], "origin": "guess"}, 2))
        self.assertFalse(routine_proposal._card_input({**CARD["steps"][1]["inputs"][0], "member": "a\u2028b"}, 2))
        self.assertIsNone(routine_proposal.canonical_proposal({**CARD, "steps": []}))

    def test_a_listing_refuses_an_output_mode_of_any_other_type(self) -> None:
        for mode in ([], {}, None, 1):
            with self.subTest(mode=mode):
                self.assertIsNone(routine_context.canonical_routine_listings([{**LISTED, "output": {"mode": mode}}]))

    def test_a_listing_bounds_each_routines_encoded_steps_and_their_sum(self) -> None:
        step = LISTED["steps"][0]
        names = [f"{name:03d}" + "a" * 124 for name in range(routine.MAX_STEP_INPUTS)]

        def listed(index: int, steps: int) -> dict:
            return {
                **LISTED,
                "routine_id": f"{index:032x}",
                "steps": [{**step, "id": f"s{number}", "inputs": names} for number in range(steps)],
            }

        def encoded(value: dict) -> int:
            return len(json.dumps(value["steps"], ensure_ascii=False, separators=(",", ":")).encode())

        fit = 1
        while encoded(listed(0, fit + 1)) <= routine_context.MAX_LISTED_STEPS_BYTES:
            fit += 1
        self.assertIsNotNone(routine_context.canonical_routine_listings([listed(0, fit)]))
        self.assertIsNone(routine_context.canonical_routine_listings([listed(0, fit + 1)]))
        many = routine_context.MAX_LISTING_STEPS_BYTES // encoded(listed(0, fit)) + 1
        self.assertIsNone(routine_context.canonical_routine_listings([listed(index, fit) for index in range(many)]))
        self.assertIsNotNone(
            routine_context.canonical_routine_listings([listed(index, fit) for index in range(many - 1)])
        )

    def test_a_request_identity_changes_a_routine_only_while_fresh(self) -> None:
        now = 2_000_000_000
        self.assertTrue(payload.request_identity_fresh(now, now))
        self.assertTrue(payload.request_identity_fresh(now + payload.REQUEST_IDENTITY_SKEW_SECONDS, now))
        self.assertFalse(payload.request_identity_fresh(now - payload.REQUEST_IDENTITY_SECONDS, now))
        self.assertFalse(payload.request_identity_fresh(now + payload.REQUEST_IDENTITY_SKEW_SECONDS + 1, now))


if __name__ == "__main__":
    unittest.main()
