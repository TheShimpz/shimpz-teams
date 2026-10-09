"""Hosted Stop ends a running turn even when that turn pauses while Stop is between its steps.

Stop cancels the turn's token before it withdraws challenges. A pause commits only while its token is current, so it
either committed before Stop (and Stop withdraws the challenge it published) or fails and rolls back its challenge
and, for a human request, its own paused Action batch.
"""

import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

import hosted_assistant_fixture as harness

from integrations import challenges as integration_challenges

api = harness.hosted_chat_api
segment = harness.hosted_chat_segment
state = harness.runtime_state
assistants = harness.hosted_assistants

TOKEN = "turn-token"
SCOPES = ("dns.read", "offline_access", "zone.read")


class HumanChallenges:
    """One Team's pending human challenge, as the process-local store keeps it."""

    def __init__(self) -> None:
        self.pending: dict[str, object] = {}

    def create(self, team_id: str, _requirement: object, payload: object) -> object:
        challenge = SimpleNamespace(team_id=team_id, payload=payload)
        self.pending[team_id] = challenge
        return challenge

    def current(self, team_id: str) -> object | None:
        return self.pending.get(team_id)

    def cancel_team(self, team_id: str) -> bool:
        return self.pending.pop(team_id, None) is not None


class HostedStopPauseRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.container = SimpleNamespace(id="container", status="running", reload=mock.Mock())
        self.lease = SimpleNamespace(owner="account_1", container_id="container")
        self.humans = HumanChallenges()
        self.purged: list[object] = []
        for patcher in (
            mock.patch.object(api.hosted_resources, "_require_current_authorization", return_value=self.container),
            mock.patch.object(state, "_lock_for", return_value=nullcontext()),
            mock.patch.object(state, "_integration_challenges", integration_challenges.IntegrationChallengeStore()),
            mock.patch.object(state, "_human_challenges", self.humans),
            mock.patch.object(
                api.hosted_chat_human, "cancel_pending", side_effect=lambda team_id: self.humans.cancel_team(team_id)
            ),
            mock.patch.object(segment, "_purge_hosted_human_pending", side_effect=self.purged.append),
            mock.patch.object(segment, "_hosted_integration_challenge_payload", return_value={}),
            mock.patch.object(segment, "_hosted_human_challenge_payload", return_value={}),
            mock.patch.dict(state._active_chat_tokens, {"team_1": TOKEN}, clear=True),
            mock.patch.dict(state._active_chat_container_ids, {"team_1": "container"}, clear=True),
            mock.patch.dict(state._active_action_container_ids, {}, clear=True),
            mock.patch.dict(state._brain_aborts, {}, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(state._cancelled_chat_tokens.discard, TOKEN)
        self.continuation = SimpleNamespace()
        self.pending = assistants._PendingHostedChat(self.continuation, (), (), "account_1", ("generation",), ())

    def _pause_integration(self) -> object:
        requirement = integration_challenges.IntegrationRequirement(
            assistant_id="shimpz-cloudflare",
            assistant_name="Shimpz Cloudflare",
            action_ids=("list-zones",),
            integrations=(("cloudflare", "cloudflare", SCOPES),),
        )
        outcome = SimpleNamespace(continuation=self.continuation)
        return segment._pause_hosted_connection("team_1", TOKEN, outcome, (requirement,), self.pending)

    def _pause_human(self) -> object:
        request = SimpleNamespace(kind="approval")
        outcome = SimpleNamespace(continuation=self.continuation, request=request)
        return segment._pause_hosted_human("team_1", TOKEN, outcome, (SimpleNamespace(request=request),), self.pending)

    def _stop_while_the_turn_publishes(self, pause) -> list[object]:
        paused: list[object] = []

        def turn_publishes() -> None:
            # Both withdrawals already ran; the running turn now publishes and commits its pause.
            if paused:
                return
            paused.append(None)
            try:
                paused[0] = pause()
            except state.ApiError as exc:
                paused[0] = exc

        self.container.reload.side_effect = turn_publishes
        stopped = api._stop_chat("team_1", self.lease)
        self.assertTrue(stopped["accepted"], "Stop missed the turn it was asked to end")
        self.assertIsInstance(paused[0], state.ApiError)
        return paused

    def test_stop_ends_an_integration_pause_published_after_its_withdrawal(self) -> None:
        self._stop_while_the_turn_publishes(self._pause_integration)

        self.assertIsNone(state._integration_challenges.current("team_1"), "the stopped turn stayed resumable")

    def test_stop_ends_a_human_pause_published_after_its_withdrawal(self) -> None:
        self._stop_while_the_turn_publishes(self._pause_human)

        self.assertIsNone(self.humans.current("team_1"), "the stopped turn stayed resumable")
        self.assertEqual(self.purged, [self.pending])

    def test_a_pause_committed_before_the_token_is_cancelled_is_withdrawn(self) -> None:
        self.assertEqual(self._pause_human(), {})

        stopped = api._stop_chat("team_1", self.lease)

        self.assertTrue(stopped["accepted"])
        self.assertIsNone(self.humans.current("team_1"))
