"""Hosted hydration of a chat segment's selected files (ADR-0093)."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hosted_assistant_fixture as harness

from prepare import service as preparation

# The harness loads the Hosted app with its Docker and state stubs; use the modules that app loaded.
hosted_attachments = harness.hosted_chat_segment.hosted_attachments
state = harness.runtime_state
FILES = [{"id": "a" * 32, "name": "notes.txt", "media_type": "text/plain", "size": 5, "sha256": "f" * 64}]


class HostedTurnAttachmentTests(unittest.TestCase):
    def setUp(self) -> None:
        storage = SimpleNamespace(get=lambda _team_id, _file_id: ({"sha256": "f" * 64, "size": 5}, b"notes"))
        patcher = mock.patch.object(state, "_storage", lambda: storage)
        patcher.start()
        self.addCleanup(patcher.stop)
        state._active_chat_tokens["team_1"] = "token"
        self.addCleanup(state._active_chat_tokens.pop, "team_1", None)

    def test_text_is_prepared_and_a_helper_holds_the_stop_slot(self) -> None:
        self.assertEqual(hosted_attachments.turn_attachments("team_1", "token", "account_1", []), ())
        attachments = hosted_attachments.turn_attachments("team_1", "token", "account_1", FILES)
        self.assertEqual(attachments[0]["content"], {"type": "text", "text": "notes", "pdf": False})
        helper = SimpleNamespace(id="helper-1")
        seen: list[object] = []

        def prepare(_files: object, *, started, stopped, **_kwargs: object) -> tuple[preparation.Attachment, ...]:
            started(helper)
            seen.append(dict(state._active_action_container_ids))
            stopped(helper)
            return ()

        with mock.patch.object(hosted_attachments.hosted_prepare, "prepare_attachments", side_effect=prepare):
            hosted_attachments.turn_attachments("team_1", "token", "account_1", FILES)
        self.assertEqual(seen, [{"team_1": ("token", "helper-1")}])
        self.assertNotIn("team_1", state._active_action_container_ids)

    def test_every_preparation_failure_is_explicit(self) -> None:
        for error, status in (
            (hosted_attachments.preparation.AttachmentLimitError("attachments-too-much-text"), 422),
            (hosted_attachments.preparation_helper.HelperUnavailableError("down"), 503),
            (hosted_attachments.chat_attachments.AttachmentIntegrityError("x"), 409),
            (hosted_attachments.team_storage.StorageNotFoundError("gone"), 404),
            (hosted_attachments.team_storage.StorageError("broken"), 503),
        ):
            with (
                self.subTest(error=error),
                mock.patch.object(hosted_attachments.hosted_prepare, "prepare_attachments", side_effect=error),
                self.assertRaises(state.ApiError) as raised,
            ):
                hosted_attachments.turn_attachments("team_1", "token", "account_1", FILES)
            self.assertEqual(int(raised.exception.status), status)
        state._cancelled_chat_tokens.add("token")
        self.addCleanup(state._cancelled_chat_tokens.discard, "token")
        with (
            mock.patch.object(hosted_attachments.hosted_prepare, "prepare_attachments", return_value=()),
            self.assertRaises(state.ApiError),
        ):
            hosted_attachments.turn_attachments("team_1", "token", "account_1", FILES)

    def test_a_stopped_turn_waiting_for_a_preparation_slot_is_refused_before_any_file_is_read(self) -> None:
        reads: list[str] = []
        storage = SimpleNamespace(
            get=lambda _team_id, file_id: reads.append(file_id) or ({"sha256": "f" * 64, "size": 5}, b"notes")
        )
        busy = threading.BoundedSemaphore(1)
        busy.acquire()
        self.addCleanup(busy.release)
        state._cancelled_chat_tokens.add("token")
        self.addCleanup(state._cancelled_chat_tokens.discard, "token")
        with (
            mock.patch.object(state, "_storage", lambda: storage),
            mock.patch.object(hosted_attachments.hosted_prepare, "_SLOTS", busy),
            self.assertRaises(state.ApiError) as raised,
        ):
            hosted_attachments.turn_attachments("team_1", "token", "account_1", FILES)
        self.assertEqual(int(raised.exception.status), 409)
        self.assertEqual(reads, [])


if __name__ == "__main__":
    unittest.main()
