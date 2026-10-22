"""Activity events (ADR-0120): a closed set of metadata fields beside the line's principal, never content."""

import unittest
from unittest import mock

from inference import memory as inference_memory
from local import audit as local_audit
from local.browser import sign_in


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
                    "origin": "https://admin.example:8443",
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
                {"event_id": "a" * 32, "phase": "requested", "origin": "https://admin.example/path"},
                {"event_id": "a" * 32, "phase": "requested", "origin": "admin"},
                {"event_id": "a" * 32, "phase": "requested", "origin": "https://" + "a" * 250 + ".example"},
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
        with mock.patch.object(local_audit, "_append", return_value="trace"):
            local_audit.record_activity(
                "browser-sign-in-expire",
                result="ok",
                principal=local_audit.TEAM,
                team_id="neo",
                activity={"event_id": "a" * 32, "phase": "completed", "origin": "team"},
            )

    def test_only_a_closed_operation_is_an_activity_event(self):
        activity = {"event_id": "a" * 32, "phase": "requested", "subject": "sign-in"}
        recorded = []
        with mock.patch.object(local_audit, "_append", side_effect=lambda *args: recorded.append(args[0]) or "trace"):
            for operation in (
                *(sign_in.START, sign_in.SAVE, sign_in.CANCEL, sign_in.DELETE, sign_in.EXPIRE),
                *(inference_memory.REMEMBER, inference_memory.FORGET, inference_memory.EVICTED),
            ):
                local_audit.record_activity(
                    operation, result="ok", principal=local_audit.TEAM, team_id="neo", activity=activity
                )
            for operation in ("browser-sign-in", "chat-memory", "memory-replace", ""):
                with self.subTest(operation=operation), self.assertRaises(ValueError):
                    local_audit.record_activity(
                        operation, result="ok", principal=local_audit.TEAM, team_id="neo", activity=activity
                    )
        self.assertEqual(set(recorded), local_audit.ACTIVITY_OPERATIONS)


if __name__ == "__main__":
    unittest.main()
