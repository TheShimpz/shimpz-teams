"""Real-Docker assertions for the Local controller's staged snapshot inventory."""

import hashlib
import json
import tempfile
import time
from pathlib import Path

from local_controller_docker_fixture import DockerFlow

from assistant import language as assistant_language
from assistant import manifest as assistant_manifest
from local.install import snapshots
from protocol.http.v1 import payload as http_payload
from tests import local_snapshot_fixtures as snapshot_fixtures
from tests.test_assistant_manifest import manifest
from tests.test_local_snapshot_admission import UNROUTED_STORED_INPUT


class LocalSnapshotInventoryMixin:
    """Stage snapshots in the contract's own daemon exactly as the CLI tags them and read Team's live inventory."""

    def _assert_local_snapshot_fast_path(self, flow: DockerFlow) -> None:
        # A staged snapshot whose Stored Input declares no reviewed routes fails the current contract: Team still lists
        # its identity, judges it inadmissible with the full install admission, and reuses that verdict while the
        # exact image and its tag are unchanged. Beside it, an image whose valid id label sits beside an invalid
        # Action label is an inadmissible shadow, and an image with no valid id label is left out: neither fails the
        # inventory.
        references = {
            name: snapshots.canonical_reference(name) for name in ("fixture-assistant", "broken-labels", "unnamed")
        }
        image_id = self._stage_snapshot(references["fixture-assistant"], UNROUTED_STORED_INPUT)
        shadow_id = self._stage_snapshot(
            references["broken-labels"],
            manifest(),
            {snapshots.ASSISTANT_LABEL: "broken-labels", snapshots.ACTIONS_LABEL: "Ping"},
        )
        self._stage_snapshot(references["unnamed"], manifest(), {snapshots.ASSISTANT_LABEL: None})
        try:
            local_status, local_catalog = self.api(flow.port, flow.token, "GET", "/v1/local-assistants")
            self.assertEqual(local_status, 200, local_catalog)
            entries = {entry["assistant_id"]: entry for entry in local_catalog["assistants"]}
            self.assertEqual(set(entries), {"fixture-assistant", "broken-labels"})
            self.assertTrue(all(http_payload.canonical_local_assistant(entry) == entry for entry in entries.values()))
            unrouted, shadow = entries["fixture-assistant"], entries["broken-labels"]
            self.assertEqual(
                (unrouted["image_id"], unrouted["admission"], unrouted["reason"]),
                (image_id, "inadmissible", "declaration-invalid"),
            )
            self.assertEqual(
                (shadow["image_id"], shadow["admission"], shadow["reason"], shadow["name"]),
                (shadow_id, "inadmissible", "labels-invalid", None),
            )
            started = time.monotonic()
            warm_status, warm_catalog = self.api(flow.port, flow.token, "GET", "/v1/local-assistants")
            warm_elapsed = time.monotonic() - started
            self.assertEqual(warm_status, 200, warm_catalog)
            self.assertEqual(warm_catalog["assistants"], local_catalog["assistants"])
            self.assertLess(warm_elapsed, 0.5, f"warm Local snapshot inventory took {warm_elapsed:.3f}s")
        finally:
            for reference in references.values():
                self.docker("image", "rm", "--force", reference, check=False)
        log_result = self.docker("logs", flow.controller)
        logs = log_result.stdout + log_result.stderr
        self.assertNotIn("Docker image event validation failed", logs)

    def _stage_snapshot(
        self,
        reference: str,
        declaration: bytes,
        overrides: dict[str, str | None] | None = None,
    ) -> str:
        """Stage one never-started snapshot exactly as the CLI tags it: its files, labels, and fixed runtime.

        ``overrides`` replaces stage labels, and a None value leaves that label out.
        """
        package, manifest, icon = snapshot_fixtures._package(declaration)
        files = {
            snapshots.SOURCE_PATH: package,
            assistant_manifest.MANIFEST_PATH: manifest,
            assistant_manifest.CONTRACT_PATH: json.dumps(
                snapshot_fixtures.MACHINE_CONTRACT, separators=(",", ":")
            ).encode(),
            assistant_language.PACK_PATH: snapshot_fixtures.PACK,
            snapshots.ICON_PATH: icon,
        }
        labels = {
            snapshots.LOCAL_STAGE_LABEL: snapshots.LOCAL_STAGE_VALUE,
            snapshots.ASSISTANT_LABEL: "fixture-assistant",
            snapshots.SOURCE_LABEL: f"sha256:{hashlib.sha256(package).hexdigest()}",
            snapshots.VERSION_LABEL: "0.1.0",
            snapshots.NAME_LABEL: "Fixture Assistant",
            snapshots.SUMMARY_LABEL: snapshot_fixtures.SUMMARY,
            snapshots.DECLARED_CREATORS_LABEL: "@fixture",
            snapshots.BUILD_LABEL: snapshot_fixtures.BUILD_DIGEST,
            snapshots.ACTIONS_LABEL: "ping",
            snapshots.INTEGRATIONS_LABEL: "",
        }
        for key, value in (overrides or {}).items():
            if value is None:
                labels.pop(key)
            else:
                labels[key] = value
        with tempfile.TemporaryDirectory() as build_root:
            copies = []
            for index, (path, contents) in enumerate(files.items()):
                Path(build_root, str(index)).write_bytes(contents)
                copies.append(f"COPY --chmod=0444 {index} {path}\n")
            Path(build_root, "Dockerfile").write_text(
                "FROM scratch\n"
                + "".join(copies)
                + "".join(f"LABEL {key}={json.dumps(value)}\n" for key, value in labels.items())
                + f"USER {snapshots.RUNTIME_USER}\n"
                + f"ENTRYPOINT {json.dumps(snapshots.RUNTIME_ENTRYPOINT)}\n",
                encoding="utf-8",
            )
            self.docker("build", "--quiet", "--tag", reference, build_root)
        return self.docker("image", "inspect", "--format", "{{.Id}}", reference).stdout.strip()
