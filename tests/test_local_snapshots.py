"""Fail-closed admission of unpublished Local Assistant snapshots."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from docker.errors import DockerException

from assistant import manifest as assistant_manifest
from install.bindings import DynamicAssistantStore
from local.install import registry as assistant_registry
from local.install import snapshots, source_package
from tests.test_assistant_manifest import manifest
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
    def test_lists_only_bounded_stage_candidates(self) -> None:
        client, _image_value, _container_value = _client()

        candidates = snapshots.list_candidates(client)

        self.assertEqual(
            candidates,
            (
                snapshots.LocalSnapshotCandidate(
                    "fixture-assistant",
                    "0.1.0",
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

    def test_rejects_source_mismatch_and_always_removes_container(self) -> None:
        client, _image_value, container = _client(source_digest="sha256:" + ("f" * 64))

        with self.assertRaisesRegex(snapshots.LocalSnapshotError, "digest"):
            snapshots.admit(client, IMAGE_ID)

        container.remove.assert_called_once_with(force=True, v=False)

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


if __name__ == "__main__":
    unittest.main()
