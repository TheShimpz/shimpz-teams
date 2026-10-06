"""A paused human turn's cleanup removes only its own Action batch, never a newer turn's (ADR-0038).

Every cleanup removes the challenge or continuation first and then its journal batch. A new turn may start in
between: it ends the paused batch, prepares its own, and may already be executing an Action. Each test runs that new
turn exactly at the journal purge, after the removal, and requires the newer batch to survive with its uncertain
evidence.
"""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
from local_controller_harness import LOOKUP_INPUT, LocalContractCase, chat_body

from action import human as action_human
from inference import client as brain_runtime_client
from inference import config as inference_config
from local import app as local_app
from tests import human_request_fixtures

GENERATION = "a" * 64
CHAT_BODY = chat_body("List zones", assistant_ids=["shimpz-cloudflare"])


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


class ObservedLock:
    """A Team lock that reports when the observed thread starts waiting for it."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.observed: threading.Thread | None = None
        self.waiting = threading.Event()

    def __enter__(self) -> bool:
        if threading.current_thread() is self.observed:
            self.waiting.set()
        return self.lock.__enter__()

    def __exit__(self, *args: object) -> None:
        self.lock.__exit__(*args)


class LocalHumanPurgeRaceTests(LocalContractCase):
    def _paused(self, directory: str) -> tuple[local_app.LocalController, dict[str, object], str]:
        class Runtime:
            purpose = staticmethod(lambda *_args: None)
            turns = 0

            def start(self, _context, _message, *, conversation=()):
                # Each turn's Brain names its own Action interrupt.
                self.turns += 1
                request = brain_runtime_client.ActionRequest(
                    f"action-{self.turns}", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT
                )
                return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

            def resume(self, _context, _results):
                raise AssertionError("an ended paused turn must not resume the Brain")

        controller = self._chat_controller(directory, Runtime())
        admitted = human_request_fixtures.list_zones_approval()
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

    def test_stop_keeps_the_continuation_of_a_turn_paused_since(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, paused, _paused_batch = self._paused(directory)
            service = controller.chat_turn_service
            newer: list[dict[str, object]] = []
            cancel_pkce = service.oauth_pkce.cancel_team

            def new_turn_pauses(team_id: str) -> int:
                # Stop already withdrew the paused challenge; a new turn starts and pauses before Stop's next step.
                if not newer:
                    self.assertIsNone(service.human_challenges.current("team_1"))
                    newer.append(service.chat("team_1", dict(CHAT_BODY), "openai", "sk-test-0123456789"))
                return cancel_pkce(team_id)

            service.oauth_pkce.cancel_team = new_turn_pauses

            stopped = service.stop_chat("team_1")

            self.assertTrue(stopped["accepted"])
            [resumed] = newer
            self.assertEqual(resumed["status"], "human-required")
            self.assertNotEqual(resumed["challenge_id"], paused["challenge_id"])
            self.assertEqual(service.human_challenges.current("team_1").id, resumed["challenge_id"])
            stored = controller.chat_continuations.current("team_1")
            self.assertIsNotNone(stored, "Stop deleted the newer turn's continuation")
            self.assertEqual(stored.challenge_id, resumed["challenge_id"])

    def test_stop_deletes_the_withdrawn_integration_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, _paused, _paused_batch = self._paused(directory)
            service = controller.chat_turn_service
            service.stop_chat("team_1")
            controller.assistant_integrations.delete_assistant("team_1", "shimpz-cloudflare")
            paused = service.chat("team_1", dict(CHAT_BODY), "openai", "sk-test-0123456789")
            self.assertEqual(paused["status"], "integrations-required")
            self.assertEqual(controller.chat_continuations.current("team_1").challenge_id, paused["challenge_id"])

            stopped = service.stop_chat("team_1")

            self.assertTrue(stopped["accepted"])
            self.assertIsNone(service.integration_challenges.current("team_1"))
            self.assertIsNone(controller.chat_continuations.current("team_1"))

    def test_stop_during_a_relocalization_leaves_no_cancelled_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, _paused, _paused_batch = self._paused(directory)
            service = controller.chat_turn_service
            locks = tuple(ObservedLock() for _ in controller._locks)
            controller._locks = locks
            reissue = service.human_challenges.reissue
            outcome: list[object] = []
            finished = threading.Event()

            def stop() -> None:
                # A failing Stop leaves no outcome, which the test thread reports below.
                try:
                    outcome.append(service.stop_chat("team_1"))
                finally:
                    finished.set()

            stopper = threading.Thread(target=stop, daemon=True)
            self.addCleanup(stopper.join, 10)

            def reissued(*args: object) -> object:
                fresh = reissue(*args)
                # Stop arrives after the fresh challenge exists and before its continuation is persisted.
                for lock in locks:
                    lock.observed = stopper
                stopper.start()
                for _ in range(1000):
                    if finished.is_set() or any(lock.waiting.is_set() for lock in locks):
                        break
                    finished.wait(0.01)
                else:
                    raise AssertionError("Stop neither finished nor waited for the Team lock")
                return fresh

            service.human_challenges.reissue = reissued
            opened = service.open_chat_human("team_1", {"locale": "pt"})
            stopper.join(10)

            self.assertFalse(stopper.is_alive())
            self.assertEqual(opened["status"], "human-required")
            self.assertEqual(len(outcome), 1, "Stop failed in its thread")
            [stopped] = outcome
            self.assertTrue(stopped["accepted"])
            self.assertIsNone(service.human_challenges.current("team_1"))
            self.assertIsNone(
                controller.chat_continuations.current("team_1"), "a cancelled continuation stayed durable"
            )

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

    @staticmethod
    def _change_provider(controller: local_app.LocalController) -> None:
        # A permitted model change while the turn is paused; the turn still names the provider it paused with.
        controller.inference_store.save("team_1", inference_config.normalize("anthropic", "claude-sonnet-5-5"))

    def test_reopening_after_a_provider_change_ends_the_paused_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, _paused, paused_batch = self._paused(directory)
            service = controller.chat_turn_service
            self._change_provider(controller)
            race = self._race(controller)

            with self.assertRaises(local_app.ApiProblem) as raised:
                service.open_chat_human("team_1", {"locale": "pt"})

            self.assertEqual(raised.exception.code, "team-context-changed")
            self.assertIsNone(service.human_challenges.current("team_1"))
            self.assertIsNone(controller.chat_continuations.current("team_1"))
            self._assert_newer_turn_kept(race, paused_batch)
            reopened = service.open_chat_human("team_1", {"locale": "pt"})
            self.assertEqual(reopened, {"team_id": "team_1", "status": "none"})

    def test_a_fresh_message_after_a_provider_change_ends_the_paused_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, paused, paused_batch = self._paused(directory)
            service = controller.chat_turn_service
            self._change_provider(controller)

            with self.assertRaises(local_app.ApiProblem) as raised:
                service.chat("team_1", dict(CHAT_BODY), "anthropic", "sk-ant-test-0123456789")

            self.assertEqual(raised.exception.code, "team-context-changed")
            self.assertIsNone(service.human_challenges.current("team_1"))
            self.assertIsNone(controller.chat_continuations.current("team_1"))
            self.assertIsNone(controller.action_state.current_batch(GENERATION))
            # The next message runs a new turn with the configured provider.
            fresh = service.chat("team_1", dict(CHAT_BODY), "anthropic", "sk-ant-test-0123456789")
            self.assertEqual(fresh["status"], "human-required")
            self.assertNotEqual(fresh["challenge_id"], paused["challenge_id"])
            self.assertNotEqual(controller.action_state.current_batch(GENERATION)[0], paused_batch)

    def test_a_fresh_message_validates_the_challenge_it_read_while_still_live(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, _paused, _paused_batch = self._paused(directory)
            service = controller.chat_turn_service
            locks = tuple(ObservedLock() for _ in controller._locks)
            controller._locks = locks
            read = service.human_challenges.current
            opened: list[object] = []
            finished = threading.Event()

            def open_another_language() -> None:
                try:
                    opened.append(service.open_chat_human("team_1", {"locale": "pt"}))
                finally:
                    finished.set()

            opener = threading.Thread(target=open_another_language, daemon=True)
            self.addCleanup(opener.join, 10)

            def current(team_id: str) -> object:
                challenge = read(team_id)
                if not opener.is_alive() and not finished.is_set():
                    # Another tab opens the request in another language after this message read it, and the model
                    # provider then changes; the reissued challenge shares the paused batch.
                    for lock in locks:
                        lock.observed = opener
                    opener.start()
                    for _ in range(1000):
                        if finished.is_set() or any(lock.waiting.is_set() for lock in locks):
                            break
                        finished.wait(0.01)
                    else:
                        raise AssertionError("the opening neither finished nor waited for the Team lock")
                    self._change_provider(controller)
                return challenge

            service.human_challenges.current = current
            with self.assertRaises(local_app.ApiProblem) as raised:
                service.chat("team_1", dict(CHAT_BODY), "anthropic", "sk-ant-test-0123456789")
            opener.join(10)

            self.assertFalse(opener.is_alive())
            self.assertEqual(raised.exception.code, "team-context-changed")
            self.assertEqual(opened, [{"team_id": "team_1", "status": "none"}])
            # The whole paused turn ended together: no live challenge is left without its batch.
            self.assertIsNone(read("team_1"))
            self.assertIsNone(controller.chat_continuations.current("team_1"))
            self.assertIsNone(controller.action_state.current_batch(GENERATION))

    def test_an_unreadable_provider_keeps_the_paused_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, paused, paused_batch = self._paused(directory)
            service = controller.chat_turn_service
            unreadable = local_app.inference_config.InferenceConfigError("inference state is unreadable")

            with (
                mock.patch.object(controller.inference_store, "load", side_effect=unreadable),
                self.assertRaises(local_app.ApiProblem) as raised,
            ):
                service.open_chat_human("team_1", {"locale": "pt"})

            self.assertEqual(raised.exception.code, "inference-not-configured")
            self.assertEqual(service.human_challenges.current("team_1").id, paused["challenge_id"])
            self.assertEqual(controller.chat_continuations.current("team_1").challenge_id, paused["challenge_id"])
            self.assertEqual(controller.action_state.current_batch(GENERATION), (paused_batch, "open"))
            reopened = service.open_chat_human("team_1", {"locale": "pt"})
            self.assertEqual(reopened["status"], "human-required")

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
