"""Hosted Stop interrupts its Team's running turn even when continuation cleanup or runtime inspection fails.

Stop ends only its own Team's continuations, so another Team's failing cleanup never reaches it. When its own cleanup
fails, or the Team runtime cannot be inspected or is not running, the cancelled turn's Brain request is still aborted
and its executing Action fail-stopped before the error is reported.
"""

from __future__ import annotations

import unittest
from contextlib import nullcontext
from http import HTTPStatus
from types import SimpleNamespace
from unittest import mock

import docker
import hosted_assistant_fixture as harness
from test_hosted_human_requests import PUBLISH_ZONE

from action import challenges as action_challenges
from integrations import challenges as integration_challenges
from tests import human_request_fixtures

api = harness.hosted_chat_api
segment = harness.hosted_chat_segment
state = harness.runtime_state
assistants = harness.hosted_assistants

TOKEN = "turn-token"


class HostedStopCleanupFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.container = SimpleNamespace(id="container", status="running", reload=mock.Mock())
        self.lease = SimpleNamespace(owner="account_1", container_id="container")
        self.now = [1000.0]
        self.humans = action_challenges.HumanChallengeStore(retain_expired=True, clock=lambda: self.now[0])
        self.brain_abort = SimpleNamespace(abort=mock.Mock())
        self.action_container = SimpleNamespace(id="action")
        self.fail_stop = mock.Mock()
        self.unavailable = state.ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Team Action execution state is unavailable")
        for patcher in (
            mock.patch.object(api.hosted_resources, "_require_current_authorization", return_value=self.container),
            mock.patch.object(state, "_lock_for", return_value=nullcontext()),
            mock.patch.object(state, "_integration_challenges", integration_challenges.IntegrationChallengeStore()),
            mock.patch.object(state, "_human_challenges", self.humans),
            mock.patch.object(segment, "_purge_hosted_human_pending", side_effect=self.unavailable),
            mock.patch.object(state._docker.containers, "get", return_value=self.action_container),
            mock.patch.object(assistants, "_fail_stop_action", self.fail_stop),
            mock.patch.dict(state._active_chat_tokens, {"team_1": TOKEN}, clear=True),
            mock.patch.dict(state._active_chat_container_ids, {"team_1": "container"}, clear=True),
            mock.patch.dict(state._active_action_container_ids, {"team_1": (TOKEN, "action")}, clear=True),
            mock.patch.dict(state._brain_aborts, {TOKEN: self.brain_abort}, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(state._cancelled_chat_tokens.discard, TOKEN)

    def _pause(self, team_id: str) -> action_challenges.PendingHumanChallenge:
        pending = assistants._PendingHostedChat(
            SimpleNamespace(), (), (), "account_1", ("generation",), paused_batch="b" * 64
        )
        request = human_request_fixtures.request("approval", title="Publish zone", description="Publish it.")
        requirement = human_request_fixtures.requirement(request, **PUBLISH_ZONE)
        return self.humans.create(team_id, requirement, pending)

    def _assert_interrupted(self) -> None:
        self.assertIn(TOKEN, state._cancelled_chat_tokens)
        self.brain_abort.abort.assert_called_once_with()
        self.fail_stop.assert_called_once_with("team_1", self.action_container)

    def test_another_teams_failing_expiry_never_blocks_this_teams_stop(self) -> None:
        other = self._pause("team_2")
        self.now[0] = other.expires_at

        result = api._stop_chat("team_1", self.lease)

        self.assertEqual((result["accepted"], result["confirmed"]), (True, True))
        self._assert_interrupted()
        # The other Team's expired continuation stays for its own cleanup.
        self.assertEqual([item.team_id for item in self.humans.drain_expired("team_2")], ["team_2"])

    def test_a_failing_cleanup_of_this_team_still_interrupts_its_turn(self) -> None:
        self._pause("team_1")

        with self.assertRaises(state.ApiError) as raised:
            api._stop_chat("team_1", self.lease)

        self.assertIs(raised.exception, self.unavailable)
        self._assert_interrupted()
        self.assertIsNone(self.humans.current("team_1"))

    def test_a_runtime_that_cannot_be_inspected_still_has_its_turn_interrupted(self) -> None:
        failure = docker.errors.APIError("unavailable")
        self.container.reload.side_effect = failure

        with self.assertRaises(docker.errors.APIError) as raised:
            api._stop_chat("team_1", self.lease)

        self.assertIs(raised.exception, failure)
        self._assert_interrupted()

    def test_a_runtime_that_is_not_running_still_has_its_turn_interrupted(self) -> None:
        self.container.status = "exited"

        with self.assertRaises(state.ApiError) as raised:
            api._stop_chat("team_1", self.lease)

        self.assertEqual(raised.exception.status, HTTPStatus.CONFLICT)
        self._assert_interrupted()


if __name__ == "__main__":
    unittest.main()
