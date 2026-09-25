"""Local profile publication resolution and durable binding contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import ssl
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from action import stored_input as action_stored_input
from install.bindings import DynamicAssistantError, DynamicAssistantStore, binding_from_resolution
from install.contract import CONTRACT_ROOT
from install.icons import AssistantIconStore
from integrations import store as integration_store
from local import app as local_app
from local.assistant import api as assistant_api
from local.assistant import resources as local_resources
from local.chat import private as local_chat_private
from local.chat import state as local_chat_state
from local.errors import ApiProblemError
from local.install import registry as local_registry
from local.install.developers import (
    DevelopersClient,
    DevelopersError,
    DevelopersProtocolError,
    PublicationNotInstallableError,
)
from local.install.registry import AssistantRegistry

RESOLUTION = json.loads((CONTRACT_ROOT / "vectors.json").read_bytes())["fixtures"]["resolve_response"]["value"]
ICON = b"canonical icon"


def _runtime_resolution() -> dict[str, object]:
    resolution = copy.deepcopy(RESOLUTION)
    resolution["icon_digest"] = f"sha256:{hashlib.sha256(ICON).hexdigest()}"
    action = resolution["machine_contract"]["actions"][0]
    action["input_schema"]["additionalProperties"] = False
    action["output_schema"]["additionalProperties"] = False
    return resolution


class _Response:
    def __init__(self, status: int, value: object, *, raw: bytes | None = None) -> None:
        self.status = status
        self._body = raw if raw is not None else json.dumps(value, separators=(",", ":")).encode()

    def read(self, amount: int) -> bytes:
        return self._body[:amount]


class _Connection:
    response = _Response(500, {})
    requests: ClassVar[list[tuple[object, ...]]] = []
    connections: ClassVar[list[tuple[tuple[object, ...], dict[str, object]]]] = []
    tunnels: ClassVar[list[tuple[object, ...]]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.arguments = args, kwargs
        self.connections.append(self.arguments)

    def request(self, *args: object, **kwargs: object) -> None:
        self.requests.append((*args, kwargs))

    def set_tunnel(self, *args: object, **kwargs: object) -> None:
        self.tunnels.append((*args, kwargs))

    def getresponse(self) -> _Response:
        return self.response

    def close(self) -> None:
        pass


class LocalPublicationInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        _Connection.requests = []
        _Connection.connections = []
        _Connection.tunnels = []

    def test_resolves_exact_publication_through_fixed_release_proxy(self) -> None:
        _Connection.response = _Response(200, copy.deepcopy(RESOLUTION))

        with mock.patch("local.install.developers.http.client.HTTPSConnection", _Connection):
            resolved = DevelopersClient().resolve(RESOLUTION["source_digest"])

        self.assertEqual(resolved, RESOLUTION)
        self.assertEqual(
            _Connection.connections[0][0],
            ("shimpz-assistant-release", 8888),
        )
        connection_options = _Connection.connections[0][1]
        self.assertEqual(connection_options["timeout"], 10)
        tls_context = connection_options["context"]
        self.assertIsInstance(tls_context, ssl.SSLContext)
        self.assertTrue(tls_context.check_hostname)
        self.assertEqual(tls_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(_Connection.tunnels, [("developers.shimpz.com", 443, {})])
        method, path, request = _Connection.requests[0]
        self.assertEqual(method, "GET")
        self.assertEqual(path, f"/api/v1/assistant-publications/{RESOLUTION['source_digest']}")
        self.assertEqual(request, {"headers": {"Accept": "application/json"}})

    def test_resolves_visibility_bounded_latest_publication_from_installed_digest(self) -> None:
        successor = copy.deepcopy(RESOLUTION)
        successor["assistant_version"] = "0.2.0"
        successor["source_digest"] = f"sha256:{'9' * 64}"
        _Connection.response = _Response(200, successor)

        with mock.patch("local.install.developers.http.client.HTTPSConnection", _Connection):
            resolved = DevelopersClient().latest(RESOLUTION["source_digest"])

        self.assertEqual(resolved, successor)
        self.assertEqual(
            _Connection.requests[0][1],
            f"/api/v1/assistant-publications/{RESOLUTION['source_digest']}/latest",
        )

    def test_fetches_the_exact_digest_bound_icon(self) -> None:
        digest = f"sha256:{hashlib.sha256(ICON).hexdigest()}"
        _Connection.response = _Response(200, None, raw=ICON)
        with mock.patch("local.install.developers.http.client.HTTPSConnection", _Connection):
            value = DevelopersClient().icon(RESOLUTION["source_digest"], digest)

        self.assertEqual(value, ICON)
        self.assertEqual(
            _Connection.requests[0][1],
            f"/api/v1/assistant-publications/{RESOLUTION['source_digest']}/icon.png",
        )
        self.assertEqual(_Connection.requests[0][2], {"headers": {"Accept": "image/png"}})

    def test_resolution_fails_closed_for_missing_or_malformed_publication(self) -> None:
        for status, error in ((404, PublicationNotInstallableError), (503, DevelopersError)):
            _Connection.response = _Response(status, {})
            with (
                self.subTest(status=status),
                mock.patch("local.install.developers.http.client.HTTPSConnection", _Connection),
                self.assertRaises(error),
            ):
                DevelopersClient().resolve(RESOLUTION["source_digest"])

        malformed = copy.deepcopy(RESOLUTION)
        malformed["assistant_id"] = "../escape"
        _Connection.response = _Response(200, malformed)
        with (
            mock.patch("local.install.developers.http.client.HTTPSConnection", _Connection),
            self.assertRaises(DevelopersProtocolError),
        ):
            DevelopersClient().resolve(RESOLUTION["source_digest"])

    def test_resolution_rejects_a_publication_without_stored_inputs(self) -> None:
        missing_stored_inputs = copy.deepcopy(RESOLUTION)
        missing_stored_inputs.pop("stored_inputs")
        for action in missing_stored_inputs["machine_contract"]["actions"]:
            action.pop("stored_inputs")
        _Connection.response = _Response(200, missing_stored_inputs)

        with (
            mock.patch("local.install.developers.http.client.HTTPSConnection", _Connection),
            self.assertRaises(DevelopersProtocolError) as raised,
        ):
            DevelopersClient().resolve(RESOLUTION["source_digest"])

        self.assertEqual(raised.exception.__cause__.code, "schema_violation")

    def test_registry_binds_publications_independently_per_team(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = AssistantRegistry(DynamicAssistantStore(Path(directory) / "bindings.json"))
            first = registry.put("team_1", _runtime_resolution())
            second = registry.put("team_2", _runtime_resolution())

            self.assertEqual(first.assistant_id, RESOLUTION["assistant_id"])
            self.assertEqual(first.image, RESOLUTION["image_reference"])
            self.assertEqual(
                first.required_image_labels,
                (
                    ("org.shimpz.assistant.id", RESOLUTION["assistant_id"]),
                    ("org.shimpz.source.digest", RESOLUTION["source_digest"]),
                ),
            )
            self.assertEqual(second, first)
            self.assertEqual(registry.get("team_1", first.assistant_id), first)
            self.assertEqual(registry.get("team_2", first.assistant_id), second)
            self.assertEqual(
                tuple((binding.team_id, binding.assistant_id) for binding in registry.team_bindings("team_2")),
                (("team_2", first.assistant_id),),
            )
            self.assertIsNone(registry.get("team_3", first.assistant_id))
            self.assertEqual(registry.team_bindings("team_3"), ())
            self.assertEqual(
                registry.identities(),
                {("team_1", first.assistant_id), ("team_2", first.assistant_id)},
            )

    def test_registry_requires_a_string_version_before_runtime_conversion(self) -> None:
        resolution = _runtime_resolution()
        binding = binding_from_resolution("team_1", resolution)

        self.assertEqual(
            AssistantRegistry.versioned(binding),
            (AssistantRegistry.spec(binding), resolution["assistant_version"]),
        )

        with self.assertRaisesRegex(DynamicAssistantError, "valid version"):
            AssistantRegistry.versioned(SimpleNamespace(document={"assistant_version": 1}))

    def test_chat_inventory_reads_and_validates_the_registry_once_for_four_assistants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DynamicAssistantStore(Path(directory) / "bindings.json")
            containers = []
            for index in range(4):
                resolution = _runtime_resolution()
                resolution["assistant_id"] = f"helper-{index}"
                if index == 0:
                    first_name = resolution["name"]
                store.put("team_1", resolution)
                containers.append(
                    SimpleNamespace(
                        id=f"container-{index}",
                        labels={local_app.ASSISTANT_LABEL: resolution["assistant_id"]},
                        status="running",
                    )
                )
            foreign = _runtime_resolution()
            foreign["assistant_id"] = "helper-0"
            foreign["name"] = "Foreign Assistant"
            store.put("team_2", foreign)
            order = []

            def list_containers(**_kwargs):
                order.append("docker")
                return containers

            read_registry = store._read

            def read_bindings():
                order.append("registry")
                return read_registry()

            lifecycle = SimpleNamespace(
                client=SimpleNamespace(containers=SimpleNamespace(list=mock.Mock(side_effect=list_containers))),
                _assistant_filters=lambda _team_id: {},
                _validate_container=mock.Mock(),
                _blocked_action_workloads=set(),
            )
            subject = SimpleNamespace(
                assistant_lifecycle=lifecycle,
                registry=AssistantRegistry(store),
            )

            with mock.patch.object(store, "_read", side_effect=read_bindings) as read:
                active = local_chat_state._active_chat_assistants(subject, "team_1", "network")

            self.assertEqual(read.call_count, 1)
            self.assertEqual(order, ["docker", "registry"])
            self.assertEqual(tuple(item.spec.assistant_id for item in active), tuple(f"helper-{i}" for i in range(4)))
            self.assertEqual(active[0].spec.name, first_name)
            self.assertEqual(lifecycle._validate_container.call_count, 4)

            lifecycle.client.containers.list.side_effect = None
            lifecycle.client.containers.list.return_value = []
            with mock.patch.object(store, "_read", wraps=store._read) as read:
                self.assertEqual(local_chat_state._active_chat_assistants(subject, "team_1", "network"), ())
            read.assert_not_called()

    def test_spec_enumeration_uses_one_team_scoped_registry_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DynamicAssistantStore(Path(directory) / "bindings.json")
            containers = []
            for index in range(4):
                resolution = _runtime_resolution()
                resolution["assistant_id"] = f"helper-{index}"
                store.put("team_1", resolution)
                labels = {"team": "team_1", "kind": "assistant", local_app.ASSISTANT_LABEL: resolution["assistant_id"]}
                containers.append(SimpleNamespace(labels=labels, name=f"team_1-helper-{index}", status="running"))
            foreign = _runtime_resolution()
            foreign["assistant_id"] = "helper-0"
            foreign["name"] = "Foreign Assistant"
            store.put("team_2", foreign)
            containers.reverse()
            foreign_only = _runtime_resolution()
            foreign_only["assistant_id"] = "foreign-only"
            store.put("team_2", foreign_only)
            order = []

            def list_containers(**_kwargs):
                order.append("docker")
                return containers

            read_registry = store._read

            def read_bindings():
                order.append("registry")
                return read_registry()

            lifecycle = SimpleNamespace(
                _network=lambda _team_id: object(),
                _assistant_filters=lambda _team_id: {},
                _base_labels=lambda team_id, kind: {"team": team_id, "kind": kind},
                _container_name=lambda team_id, assistant_id: f"{team_id}-{assistant_id}",
                _labels_include=lambda actual, expected: all(
                    actual.get(key) == value for key, value in expected.items()
                ),
                client=SimpleNamespace(containers=SimpleNamespace(list=mock.Mock(side_effect=list_containers))),
                registry=AssistantRegistry(store),
            )

            with (
                mock.patch.object(store, "_read", side_effect=read_bindings) as read,
                mock.patch.object(local_registry, "_spec", wraps=local_registry._spec) as convert,
            ):
                actual = local_resources._assistant_specs(lifecycle, "team_1")

            self.assertEqual(
                tuple(spec.assistant_id for spec in actual),
                tuple(f"helper-{index}" for index in range(4)),
            )
            self.assertEqual(order, ["docker", "registry"])
            self.assertEqual(read.call_count, 1)
            self.assertEqual(convert.call_count, 4)
            self.assertTrue(all(call.args[0].team_id == "team_1" for call in convert.call_args_list))
            homonym = next(call.args[0] for call in convert.call_args_list if call.args[0].assistant_id == "helper-0")
            self.assertEqual(homonym.document["name"], RESOLUTION["name"])

            containers.clear()
            with mock.patch.object(store, "_read", wraps=store._read) as read:
                self.assertEqual(local_resources._assistant_specs(lifecycle, "team_1"), ())
            read.assert_not_called()

            containers.append(
                SimpleNamespace(
                    labels={
                        "team": "team_1",
                        "kind": "assistant",
                        local_app.ASSISTANT_LABEL: "foreign-only",
                    },
                    name="team_1-foreign-only",
                    status="running",
                )
            )
            with self.assertRaises(ApiProblemError) as caught:
                local_resources._assistant_specs(lifecycle, "team_1")
            self.assertEqual((caught.exception.status, caught.exception.code), (409, "assistant-registry-drift"))

            containers[0].labels[local_app.ASSISTANT_LABEL] = "Bad_Id"
            with self.assertRaises(ApiProblemError) as caught:
                local_resources._assistant_specs(lifecycle, "team_1")
            self.assertEqual((caught.exception.status, caught.exception.code), (409, "assistant-registry-drift"))

    def test_private_inventories_reuse_one_team_scoped_spec_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = DynamicAssistantStore(root / "bindings.json")
            containers = []
            for index in range(4):
                resolution = _runtime_resolution()
                resolution["assistant_id"] = f"helper-{index}"
                store.put("team_1", resolution)
                labels = {"team": "team_1", "kind": "assistant", local_app.ASSISTANT_LABEL: resolution["assistant_id"]}
                containers.append(SimpleNamespace(labels=labels, name=f"team_1-helper-{index}", status="running"))
            foreign = _runtime_resolution()
            foreign["assistant_id"] = "helper-0"
            foreign["name"] = "Foreign Assistant"
            foreign["assistant_version"] = "9.9.9"
            store.put("team_2", foreign)
            order = []

            def list_containers(**_kwargs):
                order.append("docker")
                return containers

            read_registry = store._read

            def read_bindings():
                order.append("registry")
                return read_registry()

            lifecycle = SimpleNamespace(
                _network=lambda _team_id: object(),
                _assistant_filters=lambda _team_id: {},
                _base_labels=lambda team_id, kind: {"team": team_id, "kind": kind},
                _container_name=lambda team_id, assistant_id: f"{team_id}-{assistant_id}",
                _labels_include=lambda actual, expected: all(
                    actual.get(key) == value for key, value in expected.items()
                ),
                client=SimpleNamespace(containers=SimpleNamespace(list=mock.Mock(side_effect=list_containers))),
                registry=AssistantRegistry(store),
            )
            lifecycle._assistant_specs = lambda team_id: local_resources._assistant_specs(lifecycle, team_id)
            subject = SimpleNamespace(
                _lock=lambda _team_id: nullcontext(),
                assistant_lifecycle=lifecycle,
                assistant_integrations=integration_store.OAuthIntegrationStore(
                    root / "oauth-state" / "state.json", root / "oauth-key" / "key"
                ),
                assistant_stored_inputs=action_stored_input.StoredInputStore(
                    root / "input-state" / "state.json", root / "input-key" / "key"
                ),
            )

            for route in (
                local_chat_private.list_assistant_integrations,
                local_chat_private.list_assistant_stored_inputs,
            ):
                with self.subTest(route=route.__name__):
                    order.clear()
                    with (
                        mock.patch.object(store, "_read", side_effect=read_bindings) as read,
                        mock.patch.object(local_registry, "_spec", wraps=local_registry._spec) as convert,
                    ):
                        payload = route(subject, "team_1")
                    self.assertEqual(order, ["docker", "registry"])
                    self.assertEqual(read.call_count, 1)
                    self.assertEqual(convert.call_count, 4)
                    self.assertTrue(all(call.args[0].team_id == "team_1" for call in convert.call_args_list))
                    if route is local_chat_private.list_assistant_integrations:
                        first = next(item for item in payload["integrations"] if item["assistant_id"] == "helper-0")
                        self.assertEqual(
                            (first["assistant_name"], first["assistant_version"]),
                            (RESOLUTION["name"], RESOLUTION["assistant_version"]),
                        )
                    else:
                        self.assertEqual(
                            {item["assistant_id"] for item in payload["stored_inputs"]},
                            {f"helper-{index}" for index in range(4)},
                        )

            containers.clear()
            for route in (
                local_chat_private.list_assistant_integrations,
                local_chat_private.list_assistant_stored_inputs,
            ):
                with (
                    self.subTest(empty_route=route.__name__),
                    mock.patch.object(store, "_read", wraps=store._read) as read,
                ):
                    route(subject, "team_1")
                read.assert_not_called()

    def test_installed_inventory_uses_one_team_scoped_registry_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DynamicAssistantStore(Path(directory) / "bindings.json")
            containers = []
            for index in range(4):
                resolution = _runtime_resolution()
                resolution["assistant_id"] = f"helper-{index}"
                store.put("team_1", resolution)
                containers.append(
                    SimpleNamespace(
                        labels={local_app.ASSISTANT_LABEL: resolution["assistant_id"]},
                        status="running",
                    )
                )
            foreign = _runtime_resolution()
            foreign["assistant_id"] = "helper-0"
            foreign["assistant_version"] = "9.9.9"
            store.put("team_2", foreign)
            order = []

            def list_containers(**_kwargs):
                order.append("docker")
                return containers

            read_registry = store._read

            def read_bindings():
                order.append("registry")
                return read_registry()

            lifecycle = SimpleNamespace(
                _network=lambda _team_id: object(),
                _assistant_filters=lambda _team_id: {},
                _network_name=lambda _team_id: "network",
                _validate_container_profile=mock.Mock(return_value=(object(), {})),
                _validate_container_egress=mock.Mock(),
                _has_current_assistant_artifact=lambda *_args: False,
            )
            controller = SimpleNamespace(
                _lock=lambda _team_id: nullcontext(),
                assistant_lifecycle=lifecycle,
                client=SimpleNamespace(containers=SimpleNamespace(list=mock.Mock(side_effect=list_containers))),
                registry=AssistantRegistry(store),
            )

            with mock.patch.object(store, "_read", side_effect=read_bindings) as read:
                result = assistant_api.list_assistants(controller, "team_1")

            self.assertEqual(read.call_count, 1)
            self.assertEqual(order, ["docker", "registry"])
            self.assertEqual(
                tuple(item["assistant"] for item in result["assistants"]),
                tuple(f"helper-{i}" for i in range(4)),
            )
            self.assertEqual(
                {item["assistant_version"] for item in result["assistants"]},
                {RESOLUTION["assistant_version"]},
            )
            self.assertEqual(lifecycle._validate_container_profile.call_count, 4)
            self.assertEqual(lifecycle._validate_container_egress.call_count, 4)

            controller.client.containers.list.side_effect = None
            controller.client.containers.list.return_value = []
            with mock.patch.object(store, "_read", wraps=store._read) as read:
                self.assertEqual(assistant_api.list_assistants(controller, "team_1"), {"assistants": []})
            read.assert_not_called()

    def test_catalog_selects_the_latest_bound_publication(self) -> None:
        older = _runtime_resolution()
        newer = copy.deepcopy(older)
        newer["assistant_version"] = "0.2.0"
        newer["source_digest"] = f"sha256:{'9' * 64}"
        store = mock.Mock()
        store.snapshot.return_value = (
            binding_from_resolution("team_2", newer),
            binding_from_resolution("team_1", older),
        )

        catalog = AssistantRegistry(store).catalog()

        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0].version, "0.2.0")

    def test_controller_routes_a_newer_bound_publication_through_update(self) -> None:
        current = _runtime_resolution()
        successor = copy.deepcopy(current)
        successor["assistant_version"] = "0.2.0"
        successor["source_digest"] = f"sha256:{'9' * 64}"
        events: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            controller = object.__new__(local_app.LocalController)
            controller.registry = AssistantRegistry(DynamicAssistantStore(Path(directory) / "bindings.json"))
            controller.registry.put("team_1", current)
            controller.developers = mock.Mock()
            controller.developers.icon.return_value = ICON
            controller.assistant_icons = AssistantIconStore(Path(directory) / "icons")
            controller.developers.resolve.side_effect = lambda _digest: events.append("resolve") or successor
            controller.artifact_trust = mock.Mock()
            controller.artifact_trust.verify.side_effect = lambda _resolution: events.append("verify")

            def update(team_id, previous, candidate, **options):
                events.append("update")
                options["authorize_start"]()
                controller.registry.commit_replacement(
                    team_id,
                    options["previous_binding"].binding_digest,
                    options["successor_document"],
                )
                return {"assistant": candidate.assistant_id, "installed": False, "updated": True}

            controller.assistant_lifecycle = SimpleNamespace(update_assistant=update)
            result = controller.install_publication(
                "team_1",
                successor["assistant_id"],
                successor["source_digest"],
            )
            committed = controller.registry.binding("team_1", successor["assistant_id"])

        self.assertEqual(result, {"assistant": successor["assistant_id"], "installed": False, "updated": True})
        self.assertEqual(events, ["resolve", "verify", "update", "resolve"])
        self.assertIsNotNone(committed)
        self.assertEqual(committed.resolution["source_digest"], successor["source_digest"])

    def test_controller_recovers_the_same_bound_publication_idempotently(self) -> None:
        resolution = _runtime_resolution()
        events: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            controller = object.__new__(local_app.LocalController)
            controller.registry = AssistantRegistry(DynamicAssistantStore(Path(directory) / "bindings.json"))
            controller.registry.put("team_1", resolution)
            controller.developers = mock.Mock()
            controller.developers.icon.return_value = ICON
            controller.assistant_icons = AssistantIconStore(Path(directory) / "icons")
            controller.developers.resolve.side_effect = lambda _digest: events.append("resolve") or resolution
            controller.artifact_trust = mock.Mock()
            controller.artifact_trust.verify.side_effect = lambda _resolution: events.append("verify")

            def install(_team_id, assistant_id, *, authorize_start):
                events.append("recover")
                authorize_start()
                return {"assistant": assistant_id, "installed": False}

            controller.assistant_lifecycle = SimpleNamespace(install_assistant=install)
            result = controller.install_publication(
                "team_1",
                resolution["assistant_id"],
                resolution["source_digest"],
            )

        self.assertEqual(result, {"assistant": resolution["assistant_id"], "installed": False})
        self.assertEqual(events, ["resolve", "verify", "recover", "resolve"])

    def test_automatic_update_fence_rejects_a_removed_binding_before_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = object.__new__(local_app.LocalController)
            controller.registry = AssistantRegistry(DynamicAssistantStore(Path(directory) / "bindings.json"))
            controller.developers = mock.Mock()
            controller.developers.icon.return_value = ICON
            controller.assistant_icons = AssistantIconStore(Path(directory) / "icons")

            with self.assertRaises(local_app.ApiProblem) as caught:
                controller.install_publication(
                    "team_1",
                    RESOLUTION["assistant_id"],
                    RESOLUTION["source_digest"],
                    expected_binding_digest=f"sha256:{'1' * 64}",
                )

        self.assertEqual(caught.exception.code, "assistant-update-conflict")
        controller.developers.resolve.assert_not_called()

    def test_trusted_image_requires_the_bound_publication_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = AssistantRegistry(DynamicAssistantStore(Path(directory) / "bindings.json"))
            spec = registry.put("team_1", _runtime_resolution())
            image = SimpleNamespace(
                attrs={
                    "Config": {"Labels": dict(spec.required_image_labels)},
                    "RepoDigests": [spec.image],
                },
                reload=mock.Mock(),
            )
            lifecycle = SimpleNamespace(
                client=SimpleNamespace(images=SimpleNamespace(get=mock.Mock(return_value=image))),
                _image_labels_valid=local_resources._image_labels_valid,
            )

            self.assertIs(local_resources._trusted_image(lifecycle, spec), image)
            image.attrs["Config"]["Labels"]["org.shimpz.source.digest"] = "sha256:" + ("0" * 64)
            with self.assertRaises(local_app.ApiProblem) as caught:
                local_resources._trusted_image(lifecycle, spec)

        self.assertEqual(caught.exception.code, "image-contract-mismatch")

    def test_controller_verifies_and_reauthorizes_before_local_start(self) -> None:
        resolution = _runtime_resolution()
        events: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            controller = object.__new__(local_app.LocalController)
            controller.registry = AssistantRegistry(DynamicAssistantStore(Path(directory) / "bindings.json"))
            controller.developers = mock.Mock()
            controller.developers.icon.return_value = ICON
            controller.assistant_icons = AssistantIconStore(Path(directory) / "icons")
            controller.developers.resolve.side_effect = lambda _digest: events.append("resolve") or resolution
            controller.artifact_trust = mock.Mock()
            controller.artifact_trust.verify.side_effect = lambda _resolution: events.append("verify")

            def install(team_id, assistant_id, *, authorize_start):
                events.append("install")
                authorize_start()
                events.append("start")
                return {"assistant": assistant_id, "installed": True}

            controller.assistant_lifecycle = SimpleNamespace(install_assistant=install)
            result = controller.install_publication(
                "team_1",
                resolution["assistant_id"],
                resolution["source_digest"],
            )

        self.assertEqual(
            result,
            {"assistant": resolution["assistant_id"], "installed": True},
        )
        self.assertEqual(events, ["resolve", "verify", "install", "resolve", "start"])
        self.assertEqual(controller.developers.resolve.call_count, 2)
        controller.artifact_trust.verify.assert_called_once_with(resolution)

    def test_controller_refuses_a_publication_changed_before_local_start(self) -> None:
        resolution = _runtime_resolution()
        changed = copy.deepcopy(resolution)
        changed["oci_digest"] = "sha256:" + ("0" * 64)
        with tempfile.TemporaryDirectory() as directory:
            controller = object.__new__(local_app.LocalController)
            controller.registry = AssistantRegistry(DynamicAssistantStore(Path(directory) / "bindings.json"))
            controller.developers = mock.Mock()
            controller.developers.icon.return_value = ICON
            controller.assistant_icons = AssistantIconStore(Path(directory) / "icons")
            controller.developers.resolve.side_effect = (resolution, changed)
            controller.artifact_trust = mock.Mock()

            def install(_team_id, _assistant_id, *, authorize_start):
                authorize_start()
                raise AssertionError("changed publication reached local start")

            controller.assistant_lifecycle = SimpleNamespace(install_assistant=install)
            with self.assertRaises(local_app.ApiProblem) as caught:
                controller.install_publication(
                    "team_1",
                    resolution["assistant_id"],
                    resolution["source_digest"],
                )

        self.assertEqual(caught.exception.code, "assistant-not-installable")
        self.assertIsNone(controller.registry.get("team_1", resolution["assistant_id"]))
