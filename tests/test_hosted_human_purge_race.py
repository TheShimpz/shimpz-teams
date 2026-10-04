"""A paused Hosted human turn's cleanup removes only its own Action batch, never a newer turn's (ADR-0038).

Each cleanup cancels the challenge first and then removes its journal batch. A new turn may start in between: it ends
the paused batch, prepares its own, and may already be executing an Action. Each test runs that new turn exactly at
the journal purge, after the cancellation, and requires the newer batch to survive with its uncertain evidence.
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import hosted_assistant_fixture as harness

from tests import human_request_fixtures

hosted_chat_segment = harness.hosted_chat_segment
hosted_chat_human = harness.hosted_chat_human
runtime_state = harness.runtime_state
action_challenges = hosted_chat_segment.action_challenges
action_journal = hosted_chat_segment.action_journal

GENERATION = "container-1"


class NewTurnBeforePurge:
    """The real journal, where a new turn starts and begins its Action just before the first purge reaches it."""

    def __init__(self, journal: action_journal.ActionJournal, removed) -> None:
        self.journal = journal
        self.removed = removed
        self.fresh: action_journal.Batch | None = None

    def __getattr__(self, name: str) -> object:
        attribute = getattr(self.journal, name)
        if not name.startswith("purge"):
            return attribute

        def purge(*args: object) -> object:
            if self.fresh is None:
                if not self.removed():
                    raise AssertionError("the paused turn was purged before its removal")
                operation = action_journal.Operation("action-2", "b" * 64)
                self.journal.end_settled(GENERATION)
                self.fresh = self.journal.prepare_batch(GENERATION, "next-thread", (operation,))
                self.journal.begin(self.fresh, operation)
            return attribute(*args)

        return purge


class HostedHumanPurgeRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.journal = action_journal.ActionJournal(Path(directory.name) / "journal.sqlite3")
        self.addCleanup(self.journal.close)
        operation = action_journal.Operation("action-1", "c" * 64)
        # A human suspension returns its operation to prepared, so the paused batch stays open and settled.
        self.paused_batch = self.journal.prepare_batch(GENERATION, "thread", (operation,))
        self.challenges = action_challenges.HumanChallengeStore(retain_expired=True)
        descriptor = {"kind": "approval", "ordinal": 0, "title": "Publish zone", "description": "Publish it."}
        request = human_request_fixtures.admit(human_request_fixtures.fingerprinted(descriptor), ("approval",))
        self.requirement = action_challenges.HumanRequirement(
            "shimpz-cloudflare",
            "Shimpz Cloudflare",
            "publish-zone",
            "Publish zone",
            "action-1",
            request,
            "0.4.1",
            copy=human_request_fixtures.copy(request),
        )
        self.pending = harness.hosted_assistants._PendingHostedChat(
            SimpleNamespace(),
            ("shimpz-cloudflare",),
            (),
            "account_1",
            (GENERATION, "account_1", "Marketing"),
            paused_batch=self.paused_batch.fingerprint,
        )
        self.challenge = self.challenges.create("team_1", self.requirement, self.pending)
        self.race = NewTurnBeforePurge(self.journal, lambda: self.challenges.current("team_1") is None)
        for target, value in (
            ("_human_challenges", self.challenges),
            ("_action_execution_journal", lambda: self.race),
            ("_commit_chat_terminal", lambda _team_id, _token: True),
        ):
            patcher = mock.patch.object(runtime_state, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _assert_newer_turn_kept(self) -> None:
        fresh = self.race.fresh
        self.assertIsNotNone(fresh, "the cleanup never reached the journal")
        self.assertNotEqual(fresh.fingerprint, self.paused_batch.fingerprint)
        self.assertEqual(self.journal.current_batch(GENERATION), (fresh.fingerprint, "open"))
        self.assertEqual(self.journal.uncertain_fingerprint(GENERATION), fresh.fingerprint)

    def test_stop_keeps_a_newer_turns_batch(self) -> None:
        self.assertTrue(hosted_chat_human.cancel_pending("team_1"))

        self._assert_newer_turn_kept()

    def test_expiry_keeps_a_newer_turns_batch(self) -> None:
        self.challenges._clock = lambda: self.challenge.expires_at

        hosted_chat_human._expire_challenges()

        self._assert_newer_turn_kept()

    def test_denial_keeps_a_newer_turns_batch(self) -> None:
        @contextmanager
        def exclusive(_team_id, _lease):
            yield "turn-token", SimpleNamespace(id=GENERATION)

        with mock.patch.object(hosted_chat_human, "_validate_pending_context", return_value=self.pending):
            denied = hosted_chat_human.resume_chat_human(
                "team_1",
                {"challenge_id": self.challenge.id, "decision": "deny"},
                None,
                SimpleNamespace(owner="account_1"),
                exclusive,
            )

        self.assertEqual(denied["status"], "human-denied")
        self._assert_newer_turn_kept()

    def test_changed_capabilities_keep_a_newer_turns_batch(self) -> None:
        changed = (None, (), (GENERATION, "account_1", "Renamed"))
        with (
            mock.patch.object(hosted_chat_segment, "_hosted_chat_setup", return_value=changed),
            self.assertRaises(runtime_state.ApiError),
        ):
            hosted_chat_human._validate_pending_context(
                "team_1", self.challenge, SimpleNamespace(id=GENERATION), "account_1"
            )

        self._assert_newer_turn_kept()

    def test_stop_after_a_claimed_resume_keeps_the_replaying_batch(self) -> None:
        # The responder claimed the challenge and is replaying the paused batch when Stop arrives.
        self.challenges.claim("team_1", self.challenge.id)
        operation = action_journal.Operation("action-1", "c" * 64)
        replay = self.journal.prepare_batch(GENERATION, "thread", (operation,))
        self.journal.begin(replay, operation)

        self.assertFalse(hosted_chat_human.cancel_pending("team_1"))

        self.assertIsNone(self.race.fresh)
        self.assertEqual(replay.fingerprint, self.paused_batch.fingerprint)
        self.assertEqual(self.journal.current_batch(GENERATION), (replay.fingerprint, "open"))
        self.assertEqual(self.journal.uncertain_fingerprint(GENERATION), replay.fingerprint)


if __name__ == "__main__":
    unittest.main()
