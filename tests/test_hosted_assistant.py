from __future__ import annotations

import contextlib
import copy
import json
import os
import tempfile
import types
import unittest
from dataclasses import replace
from email.message import Message
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hosted_assistant_fixture import (
    HOSTED_BINDING,
    HOSTED_SPEC,
    app,
    hosted_apps,
    hosted_chat_segment,
    hosted_controller,
    hosted_resources,
    runtime_state,
)

from hosted import container as container_spec

integration_challenges = runtime_state.integration_challenges
assistant_manifest = hosted_apps.assistant_manifest
brain_runtime_client = runtime_state.brain_runtime_client
chat_orchestrator = hosted_chat_segment.chat_orchestrator
assistant_registry = hosted_apps.assistant_registry
network_policy = hosted_resources.network_policy
integration_store = runtime_state.integration_store
integration_http = runtime_state.integration_http
power_journal = runtime_state.power_journal
hosted_egress_policy = hosted_apps.egress_policy
dynamic_assistants = hosted_apps.dynamic_assistants


class _RouteHarness:
    def __init__(self, body: dict | None = None) -> None:
        self.body = body
        self.read_count = 0
        self.sent: list[tuple[HTTPStatus, dict]] = []

    def _read_team_body(self, keys: set[str]) -> dict:
        self.read_count += 1
        if self.body is None or set(self.body) != keys:
            raise AssertionError("unexpected body contract")
        return self.body

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        self.sent.append((status, payload))


