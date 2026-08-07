from __future__ import annotations

import types
import unittest
from contextlib import nullcontext
from http import HTTPStatus
from unittest import mock

from docker.errors import DockerException

from inference import client as brain_runtime_client
from integrations import challenges as integration_challenges
from integrations import flow as integration_flow
from integrations import service as integration_service
from integrations import store as integration_store
from local import app as local_app
from local.chat import pause as local_chat_pause
from local.chat import private as local_chat_private
from local.chat.types import PendingLocalChat
from power import challenges as power_challenges
from power import journal as power_journal


def _pending(
    *,
    identity: tuple[object, ...] = ("team", "a" * 64, (), [], object()),
    transcripts: tuple[object, ...] = (),
) -> PendingLocalChat:
    return PendingLocalChat(
        continuation=object(),
        assistant_ids=(),
        file_ids=(),
        provider="openai",
        identity=identity,
        transcripts=transcripts,
    )


class LocalChatPauseEdgeTests(unittest.TestCase):
    def test_human_projection_and_generation_purge_fail_closed(self) -> None:
        with (
            mock.patch.object(
                power_challenges,
                "challenge_payload",
                side_effect=power_challenges.HumanChallengeError("invalid"),
            ),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            local_chat_pause._human_response(object(), object())
        self.assertEqual(caught.exception.code, "human-request-invalid")

        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_pause._purge_human_generation(object(), None)
        self.assertEqual(caught.exception.code, "team-context-changed")

        subject = types.SimpleNamespace(
            power_state=types.SimpleNamespace(
                purge=mock.Mock(side_effect=power_journal.PowerJournalError("unavailable"))
            )
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_pause._purge_human_generation(subject, "generation")
        self.assertEqual(caught.exception.code, "power-state-unavailable")

    def test_terminal_human_failure_requires_terminal_commit(self) -> None:
        subject = types.SimpleNamespace(
            human_challenges=types.SimpleNamespace(cancel_team=mock.Mock()),
            _delete_chat_continuation=mock.Mock(),
            _purge_human_pending=mock.Mock(),
            _commit_chat_terminal=lambda *_args: False,
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_pause._terminal_human_failure(
                subject,
                "team_1",
                "token",
                _pending(),
                "denied",
            )
        self.assertEqual(caught.exception.code, "chat-stopped")

    def test_human_pause_rejects_invalid_secret_and_auth_sequences(self) -> None:
        request = types.SimpleNamespace(kind="input:text")
        outcome = types.SimpleNamespace(request=request)
        subject = types.SimpleNamespace(_terminal_human_failure=mock.Mock(return_value={"failed": True}))
        payload = _pending()

        self.assertEqual(
            local_chat_pause._pause_human(
                subject,
                "team_1",
                "token",
                outcome,
                (),
                payload,
            ),
            {"failed": True},
        )
        subject._terminal_human_failure.assert_called_with("team_1", "token", payload, "request-invalid")

        response = types.SimpleNamespace(secret=True)
        secret_payload = _pending(transcripts=(types.SimpleNamespace(responses=(response,)),))
        requirement = types.SimpleNamespace(request=request)
        local_chat_pause._pause_human(
            subject,
            "team_1",
            "token",
            outcome,
            (requirement,),
            secret_payload,
        )
        subject._terminal_human_failure.assert_called_with("team_1", "token", secret_payload, "secret-must-be-last")

        auth_request = types.SimpleNamespace(kind="auth:second-factor")
        auth_outcome = types.SimpleNamespace(request=auth_request)
        auth_requirement = types.SimpleNamespace(request=auth_request)
        local_chat_pause._pause_human(
            subject,
            "team_1",
            "token",
            auth_outcome,
            (auth_requirement,),
            payload,
        )
        subject._terminal_human_failure.assert_called_with("team_1", "token", payload, "authentication-unavailable")

    def test_human_pause_maps_store_conflict_and_rolls_back_persistence(self) -> None:
        request = types.SimpleNamespace(kind="input:text")
        requirement = types.SimpleNamespace(request=request)
        outcome = types.SimpleNamespace(request=request)
        payload = _pending()
        subject = types.SimpleNamespace(
            human_challenges=types.SimpleNamespace(
                create=mock.Mock(side_effect=power_challenges.HumanChallengeError("conflict")),
                cancel_team=mock.Mock(),
            )
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_pause._pause_human(
                subject,
                "team_1",
                "token",
                outcome,
                (requirement,),
                payload,
            )
        self.assertEqual(caught.exception.code, "human-request-conflict")

        challenge = types.SimpleNamespace(id="challenge")
        subject.human_challenges.create = lambda *_args: challenge
        subject._persist_chat_continuation = mock.Mock(
            side_effect=local_app.ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "unavailable",
                code="chat-continuation-unavailable",
            )
        )
        subject._purge_human_pending = mock.Mock()
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_pause._pause_human(
                subject,
                "team_1",
                "token",
                outcome,
                (requirement,),
                payload,
            )
        self.assertEqual(caught.exception.code, "chat-continuation-unavailable")
        subject.human_challenges.cancel_team.assert_called_with("team_1")
        subject._purge_human_pending.assert_called_with(payload)

    def test_integration_projection_and_pause_fail_closed(self) -> None:
        requirement = types.SimpleNamespace(assistant_id="assistant")
        challenge = types.SimpleNamespace(team_id="team_1", requirements=(requirement,))
        spec = types.SimpleNamespace(assistant_id="assistant")
        subject = types.SimpleNamespace(assistant_lifecycle=types.SimpleNamespace(_resolve=lambda *_args: spec))
        with (
            mock.patch.object(
                integration_flow,
                "challenge_payload",
                side_effect=integration_flow.IntegrationFlowError("invalid"),
            ),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            local_chat_pause._integration_response(subject, challenge)
        self.assertEqual(caught.exception.code, "assistant-integration-contract-invalid")

        subject = types.SimpleNamespace(
            integration_challenges=types.SimpleNamespace(
                create=mock.Mock(side_effect=integration_challenges.IntegrationChallengeError("conflict")),
                cancel_team=mock.Mock(),
            )
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_pause._pause_integration(
                subject,
                "team_1",
                "token",
                object(),
                (),
                _pending(),
            )
        self.assertEqual(caught.exception.code, "assistant-integration-challenge-conflict")

        pending_challenge = types.SimpleNamespace(id="challenge")
        subject.integration_challenges.create = lambda *_args: pending_challenge
        subject._persist_chat_continuation = mock.Mock(
            side_effect=local_app.ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "unavailable",
                code="chat-continuation-unavailable",
            )
        )
        with self.assertRaises(local_app.ApiProblem):
            local_chat_pause._pause_integration(
                subject,
                "team_1",
                "token",
                object(),
                (),
                _pending(),
            )
        subject.integration_challenges.cancel_team.assert_called_with("team_1")

        subject._persist_chat_continuation = mock.Mock()
        subject._commit_suspension = mock.Mock()
        subject._integration_response = mock.Mock(return_value={"pending": True})
        self.assertEqual(
            local_chat_pause._pause_integration(
                subject,
                "team_1",
                "token",
                object(),
                (),
                _pending(),
            ),
            {"pending": True},
        )


class LocalChatPrivateEdgeTests(unittest.TestCase):
    def test_power_integration_state_and_resolution_fail_closed(self) -> None:
        active = types.SimpleNamespace(
            spec=types.SimpleNamespace(
                assistant_id="assistant",
                powers={"power": types.SimpleNamespace(integrations=("integration",))},
                integrations={"integration": object()},
            )
        )
        subject = types.SimpleNamespace(
            assistant_integrations=types.SimpleNamespace(
                metadata=mock.Mock(side_effect=integration_store.OAuthIntegrationStoreError("unavailable"))
            ),
            _refresh_oauth_integration=mock.Mock(),
        )
        with self.assertRaises(power_journal.PowerJournalConflictError):
            local_chat_private._power_integration_generations(
                subject,
                "team_1",
                active,
                "power",
            )

        with (
            mock.patch.object(
                integration_flow,
                "resolve_power_integrations",
                side_effect=integration_flow.IntegrationFlowError("unavailable"),
            ),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            local_chat_private._resolve_power_integrations(
                subject,
                "team_1",
                active.spec,
                "power",
            )
        self.assertEqual(caught.exception.code, "assistant-integration-unavailable")

    def test_rpc_envelope_and_inventory_errors_are_mapped(self) -> None:
        request = brain_runtime_client.PowerRequest("interrupt", "assistant", "power", {})
        active = types.SimpleNamespace(spec=types.SimpleNamespace())
        subject = types.SimpleNamespace()
        with (
            mock.patch.object(
                local_chat_private.power_execution,
                "require_rpc_envelope",
                side_effect=ValueError("too large"),
            ),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            local_chat_private._require_power_rpc_envelope(
                subject,
                "team_1",
                {"assistant": active},
                request,
            )
        self.assertEqual(caught.exception.code, "assistant-power-input-too-large")

        for failure, expected_code in (
            (
                integration_store.OAuthIntegrationStoreError("unavailable"),
                "assistant-integration-state-unavailable",
            ),
            (
                integration_flow.IntegrationFlowError("invalid"),
                "assistant-integration-contract-invalid",
            ),
        ):
            with self.subTest(expected_code=expected_code):
                subject = types.SimpleNamespace(
                    _lock=lambda _team_id: nullcontext(),
                    assistant_lifecycle=types.SimpleNamespace(
                        _assistant_ids=lambda _team_id: (),
                        _resolve=mock.Mock(),
                    ),
                    assistant_integrations=object(),
                    _raise_integration_problem=local_chat_private._raise_integration_problem,
                )
                with (
                    mock.patch.object(
                        integration_flow,
                        "inventory_payload",
                        side_effect=failure,
                    ),
                    self.assertRaises(local_app.ApiProblem) as caught,
                ):
                    local_chat_private.list_assistant_integrations(subject, "team_1")
                self.assertEqual(caught.exception.code, expected_code)

    def test_authorization_start_maps_challenge_and_oauth_failures(self) -> None:
        cases = (
            (
                types.SimpleNamespace(
                    get=mock.Mock(side_effect=integration_challenges.IntegrationChallengeError("expired"))
                ),
                types.SimpleNamespace(),
                "assistant-integration-challenge-expired",
            ),
            (
                types.SimpleNamespace(get=lambda *_args: object()),
                types.SimpleNamespace(
                    authorization_url=mock.Mock(
                        side_effect=integration_service.OAuthIntegrationServiceError("unavailable")
                    )
                ),
                "assistant-integration-oauth-unavailable",
            ),
        )
        for challenges, oauth_service, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                subject = types.SimpleNamespace(
                    integration_challenges=challenges,
                    oauth_service=oauth_service,
                )
                with self.assertRaises(local_app.ApiProblem) as caught:
                    local_chat_private.start_assistant_integration_authorization(
                        subject,
                        "team_1",
                        "challenge",
                        "session",
                        "local",
                    )
                self.assertEqual(caught.exception.code, expected_code)

    def test_current_declaration_rejects_lifecycle_and_artifact_drift(self) -> None:
        declaration = object()
        spec = types.SimpleNamespace(integrations={"integration": declaration})

        def subject(*, assistant_ids=("assistant",), container=None, current=True):
            return types.SimpleNamespace(
                _lock=lambda _team_id: nullcontext(),
                assistant_lifecycle=types.SimpleNamespace(
                    _resolve=lambda *_args: spec,
                    _assistant_ids=lambda *_args, **_kwargs: assistant_ids,
                    _assistant_container=lambda *_args: container,
                    _has_current_assistant_artifact=lambda *_args: current,
                ),
            )

        with self.assertRaises(integration_service.OAuthIntegrationDeclarationError):
            local_chat_private._current_integration_declaration(
                subject(assistant_ids=()),
                "team_1",
                "assistant",
                "integration",
            )

        spec.integrations = {}
        with self.assertRaises(integration_service.OAuthIntegrationDeclarationError):
            local_chat_private._current_integration_declaration(
                subject(),
                "team_1",
                "assistant",
                "integration",
            )
        spec.integrations = {"integration": declaration}

        failing = types.SimpleNamespace(
            reload=mock.Mock(side_effect=DockerException("unavailable")),
            attrs={},
        )
        with self.assertRaises(integration_service.OAuthIntegrationDeclarationError):
            local_chat_private._current_integration_declaration(
                subject(container=failing),
                "team_1",
                "assistant",
                "integration",
            )

        for attrs, current in (([], True), ({"Config": []}, True), ({"Config": {}}, False)):
            with self.subTest(attrs=attrs, current=current):
                container = types.SimpleNamespace(reload=mock.Mock(), attrs=attrs)
                with self.assertRaises(integration_service.OAuthIntegrationDeclarationError):
                    local_chat_private._current_integration_declaration(
                        subject(container=container, current=current),
                        "team_1",
                        "assistant",
                        "integration",
                    )

    def test_oauth_mutation_failures_and_pending_projection_are_mapped(self) -> None:
        failure = integration_service.OAuthIntegrationServiceError("unavailable")
        operations = (
            (
                local_chat_private.complete_cloudflare_oauth_callback,
                {"state": "state", "claim": "claim", "session_binding": "session"},
                "complete",
            ),
            (
                local_chat_private.cancel_assistant_integration_authorization,
                {"session_binding": "session"},
                "cancel",
            ),
            (
                local_chat_private.disconnect_assistant_integration,
                {
                    "team_id": "team_1",
                    "assistant_id": "assistant",
                    "integration_id": "integration",
                },
                "disconnect",
            ),
        )
        for operation, arguments, method in operations:
            with self.subTest(operation=operation.__name__):
                subject = types.SimpleNamespace(
                    oauth_service=types.SimpleNamespace(**{method: mock.Mock(side_effect=failure)}),
                    _current_integration_declaration=mock.Mock(),
                )
                with self.assertRaises(local_app.ApiProblem) as caught:
                    operation(subject, **arguments)
                self.assertEqual(caught.exception.code, "assistant-integration-oauth-unavailable")

        subject = types.SimpleNamespace(
            assistant_lifecycle=types.SimpleNamespace(_network=mock.Mock()),
            integration_challenges=types.SimpleNamespace(current=lambda _team_id: None),
        )
        self.assertEqual(
            local_chat_private.pending_chat_integrations(subject, "team_1"),
            {"team_id": "team_1", "status": "none"},
        )
        challenge = object()
        subject.integration_challenges.current = lambda _team_id: challenge
        subject._integration_response = lambda value: {"challenge": value}
        self.assertEqual(
            local_chat_private.pending_chat_integrations(subject, "team_1"),
            {"challenge": challenge},
        )

        subject = types.SimpleNamespace(oauth_service=types.SimpleNamespace(disconnect=lambda *_args: True))
        self.assertEqual(
            local_chat_private.disconnect_assistant_integration(
                subject,
                "team_1",
                "assistant",
                "integration",
            ),
            {"disconnected": True},
        )


if __name__ == "__main__":
    unittest.main()
