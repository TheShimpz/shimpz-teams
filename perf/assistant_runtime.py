"""Compare Assistant workload containers under runc and gVisor runsc with Team's exact isolation profile.

Run from the Teams checkout with ``uv run --frozen --python 3.14 python -m perf.assistant_runtime``.
It reads one already-present Assistant image (by default the staged Cloudflare Assistant) and never builds, tags,
pulls, or deletes an image. Every container and the one internal network it creates carry a per-run label and are
removed before it exits; no Team-owned label, container, or network is touched. Each container is created with
the kwargs of ``AssistantLifecycle._create_assistant_container`` plus only ``runtime``, then admitted by Team's own
``isolation.inspect_profile``. Every Action invocation runs through Team's ``rpc_exchange`` with a broker that
answers the Action's provider call with one canned Cloudflare zones page, so no network or credential is used.
Only timings, memory counts, and enforcement probe outcomes are printed.
"""

import argparse
import base64
import concurrent.futures
import json
import secrets
import statistics
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import docker
from docker.errors import DockerException, NotFound
from docker.types import LogConfig, Ulimit

from action import execution as action_execution
from local.assistant import isolation
from local.assistant.rpc import ASSISTANT_WORKDIR
from local.labels import IMAGE_LABEL
from local.validation import half_cpu_set
from perf.percentiles import median_p95

DEFAULT_IMAGE = "shimpz-local/shimpz-cloudflare:staged"
RUNTIMES = ("runc", "runsc")
ACTION_ID = "list-zones"
ACTION_INPUT = {"page": 1, "per_page": 5}
COLD_SAMPLES = 12
WARM_SAMPLES = 240
PERF_LABEL = "com.shimpz.perf.assistant-runtime"
READY_TIMEOUT_SECONDS = 15
ZONE = {
    "id": "f" * 32,
    "name": "example.com",
    "status": "active",
    "type": "full",
    "paused": False,
    "account": {"id": "e" * 32, "name": "Example"},
}
ZONES_PAGE = {
    "success": True,
    "errors": [],
    "messages": [],
    "result": [ZONE],
    "result_info": {"page": 1, "per_page": 5, "count": 1, "total_count": 1, "total_pages": 1},
}
EXPECTED_RESULT = {
    "type": "result",
    "result": {"zones": [ZONE], "pagination": ZONES_PAGE["result_info"]},
}
# Run inside the workload as the Assistant uid; each probe reports an errno (0 = the operation was allowed).
PROBE = r"""
import ctypes, json, os, resource, socket, subprocess
def status(field):
    for line in open("/proc/self/status"):
        if line.startswith(field + ":"):
            return line.split(":", 1)[1].strip()
def outcome(operation):
    try:
        operation()
    except OSError as exc:
        return exc.errno or -1
    return 0
def write(path):
    with open(path, "w") as handle:
        handle.write("x")
def execute():
    path = "/tmp/probe-exec"
    with open(path, "w") as handle:
        handle.write("#!/opt/shimpz/runtime/bin/python3.14\n")
    os.chmod(path, 0o755)
    subprocess.run([path], check=False, timeout=5)
def raw_socket():
    socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP).close()
def egress():
    socket.create_connection(("1.1.1.1", 443), timeout=2).close()
libc = ctypes.CDLL(None, use_errno=True)
print(json.dumps({
    "uid_gid": [os.getuid(), os.getgid()],
    "cap_eff": status("CapEff"),
    "no_new_privs": libc.prctl(39, 0, 0, 0, 0),
    "seccomp_mode": libc.prctl(21, 0, 0, 0, 0),
    "kernel_release": os.uname().release,
    "nofile": list(resource.getrlimit(resource.RLIMIT_NOFILE)),
    "rootfs_write_errno": outcome(lambda: write("/opt/shimpz/probe")),
    "tmp_write_errno": outcome(lambda: write("/tmp/probe")),
    "tmp_exec_errno": outcome(execute),
    "raw_socket_errno": outcome(raw_socket),
    "egress_errno": outcome(egress),
}, sort_keys=True))
"""
# Touches every page of 192 MiB, above the 128 MiB limit.
MEMORY_PROBE = 'data = b"\\x01" * (192 << 20); print(len(data))'
# Forks sleeping children until the pids limit refuses one, then reaps them; each line reports progress.
FORK_PROBE = r"""
import os, time
children = []
try:
    for _ in range(100):
        pid = os.fork()
        if pid == 0:
            time.sleep(3)
            os._exit(0)
        children.append(pid)
        print(len(children), flush=True)
except OSError as exc:
    print("refused", exc.errno, flush=True)
for pid in children:
    os.kill(pid, 9)
    os.waitpid(pid, 0)
print("reaped", len(children), flush=True)
"""
# Starts sleeping threads until the pids limit refuses one, then joins them; each line reports progress.
THREAD_PROBE = r"""
import threading, time
threads = []
try:
    for _ in range(100):
        thread = threading.Thread(target=time.sleep, args=(3,))
        thread.start()
        threads.append(thread)
        print(len(threads), flush=True)
except RuntimeError as exc:
    print("refused", flush=True)
for thread in threads:
    thread.join()
print("joined", len(threads), flush=True)
"""
PARALLEL_INVOCATIONS = 2
PYTHON = "/opt/shimpz/runtime/bin/python3.14"


