"""Measure authenticated published Assistant installation on a disposable Local graph.

Run from the Teams checkout with ``python -m perf.local_published_install``.
The Developers and Sigstore edges are deterministic fixtures. Controller HTTP,
Docker pull, isolation, start, and uninstall are real. Setup is outside samples.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from local.assistant.isolation import ASSISTANT_MEMORY, ASSISTANT_NANO_CPUS

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM / "tests"))

from local_controller_docker_fixture import DockerFlow
from test_local_controller_docker import DockerFlowTests

from perf.local_snapshot_inventory import _percentiles
from perf.local_team_http import _residue

CONTROLLER_CPUS = 2
CONTROLLER_MEMORY_MIB = 512


class MeasurementError(RuntimeError):
    """A measured installation or Docker inspection violated the expected contract."""


def _installed_container(runner: DockerFlowTests, flow: DockerFlow) -> str:
    ids = runner._owned_ids("container", flow.space_id, "assistant")
    if len(ids) != 1:
        raise MeasurementError("installation did not create exactly one owned Assistant container")
    metadata = json.loads(runner._run("inspect", ids[0]).stdout)[0]
    host = metadata["HostConfig"]
    if metadata["State"]["Status"] != "running" or metadata["Config"]["Image"] != flow.trusted_ref:
        raise MeasurementError("the installed Assistant does not run the expected image")
    if (
        host["NanoCpus"] != ASSISTANT_NANO_CPUS
        or host["Memory"] != ASSISTANT_MEMORY
        or host["MemorySwap"] != ASSISTANT_MEMORY
        or host["CpusetCpus"] != flow.test_cpuset
    ):
        raise MeasurementError("Assistant container limits do not match the workload")
    return ids[0]


def _verify_controller_limits(runner: DockerFlowTests, flow: DockerFlow) -> None:
    metadata = json.loads(runner._run("inspect", flow.controller).stdout)[0]["HostConfig"]
    if (
        metadata["NanoCpus"] != CONTROLLER_CPUS * 1_000_000_000
        or metadata["Memory"] != CONTROLLER_MEMORY_MIB * 1_048_576
        or metadata["MemorySwap"] != CONTROLLER_MEMORY_MIB * 1_048_576
        or metadata["CpusetCpus"] != flow.test_cpuset
    ):
        raise MeasurementError("controller container limits do not match the workload")


def _host_container_count(runner: DockerFlowTests) -> int:
    return len(runner._run("container", "ls", "--all", "--quiet").stdout.splitlines())


def _present(runner: DockerFlowTests, kind: str, identity: str) -> bool:
    inspection = runner._run(kind, "inspect", identity, check=False)
    if inspection.returncode == 0:
        return True
    missing = ("no such image",) if kind == "image" else ("no such object", "no such container")
    if any(marker in inspection.stderr.lower() for marker in missing):
        return False
    raise MeasurementError(f"Docker {kind} inspection failed")


def _sample(runner: DockerFlowTests, flow: DockerFlow, *, cold: bool) -> dict[str, object]:
    if not cold:
        runner._run("pull", flow.trusted_ref)
    image_present = _present(runner, "image", flow.trusted_ref)
    if image_present == cold:
        raise MeasurementError("the image cache does not match the selected sample")
    started_monotonic = time.perf_counter_ns()
    status, body = runner._api(
        flow.port,
        flow.token,
        "POST",
        "/v1/teams/demo_team/assistants",
        {"assistant_id": "shimpz-cloudflare", "source_digest": flow.source_digest},
    )
    elapsed_ms = (time.perf_counter_ns() - started_monotonic) / 1_000_000
    if status != 200 or body.get("installed") is not True:
        raise MeasurementError("the authenticated install did not create the requested Assistant")
    container_id = _installed_container(runner, flow)
    if not _present(runner, "image", flow.trusted_ref):
        raise MeasurementError("the exact published digest was not pulled")
    image = json.loads(runner._run("image", "inspect", flow.trusted_ref).stdout)[0]
    if (
        flow.trusted_ref not in image.get("RepoDigests", [])
        or not isinstance(image.get("Id"), str)
        or not isinstance(image.get("Size"), int)
        or image["Size"] <= 0
    ):
        raise MeasurementError("the pulled image identity or size is unavailable")
    uninstall_started = time.perf_counter_ns()
    removed_status, removed = runner._api(
        flow.port,
        flow.token,
        "DELETE",
        "/v1/teams/demo_team/assistants/shimpz-cloudflare",
    )
    uninstall_ms = (time.perf_counter_ns() - uninstall_started) / 1_000_000
    if removed_status != 200 or removed.get("uninstalled") is not True:
        raise MeasurementError("the published Assistant was not uninstalled")
    if _present(runner, "image", flow.trusted_ref):
        raise MeasurementError("published uninstall left the exact digest reference present")
    if _present(runner, "container", container_id):
        raise MeasurementError("published uninstall left the owned Assistant container present")
    return {
        "cache": "absent" if cold else "prepulled",
        "install_http_ms": round(elapsed_ms, 2),
        "uninstall_http_ms": round(uninstall_ms, 2),
        "image_id": image["Id"],
        "image_size_bytes": image["Size"],
    }


def _measure(runner: DockerFlowTests, flow: DockerFlow, samples: int) -> dict[str, object]:
    # Match the host inventory that each uninstall sweeps after removing its Assistant.
    host_containers = _host_container_count(runner)
    status, body = runner._api(flow.port, flow.token, "POST", "/v1/teams/demo_team/create", {"team_name": "Demo Team"})
    if status != 200 or body.get("created") is not True:
        raise MeasurementError("the fixture Team was not created")
    observations = []
    for index in range(samples):
        order = (True, False) if index % 2 == 0 else (False, True)
        observations.extend(_sample(runner, flow, cold=cold) for cold in order)
    remaining_containers = _host_container_count(runner)
    if remaining_containers != host_containers:
        raise MeasurementError(
            f"host container count changed from {host_containers} to {remaining_containers}; "
            "rerun after daemon activity settles"
        )
    # Keep the invariant artifact identity once, separate from per-sample timings.
    images = {(item.pop("image_id"), item.pop("image_size_bytes")) for item in observations}
    if len(images) != 1:
        raise MeasurementError("the pulled image changed during the benchmark")
    image_id, image_size_bytes = images.pop()
    summary = {}
    for cache in ("absent", "prepulled"):
        arm = [item for item in observations if item["cache"] == cache]
        summary[cache] = {
            key: _percentiles([float(item[key]) for item in arm]) for key in ("install_http_ms", "uninstall_http_ms")
        }
    return {
        "artifact": {"image_id": image_id, "size_bytes": image_size_bytes, "source_digest": flow.source_digest},
        "host_containers": host_containers,
        "observations": observations,
        "summary": summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=12, help="samples per cache arm")
    args = parser.parse_args()
    if args.samples < 1 or args.samples > 24:
        parser.error("--samples must be between 1 and 24")
    runner = DockerFlowTests("test_real_pull_isolation_lifecycle_and_space_reset")
    flow = runner._new_flow()
    # The real digest is assigned by _prepare_images after this unique-name preflight.
    flow.trusted_ref = f"127.0.0.1:1/shimpz/perf-preflight@sha256:{secrets.token_hex(32)}"
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
            runner._prepare_images(flow)
            runner._start_controller(flow)
            _verify_controller_limits(runner, flow)
            result.update(_measure(runner, flow, args.samples))
            result.update(
                status="complete",
                scope=(
                    "Local published Assistant; fixture Developers and Sigstore; "
                    "real HTTP, registry, Docker and isolation"
                ),
                limits={
                    "controller_cpus": CONTROLLER_CPUS,
                    "controller_memory_mib": CONTROLLER_MEMORY_MIB,
                    "assistant_cpus": ASSISTANT_NANO_CPUS / 1_000_000_000,
                    "assistant_memory_mib": ASSISTANT_MEMORY // 1_048_576,
                    "cpuset": flow.test_cpuset,
                },
                team_image=runner._run("image", "inspect", "--format", "{{.Id}}", flow.controller_tag).stdout.strip(),
                host_processors=os.cpu_count(),
                docker_logging_driver=runner._run("info", "--format", "{{.LoggingDriver}}").stdout.strip(),
                percentile_method="nearest-rank",
                errors=0,
                phase_limits=(
                    "No internal phase attribution; the fixture substitutes publication resolution "
                    "and Sigstore verification, and the registry is loopback"
                ),
            )
    except (OSError, RuntimeError, AssertionError, ValueError, subprocess.SubprocessError) as exc:
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
                result["next_action"] = "Inspect resources bearing this run_id and remove only verified owned residue."
        else:
            flow.brain_server.shutdown()
            flow.brain_server.server_close()
            flow.brain_thread.join(timeout=2)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
