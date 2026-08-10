"""Local profile publication resolution and durable binding contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import ssl
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from install.bindings import DynamicAssistantError, DynamicAssistantStore, binding_from_resolution
from install.contract import CONTRACT_ROOT
from install.icons import AssistantIconStore
from local import app as local_app
from local.assistant import resources as local_resources
from local.install.developers import DevelopersClient, DevelopersError, PublicationNotInstallableError
from local.install.registry import PublicationRegistry

RESOLUTION = json.loads((CONTRACT_ROOT / "vectors.json").read_bytes())["fixtures"]["resolve_response"]["value"]
ICON = b"canonical icon"


def _runtime_resolution() -> dict[str, object]:
    resolution = copy.deepcopy(RESOLUTION)
    resolution["icon_digest"] = f"sha256:{hashlib.sha256(ICON).hexdigest()}"
    power = resolution["machine_contract"]["powers"][0]
    power["input_schema"]["additionalProperties"] = False
    power["output_schema"]["additionalProperties"] = False
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
            self.assertRaises(DevelopersError),
        ):
            DevelopersClient().resolve(RESOLUTION["source_digest"])

    def test_registry_binds_publications_independently_per_team(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = PublicationRegistry(DynamicAssistantStore(Path(directory) / "bindings.json"))
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
            self.assertEqual(registry.list("team_2"), (second,))
            self.assertIsNone(registry.get("team_3", first.assistant_id))
            self.assertEqual(registry.list("team_3"), ())
            self.assertEqual(
                registry.identities(),
                {("team_1", first.assistant_id), ("team_2", first.assistant_id)},
            )

    def test_registry_reads_a_versioned_runtime_from_one_binding_snapshot(self) -> None:
        resolution = _runtime_resolution()
        binding = binding_from_resolution("team_1", resolution)
        store = mock.Mock()
        store.get.return_value = binding
        registry = PublicationRegistry(store)

        self.assertEqual(
            registry.get_versioned("team_1", binding.assistant_id),
            (registry.spec(binding), resolution["assistant_version"]),
        )
        store.get.assert_called_once_with("team_1", binding.assistant_id)

        store.reset_mock()
        store.get.return_value = None
        self.assertIsNone(registry.get_versioned("team_1", binding.assistant_id))
        store.get.assert_called_once_with("team_1", binding.assistant_id)

        store.reset_mock()
        store.get.return_value = SimpleNamespace(resolution={"assistant_version": 1})
        with self.assertRaisesRegex(DynamicAssistantError, "valid Assistant version"):
            registry.get_versioned("team_1", binding.assistant_id)
        store.get.assert_called_once_with("team_1", binding.assistant_id)

    def test_controller_routes_a_newer_bound_publication_through_update(self) -> None:
        current = _runtime_resolution()
        successor = copy.deepcopy(current)
        successor["assistant_version"] = "0.2.0"
        successor["source_digest"] = f"sha256:{'9' * 64}"
        events: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            controller = object.__new__(local_app.LocalController)
            controller.registry = PublicationRegistry(DynamicAssistantStore(Path(directory) / "bindings.json"))
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
                    options["resolution"],
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
            controller.registry = PublicationRegistry(DynamicAssistantStore(Path(directory) / "bindings.json"))
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
            controller.registry = PublicationRegistry(DynamicAssistantStore(Path(directory) / "bindings.json"))
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
            registry = PublicationRegistry(DynamicAssistantStore(Path(directory) / "bindings.json"))
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
            controller.registry = PublicationRegistry(DynamicAssistantStore(Path(directory) / "bindings.json"))
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
            controller.registry = PublicationRegistry(DynamicAssistantStore(Path(directory) / "bindings.json"))
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
