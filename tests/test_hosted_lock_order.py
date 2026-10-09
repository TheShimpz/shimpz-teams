"""The Hosted mutations that exclude a chat turn take the Team lock and then only try the Team chat slot.

Each is refused before any side effect while a chat turn holds the slot, and none holds the slot while it waits for the
Team lock, which destruction holds while it awaits the slot. Repeated creation and OAuth start already follow this
order and are covered with their own operations.
"""

import threading
import types
import unittest
from http import HTTPStatus
from unittest import mock

from hosted_assistant_fixture import (
    ANCHOR_ID,
    HOSTED_BINDING,
    assistant_lifecycle,
    hosted_chat_api,
    hosted_lifecycle,
    hosted_resources,
    runtime_state,
)


class ObservedTeamLock:
    """A Team lock that reports when a caller begins waiting for it."""

    def __init__(self, lock: threading.Lock, waiting: threading.Event) -> None:
        self._lock = lock
        self._waiting = waiting

    def __enter__(self) -> bool:
        self._waiting.set()
        return self._lock.__enter__()

    def __exit__(self, *args: object) -> None:
        self._lock.__exit__(*args)


class HostedLockOrderTests(unittest.TestCase):
    @staticmethod
    def _slot_mutations(lease: object) -> tuple[tuple[str, object], ...]:
        """The seven Hosted mutations that excluded a chat turn by taking the slot before the Team lock."""
        body = {"provider": "openai", "model": None, "effort": "low"}
        return (
            (
                "install",
                lambda: assistant_lifecycle._install_assistant(
                    "team_1", HOSTED_BINDING, "account_1", lease, authorize_start=lambda: None
                ),
            ),
            ("uninstall", lambda: assistant_lifecycle._uninstall_assistant("team_1", "shimpz-cloudflare", lease)),
            ("inference", lambda: hosted_lifecycle._configure_inference("team_1", body, lease)),
            ("runtime", lambda: hosted_lifecycle._lifecycle("team_1", "restart", lease)),
            (
                "disconnect",
                lambda: hosted_chat_api._disconnect_oauth_integration(
                    "team_1", "shimpz-cloudflare", "cloudflare", lease
                ),
            ),
            (
                "stored-input",
                lambda: hosted_chat_api._clear_assistant_stored_input("team_1", "shimpz-cloudflare", "token", lease),
            ),
            ("file", lambda: hosted_lifecycle._delete_team_file("team_1", "f" * 32, lease)),
        )

    def test_hosted_lifecycle_rejects_an_active_chat_before_any_mutation(self) -> None:
        lease = types.SimpleNamespace(owner="account_1", container_id=ANCHOR_ID)
        reached = mock.Mock(side_effect=AssertionError("a mutation acted while a chat turn held the slot"))
        chat_lock = runtime_state._chat_lock_for("team_1")
        self.assertTrue(chat_lock.acquire(blocking=False))
        try:
            with (
                mock.patch.object(hosted_resources, "_require_current_authorization", reached),
                mock.patch.object(runtime_state._dynamic_assistants, "get", reached),
                mock.patch.object(runtime_state._dynamic_assistants, "put", reached),
            ):
                for name, operation in self._slot_mutations(lease):
                    with self.subTest(mutation=name), self.assertRaises(runtime_state.ApiError) as caught:
                        operation()
                    self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)
        finally:
            chat_lock.release()
        reached.assert_not_called()

    def test_a_mutation_never_holds_the_chat_slot_destruction_awaits(self) -> None:
        # Destruction holds the Team lock and then awaits the chat slot: a mutation waiting for the Team lock must not
        # be holding the slot, or destruction would time out behind it.
        lease = types.SimpleNamespace(owner="account_1", container_id=ANCHOR_ID)
        for name, operation in self._slot_mutations(lease):
            with self.subTest(mutation=name):
                self._assert_waits_without_the_slot(operation)

    def _assert_waits_without_the_slot(self, operation) -> None:
        slot = runtime_state._chat_lock_for("team_1")
        team_lock = threading.Lock()
        waiting = threading.Event()
        outcome: list[object] = []

        def run() -> None:
            try:
                outcome.append(operation())
            except (runtime_state.ApiError, AssertionError) as error:  # reported to the test thread below
                outcome.append(error)

        mutation = threading.Thread(target=run, daemon=True)
        # A mutation that got past its refusal would fail here instead of acting on anything real.
        reached = mock.Mock(side_effect=AssertionError("a mutation acted while the test held the chat slot"))
        acquired = False
        with (
            mock.patch.object(runtime_state, "_lock_for", return_value=ObservedTeamLock(team_lock, waiting)),
            mock.patch.object(hosted_resources, "_require_current_authorization", reached),
            mock.patch.object(runtime_state._dynamic_assistants, "get", reached),
            mock.patch.object(runtime_state._dynamic_assistants, "put", reached),
        ):
            team_lock.acquire()
            try:
                mutation.start()
                if waiting.wait(5):
                    acquired = slot.acquire(timeout=5)
            finally:
                # Every lock the test owns is released and the worker joined before the patches end.
                team_lock.release()
                mutation.join(5)
                if acquired:
                    slot.release()
                mutation.join(5)
        self.assertTrue(waiting.is_set(), "the mutation never reached the Team lock")
        self.assertTrue(acquired, "the mutation held the chat slot that destruction awaits")
        self.assertFalse(mutation.is_alive())
        [refused] = outcome
        self.assertIsInstance(refused, runtime_state.ApiError)
        self.assertEqual(refused.status, HTTPStatus.CONFLICT)
        reached.assert_not_called()


if __name__ == "__main__":
    unittest.main()
