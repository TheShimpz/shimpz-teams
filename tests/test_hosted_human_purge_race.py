"""A paused Hosted human turn's cleanup removes only its own Action batch, never a newer turn's (ADR-0038).

Each cleanup cancels the challenge first and then removes its journal batch. A new turn may start in between: it ends
the paused batch, prepares its own, and may already be executing an Action. Each test runs that new turn exactly at
the journal purge, after the cancellation, and requires the newer batch to survive with its uncertain evidence.
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import hosted_assistant_fixture as harness

from tests import human_request_fixtures

hosted_chat_segment = harness.hosted_chat_segment
hosted_chat_human = harness.hosted_chat_human
hosted_chat_lifecycle = harness.hosted_chat_lifecycle
hosted_lifecycle = harness.hosted_lifecycle
hosted_resources = harness.hosted_resources
runtime_state = harness.runtime_state
action_challenges = hosted_chat_segment.action_challenges
action_journal = hosted_chat_segment.action_journal

GENERATION = "container-1"


class NewTurnBeforePurge:
    """The real journal, where a new turn starts and prepares its batch just before the first cleanup reaches it.

    The cleanup is the first purge, or with ``cleanup="end_settled"`` the first ending of settled state. The new turn
    begins its Action unless ``begin`` is False, which leaves its batch prepared and settled.
    """

    def __init__(
        self, journal: action_journal.ActionJournal, removed, *, cleanup: str = "purge", begin: bool = True
    ) -> None:
        self.journal = journal
        self.removed = removed
        self.cleanup = cleanup
        self.begin = begin
        self.fresh: action_journal.Batch | None = None

    def __getattr__(self, name: str) -> object:
        attribute = getattr(self.journal, name)
        if not name.startswith(self.cleanup):
            return attribute

        def clean(*args: object) -> object:
            if self.fresh is None:
                if not self.removed():
                    raise AssertionError("the paused turn was cleaned before its removal")
                operation = action_journal.Operation("action-2", "b" * 64)
                self.journal.end_settled(GENERATION)
                self.fresh = self.journal.prepare_batch(GENERATION, "thread", (operation,))
                if self.begin:
                    self.journal.begin(self.fresh, operation)
            return attribute(*args)

        return clean


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

        hosted_chat_human._expire_challenges("team_1")

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

    def test_a_lifecycle_change_ends_only_the_paused_batch(self) -> None:
        # The cleanup ends the paused batch by its fingerprint, so a batch another turn prepared first is never ended.
        self.race = NewTurnBeforePurge(
            self.journal, lambda: self.challenges.current("team_1") is None, cleanup="end_settled", begin=False
        )

        self.assertTrue(hosted_chat_lifecycle.cancel_replayable_human("team_1", GENERATION))

        fresh = self.race.fresh
        self.assertIsNotNone(fresh, "the cancellation never reached the journal")
        self.assertEqual(self.journal.current_batch(GENERATION), (fresh.fingerprint, "open"))
        self.assertFalse(hosted_chat_lifecycle.cancel_replayable_human("team_1", GENERATION))

    def test_a_lifecycle_change_ends_the_settled_paused_batch(self) -> None:
        self.assertTrue(hosted_chat_lifecycle.cancel_replayable_human("team_1", GENERATION))

        self.assertIsNone(self.challenges.current("team_1"))
        self.assertEqual(self.journal.current_batch(GENERATION), (self.paused_batch.fingerprint, "ended"))
        self.assertIsNone(self.race.fresh)

    def test_a_lifecycle_change_after_a_claimed_resume_keeps_the_replaying_batch(self) -> None:
        self.challenges.claim("team_1", self.challenge.id)
        operation = action_journal.Operation("action-1", "c" * 64)
        replay = self.journal.prepare_batch(GENERATION, "thread", (operation,))
        self.journal.begin(replay, operation)
        self.journal.complete(replay, operation, {"done": True})

        self.assertFalse(hosted_chat_lifecycle.cancel_replayable_human("team_1", GENERATION))

        # The responder owns the replay now: its settled batch is not ended under it.
        self.assertEqual(self.journal.current_batch(GENERATION), (replay.fingerprint, "open"))

    def test_a_model_change_cannot_withdraw_the_challenge_an_executing_turn_publishes(self) -> None:
        # The executing segment holds the Team chat slot from publishing its challenge until its pause commits.
        self.challenges.cancel_team("team_1")
        config = hosted_lifecycle.inference_config.normalize()
        body = {"provider": config.provider, "model": config.model, "effort": config.effort}
        lease = SimpleNamespace(container_id=GENERATION, owner="account_1")
        paused, refused, save = self._pause_racing(lambda: hosted_lifecycle._configure_inference("team_1", body, lease))

        published = self.challenges.current("team_1")
        self.assertIsNotNone(published, "the paused turn returned a challenge that no longer exists")
        self.assertEqual(paused["challenge_id"], published.id)
        self.assertEqual(self.journal.current_batch(GENERATION), (self.paused_batch.fingerprint, "open"))
        self.assertEqual([error.status for error in refused], [HTTPStatus.CONFLICT])
        save.assert_not_called()

    def _pause_racing(self, race) -> tuple[dict, list[runtime_state.ApiError], mock.Mock]:
        """Pause the executing segment, which holds the Team chat slot, while ``race`` runs right after its publish.

        Returns the pause response, the ApiErrors ``race`` raised, and the inference save mock.
        """
        refused: list[runtime_state.ApiError] = []
        publish = self.challenges.create

        def publish_then_race(*args: object) -> action_challenges.PendingHumanChallenge:
            challenge = publish(*args)
            try:
                race()
            except runtime_state.ApiError as error:
                refused.append(error)
            return challenge

        outcome = SimpleNamespace(request=self.requirement.request, continuation=self.pending.continuation)
        chat_lock = runtime_state._chat_lock_for("team_1")
        self.assertTrue(chat_lock.acquire(blocking=False))
        try:
            with (
                mock.patch.object(self.challenges, "create", side_effect=publish_then_race),
                mock.patch.object(hosted_resources, "_require_current_authorization"),
                mock.patch.object(runtime_state._inference_store, "save") as save,
            ):
                paused = hosted_chat_segment._pause_hosted_human(
                    "team_1", "turn-token", outcome, (self.requirement,), self.pending
                )
        finally:
            chat_lock.release()
        return paused, refused, save

    def _repeat_create(self) -> dict:
        existing = SimpleNamespace(id=GENERATION, labels={"team.owner": "account_1"}, status="running")
        with (
            mock.patch.object(hosted_resources, "_cleanup_record", return_value=None),
            mock.patch.object(hosted_resources, "_get_container", return_value=existing),
            mock.patch.object(hosted_resources, "_require_team_runtime"),
            mock.patch.object(hosted_resources, "_require_team_isolation"),
            mock.patch.object(hosted_resources, "_team_name_from_anchor", return_value="Marketing"),
        ):
            return hosted_lifecycle._create("team_1", {}, "account_1")

    def test_a_repeated_create_cannot_withdraw_the_challenge_an_executing_turn_publishes(self) -> None:
        # Repeating creation of an existing Team rewrites its inference, so it waits for the Team chat slot too.
        self.challenges.cancel_team("team_1")
        paused, refused, save = self._pause_racing(self._repeat_create)

        published = self.challenges.current("team_1")
        self.assertIsNotNone(published, "the paused turn returned a challenge that no longer exists")
        self.assertEqual(paused["challenge_id"], published.id)
        self.assertEqual(self.journal.current_batch(GENERATION), (self.paused_batch.fingerprint, "open"))
        self.assertEqual([error.status for error in refused], [HTTPStatus.CONFLICT])
        save.assert_not_called()

    def test_a_repeated_create_ends_the_settled_paused_batch_before_its_inference_changes(self) -> None:
        def save(_team_id: str, _config: object) -> None:
            self.assertIsNone(self.challenges.current("team_1"))
            self.assertEqual(self.journal.current_batch(GENERATION), (self.paused_batch.fingerprint, "ended"))

        with mock.patch.object(runtime_state._inference_store, "save", side_effect=save) as saved:
            self.assertFalse(self._repeat_create()["created"])

        saved.assert_called_once()
        self.assertIsNone(self.race.fresh)


if __name__ == "__main__":
    unittest.main()
