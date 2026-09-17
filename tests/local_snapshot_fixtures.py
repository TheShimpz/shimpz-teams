"""Reusable exact-image fixtures for Local snapshot boundary tests."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from types import SimpleNamespace
from unittest import mock

from assistant import manifest as assistant_manifest
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


def fresh_installing_lifecycle(install_assistant: mock.Mock) -> SimpleNamespace:
    lifecycle = SimpleNamespace(install_assistant=install_assistant)
    lifecycle.install_fresh_local = lambda _team_id, _assistant_id, install_successor: install_successor(
        lifecycle.install_assistant
    )
    return lifecycle


def archive(name: str, contents: bytes) -> bytes:
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
        snapshots.ACTIONS_LABEL: "ping",
        snapshots.INTEGRATIONS_LABEL: "",
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
        return iter((archive(name, contents),)), {"name": name, "size": len(contents), "mode": 0o444}

    container.get_archive.side_effect = get_archive
    return container


def client(*, source_digest: str | None = None):
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
    docker_client = mock.Mock()
    docker_client.info.return_value = {"Architecture": "x86_64"}
    docker_client.images.get.return_value = image
    docker_client.api.images.return_value = [{"Id": IMAGE_ID}]
    docker_client.containers.create.return_value = container
    return docker_client, image, container
