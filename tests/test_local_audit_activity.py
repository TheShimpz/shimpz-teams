"""Activity events (ADR-0120): a closed set of metadata fields beside the line's principal, never content."""

import unittest
from unittest import mock

from local import audit as local_audit


class ActivityEventTests(unittest.TestCase):
    def test_an_activity_event_keeps_its_closed_fields_and_refuses_anything_else(self):
        appended = []
        with mock.patch.object(local_audit, "_append", side_effect=lambda *args: appended.append(args) or "trace"):
            local_audit.record_activity(
                "browser-sign-in-delete",
                result="ok",
                principal=local_audit.TEAM,
                team_id="neo",
                activity={
                    "event_id": "a" * 32,
                    "phase": "failed",
                    "subject": "sign-in",
                    "generation": 2,
                    "incarnation": "c" * 64,
                    "factor": "passkey",
                    "initiator_id": "b" * 32,
                    "origin": "supervisor-session",
                    "outcome": "sign-in-changed",
                },
            )
            for activity in (
                {"phase": "requested"},
                {"event_id": "a" * 32, "phase": "done"},
                {"event_id": "a" * 32, "phase": "requested", "cookie": "x"},
                {"event_id": "a" * 32, "phase": "requested", "subject": "Has Space"},
                {"event_id": "a" * 32, "phase": "requested", "generation": -1},
                {"event_id": "a" * 32, "phase": "requested", "generation": True},
                "not a mapping",
            ):
                with self.subTest(activity=activity), self.assertRaises(ValueError):
                    local_audit.record_activity(
                        "browser-sign-in-delete",
                        result="ok",
                        principal=local_audit.TEAM,
                        team_id="neo",
                        activity=activity,
                    )
        self.assertEqual(appended[0][3]["activity"]["generation"], 2)


if __name__ == "__main__":
    unittest.main()
