"""Fail-closed admission of unpublished Local Assistant snapshots."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from docker.errors import DockerException, ImageNotFound

from assistant import manifest as assistant_manifest
from install import bindings
from install.bindings import DynamicAssistantError, DynamicAssistantStore
from install.icons import AssistantIconError, AssistantIconStore
from install.update import AssistantUpdateStore
from local.assistant import lifecycle as assistant_lifecycle
from local.errors import ApiProblemError
from local.install import registry as assistant_registry
from local.install import service, snapshots, source_package
from tests.test_assistant_manifest import manifest
from tests.test_local_publication_install import _runtime_resolution
from tests.test_local_source_package import _packages

IMAGE_ID = "sha256:" + ("a" * 64)
BUILD_DIGEST = "sha256:" + ("b" * 64)
CREATED = "2026-08-28T17:00:00Z"
MACHINE_CONTRACT = {
    "version": 1,
    "actions": [
        {
            "id": "ping",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "output_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "integrations": [],
            "stored_inputs": [],
            "human_requests": [],
        }
    ],
}


def _archive(name: str, contents: bytes) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as bundle:
        member = tarfile.TarInfo(name)
        member.size = len(contents)
        member.mode = 0o444
        bundle.addfile(member, io.BytesIO(contents))
    return output.getvalue()


def _package() -> tuple[bytes, bytes, bytes]:
    raw = _packages()[0][1]
    records = list(source_package._read_records(raw))
    fixture_manifest = manifest()
    manifest_index = next(index for index, record in enumerate(records) if record.path == "shimpz.toml")
    records[manifest_index] = source_package._Record("shimpz.toml", False, fixture_manifest)
    package = source_package._build_archive(tuple(records))
    icon = next(record.contents for record in records if record.path == "icon.png")
    return package, fixture_manifest, icon


def _image(source_digest: str):
    labels = {
        snapshots.LOCAL_STAGE_LABEL: snapshots.LOCAL_STAGE_VALUE,
        snapshots.ASSISTANT_LABEL: "fixture-assistant",
        snapshots.SOURCE_LABEL: source_digest,
        snapshots.VERSION_LABEL: "0.1.0",
        snapshots.NAME_LABEL: "Fixture Assistant",
        snapshots.SUMMARY_LABEL: "Exercise immutable admission.",
        snapshots.DECLARED_CREATORS_LABEL: "@fixture",
        snapshots.BUILD_LABEL: BUILD_DIGEST,
    }
    attrs = {
        "Id": IMAGE_ID,
        "Architecture": "amd64",
        "RepoDigests": [],
        "RepoTags": [],
        "Created": CREATED,
        "Config": {
            "Labels": labels,
            "User": snapshots.RUNTIME_USER,
            "Entrypoint": snapshots.RUNTIME_ENTRYPOINT,
            "Cmd": None,
        },
    }
    return SimpleNamespace(id=IMAGE_ID, attrs=attrs, reload=mock.Mock())


def _container(files: dict[str, bytes]):
    container = mock.Mock()

    def get_archive(path: str):
        contents = files[path]
        name = path.rsplit("/", 1)[1]
        return iter((_archive(name, contents),)), {"name": name, "size": len(contents), "mode": 0o444}

    container.get_archive.side_effect = get_archive
    return container


def _client(*, source_digest: str | None = None):
    package, fixture_manifest, icon = _package()
    digest = source_digest or f"sha256:{hashlib.sha256(package).hexdigest()}"
    image = _image(digest)
    raw_contract = json.dumps(MACHINE_CONTRACT, separators=(",", ":")).encode()
    container = _container(
        {
            snapshots.SOURCE_PATH: package,
            assistant_manifest.MANIFEST_PATH: fixture_manifest,
            assistant_manifest.CONTRACT_PATH: raw_contract,
            snapshots.ICON_PATH: icon,
        }
    )
    client = mock.Mock()
    client.info.return_value = {"Architecture": "x86_64"}
    client.images.get.return_value = image
    client.images.list.return_value = [image]
    client.containers.create.return_value = container
    return client, image, container


class LocalSnapshotTests(unittest.TestCase):
    def test_snapshot_inventory_and_platform_fail_closed(self) -> None:
        client, image, _container_value = _client()
        client.images.list.side_effect = DockerException("offline")
        with self.assertRaisesRegex(snapshots.LocalSnapshotUnavailableError, "cannot enumerate"):
            snapshots.list_candidates(client)

        client, image, _container_value = _client()
        with (
            mock.patch.object(
                snapshots,
                "_candidate",
                side_effect=snapshots.LocalSnapshotUnavailableError("inspection failed"),
            ),
            self.assertRaisesRegex(snapshots.LocalSnapshotUnavailableError, "inspection failed"),
        ):
            snapshots.list_candidates(client)

        client, image, _container_value = _client()
        client.images.list.return_value = [image, image]
        with self.assertRaisesRegex(snapshots.LocalSnapshotError, "duplicate images"):
            snapshots.list_candidates(client)

        client, _image_value, _container_value = _client()
        client.info.side_effect = DockerException("offline")
        with self.assertRaisesRegex(snapshots.LocalSnapshotUnavailableError, "cannot report"):
            snapshots.list_candidates(client)

        client, _image_value, _container_value = _client()
        client.info.return_value = {"Architecture": "riscv64"}
        with self.assertRaisesRegex(snapshots.LocalSnapshotError, "architecture is unsupported"):
            snapshots.list_candidates(client)

    def test_candidate_inspection_and_labels_fail_closed(self) -> None:
        client, image, _container_value = _client()
        image.reload.side_effect = DockerException("offline")
        with self.assertRaisesRegex(snapshots.LocalSnapshotUnavailableError, "cannot inspect"):
            snapshots.list_candidates(client)

        client, image, _container_value = _client()
        del image.attrs["Config"]["Labels"][snapshots.ASSISTANT_LABEL]
        with self.assertRaisesRegex(snapshots.InvalidLabeledSnapshotError, "failed validation"):
            snapshots.list_candidates(client)

        client, image, _container_value = _client()
        image.attrs["Config"]["Labels"][snapshots.SOURCE_LABEL] = "not-a-digest"
        with self.assertRaisesRegex(snapshots.InvalidLabeledSnapshotError, "failed validation"):
            snapshots.list_candidates(client)

        client, image, _container_value = _client()
        image.attrs["Config"]["Labels"] = []
        with self.assertRaisesRegex(snapshots.InvalidLabeledSnapshotError, "failed validation"):
            snapshots.list_candidates(client)

    def test_exact_image_resolution_fails_closed(self) -> None:
        client, _image_value, _container_value = _client()
        with self.assertRaisesRegex(snapshots.LocalSnapshotError, "image id is invalid"):
            snapshots.admit(client, "latest")
        client.images.get.assert_not_called()

        for failure, message in (
            (ImageNotFound("missing"), "no longer available"),
            (DockerException("offline"), "cannot resolve"),
        ):
            with self.subTest(message=message):
                client, _image_value, _container_value = _client()
                client.images.get.side_effect = failure
                with self.assertRaisesRegex(snapshots.LocalSnapshotUnavailableError, message):
                    snapshots.admit(client, IMAGE_ID)

        client, image, _container_value = _client()
        image.id = "sha256:" + ("c" * 64)
        with self.assertRaisesRegex(snapshots.LocalSnapshotError, "exact Local Assistant image id"):
            snapshots.admit(client, IMAGE_ID)

    def test_lists_only_bounded_stage_candidates(self) -> None:
        client, _image_value, _container_value = _client()

        candidates = snapshots.list_candidates(client)

        self.assertEqual(
            candidates,
            (
                snapshots.LocalSnapshotCandidate(
                    "fixture-assistant",
                    "0.1.0",
                    "Fixture Assistant",
                    "Exercise immutable admission.",
                    ("@fixture",),
                    IMAGE_ID,
                    "linux/amd64",
                    CREATED,
                ),
            ),
        )
        client.images.list.assert_called_once_with(
            all=True,
            filters={"label": [f"{snapshots.LOCAL_STAGE_LABEL}={snapshots.LOCAL_STAGE_VALUE}"]},
        )
        client.containers.create.assert_not_called()

    def test_candidate_overflow_fails_before_deep_inspection(self) -> None:
        client, image, _container_value = _client()
        client.images.list.return_value = [image] * (snapshots.MAX_CANDIDATES + 1)

        with self.assertRaisesRegex(snapshots.LocalSnapshotError, "too large"):
            snapshots.list_candidates(client)

        image.reload.assert_not_called()

    def test_names_only_a_canonical_invalid_stage_labeled_image(self) -> None:
        client, image, _container_value = _client()
        image.attrs["Config"]["User"] = "0:0"

        with self.assertRaisesRegex(
            snapshots.InvalidLabeledSnapshotError,
            rf"^Local Assistant snapshot {IMAGE_ID} carries the Local stage label but failed validation$",
        ):
            snapshots.list_candidates(client)

        image.id = "malformed-image-id"
        with self.assertRaisesRegex(
            snapshots.LocalSnapshotError,
            "snapshot identity is invalid",
        ):
            snapshots.list_candidates(client)

    def test_admits_exact_image_without_starting_temporary_container(self) -> None:
        client, _image_value, container = _client()

        admitted = snapshots.admit(client, IMAGE_ID)

        self.assertEqual(admitted.record["image_id"], IMAGE_ID)
        self.assertEqual(admitted.record["assistant_id"], "fixture-assistant")
        self.assertEqual(admitted.record["platform"], "linux/amd64")
        self.assertNotIn("creators", admitted.record)
        self.assertNotIn("github", admitted.record)
        snapshots.validate_record(admitted.record)
        client.images.get.assert_called_once_with(IMAGE_ID)
        client.containers.create.assert_called_once_with(image=IMAGE_ID, network_mode="none")
        container.start.assert_not_called()
        container.remove.assert_called_once_with(force=True, v=False)

    def test_previews_only_the_validated_manifest_icon_pair(self) -> None:
        client, _image_value, container = _client()

        icon = snapshots.preview_icon(client, IMAGE_ID)

        self.assertTrue(icon.startswith(b"\x89PNG"))
        self.assertEqual(
            [call.args[0] for call in container.get_archive.call_args_list],
            [assistant_manifest.MANIFEST_PATH, snapshots.ICON_PATH],
        )
        container.start.assert_not_called()
        container.remove.assert_called_once_with(force=True, v=False)

    def test_preview_rejects_invalid_image_id_without_docker_access(self) -> None:
        client, _image_value, _container = _client()

        with self.assertRaisesRegex(snapshots.LocalSnapshotError, "image id is invalid"):
            snapshots.preview_icon(client, "latest")

        client.images.get.assert_not_called()

    def test_preview_rejects_invalid_declaration_and_cleans_up(self) -> None:
        client, _image_value, container = _client()

        with (
            mock.patch.object(
                snapshots.assistant_manifest,
                "parse_manifest_identity",
                side_effect=assistant_manifest.ManifestError("invalid"),
            ),
            self.assertRaisesRegex(snapshots.LocalSnapshotError, "preview is invalid"),
        ):
            snapshots.preview_icon(client, IMAGE_ID)

        container.remove.assert_called_once_with(force=True, v=False)

    def test_preview_rejects_display_label_drift_and_always_cleans_up(self) -> None:
        client, image, container = _client()
        image.attrs["Config"]["Labels"][snapshots.NAME_LABEL] = "Different name"

        with self.assertRaisesRegex(snapshots.LocalSnapshotError, "does not match"):
            snapshots.preview_icon(client, IMAGE_ID)

        container.remove.assert_called_once_with(force=True, v=False)

    def test_rejects_source_mismatch_and_always_removes_container(self) -> None:
        client, _image_value, container = _client(source_digest="sha256:" + ("f" * 64))

        with self.assertRaisesRegex(snapshots.LocalSnapshotError, "digest"):
            snapshots.admit(client, IMAGE_ID)

        container.remove.assert_called_once_with(force=True, v=False)

    def test_rejects_extracted_file_and_declaration_drift(self) -> None:
        client, _image_value, container = _client()
        original_get_archive = container.get_archive.side_effect

        def drift_manifest(path: str):
            if path == assistant_manifest.MANIFEST_PATH:
                return iter((_archive("shimpz.toml", b"different"),)), {
                    "name": "shimpz.toml",
                    "size": len(b"different"),
                    "mode": 0o444,
                }
            return original_get_archive(path)

        container.get_archive.side_effect = drift_manifest
        with self.assertRaisesRegex(snapshots.LocalSnapshotError, "do not match"):
            snapshots.admit(client, IMAGE_ID)

        client, image, _container_value = _client()
        image.attrs["Config"]["Labels"][snapshots.VERSION_LABEL] = "0.2.0"
        with self.assertRaisesRegex(snapshots.LocalSnapshotError, "manifest does not match"):
            snapshots.admit(client, IMAGE_ID)

        client, _image_value, _container_value = _client()
        with (
            mock.patch.object(
                snapshots.source_package,
                "admit",
                side_effect=source_package.SourcePackageError("invalid"),
            ),
            self.assertRaisesRegex(snapshots.LocalSnapshotError, "declaration is invalid"),
        ):
            snapshots.admit(client, IMAGE_ID)

    def test_extraction_and_cleanup_fail_closed(self) -> None:
        client, _image_value, container = _client()
        container.get_archive.side_effect = DockerException("unavailable")
        with self.assertRaisesRegex(snapshots.LocalSnapshotError, "could not be admitted"):
            snapshots.admit(client, IMAGE_ID)
        container.remove.assert_called_once_with(force=True, v=False)

        client, _image_value, container = _client()
        container.remove.side_effect = DockerException("unavailable")
        with self.assertRaisesRegex(snapshots.LocalSnapshotError, "could not be removed"):
            snapshots.admit(client, IMAGE_ID)

        self.assertIsNone(snapshots._remove_temporary_container(None))

    def test_record_rejects_attribution_and_provider_drift(self) -> None:
        client, _image_value, _container_value = _client()
        record = snapshots.admit(client, IMAGE_ID).record
        mutations = (
            {**record, "creators": ["@fixture"]},
            {**record, "integrations": [{"id": "cloudflare", "provider": "other", "scopes": []}]},
            {**record, "runtime": {"user": "0:0", "entrypoint": snapshots.RUNTIME_ENTRYPOINT}},
        )

        for mutation in mutations:
            with self.subTest(fields=set(mutation)), self.assertRaises(snapshots.LocalSnapshotError):
                snapshots.validate_record(mutation)

    def test_record_rejects_malformed_integration_and_stored_input_shapes(self) -> None:
        client, _image_value, _container_value = _client()
        record = snapshots.admit(client, IMAGE_ID).record

        malformed_integrations = (
            None,
            [{"id": "cloudflare", "provider": "cloudflare", "scopes": None}],
            [{"id": "cloudflare", "provider": "cloudflare", "scopes": [], "extra": True}],
        )
        for value in malformed_integrations:
            with (
                self.subTest(integrations=value),
                self.assertRaisesRegex(
                    snapshots.LocalSnapshotError,
                    "Integrations are invalid",
                ),
            ):
                snapshots.validate_record({**record, "integrations": value})

        class MissingValue(dict):
            def __getitem__(self, key):
                if key == "description":
                    raise KeyError(key)
                return super().__getitem__(key)

        malformed_stored_inputs = (
            None,
            [MissingValue(id="token", kind="input:password", label="Token", description="Secret")],
            [{"id": "token", "kind": "input:password", "label": "Token"}],
        )
        for value in malformed_stored_inputs:
            with (
                self.subTest(stored_inputs=value),
                self.assertRaisesRegex(
                    snapshots.LocalSnapshotError,
                    "Stored Inputs are invalid",
                ),
            ):
                snapshots.validate_record({**record, "stored_inputs": value})

    def test_registry_projects_local_runtime_and_replaces_only_local_bindings(self) -> None:
        client, _image_value, _container_value = _client()
        admitted = snapshots.admit(client, IMAGE_ID)
        with tempfile.TemporaryDirectory() as directory:
            registry = assistant_registry.AssistantRegistry(
                DynamicAssistantStore(
                    Path(directory) / "bindings.json",
                    local_record_validator=snapshots.validate_record,
                )
            )
            spec = registry.put_local("team_1", admitted.record)

            self.assertEqual(spec.provenance, "local")
            self.assertEqual(spec.image, IMAGE_ID)
            self.assertEqual(tuple(spec.actions), ("ping",))
            self.assertEqual(
                spec.required_image_labels,
                (
                    (snapshots.LOCAL_STAGE_LABEL, snapshots.LOCAL_STAGE_VALUE),
                    (snapshots.ASSISTANT_LABEL, "fixture-assistant"),
                    (snapshots.SOURCE_LABEL, admitted.record["source_digest"]),
                    (snapshots.VERSION_LABEL, "0.1.0"),
                ),
            )
            current = registry.binding("team_1", "fixture-assistant")
            self.assertIsNotNone(current)
            replacement = {**admitted.record, "image_id": "sha256:" + ("c" * 64)}
            candidate, candidate_spec = registry.local_replacement(
                "team_1",
                current.binding_digest,
                replacement,
            )
            self.assertFalse(assistant_registry.is_successor(current, candidate))
            self.assertEqual(candidate_spec.version, spec.version)
            self.assertEqual(
                registry.commit_local_replacement("team_1", current.binding_digest, replacement).image,
                replacement["image_id"],
            )

    def test_local_replacement_transaction_is_durable_and_profile_scoped(self) -> None:
        client, _image_value, _container_value = _client()
        admitted = snapshots.admit(client, IMAGE_ID)
        replacement = {**admitted.record, "image_id": "sha256:" + ("c" * 64)}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bindings = DynamicAssistantStore(
                root / "bindings.json",
                local_record_validator=snapshots.validate_record,
            )
            previous = bindings.put_local("team_1", admitted.record)
            updates = AssistantUpdateStore(
                root / "updates",
                local_record_validator=snapshots.validate_record,
            )

            transaction = updates.begin(previous, replacement, IMAGE_ID)

            self.assertEqual(transaction.previous.provenance, "local")
            self.assertEqual(transaction.successor.provenance, "local")
            self.assertEqual(transaction.successor.local_record, replacement)
            self.assertEqual(updates.get("team_1", "fixture-assistant"), transaction)
            with self.assertRaisesRegex(DynamicAssistantError, "unavailable in this profile"):
                AssistantUpdateStore(root / "updates").list()

    def test_service_lists_only_public_candidate_fields(self) -> None:
        client, _image_value, _container_value = _client()

        result = service.list_local_snapshots(SimpleNamespace(client=client))

        self.assertEqual(
            result,
            {
                "assistants": [
                    {
                        "assistant_id": "fixture-assistant",
                        "assistant_version": "0.1.0",
                        "name": "Fixture Assistant",
                        "summary": "Exercise immutable admission.",
                        "declared_creators": ["@fixture"],
                        "image_id": IMAGE_ID,
                        "platform": "linux/amd64",
                        "created_at": CREATED,
                        "provenance": "local",
                        "unpublished": True,
                    }
                ]
            },
        )
        self.assertNotIn("source_digest", result["assistants"][0])

    def test_service_surfaces_the_canonical_invalid_stage_labeled_image(self) -> None:
        client, image, _container_value = _client()
        image.attrs["Config"]["User"] = "0:0"

        with self.assertRaises(ApiProblemError) as caught:
            service.list_local_snapshots(SimpleNamespace(client=client))

        self.assertEqual(caught.exception.code, "local-assistant-snapshots-invalid")
        self.assertEqual(
            caught.exception.message,
            f"Local Assistant snapshot {IMAGE_ID} carries the Local stage label but failed validation",
        )

    def test_service_maps_snapshot_inventory_and_admission_failures(self) -> None:
        inventory_failures = (
            (
                snapshots.LocalSnapshotUnavailableError("offline"),
                "local-assistant-snapshots-unavailable",
            ),
            (snapshots.LocalSnapshotError("invalid"), "local-assistant-snapshots-invalid"),
        )
        for failure, code in inventory_failures:
            with (
                self.subTest(code=code),
                mock.patch.object(service.snapshots, "list_candidates", side_effect=failure),
                self.assertRaises(ApiProblemError) as caught,
            ):
                service.list_local_snapshots(SimpleNamespace(client=object()))
            self.assertEqual(caught.exception.code, code)

        with (
            mock.patch.object(
                service.snapshots,
                "admit",
                side_effect=snapshots.LocalSnapshotError("invalid"),
            ),
            self.assertRaises(ApiProblemError) as caught,
        ):
            service.install_local_snapshot(SimpleNamespace(client=object()), "team_1", IMAGE_ID)
        self.assertEqual(caught.exception.code, "local-assistant-snapshot-invalid")

    def test_service_bounds_and_maps_local_preview_work(self) -> None:
        client, _image_value, _container_value = _client()
        controller = SimpleNamespace(client=client)

        self.assertTrue(service.local_snapshot_icon(controller, IMAGE_ID).startswith(b"\x89PNG"))

        with (
            mock.patch.object(service._LOCAL_PREVIEW_SLOTS, "acquire", return_value=False),
            self.assertRaises(ApiProblemError) as busy,
        ):
            service.local_snapshot_icon(controller, IMAGE_ID)
        self.assertEqual(busy.exception.code, "local-assistant-preview-busy")

        for failure, code in (
            (snapshots.LocalSnapshotUnavailableError("offline"), "local-assistant-preview-unavailable"),
            (snapshots.LocalSnapshotError("invalid"), "local-assistant-preview-invalid"),
        ):
            with (
                self.subTest(code=code),
                mock.patch.object(service.snapshots, "preview_icon", side_effect=failure),
                self.assertRaises(ApiProblemError) as caught,
            ):
                service.local_snapshot_icon(controller, IMAGE_ID)
            self.assertEqual(caught.exception.code, code)

    def test_service_admits_then_replaces_a_published_binding_with_local(self) -> None:
        client, _image_value, _container_value = _client()
        admitted = snapshots.admit(client, IMAGE_ID)
        registry = mock.Mock()
        registry.binding.return_value = SimpleNamespace(
            provenance="published",
            assistant_id="fixture-assistant",
        )
        registry.bindings.return_value = ()
        lifecycle = mock.Mock()
        lifecycle.replace_published_with_local.side_effect = (
            lambda _team_id, _previous, install_successor: install_successor(lifecycle.install_assistant)
        )
        controller = SimpleNamespace(
            client=client,
            registry=registry,
            assistant_icons=mock.Mock(),
            assistant_lifecycle=lifecycle,
        )

        with (
            mock.patch.object(service.snapshots, "admit", return_value=admitted),
            mock.patch.object(
                service,
                "_apply_local_snapshot",
                return_value={"assistant": "fixture-assistant", "installed": True},
            ) as apply_local,
        ):
            result = service.install_local_snapshot(controller, "team_1", IMAGE_ID)

        lifecycle.replace_published_with_local.assert_called_once_with(
            "team_1",
            registry.binding.return_value,
            mock.ANY,
        )
        apply_local.assert_called_once_with(
            controller,
            "team_1",
            None,
            admitted.record,
            install_assistant=lifecycle.install_assistant,
        )
        self.assertEqual(result["provenance"], "local")

    def test_published_to_local_cutover_holds_one_chat_slot(self) -> None:
        lock = threading.Lock()
        previous = SimpleNamespace(
            provenance="published",
            assistant_id="fixture-assistant",
            binding_digest="sha256:" + ("1" * 64),
        )
        events: list[str] = []
        lifecycle = SimpleNamespace(
            registry=SimpleNamespace(binding=lambda _team_id, _assistant_id: previous),
            chat_turn_service=SimpleNamespace(_chat_lock=lambda _team_id: lock),
        )

        def uninstall(_team_id: str, _assistant_id: str) -> None:
            events.append("uninstall")
            self.assertFalse(lock.acquire(blocking=False))

        def install(_team_id: str, _assistant_id: str) -> dict[str, object]:
            events.append("install")
            self.assertTrue(lock.locked())
            return {"assistant": "fixture-assistant", "installed": True}

        lifecycle._uninstall_assistant_unguarded = uninstall
        lifecycle._install_assistant_unguarded = install

        result = assistant_lifecycle.replace_published_with_local(
            lifecycle,
            "team_1",
            previous,
            lambda install_successor: install_successor("team_1", "fixture-assistant"),
        )

        self.assertEqual(events, ["uninstall", "install"])
        self.assertEqual(result, {"assistant": "fixture-assistant", "installed": True})
        self.assertFalse(lock.locked())

    def test_published_to_local_cutover_rejects_a_changed_binding_before_uninstall(self) -> None:
        previous = SimpleNamespace(
            provenance="published",
            assistant_id="fixture-assistant",
            binding_digest="sha256:" + ("1" * 64),
        )
        lifecycle = SimpleNamespace(
            registry=SimpleNamespace(
                binding=lambda _team_id, _assistant_id: SimpleNamespace(
                    provenance="published",
                    assistant_id="fixture-assistant",
                    binding_digest="sha256:" + ("2" * 64),
                )
            ),
            chat_turn_service=SimpleNamespace(_chat_lock=lambda _team_id: threading.Lock()),
            _uninstall_assistant_unguarded=mock.Mock(),
            _install_assistant_unguarded=mock.Mock(),
        )

        with self.assertRaises(ApiProblemError) as caught:
            assistant_lifecycle.replace_published_with_local(
                lifecycle,
                "team_1",
                previous,
                mock.Mock(),
            )

        self.assertEqual(caught.exception.code, "assistant-binding-conflict")
        lifecycle._uninstall_assistant_unguarded.assert_not_called()

    def test_service_maps_local_binding_and_icon_failures(self) -> None:
        client, _image_value, _container_value = _client()
        admitted = snapshots.admit(client, IMAGE_ID)
        candidate = bindings.binding_from_local_record("team_1", admitted.record, snapshots.validate_record)

        def controller():
            registry = mock.Mock()
            registry.binding.return_value = None
            registry.bindings.return_value = ()
            return SimpleNamespace(
                client=client,
                registry=registry,
                assistant_icons=mock.Mock(),
                assistant_lifecycle=mock.Mock(),
            )

        rollback = controller()
        with (
            mock.patch.object(service.snapshots, "admit", return_value=admitted),
            mock.patch.object(
                service,
                "_apply_local_snapshot",
                side_effect=ApiProblemError(503, "rollback", code="assistant-install-rollback-incomplete"),
            ),
            self.assertRaises(ApiProblemError),
        ):
            service.install_local_snapshot(rollback, "team_1", IMAGE_ID)
        rollback.registry.delete.assert_not_called()
        rollback.assistant_icons.discard_binding.assert_called_once_with(candidate, ())

        binding_failure = controller()
        with (
            mock.patch.object(service.snapshots, "admit", return_value=admitted),
            mock.patch.object(
                service,
                "_apply_local_snapshot",
                side_effect=bindings.DynamicAssistantError("conflict"),
            ),
            self.assertRaises(ApiProblemError) as caught,
        ):
            service.install_local_snapshot(binding_failure, "team_1", IMAGE_ID)
        self.assertEqual(caught.exception.code, "assistant-binding-conflict")
        binding_failure.registry.delete.assert_called_once_with("team_1", "fixture-assistant")

        replacement_failure = controller()
        replacement_failure.registry.binding.return_value = candidate
        with (
            mock.patch.object(service.snapshots, "admit", return_value=admitted),
            mock.patch.object(
                service,
                "_apply_local_snapshot",
                side_effect=bindings.DynamicAssistantError("conflict"),
            ),
            self.assertRaises(ApiProblemError),
        ):
            service.install_local_snapshot(replacement_failure, "team_1", IMAGE_ID)
        replacement_failure.registry.delete.assert_not_called()

        icon_failure = controller()
        icon_failure.assistant_icons.put_local.side_effect = AssistantIconError("offline")
        with (
            mock.patch.object(service.snapshots, "admit", return_value=admitted),
            self.assertRaises(ApiProblemError) as caught,
        ):
            service.install_local_snapshot(icon_failure, "team_1", IMAGE_ID)
        self.assertEqual(caught.exception.code, "assistant-icon-unavailable")

        discard_failure = controller()
        discard_failure.assistant_icons.discard_binding.side_effect = AssistantIconError("offline")
        with self.assertRaises(ApiProblemError) as caught:
            service._discard_local_icon(discard_failure, candidate)
        self.assertEqual(caught.exception.code, "assistant-icon-unavailable")

    def test_apply_local_snapshot_handles_idempotence_and_binding_races(self) -> None:
        client, _image_value, _container_value = _client()
        record = snapshots.admit(client, IMAGE_ID).record
        existing = bindings.binding_from_local_record("team_1", record, snapshots.validate_record)
        spec = SimpleNamespace(assistant_id="fixture-assistant")
        controller = SimpleNamespace(registry=mock.Mock(), assistant_lifecycle=mock.Mock())
        controller.registry.local_replacement.return_value = (existing, spec)
        controller.assistant_lifecycle.install_assistant.return_value = {"installed": True}

        self.assertEqual(
            service._apply_local_snapshot(controller, "team_1", existing, record),
            {"installed": True},
        )

        changed = bindings.binding_from_local_record(
            "team_1",
            {**record, "summary": "Changed local summary"},
            snapshots.validate_record,
        )
        controller.registry.local_replacement.return_value = (changed, spec)
        with self.assertRaisesRegex(bindings.DynamicAssistantConflictError, "image id is unchanged"):
            service._apply_local_snapshot(
                controller,
                "team_1",
                existing,
                {**record, "summary": "Changed local summary"},
            )

        replacement = {**record, "image_id": "sha256:" + ("c" * 64)}
        replacement_binding = bindings.binding_from_local_record("team_1", replacement, snapshots.validate_record)
        controller.registry.local_replacement.return_value = (replacement_binding, spec)
        controller.registry.get.return_value = None
        with self.assertRaisesRegex(bindings.DynamicAssistantConflictError, "changed before update"):
            service._apply_local_snapshot(controller, "team_1", existing, replacement)

    def test_registry_rejects_cross_provenance_replacements(self) -> None:
        client, _image_value, _container_value = _client()
        record = snapshots.admit(client, IMAGE_ID).record
        publication = _runtime_resolution()
        publication["assistant_id"] = "fixture-assistant"
        with tempfile.TemporaryDirectory() as directory:
            registry = assistant_registry.AssistantRegistry(
                DynamicAssistantStore(
                    Path(directory) / "bindings.json",
                    local_record_validator=snapshots.validate_record,
                )
            )
            local_spec = registry.put_local("team_1", record)
            local_binding = registry.binding("team_1", local_spec.assistant_id)
            self.assertIsNotNone(local_binding)
            with self.assertRaisesRegex(bindings.DynamicAssistantConflictError, "provenance"):
                registry.replacement("team_1", local_binding.binding_digest, publication)
            with self.assertRaisesRegex(bindings.DynamicAssistantConflictError, "changed before replacement"):
                registry.local_replacement("team_1", "sha256:" + ("0" * 64), record)

            registry.delete("team_1", local_spec.assistant_id)
            published_spec = registry.put("team_1", publication)
            published_binding = registry.binding("team_1", published_spec.assistant_id)
            self.assertIsNotNone(published_binding)
            with self.assertRaisesRegex(bindings.DynamicAssistantConflictError, "provenance"):
                registry.local_replacement("team_1", published_binding.binding_digest, record)

        invalid = SimpleNamespace(provenance="unknown", document={}, assistant_id="fixture-assistant")
        with self.assertRaisesRegex(bindings.DynamicAssistantError, "provenance is invalid"):
            assistant_registry._runtime_identity(invalid)

    def test_service_installs_and_replaces_an_exact_local_snapshot(self) -> None:
        client, _image_value, _container_value = _client()
        admitted = snapshots.admit(client, IMAGE_ID)
        replacement_image = "sha256:" + ("c" * 64)
        replacement = snapshots.AdmittedLocalSnapshot(
            record={**admitted.record, "image_id": replacement_image},
            icon=admitted.icon,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = assistant_registry.AssistantRegistry(
                DynamicAssistantStore(
                    root / "bindings.json",
                    local_record_validator=snapshots.validate_record,
                )
            )
            icon_store = AssistantIconStore(root / "icons")
            lifecycle = SimpleNamespace(
                install_assistant=mock.Mock(return_value={"assistant": "fixture-assistant", "installed": True}),
            )
            controller = SimpleNamespace(
                client=client,
                registry=registry,
                assistant_icons=icon_store,
                assistant_lifecycle=lifecycle,
            )

            installed = service.install_local_snapshot(controller, "team_1", IMAGE_ID)

            self.assertEqual(installed["image_id"], IMAGE_ID)
            self.assertEqual(registry.binding("team_1", "fixture-assistant").provenance, "local")

            def update_assistant(team_id, _previous, _successor, **options):
                registry.commit_local_replacement(
                    team_id,
                    options["previous_binding"].binding_digest,
                    options["successor_document"],
                )
                return {"assistant": "fixture-assistant", "installed": False, "updated": True}

            lifecycle.update_assistant = mock.Mock(side_effect=update_assistant)
            with mock.patch.object(service.snapshots, "admit", return_value=replacement):
                updated = service.install_local_snapshot(controller, "team_1", replacement_image)

            self.assertTrue(updated["updated"])
            self.assertEqual(registry.get("team_1", "fixture-assistant").image, replacement_image)
            with self.assertRaises(AssistantIconError):
                icon_store.read_binding(
                    bindings.binding_from_local_record(
                        "team_1",
                        admitted.record,
                        snapshots.validate_record,
                    )
                )

    def test_service_rolls_back_new_binding_and_maps_snapshot_availability(self) -> None:
        client, _image_value, _container_value = _client()
        admitted = snapshots.admit(client, IMAGE_ID)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = assistant_registry.AssistantRegistry(
                DynamicAssistantStore(
                    root / "bindings.json",
                    local_record_validator=snapshots.validate_record,
                )
            )
            icon_store = AssistantIconStore(root / "icons")
            controller = SimpleNamespace(
                client=client,
                registry=registry,
                assistant_icons=icon_store,
                assistant_lifecycle=SimpleNamespace(
                    install_assistant=mock.Mock(side_effect=ApiProblemError(503, "failed", code="docker-start-failed"))
                ),
            )

            with self.assertRaisesRegex(ApiProblemError, "failed"):
                service.install_local_snapshot(controller, "team_1", IMAGE_ID)

            self.assertIsNone(registry.binding("team_1", "fixture-assistant"))
            candidate = bindings.binding_from_local_record(
                "team_1",
                admitted.record,
                snapshots.validate_record,
            )
            with self.assertRaises(AssistantIconError):
                icon_store.read_binding(candidate)

        with (
            mock.patch.object(
                service.snapshots,
                "admit",
                side_effect=snapshots.LocalSnapshotUnavailableError("offline"),
            ),
            self.assertRaises(ApiProblemError) as caught,
        ):
            service.install_local_snapshot(SimpleNamespace(client=object()), "team_1", IMAGE_ID)
        self.assertEqual(caught.exception.code, "local-assistant-snapshot-unavailable")


if __name__ == "__main__":
    unittest.main()
