"""A request authorized for one Hosted Team generation never reads or ends a replacement's pending gates."""

import dataclasses
import io
import sys
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hosted_assistant_fixture import (
    hosted_assistants,
    hosted_chat_api,
    hosted_chat_segment,
    hosted_controller,
    hosted_resources,
    runtime_state,
)

from action import challenges as action_challenges
from hosted import container as container_spec
from integrations import challenges as integration_challenges
from tests import human_request_fixtures

TEAM_ID = "team_1"
OLD_CONTAINER = "a" * 64
REPLACEMENT_CONTAINER = "b" * 64


def _replacement() -> SimpleNamespace:
    """The same Team id recreated by another Account after the old request was authorized."""
    name = container_spec.team_container_name(TEAM_ID)
    labels = {"team.runtime": "1", "team.id": TEAM_ID, "team.owner": "account_2"}
    return SimpleNamespace(
        id=REPLACEMENT_CONTAINER,
        name=name,
        status="running",
        labels=labels,
        attrs={"Name": f"/{name}", "Config": {"Labels": labels}},
        reload=mock.Mock(),
    )


PAUSED_BATCH = "d" * 64


def _stale_lease() -> hosted_resources._AuthorizationLease:
    return hosted_resources._AuthorizationLease(TEAM_ID, OLD_CONTAINER, "account_1", ("account", "account_1"))


def _current_lease() -> hosted_resources._AuthorizationLease:
    return hosted_resources._AuthorizationLease(TEAM_ID, REPLACEMENT_CONTAINER, "account_2", ("account", "account_2"))


def _pending_chat() -> hosted_assistants._PendingHostedChat:
    return hosted_assistants._PendingHostedChat(
        SimpleNamespace(turn=SimpleNamespace(actions=())),
        ("assistant-1",),
        ("replacement-file",),
        "account_2",
        (REPLACEMENT_CONTAINER,),
        paused_batch=PAUSED_BATCH,
    )


def _human_requirement() -> action_challenges.HumanRequirement:
    descriptor = {
        "kind": "auth:totp",
        "ordinal": 0,
        "title": "Confirm protected action",
        "description": "Use an enrolled second factor.",
    }
    human_request = human_request_fixtures.admit(human_request_fixtures.fingerprinted(descriptor), ("auth:totp",))
    return action_challenges.HumanRequirement(
        "assistant-1",
        "Assistant One",
        "replacement-action",
        "Replacement action",
        "interrupt-1",
        human_request,
        "0.4.1",
        copy=human_request_fixtures.copy(human_request),
    )


class ReplacedHostedTeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.humans = action_challenges.HumanChallengeStore(retain_expired=True)
        self.integrations = integration_challenges.IntegrationChallengeStore()
        self.humans.create(TEAM_ID, _human_requirement(), _pending_chat())
        # An Integration pause comes before its Action batch, so its continuation holds none.
        self.integrations.create(TEAM_ID, (object(),), dataclasses.replace(_pending_chat(), paused_batch=None))
        self.journal = mock.Mock()
        self.projection = mock.Mock(return_value={"status": "integration-required", "purpose": "replacement"})
        for patcher in (
            mock.patch.object(runtime_state, "_human_challenges", self.humans),
            mock.patch.object(runtime_state, "_integration_challenges", self.integrations),
            mock.patch.object(runtime_state, "_action_execution_journal", return_value=self.journal),
            mock.patch.object(hosted_resources, "_get_container", return_value=_replacement()),
            mock.patch.object(hosted_resources, "_cleanup_record", return_value=None),
            mock.patch.object(hosted_resources.network_policy, "runtime_identity_valid", return_value=True),
            mock.patch.object(hosted_chat_segment, "_hosted_integration_challenge_payload", self.projection),
            mock.patch.object(runtime_state, "_enforce_rate"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _assert_replacement_untouched(self) -> None:
        self.assertIsNotNone(self.humans.current(TEAM_ID))
        self.assertIsNotNone(self.integrations.current(TEAM_ID))
        self.projection.assert_not_called()
        self.journal.purge.assert_not_called()
        self.journal.purge_batch.assert_not_called()
        self.journal.end_settled.assert_not_called()

    @staticmethod
    def _lifecycle_locked(_team_id: str = TEAM_ID) -> bool:
        return runtime_state._lock_for(TEAM_ID).locked()

    def test_a_valid_stop_of_a_stopped_runtime_still_ends_its_gates_and_reports_the_conflict(self) -> None:
        stopped = _replacement()
        stopped.status = "exited"
        with (
            mock.patch.object(hosted_resources, "_get_container", return_value=stopped),
            mock.patch.object(hosted_resources, "_require_team_isolation"),
            self.assertRaises(runtime_state.ApiError) as caught,
        ):
            hosted_chat_api._stop_chat(TEAM_ID, _current_lease())

        self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)
        self.assertIsNone(self.humans.current(TEAM_ID))
        self.assertIsNone(self.integrations.current(TEAM_ID))
        self.journal.purge_batch.assert_called_once_with(REPLACEMENT_CONTAINER, PAUSED_BATCH)

    def test_an_oauth_start_on_a_replaced_team_is_refused_by_its_stale_authority(self) -> None:
        challenge = self.integrations.current(TEAM_ID)
        with self.assertRaises(runtime_state.ApiError) as current:
            hosted_chat_api._refuse_oauth_start(TEAM_ID, challenge.id, _current_lease())
        with self.assertRaises(runtime_state.ApiError) as stale:
            hosted_chat_api._refuse_oauth_start(TEAM_ID, challenge.id, _stale_lease())

        self.assertEqual(current.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(stale.exception.status, HTTPStatus.NOT_FOUND)

    def _handler(self) -> hosted_controller.Handler:
        handler = object.__new__(hosted_controller.Handler)
        handler.wfile = io.BytesIO()
        handler._send_json = mock.Mock()
        handler._stream_chat = mock.Mock()
        handler._read_body = mock.Mock(
            return_value={"message": "hello", "files": [], "assistant_ids": [], "conversation": [], "locale": None}
        )
        return handler

    def _request(self) -> hosted_controller._AuthorizedRequest:
        return hosted_controller._AuthorizedRequest(
            {"team_id": TEAM_ID}, TEAM_ID, ("account", "account_1"), _stale_lease(), {}
        )

    def test_ordinary_chat_with_an_old_lease_never_returns_the_replacement_gate(self) -> None:
        with self.assertRaises(runtime_state.ApiError) as caught:
            hosted_chat_api._chat(TEAM_ID, "hello", [], (), _stale_lease())

        self.assertEqual(caught.exception.status, HTTPStatus.NOT_FOUND)
        self._assert_replacement_untouched()

    def test_streamed_chat_and_pending_reads_with_an_old_lease_send_nothing(self) -> None:
        routes = {
            "stream": lambda handler: handler._route_chat_turn(self._request(), stream=True),
            "integrations": lambda handler: handler._route_chat_integrations(self._request(), submit=False),
            "human": lambda handler: handler._route_chat_human(self._request(), submit=False),
        }
        for name, route in routes.items():
            handler = self._handler()
            with self.subTest(route=name), self.assertRaises(runtime_state.ApiError) as caught:
                route(handler)

            self.assertEqual(caught.exception.status, HTTPStatus.NOT_FOUND)
            handler._send_json.assert_not_called()
            handler._stream_chat.assert_not_called()
            self._assert_replacement_untouched()

    def test_stop_with_an_old_lease_cancels_and_purges_nothing_of_the_replacement(self) -> None:
        with self.assertRaises(runtime_state.ApiError) as caught:
            hosted_chat_api._stop_chat(TEAM_ID, _stale_lease())

        self.assertEqual(caught.exception.status, HTTPStatus.NOT_FOUND)
        self._assert_replacement_untouched()

    def test_the_authorized_generation_still_reads_and_stops_its_own_gates(self) -> None:
        lease = _current_lease()
        handler = self._handler()
        request = hosted_controller._AuthorizedRequest(
            {"team_id": TEAM_ID}, TEAM_ID, ("account", "account_2"), lease, {}
        )

        handler._route_chat_integrations(request, submit=False)
        self.assertEqual(handler._send_json.call_args.args[1]["purpose"], "replacement")
        # Destruction holds the lifecycle lock, so it cannot end the generation between revalidation and the read.
        self.assertTrue(hosted_chat_api._authorized_pending(TEAM_ID, lease, self._lifecycle_locked))
        with mock.patch.object(hosted_resources, "_require_team_isolation"):
            result = hosted_chat_api._stop_chat(TEAM_ID, lease)

        self.assertTrue(result["accepted"])
        self.assertIsNone(self.humans.current(TEAM_ID))
        self.assertIsNone(self.integrations.current(TEAM_ID))
        self.journal.purge_batch.assert_called_once_with(REPLACEMENT_CONTAINER, PAUSED_BATCH)


if __name__ == "__main__":
    unittest.main()
