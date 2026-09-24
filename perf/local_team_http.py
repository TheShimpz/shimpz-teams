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
from typing import override
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

SAMPLES = 12
HOST_LIST_SAMPLES = 24
TEAM_CREATE_SAMPLES = 48
TEAM_LIST_SAMPLES = 48
TEAM_LIST_COUNTS = (1, 9, 33)
PEER_DELAYS_MS = (0, 250)
TEAM_MEMORY_MIB = 256
TEAM_CPUS = 1
OBJECTIVE = "Hello"
RESPONSE = {"intent": "ordinary-task", "query": "", "assistant_ids": [], "reply": ""}
CHAT_REPLY = "Measured reply."
CHAT_PAYLOAD = {"message": OBJECTIVE, "files": [], "assistant_ids": []}
CHAT_PROMPT = json.dumps({"files": [], "message": OBJECTIVE}, separators=(",", ":"), ensure_ascii=False)
CHAT_SPAN_PREFIX = "SHIMPZ-PERF-CHAT-ADMISSION "
CHAT_SPAN_NAMES = (
    "ContainerCollection.list",
    "AssistantRegistry.get",
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


class BrainPeer(flow_fixture.BrainLifecycleHandler):
    """Return only one closed intent decision and record the peer's own span."""

    delay_ms = 0
    dummy_key = secrets.token_urlsafe(24)
    spans: queue.Queue[float] = queue.Queue()
    turn_spans: queue.Queue[tuple[int, int]] = queue.Queue()

    @override
    def do_POST(self) -> None:
        if self.path not in {"/v1/intent-route", "/v1/turns"}:
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
        result = (
            RESPONSE if self.path == "/v1/intent-route" else {"status": "completed", "reply": CHAT_REPLY, "actions": []}
        )
        self._reply(200, result)
        ended = time.perf_counter_ns()
        if self.path == "/v1/turns":
            self.turn_spans.put((started, ended))
        else:
            self.spans.put((ended - started) / 1_000_000)

    def _valid(self, body: object) -> bool:
        if not isinstance(body, dict) or not isinstance(body.get("provider"), dict):
            return False
        provider = body["provider"]
        if self.path == "/v1/turns":
            return (
                set(body) == {"thread_id", "team_name", "assistants", "provider", "message"}
                and isinstance(body["thread_id"], str)
                and bool(body["thread_id"])
                and body["team_name"] == "Demo Team"
                and body["assistants"] == []
                and body["message"] == CHAT_PROMPT
                and provider == {"provider": "openai", "model": "gpt-5.6-terra", "api_key": self.dummy_key}
                and self.headers.get("Authorization", "").startswith("Bearer ")
            )
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


def _chat_request(flow: flow_fixture.DockerFlow) -> tuple[str, bytes, dict[str, str]]:
    path = "/v1/teams/demo_team/chat"
    encoded = json.dumps(CHAT_PAYLOAD, separators=(",", ":")).encode()
    headers = {
        "Authorization": f"Bearer {flow.token}",
        "Content-Type": "application/json",
        "Connection": "close",
        "X-Shimpz-Model-Provider": "openai",
        "X-Shimpz-Model-Api-Key": BrainPeer.dummy_key,
    }
    headers[supervisor_contract.ASSERTION_HEADER] = flow_fixture.supervisor_header(flow, "POST", path, encoded, headers)
    return path, encoded, headers


def _read_chat(flow: flow_fixture.DockerFlow) -> tuple[int, int, int, tuple[int, ...]]:
    started = time.perf_counter_ns()
    first_progress: int | None = None
    phase_ms: list[int] = []
    events: list[tuple[str, str]] = []
    terminal_at: int | None = None
    stream_bytes = 0
    path, encoded, headers = _chat_request(flow)
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


def _sample_chat(flow: flow_fixture.DockerFlow, delay_ms: int) -> dict[str, float]:
    BrainPeer.delay_ms = delay_ms
    started, first_at, terminal_at, phase_ms = _read_chat(flow)
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


def _measure_chat(flow: flow_fixture.DockerFlow) -> dict[str, object]:
    warm = _sample_chat(flow, 0)
    _sample_chat(flow, 0)
    samples: dict[int, dict[str, list[float]]] = {delay: {name: [] for name in warm} for delay in PEER_DELAYS_MS}
    for index in range(SAMPLES):
        for delay in PEER_DELAYS_MS if index % 2 == 0 else tuple(reversed(PEER_DELAYS_MS)):
            for name, value in _sample_chat(flow, delay).items():
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
        "AssistantRegistry.get": int(installed),
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


def _chat_span_summary(records: list[dict[str, object]], installed: bool) -> dict[str, object]:
    expected = 2 + SAMPLES * len(PEER_DELAYS_MS)
    if len(records) != expected:
        raise MeasurementError("chat admission span count changed")
    samples = [_chat_span_sample(record, installed) for record in records[2:]]
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
    runner: DockerFlowTests, flow: flow_fixture.DockerFlow, *, chat_spans: bool, installed: bool
) -> dict[str, object]:
    before = len(_chat_span_records(runner, flow)) if chat_spans else 0
    measured = _measure_chat(flow)
    if chat_spans:
        measured["admission_spans"] = _chat_span_summary(_chat_span_records(runner, flow)[before:], installed)
        measured["host_list_probe"] = _host_list_probe(flow, installed)
    return measured


