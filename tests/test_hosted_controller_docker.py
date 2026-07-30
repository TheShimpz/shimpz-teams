"""Live cross-tenant contract for the hosted Controller and real Docker inventory."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

TEAM = Path(__file__).resolve().parents[1]
ACCOUNT_A = "a" * 32
ACCOUNT_B = "b" * 32

from docker_harness import DockerHarnessMixin


class _AccountHandler(BaseHTTPRequestHandler):
    sessions: ClassVar[dict[str, str]] = {"session-a": ACCOUNT_A, "session-b": ACCOUNT_B}
    capability: ClassVar[str] = ""

    def log_message(self, *_args) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            request = json.loads(self.rfile.read(length))
            session_token = request["session_token"]
            binding = request["binding"]
            account_id = self.sessions[session_token]
            if (
                self.path != "/v1/internal/authority/evaluate"
                or self.headers.get("Authorization") != f"Bearer {self.capability}"
                or request["version"] != 1
                or not isinstance(binding, dict)
            ):
                raise ValueError
            encoded_binding = json.dumps(
                binding,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        except KeyError, TypeError, ValueError, json.JSONDecodeError:
            status = HTTPStatus.FORBIDDEN
            payload = {"error": "invalid session"}
        else:
            status = HTTPStatus.OK
            payload = {
                "version": 1,
                "account_id": account_id,
                "supervisor": False,
                "binding_digest": hashlib.sha256(encoded_binding).hexdigest(),
            }
            if binding["operation"] == "team-create":
                payload["owner_account_id"] = binding.get("owner_account_id", account_id)
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _developers_secrets(directory: Path) -> None:
    directory.chmod(0o755)
    values = {
        "developers-to-controller-token": uuid.uuid4().hex,
        "controller-to-developers-token": uuid.uuid4().hex,
        "assistant-registry-username": "registry-reader",
        "assistant-registry-token": uuid.uuid4().hex,
    }
    for name, value in values.items():
        path = directory / name
        path.write_text(value, encoding="ascii")
        path.chmod(0o444)
    public_key = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            Encoding.PEM,
            PublicFormat.SubjectPublicKeyInfo,
        )
    )
    key_path = directory / "delegation-public.pem"
    key_path.write_bytes(public_key)
    key_path.chmod(0o444)


def _cross_tenant_request(
    base: str,
    method: str,
    path: str,
) -> tuple[dict[str, object] | bytes | None, dict[str, str] | None]:
    bodyless = path in {
        f"{base}/stop",
        f"{base}/start",
        f"{base}/restart",
        f"{base}/chat/stop",
    }
    body: dict[str, object] | bytes | None = {} if method in {"POST", "PUT"} and not bodyless else None
    if method == "POST" and path == f"{base}/files":
        return b"x", {
            "Content-Type": "text/plain",
            "X-Shimpz-Filename": "private.txt",
        }
    return body, None


class HostedControllerDockerTests(DockerHarnessMixin, unittest.TestCase):
    maxDiff = None
    docker_cwd = TEAM
    credential_header = "X-Shimpz-Account"
    credential_prefix = ""
    api_timeout = 15
    api_read_limit = 64 * 1024 + 1
    controller_kind = "hosted Controller"

    def _wait_hosted_controller(self, container: str) -> int:
        mapping = self._run("port", container, "7077/tcp").stdout.strip()
        port = int(mapping.rsplit(":", 1)[1])

        def probe() -> int | None:
            try:
                status, _ = self._api(port, "session-b", "GET", "/v1/teams")
            except OSError, urllib.error.URLError:
                return None
            return port if status == HTTPStatus.OK else None

        return self._wait_controller(container, probe)

    @unittest.skipUnless(os.environ.get("SHIMPZ_RUN_DOCKER_TESTS") == "1", "real Docker test is opt-in")
    def test_account_b_cannot_reach_any_account_a_team_route(self) -> None:
        unique = uuid.uuid4().hex[:12]
        image = f"shimpz-team-hosted-test:{unique}"
        controller = f"shimpz-hosted-controller-{unique}"
        team_id = f"live_{unique}"
        anchor = f"team_{team_id}"
        socket_gid = str(Path("/var/run/docker.sock").stat().st_gid)
        bridge_gateway = self._run(
            "network",
            "inspect",
            "bridge",
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
        ).stdout.strip()
        account = ThreadingHTTPServer((bridge_gateway, 0), _AccountHandler)
        account_thread = threading.Thread(
            target=account.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        account_thread.start()
        developers_secrets = tempfile.TemporaryDirectory()
        developers_secrets_path = Path(developers_secrets.name)
        _developers_secrets(developers_secrets_path)
        authority_secrets = tempfile.TemporaryDirectory()
        authority_secrets_path = Path(authority_secrets.name)
        authority_secrets_path.chmod(0o750)
        authority_token = uuid.uuid4().hex + uuid.uuid4().hex
        authority_token_path = authority_secrets_path / "token"
        authority_token_path.write_text(authority_token, encoding="ascii")
        authority_token_path.chmod(0o440)
        _AccountHandler.capability = authority_token

        try:
            self._run(
                "build",
                "--file",
                "hosted/Dockerfile",
                "--build-arg",
                f"DOCKER_GID={socket_gid}",
                "--tag",
                image,
                ".",
            )
            self._run(
                "run",
                "--detach",
                "--name",
                anchor,
                "--cpus",
                "0.25",
                "--memory",
                "64m",
                "--memory-swap",
                "64m",
                "--pids-limit",
                "32",
                "--label",
                "team.runtime=1",
                "--label",
                f"team.id={team_id}",
                "--label",
                "team.name=Account A Team",
                "--label",
                f"team.owner={ACCOUNT_A}",
                "--label",
                "team.brain=runtime",
                "--label",
                "team.model=gpt-5-nano",
                "--entrypoint",
                "/bin/sh",
                image,
                "-c",
                "sleep 600",
            )
            self._run(
                "run",
                "--detach",
                "--name",
                controller,
                "--cpus",
                "1",
                "--memory",
                "512m",
                "--memory-swap",
                "512m",
                "--pids-limit",
                "128",
                "--group-add",
                socket_gid,
                "--group-add",
                str(authority_token_path.stat().st_gid),
                "--volume",
                "/var/run/docker.sock:/var/run/docker.sock",
                "--volume",
                f"{developers_secrets_path}:/run/shimpz-developers-controller:ro",
                "--volume",
                f"{authority_secrets_path}:/run/shimpz-account-team-authority:ro",
                "--env",
                f"SHIMPZ_ACCOUNT_URL=http://{bridge_gateway}:{account.server_port}",
                "--publish",
                "127.0.0.1::7077",
                image,
            )
            port = self._wait_hosted_controller(controller)

            owner_status, owner_team = self._api(port, "session-a", "GET", f"/v1/teams/{team_id}/status")
            self.assertEqual(owner_status, HTTPStatus.OK, owner_team)
            self.assertEqual(owner_team["owner"], ACCOUNT_A)

            other_status, other_teams = self._api(port, "session-b", "GET", "/v1/teams")
            self.assertEqual(other_status, HTTPStatus.OK)
            self.assertEqual(other_teams, {"teams": []})

            base = f"/v1/teams/{team_id}"
            routes = (
                ("DELETE", base),
                ("POST", f"{base}/create"),
                ("GET", f"{base}/status"),
                ("GET", f"{base}/logs?lines=1"),
                ("POST", f"{base}/stop"),
                ("POST", f"{base}/start"),
                ("POST", f"{base}/restart"),
                ("GET", f"{base}/assistant-integrations"),
                ("GET", f"{base}/assistants"),
                ("POST", f"{base}/assistants"),
                ("DELETE", f"{base}/assistants/shimpz-cloudflare"),
                ("POST", f"{base}/assistant-integrations/challenges/{'a' * 32}/authorize"),
                ("DELETE", f"{base}/assistant-integrations/shimpz-cloudflare/cloudflare"),
                ("GET", f"{base}/inference"),
                ("PUT", f"{base}/inference"),
                ("POST", f"{base}/chat"),
                ("POST", f"{base}/chat/stream"),
                ("POST", f"{base}/chat/stop"),
                ("GET", f"{base}/chat/integrations"),
                ("POST", f"{base}/chat/integrations"),
                ("GET", f"{base}/files"),
                ("POST", f"{base}/files"),
                ("DELETE", f"{base}/files/{'b' * 32}"),
            )
            expected = {"error": f"team {team_id!r} not found"}
            for method, path in routes:
                body, extra_headers = _cross_tenant_request(base, method, path)
                with self.subTest(method=method, path=path):
                    status, payload = self._api(
                        port,
                        "session-b",
                        method,
                        path,
                        body,
                        extra_headers=extra_headers,
                    )
                    self.assertEqual(status, HTTPStatus.NOT_FOUND)
                    self.assertEqual(payload, expected)
        finally:
            account.shutdown()
            account.server_close()
            account_thread.join(timeout=2)
            self._remove("rm", "--force", controller, anchor)
            self._remove("image", "rm", "--force", image)
            developers_secrets.cleanup()
            authority_secrets.cleanup()


if __name__ == "__main__":
    unittest.main()
