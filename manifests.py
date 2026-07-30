"""Turn a validated team id into docker-py kwargs for an isolated Team anchor.

The ONE place that decides what a Team container actually gets. Every security-relevant field
(security_opt, network, mounts, limits, host/browser access OFF) is a hardcoded constant here; the caller
never carries any of them, so there is nothing to override. A Team is a `shimpz-brain` with:
its OWN internal network and resource envelope, but no provider runtime, credential, Docker socket,
filesystem, browser, or application authority. Inference runs in the separate LangGraph service.
"""

from __future__ import annotations

import io
import os
import re
import tarfile
from pathlib import PurePosixPath

import docker
import docker.types

from assistant_human.assistant_registry import AssistantSpec
from container_policy import network as network_policy
from power import execution as power_execution

# Multi-instance (R137): SHIMPZ_SUFFIX names this Space's resources; empty (the default) is prod.
SUFFIX = os.environ.get("SHIMPZ_SUFFIX", "")
DEFAULT_TEAM_IMAGE = (
    "registry.k8s.io/pause:3.10.1@sha256:278fb9dbcca9518083ad1e11276933a2e96f23de604a3a08cc3c80002767d24c"
)
IMAGE = os.environ.get("SHIMPZ_TEAM_IMAGE", DEFAULT_TEAM_IMAGE)
# Hostile-tenant Teams are unconditionally locked to gVisor. This is deliberately not an
# environment setting: Docker rejects create when runsc is unavailable, and Team refuses
# lifecycle mutations until the daemon registry preserves its exact handler path, built-in security
# defaults, and every existing workload proves this exact runtime.
RUNTIME = "runsc"
RUNTIME_PATH = network_policy.TEAM_RUNTIME_PATH
CONTAINER_TMP = str(PurePosixPath("/") / "tmp")

# The lifecycle identity is intentionally not a model provider. Keeping this small trusted registry
# lets existing isolation code resolve the exact immutable image while provider/model live elsewhere.
BRAINS: dict[str, dict[str, str]] = {
    "runtime": {
        "image": IMAGE,
        "title": "Team runtime",
        "default_model": "",
    },
}
DEFAULT_BRAIN = "runtime"


def build_inbox_tar(filename: str, data: bytes) -> bytes:
    """A single-file tar for put_archive into the team's workspace inbox.

    Owned by the runtime user (uid/gid 1000 = abc) so the brain can read AND clean it up.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=filename)
        info.size = len(data)
        info.mode = 0o644
        info.uid = info.gid = 1000
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# Shared-service identities are suffix-aware. PostgreSQL and installed Assistants live on the Team core
# network; inference egress belongs to the separate shared LangGraph runtime.
POSTGRES_CONTAINER = network_policy.POSTGRES_CONTAINER

TEAM_PREFIX = network_policy.TEAM_PREFIX
NET_PREFIX = network_policy.CORE_NETWORK_PREFIX

# Per-team envelope. The hard cap is charged in full against team's global/owner
# admission budget before Docker provisioning begins; the lower cgroup reservation is only runtime
# reclaim protection, never the capacity-accounting unit. cgroup v2: mem_reservation ≈ memory.low,
# mem_limit ≈ memory.max.
MEM_LIMIT = os.environ.get("SHIMPZ_TEAM_MEM_LIMIT", "64m")
MEM_RESERVATION = os.environ.get("SHIMPZ_TEAM_MEM_RESERVATION", "16m")
NANO_CPUS = int(os.environ.get("SHIMPZ_TEAM_NANO_CPUS", str(100_000_000)))
# runsc needs roughly 100 host tasks to establish even the inert pause sandbox; 32/64/96 fail
# before the Team process starts. 128 is the measured minimum and remains a tight hard ceiling.
PIDS_LIMIT = int(os.environ.get("SHIMPZ_TEAM_PIDS_LIMIT", "128"))


hard_memory_bytes = network_policy.hard_memory_bytes
MEM_LIMIT_BYTES = network_policy.BRAIN_MEMORY_BYTES

MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def model_for_brain(brain: str, value: object = None) -> str:
    """Return one Team's validated provider model, or that provider's explicit default."""
    if brain not in BRAINS:
        raise ValueError(f"unsupported brain: {brain!r}")
    if value is None or value == "":
        return BRAINS[brain]["default_model"]
    if not isinstance(value, str):
        raise ValueError("model must be a string")
    model = value.strip()
    if not model:
        return BRAINS[brain]["default_model"]
    if MODEL_RE.fullmatch(model) is None:
        raise ValueError("model must be 1-128 safe identifier characters")
    return model


# Per-Team Assistant envelope. One installed Assistant is one hostile container inside its Team network.
ASSISTANT_MEM_LIMIT = os.environ.get("SHIMPZ_TEAM_ASSISTANT_MEM_LIMIT", "1g")
ASSISTANT_NANO_CPUS = int(os.environ.get("SHIMPZ_TEAM_ASSISTANT_NANO_CPUS", str(500_000_000)))
ASSISTANT_PIDS_LIMIT = int(os.environ.get("SHIMPZ_TEAM_ASSISTANT_PIDS_LIMIT", "256"))
ASSISTANT_MEM_LIMIT_BYTES = network_policy.ASSISTANT_MEMORY_BYTES
# The many-tenant egress proxy is attached only when an installed Assistant declares egress.
APP_EGRESS_CONTAINER = network_policy.APP_EGRESS_CONTAINER

# Keep Docker's json-file logs bounded: a hostile workload could otherwise fill the host filesystem
# without exceeding its cgroup memory or PID admission envelope.
TEAM_LOG_MAX_SIZE = network_policy.TEAM_LOG_MAX_SIZE
TEAM_LOG_MAX_FILE = network_policy.TEAM_LOG_MAX_FILE
TEAM_LOG_CONFIG = docker.types.LogConfig(
    type=docker.types.LogConfig.types.JSON,
    config={
        "labels": "team.id",
        "max-size": network_policy.TEAM_LOG_MAX_SIZE,
        "max-file": network_policy.TEAM_LOG_MAX_FILE,
    },
)


def team_container_name(team_id: str) -> str:
    return network_policy.team_container_name(team_id)


def team_network_name(team_id: str) -> str:
    return network_policy.network_name(team_id, network_policy.CORE_KIND)


def team_network_labels(team_id: str, kind: str) -> dict[str, str]:
    return network_policy.network_labels(team_id, kind)


def team_config_volume(team_id: str) -> str:
    return network_policy.volume_name(team_id, network_policy.CONFIG_VOLUME_KIND)


def team_workspace_volume(team_id: str) -> str:
    return network_policy.volume_name(team_id, network_policy.WORKSPACE_VOLUME_KIND)


def team_db_project(team_id: str) -> str:
    return f"team_{team_id}"


def team_assistant_container_name(team_id: str, assistant_id: str) -> str:
    return network_policy.team_assistant_container_name(team_id, assistant_id)


def core_deps() -> list[tuple[str, list[str]]]:
    """Shared services allowed on a Team's app/data plane."""
    return [(POSTGRES_CONTAINER, ["postgres"])]


