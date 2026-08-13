"""Edge coverage for Hosted Assistant runtime contracts."""

from __future__ import annotations

import contextlib
import sys
import unittest
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hosted_assistant_fixture as harness

assistants = harness.hosted_assistants
lifecycle = harness.assistant_lifecycle
resources = harness.hosted_resources
state = harness.runtime_state

TEAM_ID = "team_1"
ASSISTANT_ID = "shimpz-cloudflare"
ACTION_ID = "list-zones"
CONTRACT = harness.HOSTED_SPEC.contract
TURN_TOKEN = "-".join(("turn", "token"))


def _container(**changes):
    values = {
        "id": "c" * 64,
        "labels": {"team.assistant": ASSISTANT_ID},
        "attrs": {"Config": {"Image": harness.HOSTED_SPEC.image}},
        "status": "running",
        "reload": mock.Mock(),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _active(container=None, contract=CONTRACT):
    return assistants._ActiveAssistant(
        ASSISTANT_ID,
        contract,
        container or _container(),
        harness.HOSTED_SPEC.image,
        "0.4.1",
        harness.HOSTED_SPEC.summary,
    )


class HostedAssistantRuntimeEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        state._active_chat_tokens.clear()
        state._cancelled_chat_tokens.clear()
        state._active_action_container_ids.clear()
        state._blocked_action_workloads.clear()

    def tearDown(self) -> None:
        self.setUp()

    def test_hosted_adapters_preserve_contract_and_fallback_image_identity(self) -> None:
        active = _active(_container(attrs={}))
        spec = assistants._hosted_integration_spec(active)
        bindings = assistants._integration_bindings({ASSISTANT_ID: active})

        self.assertEqual(spec.assistant_id, ASSISTANT_ID)
        self.assertEqual(spec.name, "Shimpz Cloudflare")
        self.assertIn(ACTION_ID, spec.actions)
        self.assertEqual(bindings[ASSISTANT_ID].spec, spec)
        self.assertEqual(assistants._hosted_action_identity(active), (active.container.id, active.image))

        active.container.attrs = {"Config": {"Image": "runtime:image"}}
        self.assertEqual(assistants._hosted_action_identity(active), (active.container.id, "runtime:image"))

    def test_installed_assistant_rejects_absent_blocked_and_invalid_identity(self) -> None:
        with (
            mock.patch.object(lifecycle, "_resolve_team_assistant", return_value=(ASSISTANT_ID, harness.HOSTED_SPEC)),
            mock.patch.object(resources, "_get_container", return_value=None),
            self.assertRaises(state.ApiError) as absent,
        ):
            assistants._installed_assistant(TEAM_ID, ASSISTANT_ID)
        self.assertEqual(absent.exception.status, HTTPStatus.CONFLICT)

        container = _container()
        state._blocked_action_workloads.add((TEAM_ID, container.id))
        with (
            mock.patch.object(lifecycle, "_resolve_team_assistant", return_value=(ASSISTANT_ID, harness.HOSTED_SPEC)),
            self.assertRaises(state.ApiError) as blocked,
        ):
            assistants._installed_assistant(TEAM_ID, ASSISTANT_ID, candidate=container)
        self.assertEqual(blocked.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)

        state._blocked_action_workloads.clear()
        with (
            mock.patch.object(lifecycle, "_resolve_team_assistant", return_value=(ASSISTANT_ID, harness.HOSTED_SPEC)),
            mock.patch.object(assistants.network_policy, "assistant_identity_valid", return_value=False),
            self.assertRaises(state.ApiError) as invalid,
        ):
            assistants._installed_assistant(TEAM_ID, ASSISTANT_ID, candidate=container)
        self.assertEqual(invalid.exception.status, HTTPStatus.CONFLICT)

    def test_installed_assistant_uses_default_egress_store_and_validates_runtime(self) -> None:
        container = _container()
        egress = object()
        with (
            mock.patch.object(lifecycle, "_resolve_team_assistant", return_value=(ASSISTANT_ID, harness.HOSTED_SPEC)),
            mock.patch.object(assistants.network_policy, "assistant_identity_valid", return_value=True),
            mock.patch.object(resources, "_require_running_team_isolation") as isolation,
            mock.patch.object(lifecycle, "_require_assistant_allowed_hosts", return_value=("api.example",)),
            mock.patch.object(lifecycle, "_egress_store", return_value=egress),
            mock.patch.object(lifecycle, "_validate_admitted_egress", return_value="token") as admitted,
            mock.patch.object(lifecycle, "_validate_assistant_proxy_environment") as proxy,
        ):
            result = assistants._installed_assistant(TEAM_ID, ASSISTANT_ID, candidate=container)

        self.assertEqual(result, (ASSISTANT_ID, CONTRACT, container))
        isolation.assert_called_once()
        admitted.assert_called_once_with(TEAM_ID, ASSISTANT_ID, ("api.example",), egress)
        proxy.assert_called_once_with(container, "token", ("api.example",), egress)

    def test_active_inventory_contains_docker_failures_and_filters_candidates(self) -> None:
        docker_error = assistants.docker.errors.DockerException("docker")
        with (
            mock.patch.object(lifecycle, "_team_assistant_containers", side_effect=docker_error),
            self.assertRaises(state.ApiError) as listed,
        ):
            assistants._active_team_assistants(TEAM_ID)
        self.assertEqual(listed.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)

        unlabeled = _container(labels={})
        unresolved = _container(id="1", labels={"team.assistant": "unresolved"})
        stopped = _container(id="2", status="exited")
        broken = _container(id="3", reload=mock.Mock(side_effect=docker_error))

        def resolve(_team, assistant_id, *_args):
            if assistant_id == "unresolved":
                raise assistants.assistant_registry.AssistantSpecError("invalid")
            return assistant_id, harness.HOSTED_SPEC

        with (
            mock.patch.object(
                lifecycle,
                "_team_assistant_containers",
                return_value=[unlabeled, unresolved, stopped],
            ),
            mock.patch.object(
                lifecycle,
                "_dynamic_binding_snapshot",
                return_value={ASSISTANT_ID: SimpleNamespace(resolution={"assistant_version": "0.4.1"})},
            ),
            mock.patch.object(lifecycle, "_egress_store", return_value=object()),
            mock.patch.object(lifecycle, "_resolve_team_assistant", side_effect=resolve),
        ):
            self.assertEqual(assistants._active_team_assistants(TEAM_ID), ())

        with (
            mock.patch.object(lifecycle, "_team_assistant_containers", return_value=[broken]),
            mock.patch.object(lifecycle, "_dynamic_binding_snapshot", return_value={}),
            mock.patch.object(lifecycle, "_egress_store", return_value=object()),
            mock.patch.object(lifecycle, "_resolve_team_assistant", return_value=(ASSISTANT_ID, harness.HOSTED_SPEC)),
            self.assertRaises(state.ApiError) as inspected,
        ):
            assistants._active_team_assistants(TEAM_ID)
        self.assertEqual(inspected.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)

    def test_active_inventory_rejects_duplicate_running_identity(self) -> None:
        first = _container(id="1")
        second = _container(id="2")
        with (
            mock.patch.object(lifecycle, "_team_assistant_containers", return_value=[first, second]),
            mock.patch.object(
                lifecycle,
                "_dynamic_binding_snapshot",
                return_value={ASSISTANT_ID: SimpleNamespace(resolution={"assistant_version": "0.4.1"})},
            ),
            mock.patch.object(lifecycle, "_egress_store", return_value=object()),
            mock.patch.object(lifecycle, "_resolve_team_assistant", return_value=(ASSISTANT_ID, harness.HOSTED_SPEC)),
            mock.patch.object(
                assistants,
                "_installed_assistant",
                side_effect=lambda *_args, candidate=None, **_kwargs: (ASSISTANT_ID, CONTRACT, candidate),
            ),
            self.assertRaises(state.ApiError) as duplicate,
        ):
            assistants._active_team_assistants(TEAM_ID)
        self.assertEqual(duplicate.exception.status, HTTPStatus.CONFLICT)

    def test_chat_scope_validation_and_selection_are_bounded(self) -> None:
        for value in ("invalid", [ASSISTANT_ID] * (assistants.MAX_CHAT_ASSISTANTS + 1)):
            with self.subTest(value=value), self.assertRaises(state.ApiError):
                assistants._chat_assistant_ids(value)
        with self.assertRaises(state.ApiError):
            assistants._chat_assistant_ids(["INVALID"])
        with self.assertRaises(state.ApiError):
            assistants._chat_assistant_ids([ASSISTANT_ID, ASSISTANT_ID])
        self.assertEqual(assistants._chat_assistant_ids(["z", "a"]), ("a", "z"))

        active = _active()
        self.assertEqual(assistants._select_team_assistants((active,), (ASSISTANT_ID,)), (active,))
        with self.assertRaises(state.ApiError):
            assistants._select_team_assistants((active,), ("missing",))

    def test_active_action_registration_release_cancellation_and_fail_stop(self) -> None:
        container = _container()
        with self.assertRaises(state.ApiError):
            assistants._register_active_action(TEAM_ID, "wrong", container)
        state._active_chat_tokens[TEAM_ID] = "token"
        assistants._register_active_action(TEAM_ID, "token", container)
        self.assertEqual(state._active_action_container_ids[TEAM_ID], ("token", container.id))
        with self.assertRaises(state.ApiError):
            assistants._register_active_action(TEAM_ID, "token", container)
        assistants._release_active_action(TEAM_ID, "other", container.id)
        self.assertIn(TEAM_ID, state._active_action_container_ids)
        assistants._release_optional_action(TEAM_ID, "token", container.id)
        self.assertNotIn(TEAM_ID, state._active_action_container_ids)
        assistants._register_optional_action(TEAM_ID, "token", container)
        assistants._release_optional_action(TEAM_ID, "token", container.id)
        assistants._register_optional_action(TEAM_ID, None, container)
        assistants._release_optional_action(TEAM_ID, None, container.id)

        state._cancelled_chat_tokens.add("cancelled")
        with self.assertRaises(state.ApiError):
            assistants._raise_if_rpc_cancelled("cancelled")
        assistants._raise_if_rpc_cancelled(None)

        with mock.patch.object(resources, "_fail_stop_team") as stopped:
            assistants._fail_stop_action(TEAM_ID, container)
        stopped.assert_called_once_with(container, timeout=3)
        with (
            mock.patch.object(resources, "_fail_stop_team", side_effect=state.ApiError(503, "failed")),
            self.assertRaises(state.ApiError),
        ):
            assistants._fail_stop_action(TEAM_ID, container)
        self.assertIn((TEAM_ID, container.id), state._blocked_action_workloads)

    def test_rpc_wrapper_maps_encoding_exchange_and_stream_close_failures(self) -> None:
        container = _container()
        invalid = assistants.AssistantRpcRequest(TEAM_ID, container, ACTION_ID, {}, None)
        with (
            mock.patch.object(assistants.action_execution, "encode_rpc_invocation", side_effect=ValueError("large")),
            self.assertRaises(state.ApiError) as encoded,
        ):
            assistants._assistant_rpc_exchange(invalid)
        self.assertEqual(encoded.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

        request = assistants.AssistantRpcRequest(
            TEAM_ID,
            container,
            ACTION_ID,
            {"input": {}, "integrations": {}},
            None,
        )
        exchange_error = assistants.action_execution.RpcExchangeError("failed")
        with (
            mock.patch.object(state, "_docker", SimpleNamespace(api=object())),
            mock.patch.object(assistants.action_execution, "rpc_exchange", side_effect=exchange_error),
            self.assertRaises(state.ApiError),
        ):
            assistants._assistant_rpc_exchange(request)

        stream = object()
        with mock.patch.object(assistants, "_close_exec_stream", side_effect=RuntimeError("close")):
            assistants._assistant_rpc_exchange.__globals__["contextlib"]
            with contextlib.suppress(Exception):
                assistants._close_exec_stream(stream)

        with mock.patch.object(assistants, "_assistant_rpc_exchange", return_value={"ok": True}) as exchange:
            self.assertEqual(assistants._assistant_rpc(TEAM_ID, "token", container, ACTION_ID, {}), {"ok": True})
        self.assertEqual(exchange.call_args.args[0].token, "token")

    def test_integration_generation_refresh_resolution_and_envelope_failures(self) -> None:
        active = _active()
        store_error = assistants.integration_store.OAuthIntegrationStoreError("state")
        with (
            mock.patch.object(assistants.action_execution, "integration_generations", side_effect=store_error),
            self.assertRaises(assistants.action_journal.ActionJournalConflictError),
        ):
            assistants._action_integration_generations(TEAM_ID, active, ACTION_ID)

        http_error = assistants.integration_http.OAuthHTTPError("provider", "failed")
        with (
            mock.patch.object(state._oauth_http, "refresh", side_effect=http_error),
            self.assertRaises(assistants.integration_store.OAuthIntegrationReauthorizationError),
        ):
            assistants._refresh_oauth_integration("provider", (), "refresh", None)

        flow_error = assistants.integration_flow.IntegrationFlowError("contract")
        with (
            mock.patch.object(assistants.integration_flow, "resolve_action_integrations", side_effect=flow_error),
            self.assertRaises(state.ApiError),
        ):
            assistants._resolve_action_integrations(TEAM_ID, active, ACTION_ID)

        request = SimpleNamespace(assistant_id="missing")
        with self.assertRaises(state.ApiError):
            assistants._require_hosted_action_rpc_envelope(TEAM_ID, {}, request)
        with (
            mock.patch.object(assistants.action_execution, "require_rpc_envelope", side_effect=ValueError("large")),
            self.assertRaises(state.ApiError) as envelope,
        ):
            assistants._require_hosted_action_rpc_envelope(
                TEAM_ID,
                {ASSISTANT_ID: active},
                SimpleNamespace(assistant_id=ASSISTANT_ID),
            )
        self.assertEqual(envelope.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

    def test_integration_inventory_maps_store_and_contract_failures(self) -> None:
        lease = object()
        for error, status in (
            (assistants.integration_store.OAuthIntegrationStoreError("state"), HTTPStatus.SERVICE_UNAVAILABLE),
            (assistants.integration_flow.IntegrationFlowError("contract"), HTTPStatus.CONFLICT),
        ):
            with (
                mock.patch.object(resources, "_require_current_authorization"),
                mock.patch.object(assistants, "_installed_assistant_specs", return_value=()),
                mock.patch.object(assistants.integration_flow, "inventory_payload", side_effect=error),
                self.assertRaises(state.ApiError) as caught,
            ):
                assistants._assistant_integration_inventory(TEAM_ID, lease)
            self.assertEqual(caught.exception.status, status)

    def test_installed_specs_filter_invalid_and_reject_duplicate_inventory(self) -> None:
        docker_error = assistants.docker.errors.DockerException("docker")
        with (
            mock.patch.object(lifecycle, "_team_assistant_containers", side_effect=docker_error),
            self.assertRaises(state.ApiError),
        ):
            assistants._installed_assistant_specs(TEAM_ID)

        unlabeled = _container(labels={})
        invalid = _container(labels={"team.assistant": "invalid"})
        first = _container(id="1")
        second = _container(id="2")

        def resolve(_team, assistant_id):
            if assistant_id == "invalid":
                raise assistants.assistant_registry.AssistantSpecError("invalid")
            return assistant_id, harness.HOSTED_SPEC

        with (
            mock.patch.object(
                lifecycle,
                "_team_assistant_containers",
                return_value=[unlabeled, invalid, first, second],
            ),
            mock.patch.object(lifecycle, "_resolve_team_assistant", side_effect=resolve),
            self.assertRaises(state.ApiError) as duplicate,
        ):
            assistants._installed_assistant_specs(TEAM_ID)
        self.assertEqual(duplicate.exception.status, HTTPStatus.CONFLICT)

        with (
            mock.patch.object(lifecycle, "_team_assistant_containers", return_value=[first]),
            mock.patch.object(lifecycle, "_resolve_team_assistant", return_value=(ASSISTANT_ID, harness.HOSTED_SPEC)),
        ):
            specs = assistants._installed_assistant_specs(TEAM_ID)
        self.assertEqual(tuple(spec.assistant_id for spec in specs), (ASSISTANT_ID,))

    def test_action_invocation_maps_contract_change_rpc_and_projection_failures(self) -> None:
        container = _container()
        active = _active(container)
        base = {
            "team_id": TEAM_ID,
            "token": TURN_TOKEN,
            "assistant_id": ASSISTANT_ID,
            "contract": CONTRACT,
            "container": container,
            "action": ACTION_ID,
            "payload": {"page": 1, "per_page": 25},
            "validated_assistant": active,
            "integration_values": {},
        }
        for action in (None, "INVALID", "missing"):
            with self.subTest(action=action), self.assertRaises(state.ApiError):
                assistants._invoke_assistant_action(assistants.ActionInvocationRequest(**(base | {"action": action})))

        with self.assertRaises(state.ApiError):
            assistants._invoke_assistant_action(
                assistants.ActionInvocationRequest(**(base | {"payload": {"page": 0, "per_page": 25}}))
            )

        changed = replace(active, assistant_id="changed")
        with self.assertRaises(state.ApiError):
            assistants._invoke_assistant_action(
                assistants.ActionInvocationRequest(**(base | {"validated_assistant": changed}))
            )

        for error, expected_message in (
            (state.ApiError(503, "rpc"), "rpc"),
            (assistants.action_execution.RpcSecretExposureError("secret"), "exposed protected data"),
            (assistants.action_execution.RpcInvalidResultError("invalid"), "invalid result"),
        ):
            patches = [mock.patch.object(assistants, "_assistant_rpc", return_value={})]
            if isinstance(error, state.ApiError):
                patches = [mock.patch.object(assistants, "_assistant_rpc", side_effect=error)]
            else:
                patches.append(mock.patch.object(assistants.action_execution, "project_rpc_result", side_effect=error))
            with contextlib.ExitStack() as stack:
                for current in patches:
                    stack.enter_context(current)
                with self.assertRaises(state.ApiError) as caught:
                    assistants._invoke_assistant_action(assistants.ActionInvocationRequest(**base))
            self.assertIn(expected_message, caught.exception.message)

        transcript = SimpleNamespace(
            responses=(object(),),
            payloads=lambda: ({"approved": True},),
            protected_values=lambda: (),
        )
        with (
            mock.patch.object(assistants, "_assistant_rpc", return_value={"type": "result"}) as rpc,
            mock.patch.object(assistants.action_execution, "project_rpc_result", return_value={"ok": True}),
        ):
            result = assistants._invoke_assistant_action(
                assistants.ActionInvocationRequest(**(base | {"transcript": transcript}))
            )
        self.assertEqual(result["result"], {"ok": True})
        self.assertEqual(rpc.call_args.args[-1]["responses"], ({"approved": True},))

    def test_action_payload_file_and_storage_errors_are_normalized(self) -> None:
        active = _active()
        with self.assertRaises(state.ApiError):
            assistants._validate_assistant_action_input({}, ASSISTANT_ID, ACTION_ID, {})
        with self.assertRaises(state.ApiError):
            assistants._validate_assistant_action_input({ASSISTANT_ID: active}, ASSISTANT_ID, "missing", {})
        with self.assertRaises(ValueError):
            assistants._validate_action_payload(CONTRACT, "missing", {}, output=False)

        self.assertEqual(assistants._validate_chat_file_ids(None), [])
        for value in ("invalid", [object()] * (assistants.MAX_CHAT_FILES + 1)):
            with self.assertRaises(state.ApiError):
                assistants._validate_chat_file_ids(value)

        for error, status in (
            (assistants.team_storage.StorageNotFoundError("missing"), HTTPStatus.NOT_FOUND),
            (assistants.team_storage.StorageInputError("invalid"), HTTPStatus.BAD_REQUEST),
            (assistants.team_storage.StorageError("unsafe"), HTTPStatus.SERVICE_UNAVAILABLE),
        ):
            with self.assertRaises(state.ApiError) as caught:
                assistants._raise_chat_storage_error(error)
            self.assertEqual(caught.exception.status, status)

    def test_file_metadata_connection_and_reader_contain_storage_failures(self) -> None:
        with assistants._chat_file_metadata_connection(TEAM_ID, []) as reader:
            self.assertIsNone(reader)

        storage = mock.Mock()
        storage.metadata_connection.return_value = contextlib.nullcontext("reader")
        storage.metadata.return_value = [{"id": "file"}]
        with mock.patch.object(state, "_storage", return_value=storage):
            with assistants._chat_file_metadata_connection(TEAM_ID, ["file"]) as reader:
                self.assertEqual(reader, "reader")
            self.assertEqual(assistants._chat_file_metadata(TEAM_ID, ["file"], "reader"), [{"id": "file"}])

        for operation in ("metadata_connection", "metadata"):
            failed = mock.Mock()
            setattr(failed, operation, mock.Mock(side_effect=assistants.team_storage.StorageError("unsafe")))
            with mock.patch.object(state, "_storage", return_value=failed), self.assertRaises(state.ApiError):
                if operation == "metadata_connection":
                    with assistants._chat_file_metadata_connection(TEAM_ID, ["file"]):
                        pass
                else:
                    assistants._chat_file_metadata(TEAM_ID, ["file"])

    def test_model_credentials_require_owner_api_key_and_current_generation(self) -> None:
        with self.assertRaises(state.ApiError):
            assistants._model_credential("", "openai")
        secret_error = assistants.integration_secrets_client.IntegrationSecretError("service")
        with (
            mock.patch.object(assistants.integration_secrets_client, "resolve", side_effect=secret_error),
            self.assertRaises(state.ApiError) as unavailable,
        ):
            assistants._model_credential("owner", "openai")
        self.assertEqual(unavailable.exception.status, HTTPStatus.BAD_GATEWAY)

        for credential in (None, ("oauth", "secret", 1)):
            with (
                mock.patch.object(assistants.integration_secrets_client, "resolve", return_value=credential),
                self.assertRaises(state.ApiError),
            ):
                assistants._model_credential("owner", "openai")
        with mock.patch.object(
            assistants.integration_secrets_client,
            "resolve",
            return_value=("api_key", "secret", 7),
        ):
            self.assertEqual(assistants._model_credential("owner", "openai"), ("secret", 7))

        with (
            mock.patch.object(assistants.integration_secrets_client, "generation_is_current", side_effect=secret_error),
            self.assertRaises(state.ApiError) as verify,
        ):
            assistants._require_model_credential_current("owner", "openai", 7)
        self.assertEqual(verify.exception.status, HTTPStatus.BAD_GATEWAY)
        with (
            mock.patch.object(assistants.integration_secrets_client, "generation_is_current", return_value=False),
            self.assertRaises(state.ApiError),
        ):
            assistants._require_model_credential_current("owner", "openai", 7)
        with mock.patch.object(assistants.integration_secrets_client, "generation_is_current", return_value=True):
            assistants._require_model_credential_current("owner", "openai", 7)


if __name__ == "__main__":
    unittest.main()