class HostedHttpBoundaryTests(unittest.TestCase):
    def test_every_hosted_operation_has_one_dispatch_handler(self) -> None:
        strict_http = hosted_controller.strict_http
        hosted_operations = {
            route.operation
            for route in strict_http.CONTROLLER_ROUTES
            if strict_http.HOSTED_CONTROLLER in route.profiles
        }
        dispatch_groups = (
            set(hosted_controller._GLOBAL_ROUTES),
            set(hosted_controller._PREAUTHORIZED_ROUTES),
            set(hosted_controller._AUTHORIZED_ROUTES),
        )
        self.assertEqual(set().union(*dispatch_groups), hosted_operations)
        self.assertEqual(sum(map(len, dispatch_groups)), len(hosted_operations))

    def test_legacy_app_routes_are_absent(self) -> None:
        strict_http = hosted_controller.strict_http
        for method, parts in (
            ("GET", ("v1", "teams", "team_1", "apps")),
            ("POST", ("v1", "teams", "team_1", "apps")),
            (
                "DELETE",
                ("v1", "teams", "team_1", "apps", "notification-center"),
            ),
        ):
            with self.subTest(method=method):
                self.assertIsNone(
                    strict_http.resolve_controller_route(
                        strict_http.HOSTED_CONTROLLER,
                        method,
                        parts,
                    )
                )

    @staticmethod
    def _handler(body: bytes, *headers: tuple[str, str]) -> app.Handler:
        handler = object.__new__(app.Handler)
        handler.headers = Message()
        for name, value in headers:
            handler.headers.add_header(name, value)
        handler.rfile = BytesIO(body)
        return handler

    def test_operator_bearer_is_constant_time_and_duplicate_headers_fail_closed(self) -> None:
        accepted = self._handler(b"", ("Authorization", "Bearer operator-token"))
        wrong = self._handler(b"", ("Authorization", "Bearer operator-tokee"))
        duplicate = self._handler(
            b"",
            ("Authorization", "Bearer operator-token"),
            ("Authorization", "Bearer operator-token"),
        )

        with mock.patch.object(
            hosted_controller.strict_http.hmac,
            "compare_digest",
            wraps=hosted_controller.strict_http.hmac.compare_digest,
        ) as compare:
            self.assertEqual(accepted._principal(), ("operator", None))
            self.assertIsNone(wrong._principal())
            self.assertIsNone(duplicate._principal())

        self.assertEqual(compare.call_count, 2)

    def test_read_body_accepts_one_strict_json_object(self) -> None:
        body = b'{"team_name":"Marketing"}'
        handler = self._handler(
            body,
            ("Content-Length", str(len(body))),
            ("Content-Type", "application/json; charset=utf-8"),
        )

        self.assertEqual(handler._read_body(), {"team_name": "Marketing"})

    def test_read_body_rejects_ambiguous_or_non_object_documents(self) -> None:
        cases = (
            (b"{}", (("Transfer-Encoding", "chunked"), ("Content-Type", "application/json")), HTTPStatus.BAD_REQUEST),
            (b'{"a":1,"a":2}', (("Content-Type", "application/json"),), HTTPStatus.BAD_REQUEST),
            (b'{"a":NaN}', (("Content-Type", "application/json"),), HTTPStatus.BAD_REQUEST),
            (b"[]", (("Content-Type", "application/json"),), HTTPStatus.UNPROCESSABLE_ENTITY),
            (b"{}", (), HTTPStatus.UNSUPPORTED_MEDIA_TYPE),
            (b"{}", (("Content-Type", "text/plain"),), HTTPStatus.UNSUPPORTED_MEDIA_TYPE),
        )

        for body, extra_headers, expected_status in cases:
            headers = (("Content-Length", str(len(body))), *extra_headers)
            handler = self._handler(body, *headers)
            with self.subTest(body=body, headers=headers), self.assertRaises(runtime_state.ApiError) as caught:
                handler._read_body()
            self.assertEqual(caught.exception.status, expected_status)

    def test_account_installs_exact_verified_assistant_publication(self) -> None:
        source_digest = f"sha256:{'a' * 64}"
        resolution = {
            "assistant_id": "example-assistant",
            "source_digest": source_digest,
            "oci_digest": f"sha256:{'b' * 64}",
        }
        binding = SimpleNamespace(assistant_id="example-assistant")
        request = hosted_controller._AuthorizedRequest(
            {},
            "team_1",
            ("account", "account_1"),
            mock.sentinel.lease,
            {},
        )
        handler = object.__new__(app.Handler)
        handler._read_team_body = mock.Mock(
            return_value={
                "assistant_id": "example-assistant",
                "source_digest": source_digest,
            }
        )
        client = mock.Mock()
        client.resolve.return_value = resolution
        trust = mock.Mock()
        handler._publication_dependencies = mock.Mock(return_value=(client, trust))
        handler._send_json = mock.Mock()

        def install(*_args, authorize_start, **_kwargs):
            authorize_start()
            return {
                "source_digest": source_digest,
                "oci_digest": resolution["oci_digest"],
                "binding_digest": f"sha256:{'c' * 64}",
            }

        with (
            mock.patch.object(runtime_state, "_enforce_rate"),
            mock.patch.object(
                hosted_controller.dynamic_assistants,
                "binding_from_resolution",
                return_value=binding,
            ),
            mock.patch.object(
                hosted_controller.publication,
                "assistant_spec",
                return_value=mock.sentinel.spec,
            ),
            mock.patch.object(hosted_resources, "_prepare_assistant_image"),
            mock.patch.object(
                hosted_apps,
                "_install_assistant",
                side_effect=install,
            ) as install_assistant,
        ):
            handler._route_assistant_install(request)

        self.assertEqual(client.resolve.call_count, 2)
        client.resolve.assert_called_with(source_digest)
        trust.verify.assert_called_once_with(resolution)
        install_assistant.assert_called_once_with(
            "team_1",
            binding,
            "account_1",
            mock.sentinel.lease,
            authorize_start=mock.ANY,
        )
        status, payload = handler._send_json.call_args.args
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(payload["assistant"], "example-assistant")
        self.assertEqual(payload["source_digest"], source_digest)

    def test_assistant_publication_install_requires_account_identity(self) -> None:
        request = hosted_controller._AuthorizedRequest(
            {},
            "team_1",
            ("operator", None),
            mock.sentinel.lease,
            {},
        )
        handler = object.__new__(app.Handler)

        with self.assertRaises(runtime_state.ApiError) as caught:
            handler._route_assistant_install(request)

        self.assertEqual(caught.exception.status, HTTPStatus.UNAUTHORIZED)

    def test_assistant_publication_must_match_requested_identifier(self) -> None:
        source_digest = f"sha256:{'a' * 64}"
        request = hosted_controller._AuthorizedRequest(
            {},
            "team_1",
            ("account", "account_1"),
            mock.sentinel.lease,
            {},
        )
        handler = object.__new__(app.Handler)
        handler._read_team_body = mock.Mock(
            return_value={
                "assistant_id": "expected-assistant",
                "source_digest": source_digest,
            }
        )
        client = mock.Mock()
        client.resolve.return_value = {
            "assistant_id": "different-assistant",
            "source_digest": source_digest,
        }
        handler._publication_dependencies = mock.Mock(return_value=(client, mock.Mock()))

        with (
            mock.patch.object(runtime_state, "_enforce_rate"),
            self.assertRaises(runtime_state.ApiError) as caught,
        ):
            handler._route_assistant_install(request)

        self.assertEqual(caught.exception.status, HTTPStatus.NOT_FOUND)

    def test_lists_only_durable_dynamic_assistant_bindings(self) -> None:
        request = hosted_controller._AuthorizedRequest(
            {},
            "team_1",
            ("account", "account_1"),
            mock.sentinel.lease,
            {},
        )
        handler = object.__new__(app.Handler)
        handler._send_json = mock.Mock()
        inventory = {
            "team_id": "team_1",
            "assistants": [
                {"assistant": "example-assistant", "status": "running"},
            ],
        }

        with mock.patch.object(
            hosted_apps,
            "_list_assistants",
            return_value=inventory,
        ):
            handler._route_assistant_list(request)

        handler._send_json.assert_called_once_with(
            HTTPStatus.OK,
            {"assistants": [{"assistant": "example-assistant", "status": "running"}]},
            no_store=True,
        )

    def test_uninstalls_only_a_bound_dynamic_assistant(self) -> None:
        request = hosted_controller._AuthorizedRequest(
            {"assistant_id": "example-assistant"},
            "team_1",
            ("account", "account_1"),
            mock.sentinel.lease,
            {},
        )
        handler = object.__new__(app.Handler)
        handler._send_json = mock.Mock()

        with (
            mock.patch.object(
                runtime_state._dynamic_assistants,
                "get",
                return_value=SimpleNamespace(assistant_id="example-assistant"),
            ),
            mock.patch.object(
                hosted_apps,
                "_uninstall_assistant",
                return_value={"uninstalled": True},
            ) as uninstall,
            mock.patch.object(hosted_controller.audit, "log", return_value="trace"),
        ):
            handler._route_assistant_uninstall(request)

        uninstall.assert_called_once_with(
            "team_1",
            "example-assistant",
            mock.sentinel.lease,
        )
        handler._send_json.assert_called_once_with(
            HTTPStatus.OK,
            {
                "assistant": "example-assistant",
                "uninstalled": True,
                "trace_id": "trace",
            },
            no_store=True,
        )