def build_team_kwargs(
    team_id: str,
    team_name: str,
    *,
    database_url: str,
    owner: str = "",
    brain: str = DEFAULT_BRAIN,
    model: object = None,
) -> dict:
    """Kwargs for docker-py's low-level `containers.create` — never `run`.

    `run` would risk an accidental host-port publish or default-network attach; the whole isolation
    model depends on create + one explicit network. `brain` picks the agent runtime image from the
    trusted BRAINS registry (validated by the caller) and is recorded as the team.brain label.
    """
    selected_model = model_for_brain(brain, model)
    env = {
        "SHIMPZ_TEAM_ID": team_id,
        "SHIMPZ_TEAM_NAME": team_name,
    }
    return {
        "image": BRAINS[brain]["image"],
        "name": team_container_name(team_id),
        "hostname": team_id,
        "runtime": RUNTIME,
        "environment": env,
        "security_opt": ["no-new-privileges:true", "apparmor=docker-default"],
        "privileged": False,
        "read_only": True,
        "ipc_mode": "private",
        "cgroupns": "private",
        "cap_drop": ["ALL"],
        "cap_add": [],
        # The anchor and installed Assistants share only the Team's internal core network.
        "network": team_network_name(team_id),
        "mounts": [],
        "tmpfs": {CONTAINER_TMP: "size=16m,mode=1777"},
        "mem_limit": MEM_LIMIT,
        # Equal memory and memory+swap ceilings disable swap for this hostile workload. Leaving
        # MemorySwap unset lets Docker grant an additional swap allowance on swap-enabled hosts.
        "memswap_limit": MEM_LIMIT,
        "mem_reservation": MEM_RESERVATION,
        "nano_cpus": NANO_CPUS,
        "pids_limit": PIDS_LIMIT,
        "ulimits": [docker.types.Ulimit(name="nofile", soft=256, hard=256)],
        # Hostile workloads may only become runnable through Team's static+live proof. Docker
        # daemon startup or a natural process crash must never auto-start them behind that gate.
        "restart_policy": {"Name": "no"},
        "labels": {
            "team.runtime": "1",
            "team.id": team_id,
            "team.name": team_name,
            "team.owner": owner,
            "team.brain": brain,
            "team.model": selected_model,
        },
        "log_config": TEAM_LOG_CONFIG,
        "detach": True,
    }


def build_assistant_kwargs(
    team_id: str,
    assistant_id: str,
    spec: AssistantSpec,
    *,
    proxy_env: dict[str, str] | None = None,
    owner: str,
    source_digest: str,
) -> dict:
    """Exact direct-v1 runtime envelope for one digest-bound published Assistant."""
    return {
        "image": spec.image,
        "name": team_assistant_container_name(team_id, assistant_id),
        "runtime": RUNTIME,
        "environment": {
            "SHIMPZ_TEAM_ID": team_id,
            "SHIMPZ_ASSISTANT_ID": assistant_id,
            **(proxy_env or {}),
        },
        "user": power_execution.ASSISTANT_RPC_USER,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true", "apparmor=docker-default"],
        "privileged": False,
        "ipc_mode": "private",
        "cgroupns": "private",
        "network": team_network_name(team_id),
        "read_only": True,
        "tmpfs": {CONTAINER_TMP: "size=64m,mode=1777"},
        "mem_limit": ASSISTANT_MEM_LIMIT,
        "memswap_limit": ASSISTANT_MEM_LIMIT,
        "nano_cpus": ASSISTANT_NANO_CPUS,
        "pids_limit": ASSISTANT_PIDS_LIMIT,
        "ulimits": [docker.types.Ulimit(name="nofile", soft=4096, hard=4096)],
        "restart_policy": {"Name": "no"},
        "labels": {
            "team.assistant.runtime": "1",
            "team.assistant.dynamic": "1",
            "team.id": team_id,
            "team.assistant": assistant_id,
            "team.owner": owner,
            "team.assistant.source": source_digest,
            "team.assistant.image": spec.image,
        },
        "log_config": TEAM_LOG_CONFIG,
        "detach": True,
    }
