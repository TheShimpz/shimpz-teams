from __future__ import annotations

import types
import unittest
from contextlib import nullcontext
from http import HTTPStatus
from unittest import mock

from docker.errors import DockerException

from assistant import genesis as assistant_genesis
from inference import config as inference_config
from integrations import challenges as integration_challenges
from integrations import store as integration_store
from local import app as local_app
from local.chat import continuation as local_continuation
from local.chat import continuation_store
from local.chat import state as local_chat_state
from local.chat.types import ActiveAssistant, PendingLocalChat
from storage import files as team_storage


def _pending(identity: tuple[object, ...] = ()) -> PendingLocalChat:
    return PendingLocalChat(object(), (), (), "openai", identity)


def _stored(*, kind: str = "human", expires_at: int = 2_000) -> continuation_store.StoredContinuation:
    return continuation_store.StoredContinuation(
        "team_1",
        kind,
        "a" * 32,
        expires_at,
        1,
        ("binding",),
        b"payload",
    )


class LocalChatStateEdgeTests(unittest.TestCase):
    def test_file_metadata_validates_shape_and_maps_storage_failures(self) -> None:
        subject = types.SimpleNamespace()
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_state._chat_file_metadata(subject, "team_1", object())
        self.assertEqual(caught.exception.code, "invalid-files")

        cases = (
            (team_storage.StorageNotFoundError("missing"), "file-not-found"),
            (team_storage.StorageInputError("invalid"), "invalid-files"),
            (team_storage.StorageError("unavailable"), "storage-safety-failed"),
        )
        for failure, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                subject = types.SimpleNamespace(
                    storage=types.SimpleNamespace(metadata=mock.Mock(side_effect=failure)),
                    _raise_storage_problem=mock.Mock(
                        side_effect=local_app.ApiProblem(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            "storage unavailable",
                            code="storage-safety-failed",
                        )
                    ),
                )
                with self.assertRaises(local_app.ApiProblem) as caught:
                    local_chat_state._chat_file_metadata(subject, "team_1", [])
                self.assertEqual(caught.exception.code, expected_code)

    @staticmethod
    def _setup_subject() -> types.SimpleNamespace:
        network = types.SimpleNamespace(name="network", id="a" * 64)
        config = types.SimpleNamespace(provider="openai")
        return types.SimpleNamespace(
            _lock=lambda _team_id: nullcontext(),
            assistant_lifecycle=types.SimpleNamespace(
                _network=lambda _team_id: network,
                _validate_network=lambda *_args, **_kwargs: "Team",
            ),
            _active_chat_assistants=lambda *_args: (),
            _chat_file_metadata=lambda *_args: [],
            inference_store=types.SimpleNamespace(load=lambda _team_id: config),
        )

    def test_chat_setup_rejects_identity_selection_and_provider_drift(self) -> None:
        subject = self._setup_subject()
        subject.assistant_lifecycle._network = lambda _team_id: types.SimpleNamespace(
            name="network",
            id=None,
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_state._chat_setup(subject, "team_1", [], "openai", ())
        self.assertEqual(caught.exception.code, "ownership-conflict")

        subject = self._setup_subject()
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_state._chat_setup(
                subject,
                "team_1",
                [],
                "openai",
                ("missing",),
            )
        self.assertEqual(caught.exception.code, "assistant-unavailable")

        subject = self._setup_subject()
        subject.inference_store.load = mock.Mock(side_effect=inference_config.InferenceConfigError("unavailable"))
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_state._chat_setup(subject, "team_1", [], "openai", ())
        self.assertEqual(caught.exception.code, "inference-not-configured")

        subject = self._setup_subject()
        subject.inference_store.load = lambda _team_id: types.SimpleNamespace(provider="anthropic")
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_state._chat_setup(subject, "team_1", [], "openai", ())
        self.assertEqual(caught.exception.code, "inference-provider-mismatch")

    def test_genesis_resolution_rejects_docker_identity_and_contract_drift(self) -> None:
        spec = object()
        active = ActiveAssistant(spec, "container")
        subject = types.SimpleNamespace(
            client=types.SimpleNamespace(
                containers=types.SimpleNamespace(get=mock.Mock(side_effect=DockerException("unavailable")))
            )
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_state._active_assistant_genesis(subject, active)
        self.assertEqual(caught.exception.code, "assistant-genesis-unavailable")

        active = ActiveAssistant(spec, "container", types.SimpleNamespace(id="different"))
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_state._active_assistant_genesis(subject, active)
        self.assertEqual(caught.exception.code, "assistant-genesis-drift")

        container = types.SimpleNamespace(id="container")
        active = ActiveAssistant(spec, "container", container)
        subject._assistant_genesis_cache = types.SimpleNamespace(
            get=mock.Mock(side_effect=assistant_genesis.GenesisError("invalid"))
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_state._active_assistant_genesis(subject, active)
        self.assertEqual(caught.exception.code, "assistant-genesis-invalid")

    def test_active_assistant_inventory_fails_closed_on_docker_registry_and_blocking(self) -> None:
        lifecycle = types.SimpleNamespace(
            client=types.SimpleNamespace(
                containers=types.SimpleNamespace(list=mock.Mock(side_effect=DockerException("unavailable")))
            ),
            _assistant_filters=lambda _team_id: {},
        )
        subject = types.SimpleNamespace(assistant_lifecycle=lifecycle)
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_state._active_chat_assistants(subject, "team_1", "network")
        self.assertEqual(caught.exception.code, "docker-unavailable")

        container = types.SimpleNamespace(
            id="container",
            labels={local_app.ASSISTANT_LABEL: "assistant"},
            status="running",
        )
        lifecycle.client.containers.list.side_effect = None
        lifecycle.client.containers.list.return_value = [container]
        subject.registry = types.SimpleNamespace(get=lambda *_args: None)
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_state._active_chat_assistants(subject, "team_1", "network")
        self.assertEqual(caught.exception.code, "assistant-registry-drift")

        spec = types.SimpleNamespace(assistant_id="assistant")
        subject.registry.get = lambda *_args: spec
        lifecycle._validate_container = mock.Mock()
        lifecycle._blocked_action_workloads = {"container"}
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_state._active_chat_assistants(subject, "team_1", "network")
        self.assertEqual(caught.exception.code, "assistant-action-blocked")

        lifecycle._blocked_action_workloads = set()
        container.status = "exited"
        self.assertEqual(
            local_chat_state._active_chat_assistants(subject, "team_1", "network"),
            (),
        )

    def test_integration_state_mutations_map_store_failures_and_pruning(self) -> None:
        failure = integration_store.OAuthIntegrationStoreError("unavailable")
        operations = (
            (local_chat_state._delete_assistant_integration_state, "delete_assistant", ("team_1", "assistant")),
            (local_chat_state._delete_team_integration_state, "delete_team", ("team_1",)),
            (local_chat_state._delete_all_integration_state, "delete_all", ()),
        )
        for operation, method, arguments in operations:
            with self.subTest(operation=operation.__name__):
                subject = types.SimpleNamespace(
                    assistant_integrations=types.SimpleNamespace(**{method: mock.Mock(side_effect=failure)}),
                    _raise_integration_problem=mock.Mock(
                        side_effect=local_app.ApiProblem(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            "unavailable",
                            code="assistant-integration-state-unavailable",
                        )
                    ),
                )
                with self.assertRaises(local_app.ApiProblem) as caught:
                    operation(subject, *arguments)
                self.assertEqual(
                    caught.exception.code,
                    "assistant-integration-state-unavailable",
                )

        spec = types.SimpleNamespace(assistant_id="assistant", integrations={})
        subject = types.SimpleNamespace(
            assistant_integrations=types.SimpleNamespace(retain_declared=mock.Mock(side_effect=failure)),
            _raise_integration_problem=mock.Mock(
                side_effect=local_app.ApiProblem(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "unavailable",
                    code="assistant-integration-state-unavailable",
                )
            ),
        )
        with self.assertRaises(local_app.ApiProblem):
            local_chat_state._retain_declared_assistant_integration_state(
                subject,
                "team_1",
                spec,
            )

        subject.assistant_integrations.retain_declared.side_effect = None
        subject.assistant_integrations.retain_declared.return_value = True
        subject.integration_challenges = types.SimpleNamespace(cancel_team=mock.Mock())
        subject._delete_chat_continuation = mock.Mock()
        local_chat_state._retain_declared_assistant_integration_state(
            subject,
            "team_1",
            spec,
        )
        subject.integration_challenges.cancel_team.assert_called_once_with("team_1")

    def test_persistence_validates_lifetime_and_maps_codec_or_store_failures(self) -> None:
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_state._raise_chat_continuation_problem(continuation_store.ContinuationStoreError("unavailable"))
        self.assertEqual(caught.exception.code, "chat-state-unavailable")

        subject = types.SimpleNamespace(
            _raise_chat_continuation_problem=mock.Mock(
                side_effect=local_app.ApiProblem(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "unavailable",
                    code="chat-state-unavailable",
                )
            )
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_state._persist_chat_continuation(
                subject,
                "human",
                object(),
                (),
                _pending(),
            )
        self.assertEqual(caught.exception.code, "chat-state-unavailable")

        challenge = types.SimpleNamespace(
            id="a" * 32,
            team_id="team_1",
            expires_at=1.0,
        )
        with (
            mock.patch.object(local_chat_state.time, "monotonic", return_value=0),
            mock.patch.object(
                local_continuation,
                "encode",
                side_effect=local_continuation.ContinuationCodecError("invalid"),
            ),
            self.assertRaises(local_app.ApiProblem),
        ):
            local_chat_state._persist_chat_continuation(
                subject,
                "human",
                challenge,
                (),
                _pending(),
            )

    def test_restore_handles_expiry_kinds_and_challenge_errors(self) -> None:
        subject = types.SimpleNamespace()
        with mock.patch.object(local_chat_state.time, "time", return_value=2_000):
            self.assertIsNone(
                local_chat_state._restore_chat_continuation(
                    subject,
                    _stored(expires_at=2_000),
                )
            )

        decoded = types.SimpleNamespace(
            kind="human",
            requirements=(),
            pending=_pending(),
        )
        subject._raise_chat_continuation_problem = mock.Mock(
            side_effect=local_app.ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "unavailable",
                code="chat-state-unavailable",
            )
        )
        with (
            mock.patch.object(local_chat_state.time, "time", return_value=1_000),
            mock.patch.object(local_continuation, "decode", return_value=decoded),
            self.assertRaises(local_app.ApiProblem),
        ):
            local_chat_state._restore_chat_continuation(subject, _stored())

        decoded.kind = "human"
        decoded.requirements = (object(),)
        subject.human_challenges = types.SimpleNamespace(restore=mock.Mock())
        with (
            mock.patch.object(local_chat_state.time, "time", return_value=1_000),
            mock.patch.object(local_continuation, "decode", return_value=decoded),
        ):
            local_chat_state._restore_chat_continuation(subject, _stored())
        subject.human_challenges.restore.assert_called_once()

        decoded.kind = "unknown"
        with (
            mock.patch.object(local_chat_state.time, "time", return_value=1_000),
            mock.patch.object(local_continuation, "decode", return_value=decoded),
            self.assertRaises(local_app.ApiProblem),
        ):
            local_chat_state._restore_chat_continuation(subject, _stored())

        decoded.kind = "integrations"
        subject.integration_challenges = types.SimpleNamespace(
            restore=mock.Mock(side_effect=integration_challenges.IntegrationChallengeError("conflict"))
        )
        with (
            mock.patch.object(local_chat_state.time, "time", return_value=1_000),
            mock.patch.object(local_continuation, "decode", return_value=decoded),
            self.assertRaises(local_app.ApiProblem),
        ):
            local_chat_state._restore_chat_continuation(subject, _stored())

    def test_expired_human_purge_and_collection_adapters_fail_closed(self) -> None:
        subject = types.SimpleNamespace(_purge_human_pending=mock.Mock())
        self.assertIsNone(
            local_chat_state._purge_expired_human_continuation(
                subject,
                _stored(kind="integrations"),
            )
        )

        decoded = types.SimpleNamespace(kind="integrations", pending=_pending())
        subject._raise_chat_continuation_problem = mock.Mock(
            side_effect=local_app.ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "unavailable",
                code="chat-state-unavailable",
            )
        )
        with (
            mock.patch.object(local_continuation, "decode", return_value=decoded),
            self.assertRaises(local_app.ApiProblem),
        ):
            local_chat_state._purge_expired_human_continuation(subject, _stored())

        failure = continuation_store.ContinuationStoreError("unavailable")
        subject.chat_continuations = types.SimpleNamespace(drain_expired=mock.Mock(side_effect=failure))
        with self.assertRaises(local_app.ApiProblem):
            local_chat_state._restore_all_chat_continuations(subject)

        expired = _stored()
        active = _stored(kind="integrations")
        subject.chat_continuations = types.SimpleNamespace(
            drain_expired=lambda: (expired,),
            active=lambda: (active,),
        )
        subject._purge_expired_human_continuation = mock.Mock()
        subject._restore_chat_continuation = mock.Mock()
        local_chat_state._restore_all_chat_continuations(subject)
        subject._purge_expired_human_continuation.assert_called_once_with(expired)
        subject._restore_chat_continuation.assert_called_once_with(active)

        for operation, method, arguments in (
            (local_chat_state._delete_chat_continuation, "delete", ("team_1",)),
            (local_chat_state._clear_chat_continuations, "clear", ()),
        ):
            with self.subTest(operation=operation.__name__):
                subject.chat_continuations = types.SimpleNamespace(**{method: mock.Mock(side_effect=failure)})
                with self.assertRaises(local_app.ApiProblem):
                    operation(subject, *arguments)


if __name__ == "__main__":
    unittest.main()
