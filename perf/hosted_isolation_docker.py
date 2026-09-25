"""Measure the Hosted chat isolation walk against a disposable real-Docker graph.

Run from the Teams checkout with ``sg docker -c 'uv run --frozen --python 3.14
python -m perf.hosted_isolation_docker'`` when the current shell lacks the
Docker group. The inert workloads use the production security and resource
envelopes. No model provider, publication, or Assistant RPC is involved.
"""

from __future__ import annotations

import argparse
import grp
import json
import math
import os
import statistics
import tempfile
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import docker
from docker.errors import DockerException, NotFound

SAMPLES = 20
WARMUPS = 3
CASES = (1, 4, 16)
RUN_LABEL = "shimpz.perf.run"
API_OPERATIONS = ("inspect_container", "inspect_image", "inspect_network")


class MeasurementError(RuntimeError):
    """The disposable graph or observed call law was not the intended workload."""


class CleanupError(MeasurementError):
    """Owned benchmark resources remain or their absence cannot be proved."""

    def __init__(self, run_id: str, issues: list[str]) -> None:
        super().__init__("benchmark cleanup is incomplete")
        self.run_id = run_id
        self.issues = tuple(sorted(set(issues)))


def _private_environment(root: Path, run_id: str) -> None:
    os.environ["SHIMPZ_SUFFIX"] = f"-perf-{run_id}"
    os.environ["SHIMPZ_TEAM_TOKEN_GROUP"] = grp.getgrgid(os.getegid()).gr_name
    paths = {
        "SHIMPZ_TEAM_TOKEN_FILE": "token/value",
        "SHIMPZ_TEAM_ACTION_JOURNAL_PATH": "journal/journal.sqlite3",
        "SHIMPZ_TEAM_ASSISTANT_INTEGRATION_STATE_PATH": "integrations/state/data.json",
        "SHIMPZ_TEAM_ASSISTANT_INTEGRATION_KEY_PATH": "integrations/key/aes256.key",
        "SHIMPZ_TEAM_ASSISTANT_STORED_INPUT_STATE_PATH": "stored-inputs/state/data.json",
        "SHIMPZ_TEAM_ASSISTANT_STORED_INPUT_KEY_PATH": "stored-inputs/key/aes256.key",
        "SHIMPZ_TEAM_DYNAMIC_ASSISTANT_PATH": "bindings/data.json",
        "SHIMPZ_TEAM_COSIGN_TRUST_ROOT": "cosign",
        "SHIMPZ_TEAM_INFERENCE_DIR": "inference",
        "SHIMPZ_ASSISTANT_EGRESS_POLICY_DIR": "egress",
    }
    for name, relative in paths.items():
        os.environ[name] = str(root / relative)


def _percentiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
    }


def _timed(operation) -> tuple[float, float]:
    wall_start = time.perf_counter_ns()
    cpu_start = time.process_time_ns()
    operation()
    return (time.perf_counter_ns() - wall_start) / 1_000_000, (time.process_time_ns() - cpu_start) / 1_000_000


class _ApiRecorder:
    def __init__(self, client: docker.DockerClient, anchor_ref: str, assistant_refs: frozenset[str]) -> None:
        self.client = client
        self.anchor_ref = anchor_ref
        self.assistant_refs = assistant_refs
        self.spans: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self.image_spans: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self.requests = 0

    @contextmanager
    def recording(self):
        originals = {name: getattr(self.client.api, name) for name in API_OPERATIONS}
        original_request = self.client.api.request

        def counted_request(*args, **kwargs):
            self.requests += 1
            return original_request(*args, **kwargs)

        try:
            self.client.api.request = counted_request
            for name, operation in originals.items():
                setattr(self.client.api, name, self._wrap(name, operation))
            yield
        finally:
            for name, operation in originals.items():
                setattr(self.client.api, name, operation)
            self.client.api.request = original_request

    def _wrap(self, name, operation):
        def recorded(*args, **kwargs):
            return self._record(name, operation, *args, **kwargs)

        return recorded

    def _record(self, name, operation, *args, **kwargs):
        image_kind = None
        if name == "inspect_image":
            image_ref = args[0]
            if image_ref == self.anchor_ref:
                image_kind = "anchor_digest"
            elif image_ref in self.assistant_refs:
                image_kind = "assistant_tag"
            else:
                raise MeasurementError("Hosted isolation inspected an unexpected image reference")
        wall_start = time.perf_counter_ns()
        cpu_start = time.process_time_ns()
        try:
            return operation(*args, **kwargs)
        finally:
            elapsed = (
                (time.perf_counter_ns() - wall_start) / 1_000_000,
                (time.process_time_ns() - cpu_start) / 1_000_000,
            )
            self.spans[name].append(elapsed)
            if image_kind is not None:
                self.image_spans[image_kind].append(elapsed)


