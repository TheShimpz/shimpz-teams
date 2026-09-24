"""Measure authenticated published Assistant installation on a disposable Local graph.

Run from the Teams checkout with ``python -m perf.local_published_install``.
The Developers and Sigstore edges are deterministic fixtures. Controller HTTP,
Docker pull, isolation, start, and uninstall are real. Setup is outside samples.
"""

from __future__ import annotations

import argparse
import json
import math
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
SPAN_PREFIX = "SHIMPZ-PERF-UNINSTALL "
SPAN_NAMES = frozenset(
    {
        "_resolve",
        "_network",
        "_assistant_container",
        "_validate_container_profile",
        "_team_has_egress_assistant",
        "_release_assistant_egress",
        "_queue_residue",
        "sweep_residues",
        "_binding_uses_image",
        "_remove_retired_image",
        "_delete_retired_image",
        "_disconnect_egress_proxy",
        "_delete_chat_continuation",
        "_delete_assistant_integration_state",
        "_delete_assistant_stored_input_state",
        "Container.remove",
        "ImageCollection.remove",
        "Network.disconnect",
    }
)


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


def _validated_spans(record: dict[str, object], observation: dict[str, object], sequence: int) -> dict[str, object]:
    spans = record.get("spans")
    total = record.get("total_ms")
    http_ms = observation["uninstall_http_ms"]
    if (
        type(record.get("seq")) is not int
        or record["seq"] != sequence
        or isinstance(total, bool)
        or not isinstance(total, (int, float))
        or not math.isfinite(total)
        or total <= 0
        or total > http_ms + 0.05
        or not isinstance(spans, list)
    ):
        raise MeasurementError("fixture uninstall span window is incomplete")
    exclusive: dict[str, float] = {}
    calls: dict[str, int] = {}
    root_ms = 0.0
    for span in spans:
        if not isinstance(span, dict):
            raise MeasurementError("fixture uninstall span is malformed")
        name, parent, elapsed = span.get("name"), span.get("parent"), span.get("ms")
        if (
            not isinstance(name, str)
            or name not in SPAN_NAMES
            or (parent is not None and (not isinstance(parent, str) or parent not in SPAN_NAMES))
            or isinstance(elapsed, bool)
            or not isinstance(elapsed, (int, float))
            or not math.isfinite(elapsed)
            or elapsed < 0
            or elapsed > total + 0.05
        ):
            raise MeasurementError("fixture uninstall span is invalid")
        exclusive[name] = exclusive.get(name, 0.0) + elapsed
        calls[name] = calls.get(name, 0) + 1
        if parent is None:
            root_ms += elapsed
        else:
            exclusive[parent] = exclusive.get(parent, 0.0) - elapsed
    required = ("Container.remove", "ImageCollection.remove", "Network.disconnect")
    # The fixture rounds every span to 0.001 ms; allow accumulated rounding only.
    if (
        root_ms > total + 0.05
        or any(value < -0.05 for value in exclusive.values())
        or any(calls.get(name) != 1 for name in required)
    ):
        raise MeasurementError("fixture uninstall spans exceed their request window")
    return {
        "controller_ms": round(total, 2),
        "transport_ms": round(http_ms - total, 2),
        "unattributed_ms": round(total - root_ms, 2),
        "spans_exclusive_ms": {name: round(max(value, 0), 2) for name, value in exclusive.items()},
        "span_calls": calls,
    }


def _attach_uninstall_spans(runner: DockerFlowTests, flow: DockerFlow, observations: list[dict[str, object]]) -> None:
    lines = runner._run("logs", flow.controller).stdout.splitlines()
    records = [json.loads(line[len(SPAN_PREFIX) :]) for line in lines if line.startswith(SPAN_PREFIX)]
    if len(records) != len(observations):
        raise MeasurementError("fixture uninstall span count does not match HTTP samples")
    for sequence, (record, observation) in enumerate(zip(records, observations, strict=True), start=1):
        observation.update(_validated_spans(record, observation, sequence))


def _measure(runner: DockerFlowTests, flow: DockerFlow, samples: int, *, uninstall_spans: bool) -> dict[str, object]:
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
    if uninstall_spans:
        _attach_uninstall_spans(runner, flow, observations)
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
        if uninstall_spans:
            summary[cache]["uninstall_phases"] = {
                key: _percentiles([float(item[key]) for item in arm])
                for key in ("controller_ms", "transport_ms", "unattributed_ms")
            }
            names = sorted({name for item in arm for name in item["spans_exclusive_ms"]})
            summary[cache]["uninstall_spans_exclusive"] = {
                name: {
                    "calls": sum(item["span_calls"].get(name, 0) for item in arm),
                    **_percentiles(
                        [float(item["spans_exclusive_ms"][name]) for item in arm if name in item["spans_exclusive_ms"]]
                    ),
                }
                for name in names
            }
    return {
        "artifact": {"image_id": image_id, "size_bytes": image_size_bytes, "source_digest": flow.source_digest},
        "host_containers": host_containers,
        "observations": observations,
        "summary": summary,
    }


def _runner(*, uninstall_spans: bool) -> DockerFlowTests:
    runner = DockerFlowTests("test_real_pull_isolation_lifecycle_and_space_reset")
    if uninstall_spans:
        runner.controller_extra_env = ("--env", "SHIMPZ_PERF_UNINSTALL_SPANS=1")
    return runner


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=12, help="samples per cache arm")
    parser.add_argument("--uninstall-spans", action="store_true", help="record fixture-only uninstall phase spans")
    args = parser.parse_args()
    if args.samples < 1 or args.samples > 24:
        parser.error("--samples must be between 1 and 24")
    runner = _runner(uninstall_spans=args.uninstall_spans)
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
            result.update(_measure(runner, flow, args.samples, uninstall_spans=args.uninstall_spans))
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
                    "Fixture-only uninstall spans" if args.uninstall_spans else "No internal phase attribution"
                )
                + ("; fixture substitutes publication resolution and Sigstore verification; registry is loopback"),
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
