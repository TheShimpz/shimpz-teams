"""Local controller lifecycle contract against the real Docker daemon.

Developers resolution and Sigstore evidence are deterministic fixtures; controller
HTTP, Supervisor authority, registry state, Docker pull, and isolation remain real.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import nullcontext, suppress
from pathlib import Path
from types import SimpleNamespace

import docker
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from docker.errors import ImageNotFound

TEAM = Path(__file__).resolve().parents[1]
FIXTURE = TEAM / "tests" / "fixtures" / "reference-assistant"
REGISTRY_IMAGE = "registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
BUILDKIT_IMAGE = "moby/buildkit:v0.32.0@sha256:1f8167fcb0eca5b7126353d35299386945cbb8949cc516c592a49f80cfce4fa2"
MANAGED_LABEL = "com.shimpz.local.managed"
PROFILE_LABEL = "com.shimpz.local.profile"
SPACE_LABEL = "com.shimpz.local.space-id"
KIND_LABEL = "com.shimpz.local.kind"
LOCAL_PROFILE = "local-v1"

sys.path.insert(0, str(TEAM))
from docker_harness import DockerHarnessMixin
from local_controller_docker_assistant import LocalAssistantLifecycleMixin
from local_controller_docker_egress import LocalEgressRecoveryMixin
from local_controller_docker_fixture import (
    DockerFlow,
    fixture_resolution,
    fixture_source_digest,
    new_flow,
    prepare_account_egress_capability,
    runtime_secret_metadata,
    supervisor_header,
)

from install import update as assistant_update
from local import app as local_app
from local.install import snapshots
from protocol.http.v1 import supervisor as supervisor_contract


class DockerFlowTests(
    LocalAssistantLifecycleMixin,
    LocalEgressRecoveryMixin,
    DockerHarnessMixin,
    unittest.TestCase,
):
    maxDiff = None
    docker_command = "docker"
    docker_cwd = TEAM
    controller_kind = "local controller"

    def _new_flow(self) -> DockerFlow:
        self._supervisors_by_port: dict[int, DockerFlow] = {}
        return new_flow(self._run)

    def _api(
        self,
        port: int,
        credential: str | None,
        method: str,
        path: str,
        body: dict[str, object] | bytes | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        headers = dict(extra_headers or {})
        flow = self._supervisors_by_port.get(port)
        if credential is not None and flow is not None and path != "/healthz":
            headers[supervisor_contract.ASSERTION_HEADER] = supervisor_header(
                flow,
                method,
                path,
                body,
                headers,
            )
        return super()._api(
            port,
            credential,
            method,
            path,
            body,
            extra_headers=headers,
        )

    @staticmethod
    def _ownership(space_id: str, kind: str) -> dict[str, str]:
        return {
            MANAGED_LABEL: "1",
            PROFILE_LABEL: LOCAL_PROFILE,
            SPACE_LABEL: space_id,
            KIND_LABEL: kind,
        }

    def _owned_ids(self, resource: str, space_id: str, kind: str) -> list[str]:
        expected = self._ownership(space_id, kind)
        filters: list[str] = []
        for key, value in expected.items():
            filters.extend(("--filter", f"label={key}={value}"))
        if resource == "container":
            result = self._run("container", "ls", "--all", "--quiet", *filters, check=False)
        else:
            result = self._run("network", "ls", "--quiet", *filters, check=False)
        if result.returncode != 0:
            return []

        verified: list[str] = []
        for resource_id in result.stdout.splitlines():
            inspected = self._run("inspect", resource_id, check=False)
            if inspected.returncode != 0:
                continue
            try:
                metadata = json.loads(inspected.stdout)[0]
            except IndexError, TypeError, json.JSONDecodeError:
                continue
            labels = metadata.get("Config", {}).get("Labels") if resource == "container" else metadata.get("Labels")
            if isinstance(labels, dict) and all(labels.get(key) == value for key, value in expected.items()):
                verified.append(resource_id)
        return verified

    def _cleanup_owned_space(self, space_id: str) -> None:
        # Workloads must leave their networks before Docker can remove those networks.
        for container_id in self._owned_ids("container", space_id, "assistant"):
            self._remove("container", "rm", "--force", container_id)
        for network_id in self._owned_ids("network", space_id, "team"):
            self._remove("network", "rm", network_id)

    def _wait_registry(self, port: int) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/v2/", timeout=1) as response:
                    if response.status == 200:
                        return
            except OSError, urllib.error.URLError:
                time.sleep(0.2)
        self.fail("the test OCI registry did not become ready")

    def _wait_local_controller(self, container: str) -> tuple[int, str]:
        def probe() -> tuple[int, str] | None:
            state = self._run("inspect", "--format", "{{.State.Status}}", container, check=False)
            if state.returncode == 0 and state.stdout.strip() == "running":
                token_result = self._run(
                    "exec",
                    container,
                    "/opt/venv/bin/python",
                    "-c",
                    "from pathlib import Path; print(Path('/run/shimpz-local/token').read_text())",
                    check=False,
                )
                if token_result.returncode == 0 and len(token_result.stdout.strip()) == 64:
                    mapping = self._run("port", container, "7077/tcp").stdout.strip()
                    port = int(mapping.rsplit(":", 1)[1])
                    try:
                        status, _ = self._api(port, token_result.stdout.strip(), "GET", "/healthz")
                    except OSError, urllib.error.URLError:
                        pass
                    else:
                        if status == 200:
                            return port, token_result.stdout.strip()
            return None

        return self._wait_controller(container, probe, interval=0.25)

    def _prepare_images(self, flow: DockerFlow) -> None:
        self._run(
            "buildx",
            "create",
            "--name",
            flow.builder,
            "--driver",
            "docker-container",
            "--driver-opt",
            "network=host",
            "--driver-opt",
            f"image={BUILDKIT_IMAGE}",
            "--driver-opt",
            f"cpuset-cpus={flow.test_cpuset}",
            "--driver-opt",
            "memory=4g",
            "--driver-opt",
            "memory-swap=4g",
            "--bootstrap",
        )
        self._run(
            "buildx",
            "build",
            "--builder",
            flow.builder,
            "--load",
            "--tag",
            flow.fixture_tag,
            "--label",
            f"org.shimpz.source.digest={fixture_source_digest()}",
            "--file",
            str(FIXTURE / "Dockerfile"),
            str(TEAM),
        )
        fixture_id = self._run("image", "inspect", "--format", "{{.Id}}", flow.fixture_tag).stdout.strip()

        self._run(
            "run",
            "--detach",
            "--name",
            flow.registry,
            "--cpuset-cpus",
            flow.test_cpuset,
            "--cpus",
            "1",
            "--memory",
            "256m",
            "--memory-swap",
            "256m",
            "--pids-limit",
            "128",
            "--publish",
            "127.0.0.1::5000",
            REGISTRY_IMAGE,
        )
        registry_port = int(self._run("port", flow.registry, "5000/tcp").stdout.strip().rsplit(":", 1)[1])
        self._wait_registry(registry_port)
        repository_tag = f"127.0.0.1:{registry_port}/shimpz/shimpz-cloudflare:test"
        self._run("tag", flow.fixture_tag, repository_tag)
        self._run("push", repository_tag)
        repo_digests = json.loads(
            self._run("image", "inspect", "--format", "{{json .RepoDigests}}", repository_tag).stdout
        )
        flow.trusted_ref = next(
            item for item in repo_digests if item.startswith(repository_tag.rsplit(":", 1)[0] + "@")
        )
        self.assertRegex(flow.trusted_ref, r"@sha256:[0-9a-f]{64}$")

        # Remove every local fixture reference so installation must perform a real digest pull.
        self._remove("image", "rm", "--force", repository_tag, flow.fixture_tag, fixture_id)
        self.assertNotEqual(self._run("image", "inspect", flow.trusted_ref, check=False).returncode, 0)

        self._run(
            "buildx",
            "build",
            "--builder",
            flow.builder,
            "--load",
            "--file",
            str(TEAM / "local" / "Dockerfile"),
            "--tag",
            flow.controller_tag,
            str(TEAM),
        )

    def _start_controller(self, flow: DockerFlow) -> None:
        self._run("volume", "create", flow.token_volume)
        self._run("volume", "create", flow.runtime_token_volume)
        self._run("volume", "create", flow.audit_volume)
        self._run("volume", "create", flow.storage_volume)
        self._run("volume", "create", flow.inference_volume)
        self._run("volume", "create", flow.action_journal_volume)
        self._run("volume", "create", flow.publication_volume)
        self._run("volume", "create", flow.continuation_state_volume)
        self._run("volume", "create", flow.continuation_key_volume)
        self._run("volume", "create", flow.supervisor_key_volume)
        self._run("volume", "create", flow.account_egress_capability_volume)
        self._run("volume", "create", flow.egress_policy_volume)
        self._run("volume", "create", flow.egress_audit_volume)
        self._run("network", "create", flow.outbound_network)
        self._run(
            "buildx",
            "build",
            "--builder",
            flow.builder,
            "--load",
            "--file",
            str(TEAM.parent / ".egress" / "Dockerfile"),
            "--tag",
            flow.egress_proxy_tag,
            str(TEAM.parent),
        )
        public_key = flow.supervisor_private_key.public_key().public_bytes(
            Encoding.PEM,
            PublicFormat.SubjectPublicKeyInfo,
        )
        resolution = json.dumps(
            fixture_resolution(flow),
            separators=(",", ":"),
        ).encode()
        self._run(
            "run",
            "--rm",
            "--user",
            "0:0",
            "--volume",
            f"{flow.supervisor_key_volume}:/keys",
            "--volume",
            f"{flow.publication_volume}:/publications",
            "--env",
            f"SHIMPZ_TEST_SUPERVISOR_PUBLIC_KEY={base64.b64encode(public_key).decode('ascii')}",
            "--env",
            f"SHIMPZ_TEST_PUBLICATION={base64.b64encode(resolution).decode('ascii')}",
            "--entrypoint",
            "/opt/venv/bin/python",
            flow.controller_tag,
            "-c",
            "import base64,os; from pathlib import Path; "
            "p=Path('/keys/public.pem'); "
            "p.write_bytes(base64.b64decode(os.environ['SHIMPZ_TEST_SUPERVISOR_PUBLIC_KEY'],validate=True)); "
            "os.chown(p,0,10021); p.chmod(0o440); "
            "r=Path('/publications/test-resolution.json'); "
            "r.write_bytes(base64.b64decode(os.environ['SHIMPZ_TEST_PUBLICATION'],validate=True)); "
            "os.chown(r.parent,10001,10001); r.parent.chmod(0o700); "
            "os.chown(r,10001,10001); r.chmod(0o600)",
        )
        prepare_account_egress_capability(self._run, flow)
        self._run(
            "run",
            "--detach",
            "--name",
            flow.egress_proxy,
            "--network",
            flow.outbound_network,
            "--cpuset-cpus",
            flow.test_cpuset,
            "--cpus",
            "1",
            "--user",
            "10005:10005",
            "--group-add",
            "10017",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "128m",
            "--memory-swap",
            "128m",
            "--pids-limit",
            "64",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m",
            "--volume",
            f"{flow.egress_policy_volume}:/policy:ro",
            "--volume",
            f"{flow.egress_audit_volume}:/var/log/assistant-egress",
            "--label",
            "com.shimpz.local.managed=1",
            "--label",
            "com.shimpz.local.profile=local-v1",
            "--label",
            f"com.shimpz.local.space-id={flow.space_id}",
            "--label",
            "com.shimpz.local.kind=assistant-egress",
            flow.egress_proxy_tag,
            "assistant",
        )
        egress_metadata = json.loads(self._run("inspect", flow.egress_proxy).stdout)[0]
        egress_host = egress_metadata["HostConfig"]
        self.assertEqual(set(egress_host["CapDrop"]), {"ALL"})
        self.assertIn(egress_host.get("CapAdd"), (None, []))
        self.assertEqual(len(egress_host["SecurityOpt"]), 1)
        self.assertIn(
            egress_host["SecurityOpt"][0],
            {"no-new-privileges", "no-new-privileges:true"},
        )
        socket_gid = str(Path("/var/run/docker.sock").stat().st_gid)
        self._run(
            "run",
            "--detach",
            "--name",
            flow.controller,
            "--cpuset-cpus",
            flow.test_cpuset,
            "--cpus",
            "2",
            "--memory",
            "512m",
            "--memory-swap",
            "512m",
            "--pids-limit",
            "128",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=32m",
            "--group-add",
            socket_gid,
            "--group-add",
            "10016",
            "--group-add",
            "10017",
            "--group-add",
            "10021",
            "--group-add",
            "10022",
            "--volume",
            "/var/run/docker.sock:/var/run/docker.sock",
            "--volume",
            f"{flow.token_volume}:/run/shimpz-local",
            "--volume",
            f"{flow.runtime_token_volume}:/run/shimpz-brain-runtime",
            "--volume",
            f"{flow.audit_volume}:/var/log/shimpz-local",
            "--volume",
            f"{flow.storage_volume}:/var/lib/shimpz-local/storage",
            "--volume",
            f"{flow.inference_volume}:/var/lib/shimpz-local/inference",
            "--volume",
            f"{flow.action_journal_volume}:/var/lib/shimpz-local/action-journal",
            "--volume",
            f"{flow.publication_volume}:/var/lib/shimpz-local/publications",
            "--volume",
            f"{flow.continuation_state_volume}:/var/lib/shimpz-local/chat-continuations/state",
            "--volume",
            f"{flow.continuation_key_volume}:/var/lib/shimpz-local/chat-continuations/key",
            "--volume",
            f"{flow.supervisor_key_volume}:/run/shimpz-local-supervisor:ro",
            "--volume",
            f"{flow.account_egress_capability_volume}:/run/shimpz-account-egress:ro",
            "--volume",
            f"{FIXTURE / 'local-controller-fixture.py'}:/local-controller-fixture.py:ro",
            "--volume",
            f"{flow.egress_policy_volume}:/var/lib/shimpz-local/assistant-egress",
            "--env",
            f"SHIMPZ_SPACE_ID={flow.space_id}",
            "--env",
            f"SHIMPZ_ASSISTANT_EGRESS_CONTAINER={flow.egress_proxy}",
            "--env",
            "SHIMPZ_ASSISTANT_EGRESS_POLICY_DIR=/var/lib/shimpz-local/assistant-egress",
            "--env",
            "SHIMPZ_OAUTH_BROKER_PROXY_HOST=shimpz-account-egress",
            "--env",
            "SHIMPZ_OAUTH_BROKER_PROXY_CAPABILITY_FILE=/run/shimpz-account-egress/token",
            "--env",
            f"SHIMPZ_BRAIN_RUNTIME_URL=http://{flow.bridge_gateway}:{flow.brain_server.server_port}",
            "--publish",
            "127.0.0.1::7077",
            "--entrypoint",
            "/opt/venv/bin/python",
            flow.controller_tag,
            "/local-controller-fixture.py",
        )
        flow.port, flow.token = self._wait_local_controller(flow.controller)
        self._supervisors_by_port[flow.port] = flow
        journal_mode = self._run(
            "exec",
            flow.controller,
            "/opt/venv/bin/python",
            "-c",
            "import os,stat; s=os.stat('/var/lib/shimpz-local/action-journal/journal.sqlite3'); "
            "print(oct(stat.S_IMODE(s.st_mode)),s.st_uid,s.st_gid,s.st_nlink)",
        ).stdout.strip()
        self.assertEqual(journal_mode, "0o600 10001 10001 1")
        continuation_files = self._run(
            "exec",
            flow.controller,
            "/opt/venv/bin/python",
            "-c",
            "import os,stat,time; from local.chat.continuation_store import EncryptedContinuationStore; "
            "s=EncryptedContinuationStore(); "
            "s.put('demo_team','integrations','0'*32,int(time.time())+60,['thread:test'],b'opaque'); "
            "assert s.delete('demo_team'); "
            "paths=(s.state_path,s.key_path); "
            "print(' '.join(f'{oct(stat.S_IMODE(p.stat().st_mode))}:{p.stat().st_uid}:{p.stat().st_gid}' "
            "for p in paths))",
        ).stdout.strip()
        self.assertEqual(continuation_files, "0o600:10001:10001 0o600:10001:10001")

        unauthenticated, _ = self._api(flow.port, None, "GET", "/v1/assistants")
        self.assertEqual(unauthenticated, 401)
        status, catalog = self._api(flow.port, flow.token, "GET", "/v1/assistants")
        controller_logs = ""
        if status != 200:
            log_result = self._run("logs", flow.controller, check=False)
            controller_logs = (log_result.stdout + log_result.stderr)[-2000:]
        self.assertEqual(status, 200, f"{catalog}\n{controller_logs}")
        self.assertEqual(catalog["assistants"], [])
        self._assert_local_snapshot_fast_path(flow)

    def _assert_local_snapshot_fast_path(self, flow: DockerFlow) -> None:
        local_status, local_catalog = self._api(flow.port, flow.token, "GET", "/v1/local-assistants")
        self.assertEqual(local_status, 200, local_catalog)
        started = time.monotonic()
        warm_status, warm_catalog = self._api(flow.port, flow.token, "GET", "/v1/local-assistants")
        warm_elapsed = time.monotonic() - started
        self.assertEqual(warm_status, 200, warm_catalog)
        self.assertEqual(warm_catalog["assistants"], local_catalog["assistants"])
        self.assertLess(warm_elapsed, 0.5, f"warm Local snapshot inventory took {warm_elapsed:.3f}s")
        log_result = self._run("logs", flow.controller)
        logs = log_result.stdout + log_result.stderr
        self.assertNotIn("Docker image event validation failed", logs)

    def _exercise_team_storage(self, flow: DockerFlow) -> None:
        status, created = self._api(
            flow.port,
            flow.token,
            "POST",
            "/v1/teams/demo_team/create",
            {"team_name": "Demo Team"},
        )
        self.assertEqual(status, 200, created)
        self.assertTrue(created.get("created"), created)
        _, created_again = self._api(
            flow.port,
            flow.token,
            "POST",
            "/v1/teams/demo_team/create",
            {"team_name": "Demo Team"},
        )
        self.assertFalse(created_again["created"])
        _, teams = self._api(flow.port, flow.token, "GET", "/v1/teams")
        self.assertEqual(
            teams["teams"],
            [{"team_id": "demo_team", "team_name": "Demo Team", "status": "running"}],
        )

        file_status, uploaded = self._api(
            flow.port,
            flow.token,
            "POST",
            "/v1/teams/demo_team/files",
            b"Team private data",
            extra_headers={
                "Content-Type": "text/plain",
                "X-Shimpz-Filename": "brief.txt",
            },
        )
        self.assertEqual(file_status, 200)
        flow.file_id = uploaded["file"]["id"]
        self.assertRegex(flow.file_id, r"^[0-9a-f]{32}$")
        self.assertEqual(uploaded["file"]["limit_bytes"], 100 * 1024 * 1024)
        _, files = self._api(flow.port, flow.token, "GET", "/v1/teams/demo_team/files")
        self.assertEqual(files["files"][0]["id"], flow.file_id)
        self.assertEqual(files["used_bytes"], len(b"Team private data"))
        self.assertEqual(
            self._run(
                "exec",
                flow.controller,
                "test",
                "-f",
                "/var/lib/shimpz-local/storage/demo_team/files.sqlite3",
                check=False,
            ).returncode,
            0,
        )

        self._run("restart", flow.controller)
        flow.port, flow.token = self._wait_local_controller(flow.controller)
        self._supervisors_by_port[flow.port] = flow
        restart_status, files_after_restart = self._api(
            flow.port,
            flow.token,
            "GET",
            "/v1/teams/demo_team/files",
        )
        self.assertEqual(restart_status, 200, files_after_restart)
        self.assertEqual(files_after_restart["files"][0]["id"], flow.file_id)

        # A daemon-side network loss must not let a new lifecycle inherit the old opaque data.
        self._api(
            flow.port,
            flow.token,
            "POST",
            "/v1/teams/orphan_team/create",
            {"team_name": "Orphan Team"},
        )
        self._api(
            flow.port,
            flow.token,
            "POST",
            "/v1/teams/orphan_team/files",
            b"must not survive",
            extra_headers={
                "Content-Type": "application/octet-stream",
                "X-Shimpz-Filename": "stale.txt",
            },
        )
        prefix = hashlib.sha256(flow.space_id.encode("ascii")).hexdigest()[:12]
        self._run("network", "rm", f"shimpz-local-{prefix}-team-orphan_team")
        _, recreated = self._api(
            flow.port,
            flow.token,
            "POST",
            "/v1/teams/orphan_team/create",
            {"team_name": "Orphan Team"},
        )
        self.assertTrue(recreated["created"])
        _, orphan_files = self._api(flow.port, flow.token, "GET", "/v1/teams/orphan_team/files")
        self.assertEqual(orphan_files["files"], [])
        self._api(flow.port, flow.token, "DELETE", "/v1/teams/orphan_team")

    def _exercise_teardown(self, flow: DockerFlow) -> None:
        proxy_metadata = json.loads(self._run("inspect", flow.egress_proxy).stdout)[0]
        proxy_networks = proxy_metadata["NetworkSettings"]["Networks"]
        self.assertEqual(set(proxy_networks), {flow.outbound_network, flow.network_name})
        self.assertIn("shimpz-assistant-egress", proxy_networks[flow.network_name]["Aliases"])
        policy_contract = self._run(
            "exec",
            flow.controller,
            "/opt/venv/bin/python",
            "-c",
            "import json,os,stat; from pathlib import Path; "
            "p=next(Path('/var/lib/shimpz-local/assistant-egress').glob('*.json')); s=p.stat(); "
            "print(json.dumps(json.loads(p.read_text())),oct(stat.S_IMODE(s.st_mode)),s.st_uid,s.st_gid)",
        ).stdout.strip()
        self.assertEqual(
            policy_contract,
            '["api.cloudflare.com"] 0o640 10001 10017',
        )

        _, removed = self._api(
            flow.port,
            flow.token,
            "DELETE",
            "/v1/teams/demo_team/assistants/shimpz-cloudflare",
        )
        self.assertTrue(removed["uninstalled"])
        removed_again_status, removed_again = self._api(
            flow.port,
            flow.token,
            "DELETE",
            "/v1/teams/demo_team/assistants/shimpz-cloudflare",
        )
        self.assertEqual(removed_again_status, 404)
        self.assertEqual(removed_again["code"], "assistant-not-allowlisted")
        proxy_networks_after_uninstall = json.loads(self._run("inspect", flow.egress_proxy).stdout)[0][
            "NetworkSettings"
        ]["Networks"]
        self.assertEqual(set(proxy_networks_after_uninstall), {flow.outbound_network})
        remaining_policy_files = self._run(
            "exec",
            flow.controller,
            "/opt/venv/bin/python",
            "-c",
            "from pathlib import Path; p=Path('/var/lib/shimpz-local/assistant-egress'); "
            "print(len(list(p.glob('*.json'))),len(list((p/'.tokens').glob('*.token'))))",
        ).stdout.strip()
        self.assertEqual(remaining_policy_files, "0 0")
        _, deleted_file = self._api(
            flow.port,
            flow.token,
            "DELETE",
            f"/v1/teams/demo_team/files/{flow.file_id}",
        )
        self.assertTrue(deleted_file["deleted"])
        destroy_status, destroyed = self._api(flow.port, flow.token, "DELETE", "/v1/teams/demo_team")
        self.assertEqual(destroy_status, 200, destroyed)
        self.assertTrue(destroyed["destroyed"])
        self.assertTrue(destroyed["storage_removed"])
        self.assertEqual(
            destroyed["residue_absent"],
            [
                "action_checkpoints",
                "assistant_containers",
                "brain_checkpoints",
                "chat_continuations",
                "egress_policies",
                "inference_configuration",
                "integration_credentials",
                "publication_bindings",
                "runtime_state",
                "stored_inputs",
                "team_networks",
                "team_storage",
            ],
        )
        self.assertNotEqual(
            self._run(
                "exec",
                flow.controller,
                "test",
                "-e",
                "/var/lib/shimpz-local/storage/demo_team",
                check=False,
            ).returncode,
            0,
        )
        _, destroyed_again = self._api(flow.port, flow.token, "DELETE", "/v1/teams/demo_team")
        self.assertFalse(destroyed_again["destroyed"])

    def _exercise_reset(self, flow: DockerFlow) -> None:
        # Reset owns no identifiers and ignores a similarly labeled resource missing the exact kind label.
        self._run(
            "network",
            "create",
            "--internal",
            "--label",
            "com.shimpz.local.managed=1",
            "--label",
            "com.shimpz.local.profile=local-v1",
            "--label",
            f"com.shimpz.local.space-id={flow.space_id}",
            flow.foreign_network,
        )
        self._api(
            flow.port,
            flow.token,
            "POST",
            "/v1/teams/reset_team/create",
            {"team_name": "Reset Team"},
        )
        self._api(
            flow.port,
            flow.token,
            "POST",
            "/v1/teams/reset_team/assistants",
            {
                "assistant_id": "shimpz-cloudflare",
                "source_digest": flow.source_digest,
            },
        )
        self._api(
            flow.port,
            flow.token,
            "POST",
            "/v1/teams/reset_team/files",
            b"remove me",
            extra_headers={
                "Content-Type": "application/octet-stream",
                "X-Shimpz-Filename": "reset.txt",
            },
        )
        reset_status, reset = self._api(flow.port, flow.token, "DELETE", "/v1/space")
        self.assertEqual(reset_status, 200)
        self.assertEqual((reset["assistants_removed"], reset["teams_removed"]), (1, 1))
        self.assertEqual(
            reset["residue_absent"],
            [
                "action_checkpoints",
                "assistant_containers",
                "brain_checkpoints",
                "chat_continuations",
                "egress_policies",
                "inference_configuration",
                "integration_credentials",
                "publication_bindings",
                "runtime_state",
                "stored_inputs",
                "team_networks",
                "team_storage",
            ],
        )
        _, reset_again = self._api(flow.port, flow.token, "DELETE", "/v1/space")
        self.assertEqual((reset_again["assistants_removed"], reset_again["teams_removed"]), (0, 0))
        self.assertNotEqual(
            self._run(
                "exec",
                flow.controller,
                "test",
                "-e",
                "/var/lib/shimpz-local/storage/reset_team",
                check=False,
            ).returncode,
            0,
        )
        self.assertEqual(self._run("network", "inspect", flow.foreign_network, check=False).returncode, 0)

        audit = self._run(
            "exec",
            flow.controller,
            "/opt/venv/bin/python",
            "-c",
            "from pathlib import Path; print(Path('/var/log/shimpz-local/audit.jsonl').read_text())",
        ).stdout
        self.assertIn('"operation":"space-reset"', audit)
        self.assertIn('"detail":"assistant-integration-unavailable"', audit)
        self.assertNotIn("Captain", audit)
        self.assertNotIn(flow.token, audit)

        token_mode, runtime_token_mode, account_egress_capability_mode = runtime_secret_metadata(self._run, flow)
        self.assertEqual(token_mode, "0o440 10001 10010 1 64")
        self.assertEqual(runtime_token_mode, "0o440 10001 10016 1 64")
        self.assertEqual(account_egress_capability_mode, "0o440 0 10022 1 64")

        # Leave one exact-owned pair for the outer finally. This proves cleanup does not depend
        # on reaching the controller reset route and therefore also runs after an earlier failure.
        self._api(
            flow.port,
            flow.token,
            "POST",
            "/v1/teams/cleanup_team/create",
            {"team_name": "Cleanup Team"},
        )
        self._api(
            flow.port,
            flow.token,
            "POST",
            "/v1/teams/cleanup_team/assistants",
            {
                "assistant_id": "shimpz-cloudflare",
                "source_digest": flow.source_digest,
            },
        )
        self.assertEqual(len(self._owned_ids("container", flow.space_id, "assistant")), 1)
        self.assertEqual(len(self._owned_ids("network", flow.space_id, "team")), 1)

    def _cleanup(self, flow: DockerFlow) -> None:
        flow.brain_server.shutdown()
        flow.brain_server.server_close()
        flow.brain_thread.join(timeout=2)
        # Cleanup remains strictly scoped to this test's unique names/labels.
        self._remove("rm", "--force", flow.egress_proxy)
        self._cleanup_owned_space(flow.space_id)
        owned_containers = self._owned_ids("container", flow.space_id, "assistant")
        owned_networks = self._owned_ids("network", flow.space_id, "team")
        self._remove("rm", "--force", flow.controller)
        self._remove("rm", "--force", "--volumes", flow.registry)
        self._remove("network", "rm", flow.foreign_network)
        self._remove("network", "rm", flow.outbound_network)
        self._remove(
            "volume",
            "rm",
            "--force",
            flow.token_volume,
            flow.runtime_token_volume,
            flow.audit_volume,
            flow.storage_volume,
            flow.inference_volume,
            flow.action_journal_volume,
            flow.publication_volume,
            flow.continuation_state_volume,
            flow.continuation_key_volume,
            flow.supervisor_key_volume,
            flow.account_egress_capability_volume,
            flow.egress_policy_volume,
            flow.egress_audit_volume,
        )
        if flow.trusted_ref:
            self._remove("image", "rm", "--force", flow.trusted_ref)
        self._remove("image", "rm", "--force", flow.fixture_tag, flow.controller_tag, flow.egress_proxy_tag)
        self._remove("buildx", "rm", "--force", flow.builder)
        self.assertEqual(owned_containers, [])
        self.assertEqual(owned_networks, [])

    @unittest.skipUnless(os.environ.get("SHIMPZ_RUN_DOCKER_TESTS") == "1", "real Docker test is opt-in")
    def test_local_uninstall_preserves_exact_staged_image_in_daemon(self) -> None:
        client = docker.from_env()
        image_id = ""
        try:
            with (
                tempfile.TemporaryDirectory() as build_root,
                tempfile.TemporaryDirectory() as residue_root,
            ):
                Path(build_root, "Dockerfile").write_text(
                    f"FROM scratch\nLABEL {snapshots.LOCAL_STAGE_LABEL}={snapshots.LOCAL_STAGE_VALUE}\n",
                    encoding="utf-8",
                )
                image, _logs = client.images.build(path=build_root, rm=True, forcerm=True)
                image_id = image.id
                assistant_id = "local-retirement-proof"
                spec = SimpleNamespace(assistant_id=assistant_id, allowed_hosts=())
                binding = SimpleNamespace(
                    assistant_id=assistant_id,
                    provenance="local",
                    local_record={"image_id": image_id},
                )
                state = {"binding": binding}

                def delete(_team_id: str, _assistant_id: str) -> bool:
                    existed = state["binding"] is not None
                    state["binding"] = None
                    return existed

                registry = SimpleNamespace(
                    get=lambda _team_id, _assistant_id: spec if state["binding"] is not None else None,
                    binding=lambda _team_id, _assistant_id: state["binding"],
                    bindings=lambda: () if state["binding"] is None else (state["binding"],),
                    all=lambda: () if state["binding"] is None else (spec,),
                    delete=delete,
                )
                lifecycle = object.__new__(local_app.AssistantLifecycle)
                lifecycle.client = client
                lifecycle.registry = registry
                lifecycle.residues = assistant_update.AssistantResidueStore(Path(residue_root))
                lifecycle.icons = SimpleNamespace(discard_binding=lambda *_args: None)
                lifecycle._lock = lambda _team_id: nullcontext()
                lifecycle._network = lambda _team_id: SimpleNamespace(name="unused")
                lifecycle._assistant_container = lambda *_args, **_kwargs: None
                lifecycle._egress_token = lambda *_args, **_kwargs: None
                lifecycle.chat_turn_service = SimpleNamespace(
                    _chat_lock=lambda _team_id: threading.Lock(),
                    _delete_chat_continuation=lambda _team_id: None,
                    _delete_assistant_integration_state=lambda _team_id, _assistant_id: None,
                    _delete_assistant_stored_input_state=lambda _team_id, _assistant_id: None,
                )

                result = lifecycle.uninstall_assistant("team_1", assistant_id)

                self.assertEqual(result, {"assistant": assistant_id, "uninstalled": False})
                self.assertIsNone(state["binding"])
                self.assertEqual(lifecycle.residues.list(), ())
                self.assertEqual(client.images.get(image_id).id, image_id)

                lifecycle._queue_residue(image_id)
                lifecycle.sweep_residues()

                self.assertEqual(lifecycle.residues.list(), ())
                self.assertEqual(client.images.get(image_id).id, image_id)
        finally:
            if image_id:
                with suppress(ImageNotFound):
                    client.images.remove(image=image_id, force=True, noprune=False)
            client.close()

    @unittest.skipUnless(os.environ.get("SHIMPZ_RUN_DOCKER_TESTS") == "1", "real Docker test is opt-in")
    def test_real_pull_isolation_lifecycle_and_space_reset(self) -> None:
        flow = self._new_flow()
        try:
            self._prepare_images(flow)
            self._start_controller(flow)
            self._exercise_team_storage(flow)
            self._exercise_assistant(flow)
            self._exercise_assistant_recovery(flow)
            self._exercise_egress_attachment_recovery(flow)
            self._exercise_teardown(flow)
            self._exercise_reset(flow)
        finally:
            self._cleanup(flow)


if __name__ == "__main__":
    unittest.main()