class _Probe:
    def __init__(self, client, container_spec, policy, resources, run_id: str) -> None:
        self.client = client
        self.container_spec = container_spec
        self.policy = policy
        self.resources = resources
        self.run_id = run_id
        self.team_id = f"bench_{run_id}"
        self.network_name = policy.network_name(self.team_id, policy.CORE_KIND)
        self.container_names: list[str] = []
        self.tags: list[str] = []
        self.assistants: list[tuple[str, SimpleNamespace]] = []
        self.base_image = None

    def preflight(self) -> None:
        info = self.client.info()
        if not self.policy.daemon_runtime_registration_valid(
            info, self.container_spec.RUNTIME, self.container_spec.RUNTIME_PATH
        ) or not self.policy.daemon_security_options_valid(info):
            raise MeasurementError("required Docker isolation posture is unavailable")
        running_teams = self.client.containers.list(all=False, filters={"label": "team.runtime=1"})
        team_controllers = self.client.containers.list(all=False, filters={"label": "com.docker.compose.service=team"})
        test_controllers = self.client.containers.list(all=False, filters={"name": "hosted-controller"})
        if running_teams or team_controllers or test_controllers:
            raise MeasurementError("another Team workload or Controller is running on this host")
        self.base_image = self.client.images.get(self.container_spec.IMAGE)

    def _owned_labels(self, labels: dict[str, str]) -> dict[str, str]:
        return {**labels, RUN_LABEL: self.run_id}

    def _create_container(self, name: str, kwargs: dict):
        self.container_names.append(name)
        return self.client.containers.create(**kwargs)

    def create_base(self) -> None:
        labels = self._owned_labels(self.policy.network_labels(self.team_id, self.policy.CORE_KIND))
        self.client.networks.create(
            self.network_name,
            driver="bridge",
            internal=True,
            attachable=False,
            labels=labels,
        )
        network = self.client.networks.get(self.network_name)
        postgres_name = self.policy.POSTGRES_CONTAINER
        # These limits bound the inert role stand-in; they are not the PostgreSQL Service envelope.
        postgres = self._create_container(
            postgres_name,
            {
                "image": self.container_spec.IMAGE,
                "name": postgres_name,
                "network": self.network_name,
                "labels": self._owned_labels(self.policy.shared_service_labels(self.policy.POSTGRES_ROLE)),
                "nano_cpus": 100_000_000,
                "mem_limit": "64m",
                "memswap_limit": "64m",
                "pids_limit": 128,
                "read_only": True,
                "detach": True,
            },
        )
        network.disconnect(postgres)
        network.connect(postgres, aliases=["postgres"])
        postgres.start()
        anchor_kwargs = self.container_spec.build_team_kwargs(self.team_id, "Benchmark", owner="benchmark")
        anchor_kwargs["labels"] = self._owned_labels(anchor_kwargs["labels"])
        anchor = self._create_container(self.container_spec.team_container_name(self.team_id), anchor_kwargs)
        anchor.start()

    def add_assistant(self, index: int) -> None:
        assistant_id = f"helper-{index}"
        tag = f"shimpz-hosted-perf-{self.run_id}:assistant-{index}"
        try:
            self.client.images.get(tag)
        except NotFound:
            pass
        else:
            raise MeasurementError("disposable Assistant image reference already exists")
        self.tags.append(tag)
        repository, version = tag.split(":", 1)
        if self.base_image is None or not self.base_image.tag(repository, version):
            raise MeasurementError("disposable Assistant image could not be tagged")
        spec = SimpleNamespace(image=tag)
        kwargs = self.container_spec.build_assistant_kwargs(
            self.team_id,
            assistant_id,
            spec,
            owner="benchmark",
            source_digest="sha256:" + "a" * 64,
        )
        kwargs["labels"] = self._owned_labels(kwargs["labels"])
        name = self.container_spec.team_assistant_container_name(self.team_id, assistant_id)
        container = self._create_container(name, kwargs)
        network = self.client.networks.get(self.network_name)
        network.disconnect(container)
        network.connect(container, aliases=[assistant_id, f"{assistant_id}.team"])
        container.start()
        self.assistants.append((assistant_id, spec))

    def walk(self) -> None:
        memo: dict[str, object] = {}
        anchor = self.resources._get_container(self.container_spec.team_container_name(self.team_id))
        if anchor is None:
            raise MeasurementError("disposable Team anchor disappeared")
        self.resources._require_running_team_isolation(anchor, memo, refreshed=True)
        for assistant_id, spec in self.assistants:
            name = self.container_spec.team_assistant_container_name(self.team_id, assistant_id)
            assistant = self.resources._get_container(name)
            if assistant is None:
                raise MeasurementError("disposable Assistant disappeared")
            self.resources._require_running_team_isolation(assistant, memo, refreshed=True, workload_spec=spec)

    def _remove_container(self, name: str, errors: list[str]) -> None:
        try:
            container = self.client.containers.get(name)
        except NotFound:
            return
        except DockerException:
            errors.append("container lookup failed")
            return
        if (container.labels or {}).get(RUN_LABEL) != self.run_id:
            errors.append("container ownership changed")
            return
        try:
            container.remove(force=True)
            self.client.containers.get(name)
        except NotFound:
            return
        except DockerException:
            errors.append("container removal could not be proved")
        else:
            errors.append("owned container remains")

    def _remove_network(self, errors: list[str]) -> None:
        try:
            network = self.client.networks.get(self.network_name)
        except NotFound:
            return
        except DockerException:
            errors.append("network lookup failed")
            return
        if network.attrs.get("Labels", {}).get(RUN_LABEL) != self.run_id:
            errors.append("network ownership changed")
            return
        try:
            network.remove()
            self.client.networks.get(self.network_name)
        except NotFound:
            return
        except DockerException:
            errors.append("network removal could not be proved")
        else:
            errors.append("owned network remains")

    def _remove_tag(self, tag: str, errors: list[str]) -> None:
        try:
            image = self.client.images.get(tag)
        except NotFound:
            return
        except DockerException:
            errors.append("image reference lookup failed")
            return
        if self.base_image is None or image.id != self.base_image.id:
            errors.append("image reference ownership changed")
            return
        try:
            self.client.images.remove(tag, noprune=True)
            self.client.images.get(tag)
        except NotFound:
            return
        except DockerException:
            errors.append("image reference removal could not be proved")
        else:
            errors.append("owned image reference remains")

    def cleanup(self) -> None:
        errors: list[str] = []
        for name in reversed(self.container_names):
            self._remove_container(name, errors)
        self._remove_network(errors)
        for tag in reversed(self.tags):
            self._remove_tag(tag, errors)
        if self.base_image is not None:
            try:
                current = self.client.images.get(self.container_spec.IMAGE)
            except DockerException:
                errors.append("base image reference could not be proved")
            else:
                if current.id != self.base_image.id:
                    errors.append("base image reference changed")
        if errors:
            raise CleanupError(self.run_id, errors)