class HostedAllowedHostsAdmissionTests(unittest.TestCase):
    @staticmethod
    def _container_with_environment(environment: dict[str, str]):
        return types.SimpleNamespace(
            attrs={"Config": {"Env": [f"{key}={value}" for key, value in environment.items()]}},
        )

    def test_manifest_must_match_reviewed_hosts_before_admission(self) -> None:
        spec = HOSTED_SPEC
        container = types.SimpleNamespace(id="assistant-generation")
        reviewed_contracts: list[assistant_manifest.ManifestContract] = []

        def admit(_container, reviewed):
            reviewed_contracts.append(reviewed)
            return reviewed

        cache = types.SimpleNamespace(
            get=admit,
        )
        machine_cache = types.SimpleNamespace(get=lambda _container, _integrations, reviewed: reviewed)
        with (
            mock.patch.multiple(
                runtime_state,
                _assistant_allowed_hosts_cache=cache,
                _assistant_machine_contract_cache=machine_cache,
            ),
            mock.patch.object(
                hosted_apps,
                "_require_assistant_genesis",
                return_value="Use reviewed Powers.",
            ),
        ):
            self.assertEqual(hosted_apps._admit_assistant_contract(spec, container), tuple(sorted(spec.allowed_hosts)))
        self.assertEqual(len(reviewed_contracts), 1)

        self.assertEqual(
            {account.id: (account.provider, account.scopes) for account in reviewed_contracts[0].integrations},
            {
                account_id: (account.provider, tuple(sorted(account.scopes)))
                for account_id, account in spec.contract.integrations.items()
            },
        )
        exact = reviewed_contracts[0]
        account = exact.integrations[0]
        drifted = (
            replace(exact, integrations=(replace(account, provider="other"),)),
            replace(exact, integrations=(replace(account, scopes=("tweet.read",)),)),
        )
        with (
            mock.patch.multiple(
                runtime_state,
                _assistant_allowed_hosts_cache=assistant_manifest.ManifestContractCache(),
                _assistant_machine_contract_cache=machine_cache,
            ),
            mock.patch.object(assistant_manifest, "read_container_manifest_contract", return_value=exact),
        ):
            self.assertEqual(hosted_apps._require_assistant_allowed_hosts(spec, container), exact.allowed_hosts)
        for declared in drifted:
            with (
                self.subTest(declared=declared),
                mock.patch.multiple(
                    runtime_state,
                    _assistant_allowed_hosts_cache=assistant_manifest.ManifestContractCache(),
                    _assistant_machine_contract_cache=machine_cache,
                ),
                mock.patch.object(
                    assistant_manifest,
                    "read_container_manifest_contract",
                    return_value=declared,
                ),
                self.assertRaises(runtime_state.ApiError) as drift,
            ):
                hosted_apps._require_assistant_allowed_hosts(spec, container)
            self.assertEqual(drift.exception.status, HTTPStatus.CONFLICT)

        def reject(_container, _reviewed):
            raise assistant_manifest.ManifestError("mismatch")

        with (
            mock.patch.multiple(
                runtime_state,
                _assistant_allowed_hosts_cache=types.SimpleNamespace(get=reject),
                _assistant_machine_contract_cache=machine_cache,
            ),
            self.assertRaises(runtime_state.ApiError) as caught,
        ):
            hosted_apps._admit_assistant_contract(spec, container)
        self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)

    def test_assistant_readiness_does_not_require_http(self) -> None:
        container = types.SimpleNamespace(status="running", reload=mock.Mock())
        self.assertEqual(hosted_apps._wait_assistant_ready(container), (True, "running"))
        self.assertEqual(hosted_apps._assistant_ready_now(container), (True, "running"))

    def test_teardown_refuses_a_container_without_exact_assistant_identity(self) -> None:
        container = types.SimpleNamespace(attrs={}, reload=mock.Mock())
        with mock.patch.object(
            network_policy,
            "assistant_identity_valid",
            return_value=False,
        ):
            result = hosted_apps._teardown_assistant(
                "team_1",
                "shimpz-cloudflare",
                container=container,
            )
        self.assertFalse(result.artifacts_removed)

    def test_manifest_mismatch_rolls_back_before_policy_proxy_or_start(self) -> None:
        events: list[object] = []
        state = {"created": False}
        container = types.SimpleNamespace(
            id="assistant-generation",
            attrs={},
            labels={
                "team.assistant.runtime": "1",
                "team.id": "team_1",
                "team.assistant": "shimpz-cloudflare",
            },
            reload=lambda: None,
        )
        network = types.SimpleNamespace(
            disconnect=lambda target: events.append(("disconnect", target.id)),
            connect=lambda target, *, aliases: events.append(("connect-app", target.id, tuple(aliases))),
        )

        def create(**_kwargs):
            state["created"] = True
            events.append("create")
            return container

        engine = types.SimpleNamespace(containers=types.SimpleNamespace(create=create))

        def reject(_spec, _container):
            events.append("admit")
            raise runtime_state.ApiError(HTTPStatus.CONFLICT, "allowed_hosts mismatch")

        require_runtime = mock.Mock(side_effect=lambda: events.append("runtime"))
        with tempfile.TemporaryDirectory() as directory:
            Path(directory).chmod(0o770)
            with (
                mock.patch.multiple(
                    runtime_state,
                    _lock_for=lambda _team_id: contextlib.nullcontext(),
                    _docker=engine,
                    APP_EGRESS_POLICY_DIR=Path(directory),
                    APP_EGRESS_POLICY_GID=os.getgid(),
                ),
                mock.patch.multiple(
                    hosted_resources,
                    _require_current_authorization=lambda *_args, **_kwargs: types.SimpleNamespace(
                        labels={"team.name": "Marketing"}
                    ),
                    _prepare_assistant_image=lambda _spec: None,
                    _get_container=lambda _name: container if state["created"] else None,
                    _reserve_capacity=lambda *_args, **_kwargs: contextlib.nullcontext(),
                    _require_team_runtime=require_runtime,
                    _ensure_team_network=lambda _team_id: network,
                    _safe_connect=lambda *_args, **_kwargs: events.append("connect-proxy"),
                    _start_team_with_isolation=lambda _container, **_kwargs: events.append("start"),
                    _remove_team_container=lambda target: events.append(("remove-container", target.id)) or True,
                ),
                mock.patch.object(hosted_apps, "_admit_assistant_contract", side_effect=reject),
                mock.patch.object(
                    hosted_apps,
                    "_write_egress_policy",
                    side_effect=lambda *_args: events.append("write-policy"),
                ),
                mock.patch.object(
                    hosted_apps.publication,
                    "assistant_spec",
                    return_value=HOSTED_SPEC,
                ),
                mock.patch.object(hosted_apps, "_team_assistant_containers", return_value=[]),
                mock.patch.object(container_spec, "build_assistant_kwargs", return_value={}),
                mock.patch.object(network_policy, "assistant_identity_valid", return_value=True),
                self.assertRaises(runtime_state.ApiError) as caught,
            ):
                hosted_apps._install_assistant(
                    "team_1",
                    HOSTED_BINDING,
                    "account_1",
                    types.SimpleNamespace(owner="account_1"),
                    authorize_start=lambda: None,
                )
            self.assertEqual(list(Path(directory).rglob("*")), [Path(directory) / ".tokens"])

        self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)
        self.assertEqual(
            events,
            [
                "runtime",
                "create",
                ("disconnect", "assistant-generation"),
                ("connect-app", "assistant-generation", ("shimpz-cloudflare", "shimpz-cloudflare.team")),
                "admit",
                ("remove-container", "assistant-generation"),
            ],
        )
        require_runtime.assert_called_once_with()

    def test_existing_policy_bytes_must_match_the_admitted_hosts(self) -> None:
        hosts = ("api.open-meteo.com", "geocoding-api.open-meteo.com")
        with tempfile.TemporaryDirectory() as directory:
            Path(directory).chmod(0o770)
            with mock.patch.multiple(
                runtime_state,
                APP_EGRESS_POLICY_DIR=Path(directory),
                APP_EGRESS_POLICY_GID=os.getgid(),
            ):
                token = hosted_apps._assistant_egress_token("team_1", "shimpz-cloudflare")
                assert token is not None
                hosted_apps._write_egress_policy(token, hosts)
                self.assertEqual(
                    hosted_apps._validate_egress_policy("team_1", "shimpz-cloudflare", hosts),
                    token,
                )

                (Path(directory) / f"{token}.json").write_text('["evil.example"]', encoding="ascii")
                with self.assertRaises(runtime_state.ApiError) as caught:
                    hosted_apps._validate_egress_policy("team_1", "shimpz-cloudflare", hosts)
        self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)

    def test_egress_reservation_constructs_one_store_for_the_operation(self) -> None:
        hosts = ("api.open-meteo.com",)
        with tempfile.TemporaryDirectory() as directory:
            policy_root = Path(directory)
            policy_root.chmod(0o770)
            with (
                mock.patch.multiple(
                    runtime_state,
                    APP_EGRESS_POLICY_DIR=policy_root,
                    APP_EGRESS_POLICY_GID=os.getgid(),
                ),
                mock.patch.object(
                    hosted_egress_policy,
                    "EgressPolicyStore",
                    wraps=hosted_egress_policy.EgressPolicyStore,
                ) as store_constructor,
            ):
                token, environment = hosted_apps._reserve_egress_environment(
                    "team_1",
                    "shimpz-cloudflare",
                    hosts,
                )

        self.assertIsNotNone(token)
        self.assertEqual(environment, hosted_apps._egress_proxy_environment(token))
        store_constructor.assert_called_once_with(
            policy_root,
            os.getgid(),
            "localhost,127.0.0.1,::1,postgres,.team",
        )

    def test_nonempty_hosts_require_the_exact_admitted_proxy_token(self) -> None:
        token = "a" * 32
        hosts = ("api.open-meteo.com",)
        expected = hosted_apps._egress_proxy_environment(token)
        hosted_apps._validate_assistant_proxy_environment(
            self._container_with_environment(expected),
            token,
            hosts,
        )

        drifted_environments = {
            "wrong-token": {**expected, "HTTPS_PROXY": expected["HTTPS_PROXY"].replace(token, "b" * 32)},
            "missing-lowercase": {key: value for key, value in expected.items() if key != "https_proxy"},
            "http-proxy": {**expected, "HTTP_PROXY": "http://app-egress-proxy:8889"},
            "all-proxy": {**expected, "all_proxy": "http://app-egress-proxy:8889"},
        }
        for name, environment in drifted_environments.items():
            with self.subTest(name=name), self.assertRaises(runtime_state.ApiError) as caught:
                hosted_apps._validate_assistant_proxy_environment(
                    self._container_with_environment(environment),
                    token,
                    hosts,
                )
            self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)

    def test_empty_hosts_forbid_every_proxy_environment_variable(self) -> None:
        hosted_apps._validate_assistant_proxy_environment(
            self._container_with_environment({"SHIMPZ_TEAM_ID": "team_1"}),
            None,
            (),
        )

        for key in ("HTTPS_PROXY", "http_proxy", "ALL_PROXY", "no_proxy", "FTP_PROXY", "custom_proxy"):
            with self.subTest(key=key), self.assertRaises(runtime_state.ApiError) as caught:
                hosted_apps._validate_assistant_proxy_environment(
                    self._container_with_environment({key: "unexpected"}),
                    None,
                    (),
                )
            self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)

    def test_empty_hosts_build_no_proxy_environment(self) -> None:
        spec = HOSTED_SPEC
        kwargs = container_spec.build_assistant_kwargs(
            "team_1",
            "shimpz-cloudflare",
            spec,
            owner="account_1",
            source_digest="sha256:" + ("c" * 64),
        )
        environment = kwargs["environment"]

        self.assertFalse({key for key in environment if key.upper().endswith("_PROXY")})


