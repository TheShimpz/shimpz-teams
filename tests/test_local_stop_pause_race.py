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
    """A chat turn in its own thread that waits just before it publishes its challenge in ``store``."""

    def __init__(self, service, store) -> None:
        self.service = service
        self.reached = threading.Event()
        self.release = threading.Event()
        self.outcome: list[object] = []
        create = store.create

        def publish(*args: object) -> object:
            self.reached.set()
            if not self.release.wait(5):
                raise AssertionError("the turn was never released")
            return create(*args)

        store.create = publish
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
            store = service.human_challenges if human else service.integration_challenges
            turn = PausingTurn(service, store)
            self.addCleanup(turn.release.set)
            turn.start()
            withdraw = service.human_challenges.withdraw_team
            released: list[bool] = []

            def turn_publishes(team_id: str) -> object:
                # Stop has looked for both challenges; the running turn now publishes and commits its pause.
                withdrawn = withdraw(team_id)
                if not released:
                    released.append(True)
                    turn.finish()
                return withdrawn

            service.human_challenges.withdraw_team = turn_publishes

            stopped = service.stop_chat("team_1")

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
            turn = PausingTurn(service, service.human_challenges)
            self.addCleanup(turn.release.set)
            turn.start()
            network = controller.assistant_lifecycle._network

            def turn_commits(team_id: str) -> object:
                # Stop holds the Team lock but has not reached the token: the turn pauses and commits first.
                turn.finish()
                return network(team_id)

            controller.assistant_lifecycle._network = turn_commits

            stopped = service.stop_chat("team_1")

            self.assertTrue(stopped["accepted"])
            self.assertEqual(turn.outcome[0]["status"], "human-required")
            self.assertIsNone(service.human_challenges.current("team_1"))
            self.assertIsNone(controller.chat_continuations.current("team_1"))
            self.assertIsNone(controller.action_state.current_batch(GENERATION))

    def test_stop_withdraws_the_pkce_state_of_an_oauth_start_it_overlaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory, human=False)
            service = controller.chat_turn_service
            paused = service.chat("team_1", dict(CHAT_BODY), "openai", "sk-test-0123456789")
            self.assertEqual(paused["status"], "integrations-required")
            service.oauth_service = integration_service.BrokeredOAuthIntegrationService(
                challenge=service.oauth_pkce,
                store=controller.assistant_integrations,
                broker=integration_broker.OAuthBrokerClient(integration_broker.FixedBrokerTransport()),
            )
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
