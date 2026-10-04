"""Local Stop ends a running turn even when that turn pauses while Stop is between its steps.

Stop cancels the turn's token before it withdraws challenges. A pause commits only while its token is current, so it
either committed before Stop (and Stop withdraws the challenge it published) or fails and rolls back its challenge,
its continuation, and, for a human request, its own paused Action batch.
"""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
from local_controller_harness import LocalContractCase

from action import human as action_human
from inference import client as brain_runtime_client
from integrations import broker as integration_broker
from integrations import service as integration_service
from local import app as local_app
from tests import human_request_fixtures

GENERATION = "a" * 64
CHAT_BODY = {
    "message": "List zones",
    "files": [],
    "assistant_ids": ["shimpz-cloudflare"],
    "conversation": [],
    "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
    "timezone": None,
    "locale": None,
}


class Runtime:
    purpose = staticmethod(lambda *_args: None)

    def start(self, _context, _message, *, conversation=()):
        request = brain_runtime_client.ActionRequest(
            "action-1", "shimpz-cloudflare", "list-zones", {"page": 1, "per_page": 25}
        )
        return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

    def resume(self, _context, _results):
        raise AssertionError("a stopped turn must not resume the Brain")


class PausingTurn:
    """A chat turn in its own thread that waits just before its ``kind`` pause takes the Team lock to publish."""

    def __init__(self, service, kind: str, released=lambda: False) -> None:
        self.service = service
        self.reached = threading.Event()
        self.release = threading.Event()
        self.outcome: list[object] = []
        name = f"_pause_{kind}"
        pause = getattr(service, name)

        def publish(*args: object) -> object:
            self.reached.set()
            for _ in range(500):
                if self.release.wait(0.01) or released():
                    return pause(*args)
            raise AssertionError("the turn was never released")

        setattr(service, name, publish)
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self.outcome.append(self.service.chat("team_1", dict(CHAT_BODY), "openai", "sk-test-0123456789"))
        except local_app.ApiProblem as exc:
            self.outcome.append(exc)

    def start(self) -> None:
        self.thread.start()
        if not self.reached.wait(5):
            raise AssertionError("the turn never reached its pause")

    def finish(self) -> None:
        self.release.set()
        self.thread.join(5)


def wait_until(condition) -> None:
    for _ in range(500):
        if condition():
            return
        threading.Event().wait(0.01)
    raise AssertionError("the expected interleaving never happened")


class ContendedLock:
    """A Team lock that reports when an acquirer has to wait for another holder."""

    def __init__(self, contended: threading.Event) -> None:
        self.lock = threading.RLock()
        self.contended = contended

    def __enter__(self) -> bool:
        if not self.lock.acquire(blocking=False):
            self.contended.set()
            self.lock.acquire()
        return True

    def __exit__(self, *_args: object) -> None:
        self.lock.release()


