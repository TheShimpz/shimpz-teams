from __future__ import annotations

import copy
import hashlib
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from install import artifact_trust, bindings, icons
from install.bindings import DynamicAssistantStore
from local.errors import ApiProblemError
from local.install import developers
from local.install import registry as publication_registry
from local.install import service as install_service
from tests.test_local_publication_install import ICON, RESOLUTION, _Connection, _Response, _runtime_resolution


class LocalInstallEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        _Connection.requests = []
        _Connection.connections = []
        _Connection.tunnels = []

    def test_developers_client_rejects_invalid_requests_and_mismatched_responses(self) -> None:
        client = developers.DevelopersClient()
        for operation in (client.resolve, client.latest):
            with (
                self.subTest(operation=operation.__name__),
                self.assertRaises(developers.PublicationNotInstallableError),
            ):
                operation("invalid")
        with self.assertRaises(developers.PublicationNotInstallableError):
            client.icon("invalid", RESOLUTION["icon_digest"])

        mismatched = copy.deepcopy(RESOLUTION)
        mismatched["source_digest"] = f"sha256:{'9' * 64}"
        _Connection.response = _Response(200, mismatched)
        with (
            mock.patch.object(developers.http.client, "HTTPSConnection", _Connection),
            self.assertRaisesRegex(developers.DevelopersError, "does not match"),
        ):
            client.resolve(RESOLUTION["source_digest"])

    def test_developers_icon_and_transport_are_bounded(self) -> None:
        client = developers.DevelopersClient()
        digest = f"sha256:{hashlib.sha256(ICON).hexdigest()}"
        for status, raw, error in (
            (404, b"", developers.PublicationNotInstallableError),
            (503, ICON, developers.DevelopersError),
            (200, b"wrong", developers.DevelopersError),
        ):
            _Connection.response = _Response(status, None, raw=raw)
            with (
                self.subTest(status=status, raw=raw),
                mock.patch.object(developers.http.client, "HTTPSConnection", _Connection),
                self.assertRaises(error),
            ):
                client.icon(RESOLUTION["source_digest"], digest)

        connection = mock.Mock()
        connection.request.side_effect = OSError("offline")
        with (
            mock.patch.object(developers.http.client, "HTTPSConnection", return_value=connection),
            self.assertRaisesRegex(developers.DevelopersError, "unavailable"),
        ):
            client._request("/path")
        connection.close.assert_called_once_with()

        oversized = b"x" * (developers._MAX_RESPONSE_BYTES + 1)
        _Connection.response = _Response(200, None, raw=oversized)
        with (
            mock.patch.object(developers.http.client, "HTTPSConnection", _Connection),
            self.assertRaisesRegex(developers.DevelopersError, "too large"),
        ):
            client._request("/path")

        with (
            mock.patch.object(developers._CONTRACTS, "validate"),
            self.assertRaisesRegex(developers.DevelopersError, "violates its contract"),
        ):
            developers._resolution(200, b"[]")

    def test_publication_registry_rejects_conflicts_and_invalid_contracts(self) -> None:
        current = types.SimpleNamespace(assistant_id="one", resolution={"assistant_version": "1.0.0"})
        candidate = types.SimpleNamespace(assistant_id="two", resolution={"assistant_version": "2.0.0"})
        self.assertFalse(publication_registry.is_successor(current, candidate))

        with tempfile.TemporaryDirectory() as directory:
            registry = publication_registry.PublicationRegistry(
                DynamicAssistantStore(Path(directory) / "bindings.json")
            )
            resolution = _runtime_resolution()
            spec = registry.put("team_1", resolution)
            binding = registry.binding("team_1", spec.assistant_id)
            assert binding is not None
            self.assertEqual(registry.spec(binding), spec)
            self.assertEqual(registry.all(), (spec,))

            successor = copy.deepcopy(resolution)
            successor["assistant_version"] = "9.0.0"
            successor["source_digest"] = f"sha256:{'9' * 64}"
            successor["name"] = "Current Assistant"
            successor["image_reference"] = f"ghcr.io/theshimpz/shimpz-assistant@sha256:{'c' * 64}"
            successor["oci_digest"] = f"sha256:{'c' * 64}"
            current = registry.put("team_2", successor)
            self.assertEqual(registry.all(), (spec, current))
            self.assertEqual(registry.catalog(), (current,))

            same_version = copy.deepcopy(successor)
            same_version["source_digest"] = f"sha256:{'8' * 64}"
            same_version["name"] = "Deterministic Assistant"
            contender = registry.put("team_3", same_version)
            contender_binding = registry.binding("team_3", contender.assistant_id)
            current_binding = registry.binding("team_2", current.assistant_id)
            assert contender_binding is not None
            assert current_binding is not None
            expected = registry.spec(
                max(
                    (current_binding, contender_binding),
                    key=lambda candidate: candidate.binding_digest,
                )
            )
            self.assertEqual(registry.catalog(), (expected,))

            with self.assertRaises(bindings.DynamicAssistantConflictError):
                registry.replacement("team_1", f"sha256:{'0' * 64}", resolution)

            with (
                mock.patch.object(
                    publication_registry.assistant_manifest,
                    "canonical_machine_contract",
                    return_value={"actions": []},
                ),
                self.assertRaisesRegex(bindings.DynamicAssistantError, "runtime contract"),
            ):
                publication_registry._spec(binding)

        with self.assertRaisesRegex(bindings.DynamicAssistantError, "valid Assistant version"):
            publication_registry._version({"assistant_version": 1})
        with self.assertRaisesRegex(bindings.DynamicAssistantError, "valid Assistant version"):
            publication_registry._version({"assistant_version": "invalid"})

    @staticmethod
    def _service_controller() -> types.SimpleNamespace:
        registry = types.SimpleNamespace(
            binding=mock.Mock(return_value=None),
            delete=mock.Mock(),
            bindings=mock.Mock(return_value=()),
            replacement=mock.Mock(),
            get=mock.Mock(),
        )
        return types.SimpleNamespace(
            registry=registry,
            assistant_icons=types.SimpleNamespace(discard_unreferenced=mock.Mock()),
        )

    def test_install_service_maps_each_boundary_failure_and_rolls_back_new_binding(self) -> None:
        failures = (
            ApiProblemError(409, "install failed", code="install-failed"),
            developers.PublicationNotInstallableError("missing"),
            developers.DevelopersError("offline"),
            artifact_trust.ArtifactTrustError("untrusted"),
            bindings.DynamicAssistantError("binding"),
            icons.AssistantIconError("icon"),
        )
        for failure in failures:
            controller = self._service_controller()
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.object(install_service, "_resolved_publication", side_effect=failure),
                self.assertRaises(ApiProblemError),
            ):
                install_service.install_publication(controller, "team_1", "helper", f"sha256:{'1' * 64}")
            if isinstance(failure, ApiProblemError | developers.PublicationNotInstallableError):
                controller.registry.delete.assert_called_once_with("team_1", "helper")
            else:
                controller.registry.delete.assert_not_called()

        controller = self._service_controller()
        rollback_failure = ApiProblemError(503, "rollback", code="assistant-install-rollback-incomplete")
        with (
            mock.patch.object(install_service, "_resolved_publication", side_effect=rollback_failure),
            self.assertRaises(ApiProblemError),
        ):
            install_service.install_publication(controller, "team_1", "helper", f"sha256:{'1' * 64}")
        controller.registry.delete.assert_not_called()

        controller = self._service_controller()
        controller.registry.binding.return_value = types.SimpleNamespace(
            resolution={"source_digest": f"sha256:{'2' * 64}"}
        )
        with (
            mock.patch.object(
                install_service,
                "_resolved_publication",
                side_effect=developers.PublicationNotInstallableError("missing"),
            ),
            self.assertRaises(ApiProblemError),
        ):
            install_service.install_publication(controller, "team_1", "helper", f"sha256:{'1' * 64}")
        controller.registry.delete.assert_not_called()

    def test_install_service_rejects_identity_downgrade_and_binding_races(self) -> None:
        controller = types.SimpleNamespace(
            developers=types.SimpleNamespace(
                resolve=lambda _digest: {"assistant_id": "other"},
            )
        )
        with self.assertRaises(developers.PublicationNotInstallableError):
            install_service._resolved_publication(controller, "helper", f"sha256:{'1' * 64}")

        controller = self._service_controller()
        controller.registry.replacement.return_value = (object(), object())
        existing = types.SimpleNamespace(binding_digest="digest")
        with (
            mock.patch.object(install_service, "is_successor", return_value=False),
            self.assertRaises(developers.PublicationNotInstallableError),
        ):
            install_service._install_bound_publication(
                controller,
                "team_1",
                "helper",
                existing,
                resolution={},
                authorize_start=lambda: None,
            )

        controller.registry.get.return_value = None
        with (
            mock.patch.object(install_service, "is_successor", return_value=True),
            self.assertRaises(bindings.DynamicAssistantConflictError),
        ):
            install_service._install_bound_publication(
                controller,
                "team_1",
                "helper",
                existing,
                resolution={},
                authorize_start=lambda: None,
            )

        controller.assistant_icons.discard_unreferenced.side_effect = icons.AssistantIconError("unavailable")
        with self.assertRaisesRegex(ApiProblemError, "icon storage is unavailable"):
            install_service._discard_icon(controller, f"sha256:{'1' * 64}")


if __name__ == "__main__":
    unittest.main()
