"""Measure Local Team registry lookups with realistic validated bindings.

Run from the Teams checkout with
``uv run --frozen --python 3.14 python -m perf.local_registry_scan``.
The two lookup shapes use the same durable store and selected Assistant ids.
Only aggregate timings and operation counts are printed.
"""

from __future__ import annotations

import copy
import json
import math
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from install.bindings import DynamicAssistantStore
from install.contract import CONTRACT_ROOT
from local.install.registry import AssistantRegistry

SAMPLES = 100
WARMUPS = 10
CASES = ((1, 4), (1, 16), (4, 4), (4, 16))
RESOLUTION = json.loads((CONTRACT_ROOT / "vectors.json").read_bytes())["fixtures"]["resolve_response"]["value"]
RESOLUTION["machine_contract"]["actions"][0]["input_schema"]["additionalProperties"] = False
RESOLUTION["machine_contract"]["actions"][0]["output_schema"]["additionalProperties"] = False


def _store(path: Path, selected_count: int, binding_count: int) -> DynamicAssistantStore:
    store = DynamicAssistantStore(path)
    for index in range(binding_count):
        resolution = copy.deepcopy(RESOLUTION)
        resolution["assistant_id"] = f"helper-{index}"
        store.put("team_1" if index < selected_count else "team_2", resolution)
    return store


def _individual(store: DynamicAssistantStore, assistant_ids: tuple[str, ...]) -> tuple[str, ...]:
    digests = []
    for assistant_id in assistant_ids:
        binding = store.get("team_1", assistant_id)
        if binding is None:
            raise RuntimeError("a selected Assistant binding is missing")
        AssistantRegistry.spec(binding)
        digests.append(binding.binding_digest)
    return tuple(digests)


def _snapshot(store: DynamicAssistantStore, assistant_ids: tuple[str, ...]) -> tuple[str, ...]:
    bindings = {binding.assistant_id: binding for binding in store.list("team_1")}
    digests = []
    for assistant_id in assistant_ids:
        binding = bindings.get(assistant_id)
        if binding is None:
            raise RuntimeError("a selected Assistant binding is missing")
        AssistantRegistry.spec(binding)
        digests.append(binding.binding_digest)
    return tuple(digests)


def _percentiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
    }


def _measure(store: DynamicAssistantStore, assistant_ids: tuple[str, ...]) -> dict[str, object]:
    operations = {"individual": _individual, "snapshot": _snapshot}
    counts = {}
    expected = None
    for name, operation in operations.items():
        with mock.patch.object(store, "_read", wraps=store._read) as read:
            result = operation(store, assistant_ids)
            counts[name] = read.call_count
        if expected is not None and result != expected:
            raise RuntimeError("registry lookup shapes resolved different bindings")
        expected = result
    if counts != {"individual": len(assistant_ids), "snapshot": 1}:
        raise RuntimeError("registry read counts changed")
    samples: dict[str, list[float]] = {name: [] for name in operations}
    for index in range(WARMUPS + SAMPLES):
        for name in ("individual", "snapshot") if index % 2 else ("snapshot", "individual"):
            started = time.process_time_ns()
            operations[name](store, assistant_ids)
            if index >= WARMUPS:
                samples[name].append((time.process_time_ns() - started) / 1_000_000)
    return {
        "reads": counts,
        "cpu_ms": {name: _percentiles(values) for name, values in samples.items()},
    }


def main() -> None:
    results = []
    for selected_count, binding_count in CASES:
        with tempfile.TemporaryDirectory() as directory:
            store = _store(Path(directory) / "bindings.json", selected_count, binding_count)
            assistant_ids = tuple(f"helper-{index}" for index in range(selected_count))
            if {binding.assistant_id for binding in store.list("team_1")} != set(assistant_ids):
                raise RuntimeError("registry Team scoping changed")
            results.append(
                {
                    "selected": selected_count,
                    "total_bindings": binding_count,
                    "samples": SAMPLES,
                    **_measure(store, assistant_ids),
                }
            )
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "host_cpus": os.cpu_count(),
                "process_cpus": os.process_cpu_count(),
                "fixture_source": "protocol/install/v1/vectors.json resolve_response with closed Action schemas",
                "fixture_bytes": len(
                    json.dumps(RESOLUTION, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                ),
                "fixture_actions": len(RESOLUTION["machine_contract"]["actions"]),
                "completed_without_failures": True,
                "cases": results,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
