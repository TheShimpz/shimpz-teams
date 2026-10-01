"""Hosted ordinary and streamed chat share one fresh-turn admission under the exclusive chat slot."""

from __future__ import annotations

import contextlib
import hashlib
import json
import tempfile
import types
import unittest
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from unittest import mock

from hosted_assistant_fixture import ANCHOR_ID, app, hosted_chat_api, hosted_chat_segment, runtime_state

action_journal = runtime_state.action_journal
action_challenges = runtime_state.action_challenges
integration_challenges = runtime_state.integration_challenges
DONE = {"team_id": "team_1", "team_name": "Marketing", "reply": "Campaign ready.", "clarification": None}
OWNER = types.SimpleNamespace(owner="account_1")


class StreamHarness:
    _audit_security = app.Handler._audit_security

    def __init__(self) -> None:
        self.status = None
        self.headers: list[tuple[str, str]] = []
        self.wfile = BytesIO()

    def send_response(self, status) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.headers.append((name, value))

    def end_headers(self) -> None:
        pass


def _operation(interrupt_id: str) -> action_journal.Operation:
    return action_journal.Operation(interrupt_id, hashlib.sha256(interrupt_id.encode()).hexdigest())


@contextlib.contextmanager
def _exclusive_turn(_team_id, _lease):
    yield "turn-token", types.SimpleNamespace(id=ANCHOR_ID)


class HostedChatStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "journal.sqlite3"
        # A paused batch whose in-memory human challenge a Controller restart lost: one receipt, one prepared Action.
        with action_journal.ActionJournal(path) as journal:
            first, second = _operation("action-1"), _operation("action-2")
            residue = journal.prepare_batch(ANCHOR_ID, "thread", (first, second))
            journal.begin(residue, first)
            journal.complete(residue, first, {"ok": True})
            journal.begin(residue, second)
            journal.suspend(residue, second)
        self.journal = action_journal.ActionJournal(path)
        self.addCleanup(self.journal.close)
        self.turns: list[object] = []
        for patcher in (
            mock.patch.object(runtime_state, "_human_challenges", action_challenges.HumanChallengeStore()),
            mock.patch.object(
                runtime_state, "_integration_challenges", integration_challenges.IntegrationChallengeStore()
            ),
            mock.patch.object(hosted_chat_api, "_exclusive_chat_turn", _exclusive_turn),
            mock.patch.object(hosted_chat_segment, "_chat_in_turn", side_effect=self._turn),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _turn(self, _request) -> dict[str, object]:
        # The fresh turn's own batch is refused as pending unless admission ended the residue first.
        self.turns.append(self.journal.prepare_batch(ANCHOR_ID, "thread", (_operation("action-3"),)))
        return dict(DONE)

    def test_ordinary_chat_ends_restart_residue_before_its_turn(self) -> None:
        with mock.patch.object(runtime_state, "_action_execution_journal", return_value=self.journal):
            result = hosted_chat_api._chat("team_1", "Prepare the campaign", [], ("shimpz-cloudflare",), OWNER)

        self.assertEqual(result, DONE)
        self.assertEqual(len(self.turns), 1)

    def test_stream_ends_restart_residue_before_its_200_and_emits_the_exact_v2_done_shape(self) -> None:
        stream = StreamHarness()
        with mock.patch.object(runtime_state, "_action_execution_journal", return_value=self.journal):
            app.Handler._stream_chat(stream, "team_1", "Prepare the campaign", [], ("shimpz-cloudflare",), OWNER)

        size_line, chunked = stream.wfile.getvalue().split(b"\r\n", 1)
        size = int(size_line, 16)
        self.assertEqual(len(self.turns), 1)
        self.assertEqual(stream.status, HTTPStatus.OK)
        self.assertIn(("Content-Type", "application/x-ndjson"), stream.headers)
        self.assertIn(("Cache-Control", "no-store"), stream.headers)
        self.assertEqual(chunked[size:], b"\r\n0\r\n\r\n")
        self.assertEqual(json.loads(chunked[:size]), {"type": "done", **DONE})

    def test_stream_forwards_the_completed_turn_usage_on_its_done_record(self) -> None:
        usage = {
            "duration_ms": 6200,
            "models": [{"provider": "openai", "model": "gpt-6-luna", "input_tokens": 9, "output_tokens": 2}],
        }
        stream = StreamHarness()
        with (
            mock.patch.object(runtime_state, "_action_execution_journal", return_value=self.journal),
            mock.patch.object(hosted_chat_segment, "_chat_in_turn", return_value={**DONE, "usage": usage}),
        ):
            app.Handler._stream_chat(stream, "team_1", "Prepare the campaign", [], ("shimpz-cloudflare",), OWNER)

        size_line, chunked = stream.wfile.getvalue().split(b"\r\n", 1)
        self.assertEqual(json.loads(chunked[: int(size_line, 16)]), {"type": "done", **DONE, "usage": usage})

    def test_stream_refuses_unavailable_action_state_before_any_response_byte(self) -> None:
        unavailable = types.SimpleNamespace(
            end_settled=mock.Mock(side_effect=action_journal.ActionJournalError("unavailable"))
        )
        stream = StreamHarness()
        with (
            mock.patch.object(runtime_state, "_action_execution_journal", return_value=unavailable),
            self.assertRaises(runtime_state.ApiError) as refused,
        ):
            app.Handler._stream_chat(stream, "team_1", "Prepare the campaign", [], ("shimpz-cloudflare",), OWNER)

        self.assertEqual(refused.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
        unavailable.end_settled.assert_called_once_with(ANCHOR_ID)
        self.assertIsNone(stream.status)
        self.assertEqual(stream.wfile.getvalue(), b"")
        self.assertEqual(self.turns, [])


if __name__ == "__main__":
    unittest.main()