class LocalStopPauseRaceTests(LocalContractCase):
    def _controller(self, directory: str, *, human: bool) -> local_app.LocalController:
        controller = self._chat_controller(directory, Runtime())
        if human:
            descriptor = {
                "kind": "approval",
                "ordinal": 0,
                "title": "List zones",
                "description": "Allow this Action to list the reviewed Cloudflare zones.",
            }
            admitted = human_request_fixtures.admit(human_request_fixtures.fingerprinted(descriptor), ("approval",))
            controller.assistant_lifecycle.invoke = lambda *_args: (_ for _ in ()).throw(
                action_human.HumanRequestSuspensionError(admitted)
            )
        else:
            controller.assistant_integrations.delete_assistant("team_1", "shimpz-cloudflare")
        return controller

    def _stop_while_the_turn_publishes(self, *, human: bool) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory, human=human)
            service = controller.chat_turn_service
            contended = threading.Event()
            controller._locks = tuple(ContendedLock(contended) for _ in controller._locks)
            store = service.human_challenges if human else service.integration_challenges
            turn = PausingTurn(service, "human" if human else "integration")
            self.addCleanup(turn.release.set)
            turn.start()
            withdraw = service.human_challenges.withdraw_team
            released: list[bool] = []

            def turn_publishes(team_id: str) -> object:
                # Stop has looked for both challenges; the running turn now goes on to publish its pause, which
                # either waits for Stop's Team lock or, unserialized, publishes and commits at once.
                withdrawn = withdraw(team_id)
                if not released:
                    released.append(True)
                    turn.release.set()
                    wait_until(lambda: contended.is_set() or not turn.thread.is_alive())
                return withdrawn

            service.human_challenges.withdraw_team = turn_publishes

            stopped = service.stop_chat("team_1")
            turn.thread.join(5)

            self.assertFalse(turn.thread.is_alive())
            self.assertTrue(stopped["accepted"], "Stop missed the turn it was asked to end")
            self.assertIsNone(store.current("team_1"), "the stopped turn stayed resumable")
            self.assertIsNone(controller.chat_continuations.current("team_1"))
            # The rolled-back human pause leaves no paused batch of its own behind.
            self.assertIsNone(controller.action_state.current_batch(GENERATION))
            [ended] = turn.outcome
            self.assertIsInstance(ended, local_app.ApiProblem)
            self.assertEqual(ended.code, "chat-stopped")

    def test_stop_ends_a_human_pause_published_after_its_withdrawal(self) -> None:
        self._stop_while_the_turn_publishes(human=True)

    def test_stop_ends_an_integration_pause_published_after_its_withdrawal(self) -> None:
        self._stop_while_the_turn_publishes(human=False)

    def test_a_pause_committed_before_the_token_is_cancelled_is_withdrawn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory, human=True)
            service = controller.chat_turn_service
            turn = PausingTurn(service, "human")
            self.addCleanup(turn.release.set)
            turn.start()
            turn.finish()

            stopped = service.stop_chat("team_1")

            self.assertTrue(stopped["accepted"])
            self.assertEqual(turn.outcome[0]["status"], "human-required")
            self.assertIsNone(service.human_challenges.current("team_1"))
            self.assertIsNone(controller.chat_continuations.current("team_1"))
            self.assertIsNone(controller.action_state.current_batch(GENERATION))

    def _a_losing_commit_meets(self, *, human: bool, consumer) -> list[object]:
        """Hold a stopped turn inside its failing commit while ``consumer`` acts on the challenge it published."""
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory, human=human)
            service = controller.chat_turn_service
            self._configure_oauth(controller)
            contended = threading.Event()
            controller._locks = tuple(ContendedLock(contended) for _ in controller._locks)
            turn = PausingTurn(service, "human" if human else "integration")
            self.addCleanup(turn.release.set)
            turn.start()
            # Stop cancels the running turn before it publishes, so its commit will fail.
            self.assertTrue(service.stop_chat("team_1")["accepted"])
            committing = threading.Event()
            proceed = threading.Event()
            self.addCleanup(proceed.set)
            commit = service._commit_suspension

            def held_commit(*args: object) -> object:
                committing.set()
                proceed.wait(5)
                return commit(*args)

            service._commit_suspension = held_commit
            turn.release.set()
            self.assertTrue(committing.wait(5))
            consumed: list[object] = []

            def consume() -> None:
                try:
                    consumed.append(consumer(service))
                except local_app.ApiProblem as exc:
                    consumed.append(exc)

            consuming = threading.Thread(target=consume, daemon=True)
            consuming.start()
            # The consumer either waits for the publishing turn's Team lock or, unserialized, acts at once.
            wait_until(lambda: contended.is_set() or not consuming.is_alive())
            proceed.set()
            turn.thread.join(5)
            consuming.join(5)

            [ended] = turn.outcome
            self.assertIsInstance(ended, local_app.ApiProblem)
            self.assertEqual(ended.code, "chat-stopped", "the stopped turn's rollback failed")
            self.assertIsNone(service.human_challenges.current("team_1"))
            self.assertIsNone(service.integration_challenges.current("team_1"))
            self.assertIsNone(controller.chat_continuations.current("team_1"), "a continuation outlived its turn")
            self.assertIsNone(controller.action_state.current_batch(GENERATION))
            self.assertEqual(service.oauth_pkce.cancel_team("team_1"), 0, "PKCE state outlived its challenge")
            return consumed

    def test_a_relocalization_waits_for_a_losing_human_commit(self) -> None:
        [opened] = self._a_losing_commit_meets(
            human=True, consumer=lambda service: service.open_chat_human("team_1", {"locale": "pt"})
        )
        self.assertEqual(opened, {"team_id": "team_1", "status": "none"})

    def test_an_oauth_start_waits_for_a_losing_integration_commit(self) -> None:
        def start(service) -> object:
            challenge = service.integration_challenges.current("team_1")
            return service.start_assistant_integration_authorization(
                "team_1",
                challenge.id,
                "shimpz-cloudflare",
                "cloudflare",
                "browser-session-private-123456789",
                "hosted",
            )

        [started] = self._a_losing_commit_meets(human=False, consumer=start)
        self.assertIsInstance(started, local_app.ApiProblem)
        self.assertEqual(started.code, "assistant-integration-challenge-expired")

    @staticmethod
    def _configure_oauth(controller: local_app.LocalController) -> None:
        service = controller.chat_turn_service
        service.oauth_service = integration_service.BrokeredOAuthIntegrationService(
            challenge=service.oauth_pkce,
            store=controller.assistant_integrations,
            broker=integration_broker.OAuthBrokerClient(integration_broker.FixedBrokerTransport()),
        )

    def test_stop_withdraws_the_pkce_state_of_an_oauth_start_it_overlaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory, human=False)
            service = controller.chat_turn_service
            paused = service.chat("team_1", dict(CHAT_BODY), "openai", "sk-test-0123456789")
            self.assertEqual(paused["status"], "integrations-required")
            self._configure_oauth(controller)
            progressed = threading.Event()
            controller._locks = tuple(ContendedLock(progressed) for _ in controller._locks)
            reached = threading.Event()
            release = threading.Event()
            self.addCleanup(release.set)
            create = service.oauth_pkce.create

            def create_pkce(**values: object) -> object:
                reached.set()
                if not release.wait(5):
                    raise AssertionError("the OAuth start was never released")
                return create(**values)

            service.oauth_pkce.create = create_pkce
            started: list[object] = []
            start = threading.Thread(
                target=lambda: started.append(
                    service.start_assistant_integration_authorization(
                        "team_1",
                        paused["challenge_id"],
                        "shimpz-cloudflare",
                        "cloudflare",
                        "browser-session-private-123456789",
                        "hosted",
                    )
                ),
                daemon=True,
            )
            start.start()
            self.assertTrue(reached.wait(5))
            stopped: list[dict[str, object]] = []

            def stop() -> None:
                stopped.append(service.stop_chat("team_1"))
                progressed.set()

            stopping = threading.Thread(target=stop, daemon=True)
            stopping.start()
            # Stop either waits for the OAuth start's Team lock or, unserialized, finishes before the start creates.
            self.assertTrue(progressed.wait(5))
            release.set()
            start.join(5)
            stopping.join(5)

            self.assertTrue(stopped[0]["accepted"])
            self.assertEqual(set(started[0]), {"authorization_url"})
            self.assertEqual(service.oauth_pkce.cancel_team("team_1"), 0, "PKCE state outlived its withdrawn challenge")
