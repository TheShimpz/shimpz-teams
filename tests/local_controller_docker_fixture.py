"""Deterministic identities and publication edges for the Local Docker flow."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from subprocess import CompletedProcess

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from local.app import half_cpu_set
from protocol.http.v1 import supervisor as supervisor_contract

TEAM = Path(__file__).resolve().parents[1]
FIXTURE = TEAM / "tests" / "fixtures" / "reference-assistant"


def fixture_source_digest() -> str:
    manifest = (FIXTURE / "shimpz.toml").read_bytes()
    machine_contract = (FIXTURE / "shimpz.contract.json").read_bytes()
    return f"sha256:{hashlib.sha256(manifest + machine_contract).hexdigest()}"


class BrainLifecycleHandler(BaseHTTPRequestHandler):
    """Minimal real HTTP peer for the controller's closed thread-deletion contract."""

    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            document = json.loads(body)
        except UnicodeError, json.JSONDecodeError:
            document = None
        valid = (
            self.path == "/v1/threads/delete"
            and isinstance(document, dict)
            and set(document) == {"thread_id"}
            and isinstance(document["thread_id"], str)
        )
        response = json.dumps({"status": "deleted"} if valid else {"error": "invalid request"}).encode()
        self.send_response(200 if valid else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(response)


@dataclass(slots=True)
class DockerFlow:
    builder: str
    registry: str
    controller: str
    egress_proxy: str
    fixture_tag: str
    controller_tag: str
    egress_proxy_tag: str
    token_volume: str
    runtime_token_volume: str
    audit_volume: str
    storage_volume: str
    inference_volume: str
    power_journal_volume: str
    publication_volume: str
    continuation_state_volume: str
    continuation_key_volume: str
    supervisor_key_volume: str
    account_egress_capability_volume: str
    egress_policy_volume: str
    egress_audit_volume: str
    space_id: str
    foreign_network: str
    outbound_network: str
    test_cpuset: str
    bridge_gateway: ipaddress.IPv4Address
    brain_server: ThreadingHTTPServer
    brain_thread: threading.Thread
    supervisor_private_key: Ed25519PrivateKey
    supervisor_id: str
    supervisor_session: str
    trusted_ref: str = ""
    port: int = 0
    token: str = ""
    file_id: str = ""
    network_name: str = ""
    assistant_name: str = ""
    original_assistant_id: str = ""
    source_digest: str = ""


def new_flow(run: Callable[..., CompletedProcess[str]]) -> DockerFlow:
    unique = uuid.uuid4().hex[:12]
    daemon_processors = int(run("info", "--format", "{{.NCPU}}").stdout.strip())
    test_cpuset = half_cpu_set(daemon_processors)
    bridge_gateway = ipaddress.IPv4Address(
        run(
            "network",
            "inspect",
            "bridge",
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
        ).stdout.strip()
    )
    brain_server = ThreadingHTTPServer((str(bridge_gateway), 0), BrainLifecycleHandler)
    brain_thread = threading.Thread(
        target=brain_server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    brain_thread.start()
    return DockerFlow(
        builder=f"shimpz-local-test-{unique}",
        registry=f"shimpz-registry-{unique}",
        controller=f"shimpz-controller-{unique}",
        egress_proxy=f"shimpz-egress-proxy-{unique}",
        fixture_tag=f"shimpz-cloudflare-test:{unique}",
        controller_tag=f"shimpz-team-local-test:{unique}",
        egress_proxy_tag=f"shimpz-assistant-egress-test:{unique}",
        token_volume=f"shimpz-local-token-{unique}",
        runtime_token_volume=f"shimpz-local-runtime-token-{unique}",
        audit_volume=f"shimpz-local-audit-{unique}",
        storage_volume=f"shimpz-local-storage-{unique}",
        inference_volume=f"shimpz-local-inference-{unique}",
        power_journal_volume=f"shimpz-local-power-journal-{unique}",
        publication_volume=f"shimpz-local-publication-{unique}",
        continuation_state_volume=f"shimpz-local-continuation-state-{unique}",
        continuation_key_volume=f"shimpz-local-continuation-key-{unique}",
        supervisor_key_volume=f"shimpz-local-supervisor-key-{unique}",
        account_egress_capability_volume=f"shimpz-account-egress-capability-{unique}",
        egress_policy_volume=f"shimpz-local-egress-policy-{unique}",
        egress_audit_volume=f"shimpz-local-egress-audit-{unique}",
        space_id=f"test-space-{unique}",
        foreign_network=f"shimpz-foreign-{unique}",
        outbound_network=f"shimpz-egress-outbound-{unique}",
        test_cpuset=test_cpuset,
        bridge_gateway=bridge_gateway,
        brain_server=brain_server,
        brain_thread=brain_thread,
        supervisor_private_key=Ed25519PrivateKey.generate(),
        supervisor_id=secrets.token_hex(16),
        supervisor_session=secrets.token_hex(32),
        source_digest=fixture_source_digest(),
    )


def prepare_account_egress_capability(
    run: Callable[..., CompletedProcess[str]],
    flow: DockerFlow,
) -> None:
    run(
        "run",
        "--rm",
        "--user",
        "0:0",
        "--network",
        "none",
        "--volume",
        f"{flow.account_egress_capability_volume}:/run/shimpz-account-egress",
        "--entrypoint",
        "/opt/venv/bin/python",
        flow.controller_tag,
        "-c",
        "import os; from pathlib import Path; "
        "p=Path('/run/shimpz-account-egress/token'); p.write_text('a'*64,encoding='ascii'); "
        "os.chown(p,0,10022); p.chmod(0o440)",
    )


def runtime_secret_metadata(
    run: Callable[..., CompletedProcess[str]],
    flow: DockerFlow,
) -> tuple[str, str, str]:
    results = tuple(
        run(
            "exec",
            flow.controller,
            "/opt/venv/bin/python",
            "-c",
            "import os,stat,sys; s=os.stat(sys.argv[1]); "
            "print(oct(stat.S_IMODE(s.st_mode)),s.st_uid,s.st_gid,s.st_nlink,s.st_size)",
            path,
        ).stdout.strip()
        for path in (
            "/run/shimpz-local/token",
            "/run/shimpz-brain-runtime/token",
            "/run/shimpz-account-egress/token",
        )
    )
    return (
        results[0],
        results[1],
        results[2],
    )


def _segment(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _request_body(
    encoded: bytes | None,
    headers: dict[str, str],
) -> dict[str, object]:
    if encoded is None:
        return {
            "kind": "none",
            "length": 0,
            "sha256": supervisor_contract.EMPTY_SHA256,
        }
    filename = headers.get("X-Shimpz-Filename")
    if filename is not None:
        return {
            "kind": "file",
            "length": len(encoded),
            "filename": filename,
            "media_type": headers["Content-Type"],
        }
    return {
        "kind": "json",
        "length": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _request_model(headers: dict[str, str]) -> dict[str, str] | None:
    provider = headers.get("X-Shimpz-Model-Provider")
    api_key = headers.get("X-Shimpz-Model-Api-Key")
    if provider is None or api_key is None:
        return None
    return {
        "provider": provider,
        "key_sha256": hashlib.sha256(api_key.encode("ascii")).hexdigest(),
    }


def supervisor_header(
    flow: DockerFlow,
    method: str,
    path: str,
    body: dict[str, object] | bytes | None,
    headers: dict[str, str],
) -> str:
    encoded = (
        body if isinstance(body, bytes) else None if body is None else json.dumps(body, separators=(",", ":")).encode()
    )
    issued_at = int(time.time())
    claims: dict[str, object] = {
        "v": 1,
        "aud": supervisor_contract.ASSERTION_AUDIENCE,
        "sub": flow.supervisor_id,
        "session_sha256": hashlib.sha256(flow.supervisor_session.encode("ascii")).hexdigest(),
        "jti": secrets.token_hex(16),
        "iat": issued_at,
        "exp": issued_at + supervisor_contract.ASSERTION_MAX_TTL_SECONDS,
        "method": method,
        "path": path,
        "body": _request_body(encoded, headers),
    }
    model = _request_model(headers)
    if model is not None:
        claims["model"] = model
    header = _segment(supervisor_contract.canonical_json(supervisor_contract.JWT_HEADER))
    payload = _segment(supervisor_contract.claims_json(claims))
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = _segment(flow.supervisor_private_key.sign(signing_input))
    return f"Bearer {header}.{payload}.{signature}"


def fixture_resolution(flow: DockerFlow) -> dict[str, object]:
    resolution = json.loads((TEAM / "protocol" / "install" / "v1" / "vectors.json").read_bytes())["fixtures"][
        "resolve_response"
    ]["value"]
    manifest = (FIXTURE / "shimpz.toml").read_bytes()
    machine_contract = (FIXTURE / "shimpz.contract.json").read_bytes()
    flow.source_digest = fixture_source_digest()
    resolution.update(
        {
            "assistant_id": "shimpz-cloudflare",
            "name": "Shimpz Cloudflare",
            "assistant_version": "0.1.0",
            "creators": ["@roxygens"],
            "source_digest": flow.source_digest,
            "image_reference": flow.trusted_ref,
            "oci_digest": flow.trusted_ref.rsplit("@", 1)[1],
            "manifest_digest": f"sha256:{hashlib.sha256(manifest).hexdigest()}",
            "machine_contract_digest": f"sha256:{hashlib.sha256(machine_contract).hexdigest()}",
            "machine_contract": json.loads(machine_contract),
            "allowed_hosts": ["api.cloudflare.com"],
            "integrations": [
                {
                    "id": "cloudflare",
                    "provider": "cloudflare",
                    "scopes": ["dns.read", "offline_access", "zone.read"],
                }
            ],
        }
    )
    return resolution