class MeasurementError(RuntimeError):
    """The workload did not behave as Team requires, so the run measured something else."""


@dataclass(frozen=True, slots=True)
class Bench:
    client: docker.DockerClient
    image_id: str
    network: str
    cpuset: str
    run_id: str
    cold_samples: int
    warm_samples: int


class CannedCloudflare:
    """Answer the Action's one zones call with a fixed page, refusing any other frame."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, frame: object, _deadline: float) -> bytes:
        self.calls += 1
        if (
            not isinstance(frame, dict)
            or frame.get("method") != "GET"
            or not str(frame.get("url", "")).startswith("https://api.cloudflare.com/client/v4/zones?")
        ):
            raise ValueError("unexpected provider call")
        body = base64.b64encode(json.dumps(ZONES_PAGE).encode()).decode("ascii")
        reply = {"status": 200, "headers": [["content-type", "application/json"]], "body": body}
        return json.dumps(reply, separators=(",", ":")).encode("ascii")

    def release(self) -> None:
        """Hold nothing between calls."""


def _container_kwargs(bench: Bench, runtime: str, name: str) -> dict[str, object]:
    """Team's Assistant create kwargs verbatim, with this run's labels and the runtime under comparison."""
    return {
        "image": bench.image_id,
        "name": name,
        "command": None,
        "detach": True,
        "user": action_execution.ASSISTANT_RPC_USER,
        "network": bench.network,
        "labels": {PERF_LABEL: bench.run_id, IMAGE_LABEL: bench.image_id},
        "environment": {
            "SHIMPZ_ASSISTANT_ID": "perf-runtime",
            "SHIMPZ_TEAM_ID": "perf-runtime",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "privileged": False,
        "ipc_mode": "private",
        "cgroupns": "private",
        "mem_limit": isolation.ASSISTANT_MEMORY,
        "memswap_limit": isolation.ASSISTANT_MEMORY,
        "nano_cpus": isolation.ASSISTANT_NANO_CPUS,
        "cpuset_cpus": bench.cpuset,
        "pids_limit": isolation.ASSISTANT_PIDS,
        "tmpfs": isolation.ASSISTANT_TMPFS,
        "ulimits": [
            Ulimit(name="nofile", soft=isolation.ASSISTANT_NOFILE_LIMIT, hard=isolation.ASSISTANT_NOFILE_LIMIT)
        ],
        "restart_policy": {"Name": "no"},
        "log_config": LogConfig(type=LogConfig.types.NONE),
        "runtime": runtime,
    }


def _admit(bench: Bench, container, runtime: str) -> None:
    """Require Team's own isolation admission and the requested runtime, or the comparison is void."""
    container.reload()
    admitted = isolation.inspect_profile(
        container.attrs,
        container.name,
        {PERF_LABEL: bench.run_id},
        container.name,
        isolation.ImageIdentity(bench.image_id, "local"),
        bench.network,
        bench.cpuset,
    )
    if admitted is None or (container.attrs.get("HostConfig") or {}).get("Runtime") != runtime:
        raise MeasurementError("the container does not hold Team's isolation profile")


def _wait_running(container) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        container.reload()
        if container.status == "running":
            return
        if container.status != "created":
            break
        time.sleep(0.005)
    raise MeasurementError("the Assistant container did not become ready")


def _launch(bench: Bench, runtime: str, label: str) -> tuple[object, float, float]:
    """Create and start one admitted workload; return it with its create and start latencies in milliseconds."""
    name = f"shimpz-perf-runtime-{bench.run_id}-{runtime}-{label}"
    started = time.perf_counter()
    container = bench.client.containers.create(**_container_kwargs(bench, runtime, name))
    created = time.perf_counter()
    _admit(bench, container, runtime)
    admitted = time.perf_counter()
    container.start()
    _wait_running(container)
    running = time.perf_counter()
    return container, (created - started) * 1000, (running - admitted) * 1000


def _fail_stop() -> None:
    """Team would stop the workload here; the run reports the failure instead."""