def _measure_chat_inventory(
    runner: DockerFlowTests, flow: flow_fixture.DockerFlow, *, chat_spans: bool
) -> dict[str, object]:
    none_initial = _measure_chat_block(runner, flow, chat_spans=chat_spans, installed=False)
    try:
        runner._exercise_assistant(flow)
    except AssertionError as exc:
        raise MeasurementError("reference Assistant installation contract failed") from exc
    _require_installed_assistant(runner, flow)
    one_unselected = _measure_chat_block(runner, flow, chat_spans=chat_spans, installed=True)
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
        "none_restored": none_restored,
    }


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


def _configured_runner() -> tuple[DockerFlowTests, bool]:
    if sys.argv[1:] not in ([], ["--chat-spans"]):
        raise SystemExit("usage: python -m perf.local_team_http [--chat-spans]")
    chat_spans = sys.argv[1:] == ["--chat-spans"]
    runner = DockerFlowTests("test_real_pull_isolation_lifecycle_and_space_reset")
    if chat_spans:
        runner.controller_extra_env = ("--env", "SHIMPZ_PERF_CHAT_SPANS=1")
    return runner, chat_spans


def main() -> int:
    runner, chat_spans = _configured_runner()
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
            samples = _measure(runner, flow)
            chat_inventory = _measure_chat_inventory(runner, flow, chat_spans=chat_spans)
            team_create = _measure_team_create(runner, flow)
            team_list = _measure_team_list(runner, flow)
            result["samples"] = samples
            result["chat_inventory"] = chat_inventory
            result["team_create"] = team_create
            result["team_list"] = team_list
            result.update(
                status="complete",
                scope=(
                    "authenticated Local Team HTTP for intent routing, Brain-only chat with 0/1/0 installed "
                    "reference Assistants, Team creation, and Team listing (1/9/33); the installed Assistant "
                    "runs but is never selected or exposed to the Brain; deterministic Brain peer requires an "
                    "empty Assistant tuple; one connection per sample; each chat arm reuses one Team and "
                    "stateless Brain peer across 2 warmups and 24 measured turns; the first post-install chat "
                    "is excluded from steady-state samples; "
                    "excludes Admin WebSocket, browser, and real provider"
                ),
                chat_timing_definition=(
                    "first progress and terminal include client Supervisor signing; unattributed time starts "
                    "after first progress and includes unspanned context checks, commit, transport, scheduling, "
                    "and up to 3 ms of phase truncation"
                ),
                chat_phase_resolution_ms=1,
                chat_span_mode=chat_spans,
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
