"""Hosted Stop and Team destruction abort the Brain request the chat turn is blocked on (ADR-0079)."""

from __future__ import annotations

import time
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

import hosted_assistant_fixture as harness
from test_brain_runtime_client import context
from test_local_chat_brain_abort import _silent_client, _submit

api = harness.hosted_chat_api
# The client of the loaded Hosted app, whose Brain request reads the abort that app's turn registers. A separately
# imported copy, which a run of these tests alone gets, would never see that abort and stay blocked in the turn.
brain_runtime_client = harness.hosted_chat_segment.brain_runtime_client
state = harness.runtime_state
lifecycle = harness.hosted_lifecycle


class HostedStopAbortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.brain, self.client = _silent_client(self, brain_runtime_client)
        self.container = SimpleNamespace(id="container", status="running", reload=mock.Mock())
        self.lease = SimpleNamespace(owner="account_1", container_id="container")
        for patcher in (
            mock.patch.object(api.hosted_resources, "_require_current_authorization", return_value=self.container),
            mock.patch.object(state, "_lock_for", return_value=nullcontext()),
            mock.patch.object(api.hosted_chat_lifecycle, "cancel_paused_integration", return_value=False),
            mock.patch.object(api.hosted_chat_human, "cancel_pending", return_value=False),
            mock.patch.dict(state._active_chat_tokens, {}, clear=True),
            mock.patch.dict(state._active_chat_container_ids, {}, clear=True),
            mock.patch.dict(state._active_action_container_ids, {}, clear=True),
            mock.patch.dict(state._brain_aborts, {}, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _blocked_turn(self):
        def turn() -> None:
            with api._exclusive_chat_turn("team_1", self.lease):
                self.client.start(context("sk-test-0123456789abcdef"), "Hello", conversation=())

        future = _submit(turn)

        def release() -> None:
            # Whatever the test's outcome, its turn ends and frees the Team chat slot before the next test runs.
            for abort in tuple(state._brain_aborts.values()):
                abort.abort()
            future.exception(5)

        self.addCleanup(release)
        self.assertTrue(self.brain.received.wait(5))
        return future

    def test_stop_wakes_the_blocked_brain_request_and_releases_the_turn(self):
        future = self._blocked_turn()
        started = time.monotonic()
        result = api._stop_chat("team_1", self.lease)

        self.assertIsInstance(future.exception(5), brain_runtime_client.BrainRuntimeError)
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(result["accepted"])
        self.assertTrue(self.brain.hung_up.wait(5))
        self.assertEqual(state._brain_aborts, {})
        self.assertEqual(state._cancelled_chat_tokens & {"token"}, set())
        # The Team chat lock is free for the next turn at once.
        with api._exclusive_chat_turn("team_1", self.lease):
            pass

    def test_destroying_the_team_aborts_its_brain_request(self):
        future = self._blocked_turn()
        state._close_chat_registration("team_1")
        self.addCleanup(state._reopen_chat_registration, "team_1")
        self.assertIsInstance(future.exception(5), brain_runtime_client.BrainRuntimeError)
        self.assertTrue(self.brain.hung_up.wait(5))
        self.assertEqual(state._brain_aborts, {})

    def _refused(self) -> None:
        # The turn won the chat lock and passed authorization, but destruction closed registration first.
        with (
            self.assertRaisesRegex(state.ApiError, "being destroyed"),
            api._exclusive_chat_turn("team_1", self.lease),
        ):
            self.fail("a Team whose destruction began must not start a turn")
        self.assertEqual(state._active_chat_tokens, {})
        self.assertEqual(state._brain_aborts, {})
        lock = state._chat_lock_for("team_1")
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_registration_stays_closed_after_a_failed_destruction_until_one_succeeds(self):
        cleanup_lease = SimpleNamespace(owner="account_1", container_id="container", cleanup_nonce="nonce")
        self.addCleanup(state._reopen_chat_registration, "team_1")
        with (
            mock.patch.object(lifecycle.hosted_resources, "_require_cleanup_authorization"),
            mock.patch.object(
                lifecycle, "_stop_and_tear_down", side_effect=state.ApiError(lifecycle.HTTPStatus.CONFLICT, "timeout")
            ),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._destroy("team_1", cleanup_lease)
        self._refused()
        with (
            mock.patch.object(lifecycle.hosted_resources, "_require_cleanup_authorization"),
            mock.patch.object(lifecycle, "_stop_and_tear_down", return_value={"destroyed": True}),
        ):
            self.assertEqual(lifecycle._destroy("team_1", cleanup_lease), {"destroyed": True})
        # Destruction completed, so the next turn registers again.
        with api._exclusive_chat_turn("team_1", self.lease) as (token, _container):
            self.assertEqual(state._active_chat_tokens, {"team_1": token})

    def test_closing_without_an_active_turn_or_handle_still_closes_registration(self):
        state._close_chat_registration("team_1")
        self.assertIn("team_1", state._draining_chats)
        self._refused()
        state._reopen_chat_registration("team_1")
        with mock.patch.dict(state._active_chat_tokens, {"team_1": "token"}):
            state._close_chat_registration("team_1")
            self.assertIn("token", state._cancelled_chat_tokens)
        state._reopen_chat_registration("team_1")
        state._cancelled_chat_tokens.discard("token")
        self.assertEqual(state._draining_chats, set())


if __name__ == "__main__":
    unittest.main()
