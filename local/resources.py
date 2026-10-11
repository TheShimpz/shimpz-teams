"""The Space's read-only resource signal for the Supervisor: running services, memory, and its storage filesystem.

Every value is measured now and never estimated: an unknown pair is null, never zero, and a service is counted only
while Docker reports it running (never "healthy"). One budget bounds every Docker call of a measurement.
"""

import json
import os
import time

from docker.errors import DockerException

from local.assistant.egress import PROFILE
from local.errors import docker_unavailable
from local.labels import KIND_LABEL, MANAGED_LABEL, PROFILE_LABEL, SPACE_LABEL

# The Local graph's long-running services (the CLI's Space graph members); its one-shot init job is not one.
SERVICES = frozenset(
    {
        "shimpz-team",
        "shimpz-assistant-egress",
        "shimpz-assistant-release",
        "shimpz-account-egress",
        "shimpz-brain-egress",
        "shimpz-brain",
        "shimpz-admin",
    }
)
PROJECT_LABEL = "com.docker.compose.project=shimpz-space"
BUDGET_SECONDS = 5.0


class _BudgetSpentError(Exception):
    """The measurement's budget ran out before a Docker call."""


def _get(api, deadline: float, path: str, *args: str, params: dict | None = None) -> object:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _BudgetSpentError
    return api._result(api._get(api._url(path, *args), params=params, timeout=remaining), json=True)


def _running(api, deadline: float, labels: list[str]) -> list[dict]:
    try:
        containers = _get(
            api,
            deadline,
            "/containers/json",
            params={"filters": _filters(labels)},
        )
    except (DockerException, OSError, ValueError, _BudgetSpentError) as exc:
        raise docker_unavailable() from exc
    if not isinstance(containers, list) or not all(isinstance(item, dict) for item in containers):
        raise docker_unavailable()
    return containers


def _filters(labels: list[str]) -> str:
    return json.dumps({"label": labels, "status": ["running"]})


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _usage(stats: object) -> int | None:
    """One container's memory use as the Docker CLI reports it: usage less its inactive file cache."""
    memory = stats.get("memory_stats") if isinstance(stats, dict) else None
    usage = memory.get("usage") if isinstance(memory, dict) else None
    if not _count(usage):
        return None
    details = memory.get("stats")
    details = details if isinstance(details, dict) else {}
    for key in ("total_inactive_file", "inactive_file"):
        cache = details.get(key)
        if _count(cache) and cache < usage:
            return usage - cache
    return usage


def _memory(api, deadline: float, ids: list[str]) -> tuple[int, int] | tuple[None, None]:
    try:
        info = _get(api, deadline, "/info")
        total = info.get("MemTotal") if isinstance(info, dict) else None
        used = 0
        for container_id in ids:
            usage = _usage(
                _get(api, deadline, "/containers/{0}/stats", container_id, params={"stream": "0", "one-shot": "1"})
            )
            if usage is None:
                return None, None
            used += usage
    except DockerException, OSError, ValueError, _BudgetSpentError:
        return None, None
    if not _count(total) or total == 0 or time.monotonic() > deadline:
        return None, None
    return used, total


def _storage(root: os.PathLike) -> tuple[int, int] | tuple[None, None]:
    try:
        filesystem = os.statvfs(root)
    except OSError:
        return None, None
    total = filesystem.f_blocks * filesystem.f_frsize
    if total <= 0:
        return None, None
    return (filesystem.f_blocks - filesystem.f_bfree) * filesystem.f_frsize, total


def _service(item: dict) -> str | None:
    """The Space service a running Compose-project container is, by its one fixed name, or None."""
    names = item.get("Names")
    if not isinstance(names, list) or len(names) != 1 or not isinstance(names[0], str):
        return None
    name = names[0].removeprefix("/")
    return name if name in SERVICES else None


def space_resources(self) -> dict[str, object]:
    """Measure the Space now; Docker being unreachable for the container listing is the only failure."""
    api = self.client.api
    deadline = time.monotonic() + BUDGET_SECONDS
    members = [item for item in _running(api, deadline, [PROJECT_LABEL]) if _service(item) is not None]
    owned = [f"{MANAGED_LABEL}=1", f"{PROFILE_LABEL}={PROFILE}", f"{SPACE_LABEL}={self.space_id}"]
    assistants = _running(api, deadline, [*owned, f"{KIND_LABEL}=assistant"])
    ids = [item.get("Id") for item in (*members, *assistants)]
    if all(isinstance(container_id, str) and container_id for container_id in ids):
        memory_used, memory_total = _memory(api, deadline, ids)
    else:
        memory_used, memory_total = None, None
    storage_used, storage_total = _storage(self.storage.root)
    return {
        "services_running": len({_service(item) for item in members}),
        "services_total": len(SERVICES),
        "memory_used_bytes": memory_used,
        "memory_total_bytes": memory_total,
        "storage_used_bytes": storage_used,
        "storage_total_bytes": storage_total,
    }