def _invoke(bench: Bench, container) -> float:
    """Run one Action through Team's RPC exchange; return its latency, refusing any other outcome."""
    encoded = action_execution.encode_rpc_invocation(ACTION_INPUT, (), str(uuid.uuid4()))
    strategy = action_execution.RpcExchangeStrategy(
        api=bench.client.api,
        user=action_execution.ASSISTANT_RPC_USER,
        workdir=ASSISTANT_WORKDIR,
        timeout=action_execution.RPC_TIMEOUT_SECONDS,
        maximum=action_execution.MAX_RPC_RESPONSE_BYTES,
        transport_errors=(DockerException,),
        fail_stop=_fail_stop,
        cancelled=lambda _exc: None,
        close_stream=action_execution.close_exec_stream,
        broker=CannedCloudflare(),
    )
    started = time.perf_counter()
    try:
        response = action_execution.rpc_exchange(
            container.id, [action_execution.ACTION_COMMAND, ACTION_ID], encoded, strategy
        )
    except action_execution.RpcExchangeError as exc:
        raise MeasurementError(f"the Action failed: {exc.kind} {exc.condition}") from exc
    elapsed = (time.perf_counter() - started) * 1000
    if response != EXPECTED_RESULT:
        raise MeasurementError("the Action returned an unexpected result")
    return elapsed


def _remove(container) -> None:
    try:
        container.remove(force=True)
    except NotFound:
        return


def _cold(bench: Bench, runtime: str, index: int) -> dict[str, float]:
    container, create_ms, start_ms = _launch(bench, runtime, f"cold{index}")
    try:
        first_ms = _invoke(bench, container)
    finally:
        _remove(container)
    return {"create_ms": create_ms, "start_ms": start_ms, "first_invocation_ms": first_ms}


def _cgroup_dir(container) -> Path | None:
    """The host cgroup v2 directory of the container's first process, when the host exposes it."""
    container.reload()
    pid = (container.attrs.get("State") or {}).get("Pid")
    try:
        line = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii").strip()
    except OSError:
        return None
    return Path("/sys/fs/cgroup") / line.partition("::")[2].lstrip("/")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii").strip()
    except OSError:
        return None


def _rss_kib(pid: str) -> int:
    status = _read(Path(f"/proc/{pid}/status")) or ""
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


def _memory(container) -> dict[str, object]:
    """Host-side cgroup memory and limits, and the summed RSS of every host process in the container's cgroup."""
    directory = _cgroup_dir(container)
    if directory is None:
        return {"cgroup": None}
    procs = (_read(directory / "cgroup.procs") or "").split()
    current = _read(directory / "memory.current")
    peak = _read(directory / "memory.peak")
    return {
        "cgroup_processes": len(procs),
        "process_rss_mib": round(sum(_rss_kib(pid) for pid in procs) / 1024, 1),
        "memory_current_mib": round(int(current) / (1 << 20), 1) if current else None,
        "memory_peak_mib": round(int(peak) / (1 << 20), 1) if peak else None,
        "memory_max": _read(directory / "memory.max"),
        "pids_max": _read(directory / "pids.max"),
        "pids_current": _read(directory / "pids.current"),
        "cpuset": _read(directory / "cpuset.cpus"),
        "cpu_max": _read(directory / "cpu.max"),
    }


def _probe(container) -> dict[str, object]:
    result = container.exec_run(
        [PYTHON, "-c", PROBE], user=action_execution.ASSISTANT_RPC_USER, workdir=ASSISTANT_WORKDIR, demux=True
    )
    stdout, stderr = result.output
    if result.exit_code != 0:
        return {"exit_code": result.exit_code, "stderr": (stderr or b"").decode("ascii", "replace")[-400:]}
    return json.loads(stdout)


def _limit(bench: Bench, runtime: str, label: str, script: str) -> dict[str, object]:
    """Exceed one resource limit in a disposable workload and report how the runtime refused it."""
    container, _create_ms, _start_ms = _launch(bench, runtime, label)
    try:
        result = container.exec_run(
            [PYTHON, "-c", script], user=action_execution.ASSISTANT_RPC_USER, workdir=ASSISTANT_WORKDIR, demux=True
        )
        stdout, _stderr = result.output
        container.reload()
        state = container.attrs.get("State") or {}
        return {
            "exec_exit_code": result.exit_code,
            "last_stdout_line": ((stdout or b"").decode("ascii", "replace").strip().splitlines() or [""])[-1],
            "container_running_after": state.get("Running"),
            "oom_killed": state.get("OOMKilled"),
        }
    finally:
        _remove(container)


