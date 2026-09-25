"""Measure repeated Hosted network-policy scans inside one validation boundary.

Run from the Teams checkout with
``uv run --frozen --python 3.14 python -m perf.hosted_network_validation``.
The Docker API is an in-process fixture; the production network policy runs on
Engine-shaped topology from the Team contracts. Only aggregate timings print.
"""

from __future__ import annotations

import copy
import json
import math
import os
import statistics
import sys
import time
import types
from pathlib import Path
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM / "tests"))

import hosted_assistant_fixture as fixture
from test_network_policy import TEAM_ID, _valid_topology

policy = fixture.hosted_resources.network_policy

CASES = (1, 4, 16)
WARMUPS = 20
SAMPLES = 200


def _topology(assistants: int) -> tuple[dict, dict[str, dict]]:
    network, containers = _valid_topology()
    template = containers.pop("assistant-id")
    del network["Containers"]["assistant-id"]
    network_name = policy.network_name(TEAM_ID, policy.CORE_KIND)
    for index in range(assistants):
        assistant_id = f"helper-{index}"
        container_id = f"assistant-{index}"
        container = copy.deepcopy(template)
        container["Id"] = container_id
        container["Name"] = f"/{policy.team_assistant_container_name(TEAM_ID, assistant_id)}"
        container["Config"]["Labels"]["team.assistant"] = assistant_id
        container["NetworkSettings"]["Networks"][network_name]["Aliases"] = [assistant_id, f"{assistant_id}.team"]
        containers[container_id] = container
        network["Containers"][container_id] = {}
    return network, containers


def _percentiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
    }


def _measure(assistants: int) -> dict[str, object]:
    network_data, containers = _topology(assistants)
    network = types.SimpleNamespace(id=network_data["Id"], attrs=network_data, reload=lambda: None)
    models = {
        identity: types.SimpleNamespace(id=identity, name=metadata["Name"].removeprefix("/"), attrs=metadata)
        for identity, metadata in containers.items()
    }
    engine = types.SimpleNamespace(containers=types.SimpleNamespace(get=models.__getitem__))

    def check() -> None:
        memo: dict[str, object] = {}
        for index in range(assistants + 1):
            fixture.hosted_resources._require_network_policy(
                network,
                TEAM_ID,
                policy.CORE_KIND,
                require_runtime=index == 0,
                require_dependencies=True,
                inspect_memo=memo,
            )

    def once() -> None:
        fixture.hosted_resources._require_network_policy(
            network,
            TEAM_ID,
            policy.CORE_KIND,
            require_runtime=True,
            require_dependencies=True,
            inspect_memo={},
        )

    with mock.patch.object(fixture.runtime_state, "_docker", engine):
        with mock.patch.object(policy, "network_members_valid", wraps=policy.network_members_valid) as scan:
            check()
            scan_count = scan.call_count
        if not 1 <= scan_count <= assistants + 1:
            raise RuntimeError("network scan count is outside one validation boundary")
        samples: dict[str, list[float]] = {"check": [], "once": []}
        operations = {"check": check, "once": once}
        for index in range(WARMUPS + SAMPLES):
            for name in ("check", "once") if index % 2 else ("once", "check"):
                started = time.process_time_ns()
                operations[name]()
                if index >= WARMUPS:
                    samples[name].append((time.process_time_ns() - started) / 1_000_000)
    return {
        "assistants": assistants,
        "network_members": len(network_data["Containers"]),
        "policy_scans_per_check": scan_count,
        "samples": SAMPLES,
        "cpu_ms": {name: _percentiles(values) for name, values in samples.items()},
    }


def main() -> None:
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "host_cpus": os.cpu_count(),
                "process_cpus": os.process_cpu_count(),
                "docker": "in-process fixture; no daemon or container limits",
                "cases": [_measure(assistants) for assistants in CASES],
                "completed_without_failures": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