class HostedDynamicAssistantResolutionTests(unittest.TestCase):
    @staticmethod
    def _resolution() -> dict[str, object]:
        vectors = json.loads(
            (Path(__file__).resolve().parents[1] / "protocol" / "install" / "v1" / "vectors.json").read_bytes()
        )
        resolution = copy.deepcopy(vectors["fixtures"]["resolve_response"]["value"])
        power = resolution["machine_contract"]["powers"][0]
        power["input_schema"]["additionalProperties"] = False
        power["output_schema"]["additionalProperties"] = False
        return resolution

    def test_dynamic_resolution_is_team_scoped_and_digest_bound(self) -> None:
        resolution = self._resolution()

        with tempfile.TemporaryDirectory() as directory:
            store = dynamic_assistants.DynamicAssistantStore(Path(directory) / "bindings.json")
            store.put("team_1", resolution)
            with mock.patch.object(runtime_state, "_dynamic_assistants", store):
                assistant_id, spec = hosted_apps._resolve_team_assistant("team_1", "hello-world")
                with self.assertRaises(assistant_registry.AssistantSpecError):
                    hosted_apps._resolve_team_assistant("team_2", "hello-world")

        self.assertEqual(assistant_id, "hello-world")
        self.assertEqual(spec.image, resolution["image_reference"])
        self.assertEqual(spec.required_image_labels[1][1], resolution["source_digest"])

    def test_dynamic_resolution_is_a_trusted_isolation_role(self) -> None:
        resolution = self._resolution()
        container = types.SimpleNamespace(
            attrs={
                "Config": {
                    "Labels": {
                        "team.id": "team_1",
                        "team.assistant": "hello-world",
                        "team.assistant.runtime": "1",
                        "team.assistant.dynamic": "1",
                    }
                }
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            store = dynamic_assistants.DynamicAssistantStore(Path(directory) / "bindings.json")
            store.put("team_1", resolution)
            with (
                mock.patch.object(runtime_state, "_dynamic_assistants", store),
                mock.patch.object(network_policy, "brain_identity_valid", return_value=False),
                mock.patch.object(hosted_resources, "_trusted_image_id", return_value="sha256:image"),
            ):
                trusted = hosted_resources._trusted_workload_image(container, "team_1")

        self.assertEqual(trusted, (resolution["image_reference"], "sha256:image", True))

    def test_dynamic_resolution_with_preloaded_spec_keeps_compact_posture(self) -> None:
        resolution = self._resolution()
        container = types.SimpleNamespace(
            attrs={
                "Config": {
                    "Labels": {
                        "team.id": "team_1",
                        "team.assistant": "hello-world",
                        "team.assistant.runtime": "1",
                        "team.assistant.dynamic": "1",
                    }
                },
                "State": {"Running": True},
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            store = dynamic_assistants.DynamicAssistantStore(Path(directory) / "bindings.json")
            binding = store.put("team_1", resolution)
            spec = hosted_apps.publication.assistant_spec(binding)
            with (
                mock.patch.object(network_policy, "brain_identity_valid", return_value=False),
                mock.patch.object(hosted_resources, "_trusted_image_id", return_value="sha256:image"),
            ):
                trusted = hosted_resources._trusted_workload_image(
                    container,
                    "team_1",
                    workload_spec=spec,
                )

        self.assertEqual(trusted, (resolution["image_reference"], "sha256:image", True))
        network = types.SimpleNamespace(attrs={})
        with (
            mock.patch.object(hosted_resources, "_team_runtime", return_value=container_spec.RUNTIME),
            mock.patch.object(
                hosted_resources,
                "_trusted_workload_image",
                return_value=(resolution["image_reference"], "sha256:image", True),
            ) as trusted_image,
            mock.patch.object(network_policy, "workload_security_valid", return_value=True) as posture,
            mock.patch.object(network_policy, "brain_identity_valid", return_value=False),
            mock.patch.object(network_policy, "workload_endpoint_valid", return_value=True),
            mock.patch.object(network_policy, "workload_live_membership_valid", return_value=True),
            mock.patch.object(hosted_resources, "_require_network_policy"),
            mock.patch.object(runtime_state._docker.networks, "get", return_value=network),
        ):
            hosted_resources._require_running_team_isolation(
                container,
                refreshed=True,
                workload_spec=spec,
            )

        self.assertIs(trusted_image.call_args.args[2], spec)
        self.assertTrue(posture.call_args.kwargs["compact_assistant_runtime"])

    def test_dynamic_install_persists_the_binding_and_returns_immutable_evidence(self) -> None:
        resolution = self._resolution()
        lease = types.SimpleNamespace(owner="creator_1")
        installed = {
            "team_id": "team_1",
            "assistant": "hello-world",
            "status": "running",
            "installed": True,
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incoming = dynamic_assistants.DynamicAssistantStore(root / "incoming.json").put(
                "team_1",
                resolution,
            )
            retained = dynamic_assistants.DynamicAssistantStore(root / "retained.json")
            with (
                mock.patch.object(runtime_state, "_dynamic_assistants", retained),
                mock.patch.object(hosted_apps, "_install_assistant_locked", return_value=installed),
            ):
                result = hosted_apps._install_assistant(
                    "team_1",
                    incoming,
                    "creator_1",
                    lease,
                    authorize_start=lambda: None,
                )

            self.assertIsNotNone(retained.get("team_1", "hello-world"))

        self.assertEqual(result["source_digest"], resolution["source_digest"])
        self.assertEqual(result["oci_digest"], resolution["oci_digest"])
        self.assertEqual(result["binding_digest"], incoming.binding_digest)

    def test_failed_dynamic_install_removes_binding_after_complete_rollback(self) -> None:
        resolution = self._resolution()
        lease = types.SimpleNamespace(owner="creator_1")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incoming = dynamic_assistants.DynamicAssistantStore(root / "incoming.json").put(
                "team_1",
                resolution,
            )
            retained = dynamic_assistants.DynamicAssistantStore(root / "retained.json")
            with (
                mock.patch.object(runtime_state, "_dynamic_assistants", retained),
                mock.patch.object(
                    hosted_apps,
                    "_install_assistant_locked",
                    side_effect=runtime_state.ApiError(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "rolled back",
                    ),
                ),
                self.assertRaises(runtime_state.ApiError),
            ):
                hosted_apps._install_assistant(
                    "team_1",
                    incoming,
                    "creator_1",
                    lease,
                    authorize_start=lambda: None,
                )

            self.assertIsNone(retained.get("team_1", "hello-world"))

    def test_failed_dynamic_install_retains_binding_for_incomplete_rollback(self) -> None:
        resolution = self._resolution()
        lease = types.SimpleNamespace(owner="creator_1")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incoming = dynamic_assistants.DynamicAssistantStore(root / "incoming.json").put(
                "team_1",
                resolution,
            )
            retained = dynamic_assistants.DynamicAssistantStore(root / "retained.json")
            with (
                mock.patch.object(runtime_state, "_dynamic_assistants", retained),
                mock.patch.object(
                    hosted_apps,
                    "_install_assistant_locked",
                    side_effect=hosted_apps._IncompleteInstallRollback(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "rollback is incomplete",
                    ),
                ),
                self.assertRaises(hosted_apps._IncompleteInstallRollback),
            ):
                hosted_apps._install_assistant(
                    "team_1",
                    incoming,
                    "creator_1",
                    lease,
                    authorize_start=lambda: None,
                )

            self.assertIsNotNone(retained.get("team_1", "hello-world"))
