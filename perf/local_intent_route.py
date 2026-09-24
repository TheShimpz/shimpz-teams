"""Measure authenticated Local Team intent routing with a deterministic Brain peer.

Run from the Teams checkout with ``python -m perf.local_intent_route``. The
fixture builds current Team and egress images in a disposable Docker graph.
Only timing and resource counts are printed; no request body or key is logged.
"""

from __future__ import annotations

import json
import math
import os
import queue
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import override
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
# The Docker fixture is shared with the live Team suite; keep its graph and cleanup.
sys.path.insert(0, str(TEAM / "tests"))

import local_controller_docker_fixture as flow_fixture
from test_local_controller_docker import BUILDKIT_IMAGE, DockerFlowTests

SAMPLES = 12
PEER_DELAYS_MS = (0, 250)
TEAM_MEMORY_MIB = 256
TEAM_CPUS = 1
OBJECTIVE = "Hello"
RESPONSE = {"intent": "ordinary-task", "query": "", "assistant_ids": [], "reply": ""}


class MeasurementError(RuntimeError):
    """The disposable graph or one timing sample failed its contract."""


class BrainPeer(flow_fixture.BrainLifecycleHandler):
    """Return only one closed intent decision and record the peer's own span."""

    delay_ms = 0
    dummy_key = secrets.token_urlsafe(24)
    spans: queue.Queue[float] = queue.Queue()

    @override
    def do_POST(self) -> None:
        if self.path != "/v1/intent-route":
            super().do_POST()
            return
        started = time.perf_counter_ns()
        length = self.headers.get("Content-Length", "")
        if not length.isdecimal() or int(length) > 16_384:
            self._reply(400, {"error": "invalid request"})
            return
        try:
            body = json.loads(self.rfile.read(int(length)))
        except UnicodeError, json.JSONDecodeError:
            self._reply(400, {"error": "invalid request"})
            return
        if not self._valid(body):
            self._reply(400, {"error": "invalid request"})
            return
        time.sleep(self.delay_ms / 1_000)
        self._reply(200, RESPONSE)
        self.spans.put((time.perf_counter_ns() - started) / 1_000_000)

    def _valid(self, body: object) -> bool:
        if not isinstance(body, dict) or not isinstance(body.get("provider"), dict):
            return False
        provider = body["provider"]
        return (
            set(body)
            == {
                "provider",
                "objective",
                "expected_intent",
                "candidates",
                "lifecycle_reference",
                "conversation",
                "language_exemplar",
            }
            and provider == {"provider": "openai", "model": "gpt-5.6-terra", "api_key": self.dummy_key}
            and body["objective"] == OBJECTIVE
            and body["expected_intent"] is None
            and body["candidates"] == []
            and body["lifecycle_reference"] is None
            and body["conversation"] == []
            and body["language_exemplar"] is None
            and self.headers.get("Authorization", "").startswith("Bearer ")
        )

    def _reply(self, status: int, body: dict[str, object]) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)