def _expected_calls(assistants: int) -> dict[str, int]:
    return {
        "inspect_container": 2 * assistants + 3,
        "inspect_image": assistants + 1,
        "inspect_network": 2,
    }


def _measure_case(probe: _Probe) -> dict[str, object]:
    assistants = len(probe.assistants)
    first_wall, first_cpu = _timed(probe.walk)
    for _ in range(WARMUPS):
        probe.walk()
    raw: dict[str, list[float]] = {"wall": [], "cpu": []}
    api: dict[str, dict[str, list[float]]] = {
        name: {"wall": [], "cpu": [], "per_check_wall": [], "per_check_cpu": []} for name in API_OPERATIONS
    }
    image_refs: dict[str, dict[str, list[float]]] = {
        name: {"wall": [], "cpu": []} for name in ("anchor_digest", "assistant_tag")
    }
    for sample in range(SAMPLES):
        recorder = _ApiRecorder(probe.client, probe.container_spec.IMAGE, frozenset(probe.tags))
        operations = ("raw", "api") if sample % 2 else ("api", "raw")
        for operation in operations:
            if operation == "raw":
                wall, cpu = _timed(probe.walk)
                raw["wall"].append(wall)
                raw["cpu"].append(cpu)
            else:
                with recorder.recording():
                    probe.walk()
        counts = {name: len(spans) for name, spans in recorder.spans.items()}
        expected = _expected_calls(assistants)
        if (
            counts != expected
            or recorder.requests != sum(expected.values())
            or len(recorder.image_spans["anchor_digest"]) != 1
            or len(recorder.image_spans["assistant_tag"]) != assistants
        ):
            raise MeasurementError("Hosted isolation Docker operation law changed")
        for name, spans in recorder.spans.items():
            api[name]["wall"].extend(item[0] for item in spans)
            api[name]["cpu"].extend(item[1] for item in spans)
            api[name]["per_check_wall"].append(sum(item[0] for item in spans))
            api[name]["per_check_cpu"].append(sum(item[1] for item in spans))
        for name, spans in recorder.image_spans.items():
            image_refs[name]["wall"].extend(item[0] for item in spans)
            image_refs[name]["cpu"].extend(item[1] for item in spans)
    return {
        "assistants": assistants,
        "network_members": assistants + 2,
        "first_walk_after_growth_ms": {"wall": round(first_wall, 3), "cpu": round(first_cpu, 3)},
        "samples": SAMPLES,
        "warmups": WARMUPS,
        "calls_per_check": _expected_calls(assistants),
        "raw_total_ms": {name: _percentiles(values) for name, values in raw.items()},
        "docker_api_ms": {
            name: {metric: _percentiles(values) for metric, values in timings.items()} for name, timings in api.items()
        },
        "image_ref_ms": {
            name: {
                "observations": len(timings["wall"]),
                **{metric: _percentiles(values) for metric, values in timings.items()},
            }
            for name, timings in image_refs.items()
        },
    }