def _parallel(bench: Bench, container) -> dict[str, object]:
    """Run concurrent invocations on one workload, as Team's Docker call pool admits them."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL_INVOCATIONS) as pool:
        futures = [pool.submit(_invoke, bench, container) for _ in range(PARALLEL_INVOCATIONS)]
    failures = [str(future.exception()) for future in futures if future.exception() is not None]
    return {
        "concurrency": PARALLEL_INVOCATIONS,
        "succeeded": PARALLEL_INVOCATIONS - len(failures),
        "failures": failures,
    }


def _warm(bench: Bench, runtime: str) -> dict[str, object]:
    container, _create_ms, _start_ms = _launch(bench, runtime, "warm")
    try:
        idle = _memory(container)
        _invoke(bench, container)
        samples = [_invoke(bench, container) for _ in range(bench.warm_samples)]
        after = _memory(container)
        parallel = _parallel(bench, container)
        probe = _probe(container)
    finally:
        _remove(container)
    return {
        "warm_invocations": {**_summary(samples), "n": len(samples)},
        "memory_idle": idle,
        "memory_after_warm": after,
        "parallel": parallel,
        "enforcement": probe,
    }


def _summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        **median_p95(ordered),
        "p99_ms": round(ordered[max(0, round(len(ordered) * 0.99) - 1)], 3),
        "mean_ms": round(statistics.fmean(ordered), 3),
        "max_ms": round(ordered[-1], 3),
    }


def _cold_series(bench: Bench) -> dict[str, dict[str, object]]:
    """Interleave runtimes, alternating which goes first, so host drift lands on both equally."""
    rows: dict[str, list[dict[str, float]]] = {runtime: [] for runtime in RUNTIMES}
    for index in range(bench.cold_samples):
        order = RUNTIMES if index % 2 == 0 else tuple(reversed(RUNTIMES))
        for runtime in order:
            rows[runtime].append(_cold(bench, runtime, index))
    return {
        runtime: {
            key: {**_summary([row[key] for row in samples]), "n": len(samples)}
            for key in ("create_ms", "start_ms", "first_invocation_ms")
        }
        | {"create_start_ms": _summary([row["create_ms"] + row["start_ms"] for row in samples])}
        for runtime, samples in rows.items()
    }


def _runsc_platform(client: docker.DockerClient) -> str | None:
    runtime = (client.info().get("Runtimes") or {}).get("runsc")
    if runtime is None:
        raise MeasurementError("the Docker daemon has no runsc runtime")
    features = json.loads((runtime.get("status") or {}).get("org.opencontainers.runtime-spec.features", "{}"))
    return (features.get("annotations") or {}).get("dev.gvisor.flag.platform")


def _cleanup(client: docker.DockerClient, run_id: str) -> int:
    """Remove every container and network this run labelled; return how many remain."""
    selector = {"label": f"{PERF_LABEL}={run_id}"}
    for container in client.containers.list(all=True, filters=selector):
        _remove(container)
    for network in client.networks.list(filters=selector):
        network.remove()
    return len(client.containers.list(all=True, filters=selector)) + len(client.networks.list(filters=selector))


def run(image_ref: str, cold_samples: int, warm_samples: int) -> dict[str, object]:
    client = docker.from_env()
    run_id = secrets.token_hex(4)
    image = client.images.get(image_ref)
    report: dict[str, object] = {
        "image_id": image.id,
        "action": ACTION_ID,
        "runsc_platform": _runsc_platform(client),
    }
    network = client.networks.create(
        f"shimpz-perf-runtime-{run_id}",
        driver="bridge",
        internal=True,
        attachable=False,
        check_duplicate=True,
        labels={PERF_LABEL: run_id},
    )
    try:
        cpuset = half_cpu_set(client.info().get("NCPU"))
        bench = Bench(client, image.id, network.name, cpuset, run_id, cold_samples, warm_samples)
        report["cold"] = _cold_series(bench)
        report["warm"] = {runtime: _warm(bench, runtime) for runtime in RUNTIMES}
        report["memory_limit"] = {runtime: _limit(bench, runtime, "oom", MEMORY_PROBE) for runtime in RUNTIMES}
        report["pids_limit_processes"] = {runtime: _limit(bench, runtime, "pids", FORK_PROBE) for runtime in RUNTIMES}
        report["pids_limit_threads"] = {
            runtime: _limit(bench, runtime, "threads", THREAD_PROBE) for runtime in RUNTIMES
        }
    finally:
        report["residue"] = _cleanup(client, run_id)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="an already-present Assistant image reference")
    parser.add_argument("--cold-samples", type=int, default=COLD_SAMPLES)
    parser.add_argument("--warm-samples", type=int, default=WARM_SAMPLES)
    args = parser.parse_args()
    print(json.dumps(run(args.image, args.cold_samples, args.warm_samples), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
