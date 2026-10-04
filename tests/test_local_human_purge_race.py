"""A paused human turn's cleanup removes only its own Action batch, never a newer turn's (ADR-0038).

Every cleanup removes the challenge or continuation first and then its journal batch. A new turn may start in
between: it ends the paused batch, prepares its own, and may already be executing an Action. Each test runs that new
turn exactly at the journal purge, after the removal, and requires the newer batch to survive with its uncertain
evidence.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
from local_controller_harness import LocalContractCase

from action import human as action_human
from inference import client as brain_runtime_client
from local import app as local_app
from tests import human_request_fixtures

GENERATION = "a" * 64
LOOKUP_INPUT = {"page": 1, "per_page": 25}
CHAT_BODY = {
    "message": "List zones",
    "files": [],
    "assistant_ids": ["shimpz-cloudflare"],
    "conversation": [],
    "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
    "timezone": None,
    "locale": None,
}


class NewTurnBeforePurge:
    """The real journal, where a new turn starts and begins its Action just before the first purge reaches it."""

    def __init__(self, journal: local_app.action_journal.ActionJournal, removed) -> None:
        self.journal = journal
        self.removed = removed
        self.fresh: local_app.action_journal.Batch | None = None

    def __getattr__(self, name: str) -> object:
        attribute = getattr(self.journal, name)
        if not name.startswith("purge"):
            return attribute

        def purge(*args: object) -> object:
            if self.fresh is None:
                if not self.removed():
                    raise AssertionError("the paused turn was purged before its removal")
                operation = local_app.action_journal.Operation("action-2", "b" * 64)
                self.journal.end_settled(GENERATION)
                self.fresh = self.journal.prepare_batch(GENERATION, "next-thread", (operation,))
                self.journal.begin(self.fresh, operation)
            return attribute(*args)

        return purge


class LocalHumanPurgeRaceTests(LocalContractCase):
    @staticmethod
    def _approval_request() -> action_human.HumanRequest:
        descriptor = {
            "kind": "approval",
            "ordinal": 0,
            "title": "List zones",
            "description": "Allow this Action to list the reviewed Cloudflare zones.",
        }
        return human_request_fixtures.admit(human_request_fixtures.fingerprinted(descriptor), ("approval",))

    def _paused(self, directory: str) -> tuple[local_app.LocalController, dict[str, object], str]:
        request = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

        class Runtime:
            purpose = staticmethod(lambda *_args: None)

            def start(self, _context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def resume(self, _context, _results):
                raise AssertionError("an ended paused turn must not resume the Brain")

        controller = self._chat_controller(directory, Runtime())
        admitted = self._approval_request()
        controller.assistant_lifecycle.invoke = lambda *_args: (_ for _ in ()).throw(
            action_human.HumanRequestSuspensionError(admitted)
        )
        paused = controller.chat_turn_service.chat("team_1", dict(CHAT_BODY), "openai", "sk-test-0123456789")
        self.assertEqual(paused["status"], "human-required")
        current = controller.action_state.current_batch(GENERATION)
        self.assertIsNotNone(current)
        return controller, paused, current[0]

    def _race(self, controller: local_app.LocalController) -> NewTurnBeforePurge:
        service = controller.chat_turn_service
        race = NewTurnBeforePurge(
            controller.action_state,
            lambda: (
                service.human_challenges.current("team_1") is None
                and controller.chat_continuations.current("team_1") is None
            ),
        )
        service.action_state = race
        return race

    def _assert_newer_turn_kept(self, race: NewTurnBeforePurge, paused_batch: str) -> None:
        self.assertIsNotNone(race.fresh, "the cleanup never reached the journal")
        self.assertNotEqual(race.fresh.fingerprint, paused_batch)
        self.assertEqual(race.journal.current_batch(GENERATION), (race.fresh.fingerprint, "open"))
        self.assertEqual(race.journal.uncertain_fingerprint(GENERATION), race.fresh.fingerprint)

    def test_stop_keeps_a_newer_turns_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, _paused, paused_batch = self._paused(directory)
            race = self._race(controller)

            stopped = controller.chat_turn_service.stop_chat("team_1")

            self.assertTrue(stopped["accepted"])
            self._assert_newer_turn_kept(race, paused_batch)

    def test_denial_keeps_a_newer_turns_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, paused, paused_batch = self._paused(directory)
            race = self._race(controller)

            denied = controller.chat_turn_service.resume_chat_human(
                "team_1",
                {"challenge_id": paused["challenge_id"], "decision": "deny"},
                "openai",
                "sk-test-0123456789",
            )

            self.assertEqual(denied["status"], "human-denied")
            self._assert_newer_turn_kept(race, paused_batch)

    def test_changed_capabilities_keep_a_newer_turns_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, paused, paused_batch = self._paused(directory)
            race = self._race(controller)

            with self.assertRaises(local_app.ApiProblem) as raised:
                controller.chat_turn_service.resume_chat_human(
                    "team_1",
                    {"challenge_id": paused["challenge_id"], "decision": "deny"},
                    "anthropic",
                    "sk-test-0123456789",
                )

            self.assertEqual(raised.exception.code, "team-context-changed")
            self._assert_newer_turn_kept(race, paused_batch)

    def test_expiry_in_a_running_controller_keeps_a_newer_turns_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, _paused, paused_batch = self._paused(directory)
            service = controller.chat_turn_service
            challenge = service.human_challenges.current("team_1")
            race = self._race(controller)
            service.human_challenges._clock = lambda: challenge.expires_at

            service._expire_human_challenges()

            self._assert_newer_turn_kept(race, paused_batch)

    def test_expiry_at_restart_keeps_a_newer_turns_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, _paused, paused_batch = self._paused(directory)
            reopened = local_app.local_chat_continuation_store.EncryptedContinuationStore(
                controller.chat_continuations.state_path,
                controller.chat_continuations.key_path,
                now=lambda: 2_200_000_000,
            )
            race = NewTurnBeforePurge(controller.action_state, lambda: reopened.current("team_1") is None)
            restarted = local_app.ChatTurnService(
                local_app.ChatTurnDependencies(
                    action_state=race,
                    integration_challenges=local_app.integration_challenges.IntegrationChallengeStore(),
                    human_challenges=local_app.action_challenges.HumanChallengeStore(),
                    chat_continuations=reopened,
                )
            )

            restarted._restore_all_chat_continuations()

            self._assert_newer_turn_kept(race, paused_batch)

    def test_stop_after_a_claimed_resume_keeps_the_replaying_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, paused, paused_batch = self._paused(directory)
            service = controller.chat_turn_service
            during_stop: list[object] = []

            def invoke(*_args):
                # The responder claimed the challenge and is replaying the paused batch when Stop arrives.
                stopped = service.stop_chat("team_1")
                during_stop.append(
                    (
                        stopped["accepted"],
                        service.action_state.current_batch(GENERATION),
                        service.action_state.uncertain_fingerprint(GENERATION),
                    )
                )
                return {"result": {}}

            controller.assistant_lifecycle.invoke = invoke
            # The executing Action's container is fail-stopped elsewhere; only the journal outcome matters here.
            controller.assistant_lifecycle._fail_stop_action = lambda _container: None
            with self.assertRaises(local_app.ApiProblem):
                service.resume_chat_human(
                    "team_1",
                    {"challenge_id": paused["challenge_id"], "decision": "submit", "value": True},
                    "openai",
                    "sk-test-0123456789",
                )

        # Stop ends the running turn but never removes the batch whose outcome is now uncertain.
        self.assertEqual(during_stop, [(True, (paused_batch, "open"), paused_batch)])


if __name__ == "__main__":
    import unittest

    unittest.main()