def _run(max_assistants: int) -> dict[str, object]:
    run_id = uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="shimpz-hosted-perf-") as directory:
        _private_environment(Path(directory), run_id)
        from core.container import network as policy
        from hosted import container as container_spec
        from hosted import state as runtime_state
        from hosted.team import resources

        client = runtime_state._docker
        probe = _Probe(client, container_spec, policy, resources, run_id)
        try:
            probe.preflight()
            probe.create_base()
            cases = []
            for index in range(max_assistants):
                probe.add_assistant(index)
                if index + 1 in CASES:
                    cases.append(_measure_case(probe))
            info = client.info()
            return {
                "status": "complete",
                "run_id": run_id,
                "docker_version": client.version().get("Version"),
                "host_cpus": info.get("NCPU"),
                "host_memory_gib": round(info.get("MemTotal", 0) / 1024**3, 1),
                "process_cpus": os.process_cpu_count(),
                "limits": {
                    "runtime_nano_cpus": container_spec.NANO_CPUS,
                    "runtime_memory_bytes": container_spec.MEM_LIMIT_BYTES,
                    "runtime_pids": container_spec.PIDS_LIMIT,
                    "assistant_nano_cpus": container_spec.ASSISTANT_NANO_CPUS,
                    "assistant_memory_bytes": container_spec.ASSISTANT_MEM_LIMIT_BYTES,
                    "assistant_pids": container_spec.ASSISTANT_PIDS_LIMIT,
                },
                "cases": cases,
                "scope": (
                    "host-process isolation walk with real Docker; inert postgres and Assistant workloads; "
                    "distinct local tags on one image; no egress proxy, HTTP, credential, provider, "
                    "publication verification, or Action RPC"
                ),
            }
        finally:
            try:
                probe.cleanup()
            finally:
                client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-assistants", type=int, choices=CASES, default=16)
    args = parser.parse_args()
    try:
        result = _run(args.max_assistants)
    except CleanupError as exc:
        print(
            json.dumps(
                {
                    "status": "residue_or_unknown",
                    "run_id": exc.run_id,
                    "issues": exc.issues,
                    "next_action": "inspect resources with the exact shimpz.perf.run label before any retry",
                }
            )
        )
        return 1
    except MeasurementError as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "reason": str(exc)}))
        return 1
    except (AttributeError, DockerException, LookupError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
