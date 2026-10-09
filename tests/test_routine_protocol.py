"""The Recorded Routine wire forms Admin admits from Team, at their edges (ADR-0101)."""

import json
import unittest
from pathlib import Path

from protocol.http.v1 import payload, routine, routine_notice, routine_proposal, routine_run

VECTORS = json.loads((Path(__file__).resolve().parents[1] / "protocol/http/v1/vectors.json").read_bytes())
CARD = VECTORS["routine_proposal"]["valid"][0]


class RoutineProtocolEdgeTests(unittest.TestCase):
    def test_schedules_name_their_cap_mode_and_active_time(self) -> None:
        self.assertEqual(routine.daily_cap({"kind": "hourly", "every": 7}), 4)
        self.assertEqual(routine_run.run_mode({"kind": "continuous", "gap": 30, "cap": 1000}), "continuous")
        self.assertEqual(routine_run.run_mode({"kind": "daily", "time": "09:00"}), "scheduled")
        self.assertEqual(routine_run.active_seconds(8), routine_run.SHORT_ACTIVE_SECONDS)
        self.assertEqual(routine_run.active_seconds(320), routine_run.MAX_ACTIVE_SECONDS)

    def test_closed_forms_refuse_anything_but_their_own_shape(self) -> None:
        self.assertIsNone(routine.canonical_output([]))
        self.assertIsNone(routine_notice.canonical_notice_detail(None, {}))
        self.assertIsNone(routine_notice.canonical_notice_detail("deleted", []))
        self.assertFalse(routine_proposal._card_input([], 1))
        self.assertFalse(routine_proposal._card_input({**CARD["steps"][1]["inputs"][0], "origin": "guess"}, 2))
        self.assertFalse(routine_proposal._card_input({**CARD["steps"][1]["inputs"][0], "member": "a\u2028b"}, 2))
        self.assertFalse(routine_proposal._card_permitted({}))
        self.assertFalse(routine_proposal._card_permitted([CARD["permitted"][0]] * (routine.MAX_PERMITTED + 1)))
        self.assertFalse(routine_proposal._card_permitted([{**CARD["permitted"][0], "read_only": 1}]))

    def test_a_request_identity_changes_a_routine_only_while_fresh(self) -> None:
        now = 2_000_000_000
        self.assertTrue(payload.request_identity_fresh(now, now))
        self.assertTrue(payload.request_identity_fresh(now + payload.REQUEST_IDENTITY_SKEW_SECONDS, now))
        self.assertFalse(payload.request_identity_fresh(now - payload.REQUEST_IDENTITY_SECONDS, now))
        self.assertFalse(payload.request_identity_fresh(now + payload.REQUEST_IDENTITY_SKEW_SECONDS + 1, now))


if __name__ == "__main__":
    unittest.main()
