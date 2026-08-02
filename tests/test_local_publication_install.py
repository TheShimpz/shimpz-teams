"""Local profile publication resolution and durable binding contracts."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from install.bindings import DynamicAssistantStore
from install.contract import CONTRACT_ROOT
from local import app as local_app
from local.assistant import resources as local_resources
from local.install.developers import DevelopersClient, DevelopersError, PublicationNotInstallableError
from local.install.registry import PublicationRegistry

RESOLUTION = json.loads((CONTRACT_ROOT / "vectors.json").read_bytes())["fixtures"]["resolve_response"]["value"]


def _runtime_resolution() -> dict[str, object]:
    resolution = copy.deepcopy(RESOLUTION)
    power = resolution["machine_contract"]["powers"][0]
    power["input_schema"]["additionalProperties"] = False
    power["output_schema"]["additionalProperties"] = False
    return resolution


class _Response:
    def __init__(self, status: int, value: object) -> None:
        self.status = status
        self._body = json.dumps(value, separators=(",", ":")).encode()

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
            _Connection.connections,
            [(("shimpz-assistant-release", 8888), {"timeout": 10})],
        )
        self.assertEqual(_Connection.tunnels, [("developers.shimpz.com", 443, {})])
        method, path, request = _Connection.requests[0]
        self.assertEqual(method, "GET")
        self.assertEqual(path, f"/api/v1/assistant-publications/{RESOLUTION['source_digest']}")
        self.assertEqual(request, {"headers": {"Accept": "application/json"}})

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
            self.assertEqual(
                registry.identities(),
                {("team_1", first.assistant_id), ("team_2", first.assistant_id)},
            )

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
        with tempfile.TemporaryDirectory() as directory:
            controller = object.__new__(local_app.LocalController)
            controller.registry = PublicationRegistry(DynamicAssistantStore(Path(directory) / "bindings.json"))
            controller.developers = mock.Mock()
            controller.developers.resolve.return_value = resolution
            controller.artifact_trust = mock.Mock()

            def install(team_id, assistant_id, *, authorize_start):
                authorize_start()
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
        self.assertEqual(controller.developers.resolve.call_count, 2)
        controller.artifact_trust.verify.assert_called_once_with(resolution)
