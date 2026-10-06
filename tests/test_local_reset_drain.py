"""Space reset and Team destruction end every paused turn only once nothing can recreate one.

Both stop and drain the running turns first, so a turn pausing meanwhile fails its commit and rolls back; both clear
challenges, continuations, and PKCE state under the Team lock, so a relocalization that holds it persists first and
is cleared after. Reset also keeps turn registration closed until it ends.
"""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
from local_controller_harness import LocalContractCase
from test_local_human_purge_race import ObservedLock
from test_local_space_reset import LOCAL_TEAM_RESIDUES
from test_local_stop_pause_race import CHAT_BODY, PausingTurn, Runtime

from action import human as action_human
from local import app as local_app
from local import lifecycle as local_lifecycle
from local.chat import service as local_chat_service
from tests import human_request_fixtures


class ObservedSlot:
    """A Team execution slot that reports when an acquirer has to wait for its holder."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.waiting = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self.lock.acquire(blocking=False):
            return True
        if not blocking:
            return False
        self.waiting.set()
        return self.lock.acquire(timeout=timeout)

    def release(self) -> None:
        self.lock.release()


class LocalResetDrainTests(LocalContractCase):
    def _controller(self, directory: str) -> local_app.LocalController:
        controller = self._chat_controller(directory, Runtime())
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
        controller.client = object()
        # Reset's and destruction's Docker, Brain, and storage teardown is proven elsewhere; only paused turns matter.
        remaining = set(LOCAL_TEAM_RESIDUES) - {"chat_continuations", "preparation_helpers"}
        controller._reset_inventory = lambda: ([], [])
        controller._remove_space_resources = lambda *_args: (False, set(remaining))
        network = controller.assistant_lifecycle._network
        controller.assistant_lifecycle._network = lambda team_id, **_kwargs: network(team_id)
        controller._team_assistant_containers = lambda _team_id: []
        controller._validate_destroy_containers = lambda *_args: None
        controller._delete_team_conversation = lambda *_args: None
        controller._delete_team_routines = lambda _team_id: None
        controller._remove_team_assistants = lambda *_args: 0
        controller._delete_team_persistence = lambda _team_id: False
        controller._remove_team_network = lambda _network: False
        controller._delete_team_private_state = lambda _team_id: None
        patcher = mock.patch.object(local_lifecycle.local_prepare, "remove_helpers")
        patcher.start()
        self.addCleanup(patcher.stop)
        return controller

    def _assert_no_paused_turn(self, controller: local_app.LocalController, result: dict[str, object]) -> None:
        service = controller.chat_turn_service
        self.assertIn("chat_continuations", result["residue_absent"])
        self.assertIsNone(controller.chat_continuations.current("team_1"), "a paused turn survived its cleanup")
        self.assertIsNone(service.human_challenges.current("team_1"))
        self.assertIsNone(service.integration_challenges.current("team_1"))

    def _drain_a_turn_pausing_meanwhile(self, operation: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            service = controller.chat_turn_service
            turn = PausingTurn(service, "human", lambda: bool(service._cancelled_chat_tokens))
            self.addCleanup(turn.release.set)
            turn.start()
            store = controller.chat_continuations
            cleanup = "clear" if operation == "reset" else "delete"
            original = getattr(store, cleanup)

            def turn_pauses(*args: object) -> object:
                # The running turn publishes and commits its pause right after this cleanup ran.
                removed = original(*args)
                if threading.current_thread() is not turn.thread:
                    turn.finish()
                return removed

            with mock.patch.object(store, cleanup, side_effect=turn_pauses):
                result = (
                    controller.reset_space()
                    if operation == "reset"
                    else local_lifecycle._destroy_confirmed_team(controller, "team_1")
                )

            self.assertFalse(turn.thread.is_alive())
            self._assert_no_paused_turn(controller, result)
            [ended] = turn.outcome
            self.assertIsInstance(ended, local_app.ApiProblem)
            self.assertEqual(ended.code, "chat-stopped")

    def test_reset_drains_a_turn_that_pauses_meanwhile(self) -> None:
        self._drain_a_turn_pausing_meanwhile("reset")

    def test_destruction_drains_a_turn_that_pauses_meanwhile(self) -> None:
        self._drain_a_turn_pausing_meanwhile("destroy")

    def _clear_after_a_relocalization(self, operation: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            service = controller.chat_turn_service
            paused = service.chat("team_1", dict(CHAT_BODY), "openai", "sk-test-0123456789")
            self.assertEqual(paused["status"], "human-required")
            locks = tuple(ObservedLock() for _ in controller._locks)
            controller._locks = locks
            outcome: list[dict[str, object]] = []
            finished = threading.Event()

            def run() -> None:
                try:
                    outcome.append(
                        controller.reset_space()
                        if operation == "reset"
                        else local_lifecycle._destroy_confirmed_team(controller, "team_1")
                    )
                finally:
                    finished.set()

            runner = threading.Thread(target=run, daemon=True)
            self.addCleanup(runner.join, 10)
            reissue = service.human_challenges.reissue

            def reissued(*args: object) -> object:
                fresh = reissue(*args)
                # The cleanup arrives after the fresh challenge exists and before its continuation is persisted.
                for lock in locks:
                    lock.observed = runner
                runner.start()
                for _ in range(1000):
                    if finished.is_set() or any(lock.waiting.is_set() for lock in locks):
                        break
                    finished.wait(0.01)
                else:
                    raise AssertionError("the cleanup neither finished nor waited for the Team lock")
                return fresh

            service.human_challenges.reissue = reissued
            opened = service.open_chat_human("team_1", {"locale": "pt"})
            runner.join(10)

            self.assertEqual(opened["status"], "human-required")
            self.assertEqual(len(outcome), 1, "the cleanup failed in its thread")
            self._assert_no_paused_turn(controller, outcome[0])

    def test_reset_clears_a_relocalized_continuation(self) -> None:
        self._clear_after_a_relocalization("reset")

    def test_destruction_clears_a_relocalized_continuation(self) -> None:
        self._clear_after_a_relocalization("destroy")

    def test_a_turn_that_registers_while_the_space_resets_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self._controller(directory).chat_turn_service
            with service._drained_chat():
                with self.assertRaises(local_app.ApiProblem) as refused, service._exclusive_chat_turn("team_1"):
                    raise AssertionError("a turn registered while the Space was resetting")
                self.assertEqual(refused.exception.code, "space-resetting")
                self.assertEqual(service._active_chat_tokens, {})
            # The refused turn released its slot, and registration reopens once the reset ends.
            with service._exclusive_chat_turn("team_1") as token:
                self.assertEqual(service._active_chat_tokens, {"team_1": token})

    def test_a_reset_that_cannot_drain_releases_every_slot_and_reopens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            service = controller.chat_turn_service
            stopped = service._chat_lock("team_1")
            stuck = service._chat_lock("team_2")
            self.assertTrue(stuck.acquire(blocking=False))
            self.addCleanup(stuck.release)
            service._active_chat_tokens.update({"team_1": "stopped-turn", "team_2": "stuck-turn"})

            with (
                mock.patch.object(local_chat_service, "DRAIN_SECONDS", 0.05),
                self.assertRaises(local_app.ApiProblem) as refused,
            ):
                controller.reset_space()

            self.assertEqual(refused.exception.code, "chat-active")
            self.assertEqual(service._cancelled_chat_tokens, {"stopped-turn", "stuck-turn"})
            self.assertFalse(service._chat_closed)
            self.assertTrue(stopped.acquire(blocking=False), "a drained slot stayed held")
            stopped.release()

    def test_reset_waits_for_a_committed_turn_that_still_holds_its_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            service = controller.chat_turn_service
            # A turn committed its reply, so its token is gone, but it still holds its slot until it fully ends.
            slot = ObservedSlot()
            service._chat_locks["team_1"] = slot
            self.assertTrue(slot.acquire())
            events: list[str] = []
            remove = controller._remove_space_resources
            controller._remove_space_resources = lambda *args: events.append("cleanup") or remove(*args)
            finished = threading.Event()
            reset = threading.Thread(target=lambda: (controller.reset_space(), finished.set()), daemon=True)
            reset.start()
            for _ in range(500):
                if slot.waiting.is_set() or finished.is_set():
                    break
                finished.wait(0.01)
            events.append("post-commit write")
            slot.release()
            reset.join(5)

            self.assertTrue(finished.is_set())
            self.assertEqual(events, ["post-commit write", "cleanup"], "reset cleared before the turn left its slot")
