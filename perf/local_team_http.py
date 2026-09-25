"""Measure authenticated Local Team HTTP routes with a deterministic Brain peer.

Run from the Teams checkout with ``python -m perf.local_team_http``. The
fixture builds current Team and egress images in a disposable Docker graph.
Only timing and resource counts are printed; no request body or key is logged.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import queue
import secrets
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import docker
from docker.errors import DockerException

from local.assistant import resources as assistant_resources
from protocol.http.v1 import progress as progress_contract
from protocol.http.v1 import supervisor as supervisor_contract

TEAM = Path(__file__).resolve().parents[1]
# The Docker fixture is shared with the live Team suite; keep its graph and cleanup.
sys.path.insert(0, str(TEAM / "tests"))

import local_controller_docker_fixture as flow_fixture
from test_local_controller_docker import DockerFlowTests

from perf.brain_peer import CHAT_REPLY, OBJECTIVE, RESPONSE, SELECTED_ASSISTANT_ID, BrainPeer

SAMPLES = 12
HOST_LIST_SAMPLES = 24
ASSISTANT_AGE_CHECKPOINTS_SECONDS = (60, 300)
TEAM_CREATE_SAMPLES = 48
TEAM_LIST_SAMPLES = 48
TEAM_LIST_COUNTS = (1, 9, 33)
PEER_DELAYS_MS = (0, 250)
TEAM_MEMORY_MIB = 256
TEAM_CPUS = 1
CHAT_PAYLOAD = {"message": OBJECTIVE, "files": [], "assistant_ids": []}
CHAT_SPAN_PREFIX = "SHIMPZ-PERF-CHAT-ADMISSION "
INVENTORY_SPAN_PREFIX = "SHIMPZ-PERF-INVENTORY "
INVENTORY_SPAN_NAMES = (
    "AssistantLifecycle._network",
    "AssistantLifecycle._egress_proxy",
    "AssistantLifecycle._validate_container_profile",
    "AssistantLifecycle._validate_container_egress",
    "AssistantLifecycle._admit_assistant_allowed_hosts",
    "AssistantRegistry.team_bindings",
    "AssistantRegistry.versioned",
    "ManifestContractCache.get",
    "MachineContractCache.get",
    "ContainerCollection.list",
    "ContainerCollection.get",
    "Container.reload",
    "Container.get_archive",
    "NetworkCollection.get",
)
CHAT_SPAN_NAMES = (
    "ContainerCollection.list",
    "AssistantRegistry.team_bindings",
    "AssistantRegistry.spec",
    "AssistantLifecycle._validate_container",
    "AssistantLifecycle._egress_proxy",
    "ContainerCollection.get",
    "AssistantLifecycle._admit_assistant_allowed_hosts",
    "reviewed_manifest_contract",
    "ManifestContractCache.get",
    "MachineContractCache.get",
    "Container.get_archive",
)


class MeasurementError(RuntimeError):
    """The disposable graph or one timing sample failed its contract."""


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
        {"provider": "openai", "model": "gpt-6-sol"},
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


def _chat_request(flow: flow_fixture.DockerFlow, *, selected: bool) -> tuple[str, bytes, dict[str, str]]:
    path = "/v1/teams/demo_team/chat"
    payload = CHAT_PAYLOAD | {"assistant_ids": [SELECTED_ASSISTANT_ID] if selected else []}
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "Authorization": f"Bearer {flow.token}",
        "Content-Type": "application/json",
        "Connection": "close",
        "X-Shimpz-Model-Provider": "openai",
        "X-Shimpz-Model-Api-Key": BrainPeer.dummy_key,
    }
    headers[supervisor_contract.ASSERTION_HEADER] = flow_fixture.supervisor_header(flow, "POST", path, encoded, headers)
    return path, encoded, headers


def _read_chat(flow: flow_fixture.DockerFlow, *, selected: bool) -> tuple[int, int, int, tuple[int, ...]]:
    started = time.perf_counter_ns()
    first_progress: int | None = None
    phase_ms: list[int] = []
    events: list[tuple[str, str]] = []
    terminal_at: int | None = None
    stream_bytes = 0
    path, encoded, headers = _chat_request(flow, selected=selected)
    connection = http.client.HTTPConnection("127.0.0.1", flow.port, timeout=30)
    try:
        connection.request("POST", path, encoded, headers)
        response = connection.getresponse()
        with response:
            if (
                response.status != 200
                or response.headers.get("Content-Type") != "application/x-ndjson"
                or response.headers.get("Transfer-Encoding") != "chunked"
            ):
                raise MeasurementError("Team chat stream status or headers changed")
            for sequence in range(1, progress_contract.MAX_EVENTS + 2):
                line = response.readline(progress_contract.MAX_LINE_BYTES + 1)
                stream_bytes += len(line)
                if stream_bytes > progress_contract.MAX_STREAM_BYTES:
                    raise MeasurementError("Team chat stream exceeded its bound")
                record = progress_contract.decode_line(line)
                observed = time.perf_counter_ns()
                if record["type"] == "terminal":
                    body = record["body"]
                    if (
                        record["status"] != 200
                        or set(body) != {"team_id", "team_name", "reply", "trace_id"}
                        or body["team_id"] != "demo_team"
                        or body["team_name"] != "Demo Team"
                        or body["reply"] != CHAT_REPLY
                        or not isinstance(body["trace_id"], str)
                        or len(body["trace_id"]) != 32
                        or response.read(1) != b""
                    ):
                        raise MeasurementError("Team chat terminal changed")
                    terminal_at = observed
                    break
                if record["seq"] != sequence:
                    raise MeasurementError("Team chat progress sequence changed")
                if first_progress is None:
                    first_progress = observed
                events.append((record["phase"], record["state"]))
                if record["state"] == "finished":
                    phase_ms.append(record["elapsed_ms"])
    finally:
        connection.close()
    if (
        first_progress is None
        or terminal_at is None
        or events
        != [
            ("team-context", "started"),
            ("team-context", "finished"),
            ("model", "started"),
            ("model", "finished"),
            ("team-context", "started"),
            ("team-context", "finished"),
        ]
    ):
        raise MeasurementError("Team chat progress did not complete")
    return started, first_progress, terminal_at, tuple(phase_ms)


def _sample_chat(flow: flow_fixture.DockerFlow, delay_ms: int, *, selected: bool = False) -> dict[str, float]:
    BrainPeer.delay_ms = delay_ms
    BrainPeer.selected_assistant = selected
    started, first_at, terminal_at, phase_ms = _read_chat(flow, selected=selected)
    peer_started, peer_ended = BrainPeer.turn_spans.get(timeout=2)
    if delay_ms and first_at >= peer_ended:
        raise MeasurementError("Team chat stream did not deliver early progress")
    return {
        "team_first_progress_ms": (first_at - started) / 1_000_000,
        "team_terminal_ms": (terminal_at - started) / 1_000_000,
        "peer_handler_ms": (peer_ended - peer_started) / 1_000_000,
        "team_context_initial_ms": float(phase_ms[0]),
        "model_ms": float(phase_ms[1]),
        "team_context_revalidation_ms": float(phase_ms[2]),
        "unattributed_after_first_progress_ms": (terminal_at - first_at) / 1_000_000 - sum(phase_ms),
    }


def _measure_chat(flow: flow_fixture.DockerFlow, *, selected: bool = False) -> dict[str, object]:
    warm = _sample_chat(flow, 0, selected=selected)
    _sample_chat(flow, 0, selected=selected)
    samples: dict[int, dict[str, list[float]]] = {delay: {name: [] for name in warm} for delay in PEER_DELAYS_MS}
    for index in range(SAMPLES):
        for delay in PEER_DELAYS_MS if index % 2 == 0 else tuple(reversed(PEER_DELAYS_MS)):
            for name, value in _sample_chat(flow, delay, selected=selected).items():
                samples[delay][name].append(value)
    return {
        str(delay): {name: _percentiles(values) for name, values in samples[delay].items()} for delay in PEER_DELAYS_MS
    }


def _require_installed_assistant(runner: DockerFlowTests, flow: flow_fixture.DockerFlow) -> None:
    owned = runner._owned_ids("container", flow.space_id, "assistant")
    if len(owned) != 1:
        raise MeasurementError("reference Assistant inventory changed during the measured arm")
    # Docker lists abbreviated IDs; inspect resolves the exact installed generation.
    metadata = json.loads(runner._run("inspect", owned[0]).stdout)[0]
    if metadata["Id"] != flow.original_assistant_id:
        raise MeasurementError("reference Assistant identity changed during the measured arm")
    if metadata["State"]["Status"] != "running":
        raise MeasurementError("reference Assistant stopped during the measured arm")


def _chat_span_records(runner: DockerFlowTests, flow: flow_fixture.DockerFlow) -> list[dict[str, object]]:
    logs = runner._run("logs", flow.controller)
    records = []
    for line in (logs.stdout + logs.stderr).splitlines():
        if line.startswith(CHAT_SPAN_PREFIX):
            record = json.loads(line.removeprefix(CHAT_SPAN_PREFIX))
            if not isinstance(record, dict):
                raise MeasurementError("chat admission span record is invalid")
            records.append(record)
    return records


def _chat_span_sample(record: dict[str, object], installed: bool) -> dict[str, float]:
    spans = record.get("spans")
    total = record.get("total_ms")
    if not isinstance(spans, list) or not isinstance(total, (int, float)) or total < 0:
        raise MeasurementError("chat admission span record is invalid")
    durations = dict.fromkeys(CHAT_SPAN_NAMES, 0.0)
    counts = dict.fromkeys(CHAT_SPAN_NAMES, 0)
    children = dict.fromkeys(CHAT_SPAN_NAMES, 0.0)
    top_level = 0.0
    for span in spans:
        if not isinstance(span, dict) or set(span) != {"name", "parent", "ms"}:
            raise MeasurementError("chat admission span shape changed")
        name, parent, elapsed = span["name"], span["parent"], span["ms"]
        if name not in durations or (parent is not None and parent not in durations):
            raise MeasurementError("chat admission span ownership changed")
        if not isinstance(elapsed, (int, float)) or elapsed < 0:
            raise MeasurementError("chat admission span duration is invalid")
        durations[name] += elapsed
        counts[name] += 1
        if parent is None:
            top_level += elapsed
        else:
            children[parent] += elapsed
    required = {
        "ContainerCollection.list": 1,
        "AssistantRegistry.team_bindings": int(installed),
        "AssistantRegistry.spec": int(installed),
        "AssistantLifecycle._validate_container": int(installed),
        "AssistantLifecycle._admit_assistant_allowed_hosts": int(installed),
        "AssistantLifecycle._egress_proxy": int(installed),
        "ManifestContractCache.get": int(installed),
        "MachineContractCache.get": int(installed),
        "Container.get_archive": 0,
        # Docker SDK list() inspects the Assistant with get(); the proxy adds another get().
        "ContainerCollection.get": 2 * int(installed),
    }
    if (
        any(counts[name] != count for name, count in required.items())
        or total + 0.02 < top_level
        or any(children[name] > durations[name] + 0.02 for name in CHAT_SPAN_NAMES)
    ):
        raise MeasurementError("chat admission operation count or span arithmetic changed")
    exclusive = {
        f"{name}_exclusive_ms": max(0.0, durations[name] - children[name])
        for name in (
            "ContainerCollection.list",
            "AssistantRegistry.spec",
            "AssistantLifecycle._validate_container",
            "AssistantLifecycle._egress_proxy",
            "AssistantLifecycle._admit_assistant_allowed_hosts",
        )
    }
    return (
        {"total_ms": float(total), "remainder_ms": max(0.0, total - top_level)}
        | durations
        | exclusive
        | {f"{name}_calls": count for name, count in counts.items()}
    )


def _chat_span_summary(
    records: list[dict[str, object]], installed: bool, *, scans_per_turn: int = 1, revalidation: bool = False
) -> dict[str, object]:
    expected = 2 + SAMPLES * len(PEER_DELAYS_MS)
    if len(records) != expected * scans_per_turn:
        raise MeasurementError("chat admission span count changed")
    selected = [record for index, record in enumerate(records) if (index % scans_per_turn != 0) == revalidation]
    warmups = 2 * (scans_per_turn - 1 if revalidation else 1)
    samples = [_chat_span_sample(record, installed) for record in selected[warmups:]]
    names = samples[0]
    return {
        name: _percentiles([sample[name] for sample in samples])
        if not name.endswith("_calls")
        else {"min": min(sample[name] for sample in samples), "max": max(sample[name] for sample in samples)}
        for name in names
    }


def _host_list_probe(flow: flow_fixture.DockerFlow, installed: bool) -> dict[str, object]:
    try:
        filters = assistant_resources._assistant_filters(SimpleNamespace(space_id=flow.space_id), "demo_team")
    except (AttributeError, TypeError, ValueError) as exc:
        raise MeasurementError("host Docker list probe filter construction changed") from exc
    selected = filters.get("filters") if isinstance(filters, dict) else None
    labels = selected.get("label") if isinstance(selected, dict) else None
    if (
        not isinstance(filters, dict)
        or set(filters) != {"all", "filters"}
        or filters["all"] is not True
        or not isinstance(selected, dict)
        or set(selected) != {"label"}
        or not isinstance(labels, list)
        or len(labels) != 5
        or any(not isinstance(item, str) or "=" not in item for item in labels)
    ):
        raise MeasurementError("host Docker list probe filter shape changed")
    expected = int(installed)
    try:
        client = docker.from_env(timeout=10)
    except DockerException as exc:
        raise MeasurementError("host Docker list probe could not connect") from exc
    try:
        before_count = len(client.api.containers(all=True))
        arms = {
            "raw": lambda: client.api.containers(**filters),
            "full": lambda: client.containers.list(**filters),
        }
        for call in arms.values():
            for _ in range(2):
                if len(call()) != expected:
                    raise MeasurementError("host Docker list probe match count changed")
        samples: dict[str, list[float]] = {name: [] for name in arms}
        for index in range(HOST_LIST_SAMPLES):
            for name in ("raw", "full") if index % 2 == 0 else ("full", "raw"):
                started = time.perf_counter_ns()
                found = arms[name]()
                samples[name].append((time.perf_counter_ns() - started) / 1_000_000)
                if len(found) != expected:
                    raise MeasurementError("host Docker list probe match count changed")
        if len(client.api.containers(all=True)) != before_count:
            raise MeasurementError("host Docker container count changed during list probe")
        return {
            "matched": expected,
            "host_containers": before_count,
            "raw": _percentiles(samples["raw"]),
            "full": _percentiles(samples["full"]),
        }
    except DockerException as exc:
        raise MeasurementError("host Docker list probe failed") from exc
    finally:
        client.close()


def _measure_chat_block(
    runner: DockerFlowTests, flow: flow_fixture.DockerFlow, *, chat_spans: bool, installed: bool, selected: bool = False
) -> dict[str, object]:
    before = len(_chat_span_records(runner, flow)) if chat_spans else 0
    measured = _measure_chat(flow, selected=selected)
    if chat_spans:
        records = _chat_span_records(runner, flow)[before:]
        scans_per_turn = 3 if selected else 1
        measured["admission_spans"] = _chat_span_summary(records, installed, scans_per_turn=scans_per_turn)
        if selected:
            measured["revalidation_spans"] = _chat_span_summary(
                records, installed, scans_per_turn=scans_per_turn, revalidation=True
            )
        measured["host_list_probe"] = _host_list_probe(flow, installed)
    return measured


def _measure_chat_inventory(
    runner: DockerFlowTests, flow: flow_fixture.DockerFlow, *, chat_spans: bool, age_probe: bool
) -> dict[str, object]:
    if age_probe and not chat_spans:
        raise MeasurementError("Assistant age probe requires chat spans")
    none_initial = _measure_chat_block(runner, flow, chat_spans=chat_spans, installed=False)
    try:
        runner._exercise_assistant(flow)
    except AssertionError as exc:
        raise MeasurementError("reference Assistant installation contract failed") from exc
    _require_installed_assistant(runner, flow)
    installed_at = time.monotonic()
    one_unselected = _measure_chat_block(runner, flow, chat_spans=chat_spans, installed=True)
    _require_installed_assistant(runner, flow)
    one_selected = _measure_chat_block(runner, flow, chat_spans=chat_spans, installed=True, selected=True)
    _require_installed_assistant(runner, flow)
    aged_selected: dict[str, object] = {}
    if age_probe:
        initial_count = one_selected["host_list_probe"]["host_containers"]
        for checkpoint in ASSISTANT_AGE_CHECKPOINTS_SECONDS:
            time.sleep(max(0.0, checkpoint - (time.monotonic() - installed_at)))
            _require_installed_assistant(runner, flow)
            elapsed = time.monotonic() - installed_at
            if elapsed < checkpoint:
                raise MeasurementError("Assistant age checkpoint was not reached")
            measured = _measure_chat_block(runner, flow, chat_spans=chat_spans, installed=True, selected=True)
            if measured["host_list_probe"]["host_containers"] != initial_count:
                raise MeasurementError("host Docker container population changed during Assistant age probe")
            aged_selected[str(checkpoint)] = {"elapsed_since_install_seconds": round(elapsed, 2), **measured}
            _require_installed_assistant(runner, flow)
    status, body = runner._api(flow.port, flow.token, "DELETE", "/v1/teams/demo_team/assistants/shimpz-cloudflare")
    if status != 200 or body.get("uninstalled") is not True:
        raise MeasurementError("reference Assistant uninstall failed")
    if runner._owned_ids("container", flow.space_id, "assistant"):
        raise MeasurementError("reference Assistant remained after uninstall")
    none_restored = _measure_chat_block(runner, flow, chat_spans=chat_spans, installed=False)
    return {
        "none_initial": none_initial,
        "one_unselected": one_unselected,
        "one_selected": one_selected,
        "aged_selected": aged_selected,
        "none_restored": none_restored,
    }


def _inventory_span_records(runner: DockerFlowTests, flow: flow_fixture.DockerFlow) -> list[dict[str, object]]:
    logs = runner._run("logs", flow.controller)
    records = []
    for line in (logs.stdout + logs.stderr).splitlines():
        if line.startswith(INVENTORY_SPAN_PREFIX):
            record = json.loads(line.removeprefix(INVENTORY_SPAN_PREFIX))
            if not isinstance(record, dict):
                raise MeasurementError("Assistant inventory span record is invalid")
            records.append(record)
    return records


def _inventory_span_sample(record: dict[str, object]) -> dict[str, float | int]:
    if set(record) != {"seq", "total_ms", "spans"} or not isinstance(record["spans"], list):
        raise MeasurementError("Assistant inventory span record shape changed")
    total = record["total_ms"]
    if not isinstance(total, (int, float)) or total < 0:
        raise MeasurementError("Assistant inventory span duration is invalid")
    durations = dict.fromkeys(INVENTORY_SPAN_NAMES, 0.0)
    counts = dict.fromkeys(INVENTORY_SPAN_NAMES, 0)
    children = dict.fromkeys(INVENTORY_SPAN_NAMES, 0.0)
    get_parents = dict.fromkeys(("ContainerCollection.list", "Container.reload", "AssistantLifecycle._egress_proxy"), 0)
    top_level = 0.0
    for span in record["spans"]:
        if not isinstance(span, dict) or set(span) != {"name", "parent", "ms"}:
            raise MeasurementError("Assistant inventory child span shape changed")
        name, parent, elapsed = span["name"], span["parent"], span["ms"]
        if name not in durations or (parent is not None and parent not in durations):
            raise MeasurementError("Assistant inventory span ownership changed")
        if not isinstance(elapsed, (int, float)) or elapsed < 0:
            raise MeasurementError("Assistant inventory child span duration is invalid")
        durations[name] += elapsed
        counts[name] += 1
        if parent is None:
            top_level += elapsed
        else:
            children[parent] += elapsed
        if name == "ContainerCollection.get" and parent in get_parents:
            get_parents[parent] += 1
    if total + 0.02 < top_level or any(children[name] > durations[name] + 0.02 for name in INVENTORY_SPAN_NAMES):
        raise MeasurementError("Assistant inventory span arithmetic changed")
    exclusive = {
        f"{name}_exclusive_ms": max(0.0, durations[name] - children[name])
        for name in (
            "AssistantLifecycle._network",
            "AssistantLifecycle._egress_proxy",
            "AssistantLifecycle._validate_container_profile",
            "AssistantLifecycle._validate_container_egress",
            "ContainerCollection.list",
            "Container.reload",
        )
    }
    return (
        {"total_ms": float(total), "remainder_ms": max(0.0, total - top_level)}
        | durations
        | exclusive
        | {f"{name}_calls": count for name, count in counts.items()}
        | {f"ContainerCollection.get_from_{name}_calls": count for name, count in get_parents.items()}
    )


def _inventory_span_summary(records: list[dict[str, object]], installed: bool) -> dict[str, object]:
    if len(records) != SAMPLES + 2:
        raise MeasurementError("Assistant inventory span record count changed")
    samples = [_inventory_span_sample(record) for record in records[2:]]
    expected = {
        "NetworkCollection.get_calls": 1,
        "ContainerCollection.list_calls": 1,
        "AssistantLifecycle._network_calls": 1,
        "AssistantRegistry.team_bindings_calls": int(installed),
        "AssistantRegistry.versioned_calls": int(installed),
        "AssistantLifecycle._validate_container_profile_calls": int(installed),
        "AssistantLifecycle._validate_container_egress_calls": int(installed),
        "AssistantLifecycle._admit_assistant_allowed_hosts_calls": int(installed),
        "AssistantLifecycle._egress_proxy_calls": int(installed),
        "Container.reload_calls": int(installed),
        "ContainerCollection.get_calls": 3 * int(installed),
        "ContainerCollection.get_from_ContainerCollection.list_calls": int(installed),
        "ContainerCollection.get_from_Container.reload_calls": int(installed),
        "ContainerCollection.get_from_AssistantLifecycle._egress_proxy_calls": int(installed),
        "ManifestContractCache.get_calls": int(installed),
        "MachineContractCache.get_calls": int(installed),
        "Container.get_archive_calls": 0,
    }
    if any(sample.get(name) != value for sample in samples for name, value in expected.items()):
        raise MeasurementError("Assistant inventory operation count changed")
    return {
        name: {"min": min(sample[name] for sample in samples), "max": max(sample[name] for sample in samples)}
        if name.endswith("_calls")
        else _percentiles([sample[name] for sample in samples])
        for name in samples[0]
    }


def _host_container_count(runner: DockerFlowTests) -> int:
    return len(runner._run("ps", "--all", "--quiet").stdout.splitlines())


def _measure_inventory_route_block(
    runner: DockerFlowTests,
    flow: flow_fixture.DockerFlow,
    *,
    installed: bool,
    spans: bool,
) -> dict[str, object]:
    expected = {
        "assistants": [
            {
                "assistant": "shimpz-cloudflare",
                "assistant_version": "0.1.0",
                "status": "running",
                "provenance": "published",
            }
        ]
        if installed
        else []
    }
    before = len(_inventory_span_records(runner, flow)) if spans else 0
    host_containers = _host_container_count(runner)
    samples: list[float] = []
    for index in range(-2, SAMPLES):
        started = time.perf_counter_ns()
        status, body = runner._api(flow.port, flow.token, "GET", "/v1/teams/demo_team/assistants")
        elapsed = (time.perf_counter_ns() - started) / 1_000_000
        trace_id = body.get("trace_id") if isinstance(body, dict) else None
        payload = {key: value for key, value in body.items() if key != "trace_id"} if isinstance(body, dict) else None
        if (
            status != 200
            or payload != expected
            or not isinstance(trace_id, str)
            or len(trace_id) != 32
            or any(character not in "0123456789abcdef" for character in trace_id)
        ):
            rows = body.get("assistants") if isinstance(body, dict) else None
            row_count = len(rows) if isinstance(rows, list) else -1
            statuses = [row.get("status") for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
            raise MeasurementError(
                "Assistant inventory route returned a noncanonical result: "
                f"installed={installed}, HTTP={status}, code={body.get('code') if isinstance(body, dict) else None}, "
                f"keys={sorted(body) if isinstance(body, dict) else []}, row_count={row_count}, statuses={statuses}"
            )
        if index >= 0:
            samples.append(elapsed)
    if _host_container_count(runner) != host_containers:
        raise MeasurementError("host Docker container count changed during inventory route samples")
    result: dict[str, object] = {"client_http_ms": _percentiles(samples), "host_containers": host_containers}
    if spans:
        result["spans"] = _inventory_span_summary(_inventory_span_records(runner, flow)[before:], installed)
    return result


def _measure_inventory_route(
    runner: DockerFlowTests, flow: flow_fixture.DockerFlow, *, spans: bool
) -> dict[str, object]:
    none_initial = _measure_inventory_route_block(runner, flow, installed=False, spans=spans)
    try:
        runner._exercise_assistant(flow)
    except AssertionError as exc:
        raise MeasurementError("reference Assistant installation contract failed") from exc
    _require_installed_assistant(runner, flow)
    one_installed = _measure_inventory_route_block(runner, flow, installed=True, spans=spans)
    _require_installed_assistant(runner, flow)
    status, body = runner._api(flow.port, flow.token, "DELETE", "/v1/teams/demo_team/assistants/shimpz-cloudflare")
    if status != 200 or body.get("uninstalled") is not True:
        raise MeasurementError("reference Assistant uninstall failed")
    if runner._owned_ids("container", flow.space_id, "assistant"):
        raise MeasurementError("reference Assistant remained after uninstall")
    none_restored = _measure_inventory_route_block(runner, flow, installed=False, spans=spans)
    return {"none_initial": none_initial, "one_installed": one_installed, "none_restored": none_restored}


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


def _measure_team_create(runner: DockerFlowTests, flow: flow_fixture.DockerFlow) -> dict[str, object]:
    samples: dict[str, list[float]] = {"new_ms": [], "existing_ms": []}
    for index in range(-2, TEAM_CREATE_SAMPLES):
        team_id = f"perf_{index + 2:03d}"
        path = f"/v1/teams/{team_id}/create"
        # An existing creation follows each new creation for the same Team in both revisions.
        for name, expected_created in (("new_ms", True), ("existing_ms", False)):
            started = time.perf_counter_ns()
            status, body = runner._api(flow.port, flow.token, "POST", path, {"team_name": "Performance Team"})
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            if status != 200 or body.get("created") is not expected_created:
                raise MeasurementError("Team creation returned a noncanonical result")
            if index >= 0:
                samples[name].append(elapsed_ms)
        status, body = runner._api(flow.port, flow.token, "DELETE", f"/v1/teams/{team_id}")
        if status != 200 or not isinstance(body.get("residue_absent"), list):
            raise MeasurementError("measured Team cleanup failed")
    return {name: _percentiles(values) for name, values in samples.items()}


def _sample_team_list(runner: DockerFlowTests, flow: flow_fixture.DockerFlow, expected: dict[str, str]) -> float:
    started = time.perf_counter_ns()
    status, body = runner._api(flow.port, flow.token, "GET", "/v1/teams")
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    teams = [{"team_id": team_id, "team_name": expected[team_id], "status": "running"} for team_id in sorted(expected)]
    if status != 200 or body.get("teams") != teams:
        raise MeasurementError("Team listing returned a noncanonical result")
    return elapsed_ms


def _measure_team_list(runner: DockerFlowTests, flow: flow_fixture.DockerFlow) -> dict[str, object]:
    expected = {"demo_team": "Demo Team"}
    results: dict[str, object] = {}
    for count in TEAM_LIST_COUNTS:
        while len(expected) < count:
            team_id = f"perf_list_{len(expected):02d}"
            status, body = runner._api(
                flow.port, flow.token, "POST", f"/v1/teams/{team_id}/create", {"team_name": "List Team"}
            )
            if status != 200 or body.get("created") is not True:
                raise MeasurementError("measured Team listing setup failed")
            expected[team_id] = "List Team"
        for _ in range(2):
            _sample_team_list(runner, flow, expected)
        results[str(count)] = _percentiles(
            [_sample_team_list(runner, flow, expected) for _ in range(TEAM_LIST_SAMPLES)]
        )
    for team_id in sorted(expected.keys() - {"demo_team"}):
        status, body = runner._api(flow.port, flow.token, "DELETE", f"/v1/teams/{team_id}")
        if status != 200 or not isinstance(body.get("residue_absent"), list):
            raise MeasurementError("measured Team listing cleanup failed")
    return results


def _configured_runner() -> tuple[DockerFlowTests, bool, bool, bool, bool]:
    if sys.argv[1:] not in (
        [],
        ["--chat-spans"],
        ["--chat-spans", "--age-probe"],
        ["--inventory-route"],
        ["--inventory-route", "--inventory-spans"],
    ):
        raise SystemExit(
            "usage: python -m perf.local_team_http [--chat-spans [--age-probe] | --inventory-route [--inventory-spans]]"
        )
    chat_spans = "--chat-spans" in sys.argv[1:]
    age_probe = "--age-probe" in sys.argv[1:]
    inventory_route = "--inventory-route" in sys.argv[1:]
    inventory_spans = "--inventory-spans" in sys.argv[1:]
    runner = DockerFlowTests("test_real_pull_isolation_lifecycle_and_space_reset")
    if chat_spans:
        runner.controller_extra_env = ("--env", "SHIMPZ_PERF_CHAT_SPANS=1")
    if inventory_spans:
        runner.controller_extra_env = ("--env", "SHIMPZ_PERF_INVENTORY_SPANS=1")
    return runner, chat_spans, age_probe, inventory_route, inventory_spans


def _record_workload(
    result: dict[str, object],
    runner: DockerFlowTests,
    flow: flow_fixture.DockerFlow,
    *,
    chat_spans: bool,
    age_probe: bool,
    inventory_route: bool,
    inventory_spans: bool,
) -> None:
    if inventory_route:
        result["inventory_route"] = _measure_inventory_route(runner, flow, spans=inventory_spans)
    else:
        result["samples"] = _measure(runner, flow)
        result["chat_inventory"] = _measure_chat_inventory(runner, flow, chat_spans=chat_spans, age_probe=age_probe)
        result["team_create"] = _measure_team_create(runner, flow)
        result["team_list"] = _measure_team_list(runner, flow)
    result.update(
        status="complete",
        scope=(
            "authenticated Local Team HTTP Assistant inventory with 0/1/0 installed reference Assistants; "
            "one Team, sequential requests, 2 warmups and 12 measured GETs per arm; real digest-pull "
            "installation and uninstall; excludes Admin, browser, chat, and provider"
        )
        if inventory_route
        else (
            "authenticated Local Team HTTP for intent routing, Brain-only chat with 0/1/1/0 installed "
            "reference Assistants, Team creation, and Team listing (1/9/33); the installed Assistant "
            "is unselected in one arm and selected in the next, with its Genesis and Actions sent to the "
            "deterministic Brain peer but no Action invoked; one connection per sample; each chat arm "
            "reuses one Team and "
            "stateless Brain peer across 2 warmups and 24 measured turns; the first post-install chat "
            "is excluded from steady-state samples; "
            "excludes Admin WebSocket, browser, and real provider"
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
    if inventory_route:
        result["inventory_span_mode"] = inventory_spans
        result["inventory_timing_definition"] = (
            "client HTTP includes Supervisor signing and log emission in span mode; root span excludes "
            "HTTP transport and span log emission; repeated spans are summed per request, parent spans include "
            "their children, and exclusive spans subtract timed children; compare client timing only within "
            "the same mode"
        )
    else:
        result.update(
            chat_timing_definition=(
                "first progress and terminal include client Supervisor signing; unattributed time starts "
                "after first progress and includes unspanned context checks, commit, transport, scheduling, "
                "and up to 3 ms of phase truncation"
            ),
            chat_phase_resolution_ms=1,
            chat_span_mode=chat_spans,
            assistant_age_probe=age_probe,
            assistant_age_checkpoints_seconds=ASSISTANT_AGE_CHECKPOINTS_SECONDS if age_probe else None,
            host_list_probe_definition=(
                "raw API (daemon, socket transport, JSON decode) versus full Docker SDK list in the harness "
                "process outside the Team's 1-CPU cgroup, using the Team's five exact owned-Assistant filters; "
                "sampled after each measured chat block with no chat turn in flight"
            )
            if chat_spans
            else None,
            outside_peer_definition=(
                "Team HTTP elapsed minus peer handler elapsed; includes connection and peer header parsing"
            ),
        )


def main() -> int:
    runner, chat_spans, age_probe, inventory_route, inventory_spans = _configured_runner()
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
            runner._prepare_images(flow)
            _start(runner, flow)
            _record_workload(
                result,
                runner,
                flow,
                chat_spans=chat_spans,
                age_probe=age_probe,
                inventory_route=inventory_route,
                inventory_spans=inventory_spans,
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