def _volumes(flow: flow_fixture.DockerFlow) -> tuple[str, ...]:
    return (
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


def _residue(runner: DockerFlowTests, flow: flow_fixture.DockerFlow) -> list[str]:
    runner._run("info", "--format", "{{.ServerVersion}}")
    resources = (
        *(("container", name) for name in (flow.controller, flow.egress_proxy, flow.registry)),
        *(("network", name) for name in (flow.outbound_network, flow.foreign_network)),
        *(("volume", name) for name in _volumes(flow)),
        *(("image", name) for name in (flow.controller_tag, flow.egress_proxy_tag, flow.fixture_tag, flow.trusted_ref)),
    )
    present: list[str] = []
    for kind, name in resources:
        inspection = runner._run(kind, "inspect", name, check=False)
        if inspection.returncode == 0:
            present.append(f"{kind}:{name}")
        elif not any(marker in inspection.stderr.lower() for marker in ("no such", "not found")):
            raise MeasurementError(f"Docker resource inspection failed for {kind}:{name}")
    builder = runner._run("buildx", "inspect", flow.builder, check=False)
    if builder.returncode == 0:
        present.append(f"builder:{flow.builder}")
    elif not any(marker in builder.stderr.lower() for marker in ("no builder", "not found")):
        raise MeasurementError(f"Docker builder inspection failed for {flow.builder}")
    return present


def _build(runner: DockerFlowTests, flow: flow_fixture.DockerFlow) -> None:
    runner._run(
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
    runner._run(
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


def _bounded_controller_run(runner: DockerFlowTests, flow: flow_fixture.DockerFlow):
    original = runner._run
    changed = [False]

    def run(*arguments: str, **kwargs):
        if arguments[:4] != ("run", "--detach", "--name", flow.controller):
            return original(*arguments, **kwargs)
        options = list(arguments)
        for flag, old, new in (
            ("--cpus", "2", str(TEAM_CPUS)),
            ("--memory", "512m", f"{TEAM_MEMORY_MIB}m"),
            ("--memory-swap", "512m", f"{TEAM_MEMORY_MIB}m"),
        ):
            position = options.index(flag) + 1
            if options[position] != old:
                raise MeasurementError("Team fixture resource limit changed")
            options[position] = new
        changed[0] = True
        return original(*options, **kwargs)

    return run, changed


def _start(runner: DockerFlowTests, flow: flow_fixture.DockerFlow) -> None:
    replacement, changed = _bounded_controller_run(runner, flow)
    with mock.patch.object(runner, "_run", side_effect=replacement):
        runner._start_controller(flow)
    if not changed[0]:
        raise MeasurementError("Team fixture controller was not started")
    metadata = json.loads(runner._run("inspect", flow.controller).stdout)[0]["HostConfig"]
    if (
        metadata["NanoCpus"] != TEAM_CPUS * 1_000_000_000
        or metadata["Memory"] != TEAM_MEMORY_MIB * 1_048_576
        or metadata["MemorySwap"] != TEAM_MEMORY_MIB * 1_048_576
    ):
        raise MeasurementError("Team container limits do not match the workload")
    status, _ = runner._api(
        flow.port,
        flow.token,
        "POST",
        "/v1/teams/demo_team/create",
        {"team_name": "Demo Team"},
    )
    if status != 200:
        raise MeasurementError("disposable Team creation failed")
    status, _ = runner._api(
        flow.port,
        flow.token,
        "PUT",
        "/v1/teams/demo_team/inference",
        {"provider": "openai", "model": "gpt-5.6-terra"},
    )
    if status != 200:
        raise MeasurementError("disposable Team inference setup failed")


def _percentiles(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50_ms": round(ordered[math.ceil(len(ordered) * 0.50) - 1], 2),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 2),
    }


def _sample(runner: DockerFlowTests, flow: flow_fixture.DockerFlow, delay_ms: int) -> tuple[float, float]:
    BrainPeer.delay_ms = delay_ms
    started = time.perf_counter_ns()
    status, body = runner._api(
        flow.port,
        flow.token,
        "POST",
        "/v1/teams/demo_team/chat/intent-route",
        {
            "objective": OBJECTIVE,
            "expected_intent": None,
            "candidates": [],
            "lifecycle_reference": None,
            "conversation": [],
            "language_exemplar": None,
        },
        extra_headers={
            "X-Shimpz-Model-Provider": "openai",
            "X-Shimpz-Model-Api-Key": BrainPeer.dummy_key,
        },
    )
    total_ms = (time.perf_counter_ns() - started) / 1_000_000
    if status != 200 or {key: body.get(key) for key in ("intent", "query", "assistant_ids", "reply")} != RESPONSE:
        raise MeasurementError("Team intent route returned a noncanonical result")
    peer_ms = BrainPeer.spans.get(timeout=2)
    return total_ms, peer_ms


def _measure(runner: DockerFlowTests, flow: flow_fixture.DockerFlow) -> dict[str, object]:
    for _ in range(2):
        _sample(runner, flow, 0)
    samples: dict[int, dict[str, list[float]]] = {
        delay: {"team_http_ms": [], "peer_ms": [], "residual_ms": []} for delay in PEER_DELAYS_MS
    }
    for index in range(SAMPLES):
        for delay in PEER_DELAYS_MS if index % 2 == 0 else tuple(reversed(PEER_DELAYS_MS)):
            total, peer = _sample(runner, flow, delay)
            values = samples[delay]
            values["team_http_ms"].append(total)
            values["peer_ms"].append(peer)
            values["residual_ms"].append(total - peer)
    return {
        str(delay): {name: _percentiles(values) for name, values in samples[delay].items()} for delay in PEER_DELAYS_MS
    }


def main() -> int:
    runner = DockerFlowTests("test_real_pull_isolation_lifecycle_and_space_reset")
    with mock.patch.object(flow_fixture, "BrainLifecycleHandler", BrainPeer):
        flow = runner._new_flow()
    flow.trusted_ref = f"127.0.0.1:1/shimpz/perf-placeholder@sha256:{secrets.token_hex(32)}"
    result: dict[str, object] = {"status": "error", "run_id": flow.space_id}
    owns_names = False
    try:
        existing = _residue(runner, flow)
        if existing:
            result.update(
                status="preflight-error",
                residue=existing,
                next_action="Inspect the named resources; this run did not clean them because ownership is unknown.",
            )
        else:
            owns_names = True
            _build(runner, flow)
            _start(runner, flow)
            samples = _measure(runner, flow)
            result["samples"] = samples
            result.update(
                status="complete",
                scope="authenticated Local Team HTTP and deterministic Brain peer; excludes Admin, browser, provider",
                outside_peer_definition=(
                    "Team HTTP elapsed minus peer handler elapsed; includes connection and peer header parsing"
                ),
                percentile_method="nearest-rank",
                errors=0,
                team_cpus=TEAM_CPUS,
                team_memory_mib=TEAM_MEMORY_MIB,
                team_cpuset=flow.test_cpuset,
                host_processors=os.cpu_count(),
                docker_logging_driver=runner._run("info", "--format", "{{.LoggingDriver}}").stdout.strip(),
                team_image=runner._run("image", "inspect", "--format", "{{.Id}}", flow.controller_tag).stdout.strip(),
            )
    except (OSError, RuntimeError, AssertionError, ValueError, queue.Empty, subprocess.SubprocessError) as exc:
        result.update(status="error", error_type=type(exc).__name__)
        if isinstance(exc, MeasurementError):
            result["detail"] = str(exc)
    finally:
        if owns_names:
            try:
                runner._cleanup(flow)
            except (OSError, RuntimeError, AssertionError, ValueError, subprocess.SubprocessError) as exc:
                result.update(status="cleanup-error", cleanup_error_type=type(exc).__name__)
            try:
                remaining = _residue(runner, flow)
                if remaining:
                    result.update(status="cleanup-error", residue=remaining)
            except (OSError, RuntimeError, AssertionError, ValueError, subprocess.SubprocessError) as exc:
                result.update(status="cleanup-error", residue_check_error_type=type(exc).__name__)
            if result["status"] == "cleanup-error":
                suffix = flow.space_id.removeprefix("test-space-")
                result["next_action"] = (
                    f"Inspect Docker resources bearing run suffix {suffix}; remove only verified owned residue."
                )
        else:
            flow.brain_server.shutdown()
            flow.brain_server.server_close()
            flow.brain_thread.join(timeout=2)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
