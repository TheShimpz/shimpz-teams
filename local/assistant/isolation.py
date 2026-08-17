"""Pure isolation-profile admission for local Assistant containers."""

from __future__ import annotations

from pathlib import PurePosixPath

from egress import policy as egress_policy
from local.install.runtime import is_digest_ref

ASSISTANT_UID = "10001:10001"
ASSISTANT_MEMORY = 128 * 1024 * 1024
ASSISTANT_NANO_CPUS = 250_000_000
ASSISTANT_PIDS = 64
ASSISTANT_TMPFS = {str(PurePosixPath("/") / "tmp"): "size=256m"}
ASSISTANT_NOFILE_LIMIT = 1024
ASSISTANT_ULIMITS = [{"Name": "nofile", "Soft": ASSISTANT_NOFILE_LIMIT, "Hard": ASSISTANT_NOFILE_LIMIT}]
_ENABLED_NO_NEW_PRIVILEGES = frozenset({"no-new-privileges", "no-new-privileges:true"})
_ALL_PROXY_VARIABLES = frozenset(
    {
        "HTTPS_PROXY",
        "https_proxy",
        "NO_PROXY",
        "no_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    }
)
_UNSUPPORTED_PROXY_VARIABLES = frozenset({"HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"})


def _security_options_valid(options: object) -> bool:
    """Accept only Docker's two enabled renderings of the requested NNP option."""
    return isinstance(options, list) and len(options) == 1 and str(options[0]) in _ENABLED_NO_NEW_PRIVILEGES


def egress_environment_valid(
    environment: dict[str, str],
    expected_proxy_environment: dict[str, str] | None,
) -> bool:
    """Require the exact reviewed proxy environment, or no proxy variables for offline Assistants."""
    if expected_proxy_environment is None:
        return not _ALL_PROXY_VARIABLES.intersection(environment)
    return all(environment.get(key) == value for key, value in expected_proxy_environment.items()) and not (
        _UNSUPPORTED_PROXY_VARIABLES.intersection(environment)
    )


def inspect_profile(
    attrs: dict,
    container_name: str,
    expected_labels: dict[str, str],
    expected_name: str,
    reviewed_image: str,
    network_name: str,
    cpuset_cpus: str,
) -> tuple[dict, dict[str, str]] | None:
    """Return admitted config/environment, or ``None`` for any profile drift."""
    config = attrs.get("Config") or {}
    host = attrs.get("HostConfig") or {}
    labels = config.get("Labels") or {}
    installed_image = labels.get("com.shimpz.local.image")
    networks = (attrs.get("NetworkSettings") or {}).get("Networks") or {}
    environment = egress_policy.environment_map(config.get("Env"))
    if (
        environment is None
        or not isinstance(labels, dict)
        or not all(labels.get(key) == value for key, value in expected_labels.items())
        or container_name != expected_name
        or not is_digest_ref(installed_image)
        or config.get("Image") != installed_image
        or installed_image.rpartition("@sha256:")[0] != reviewed_image.rpartition("@sha256:")[0]
        or config.get("User") != ASSISTANT_UID
        or host.get("ReadonlyRootfs") is not True
        or set(host.get("CapDrop") or []) != {"ALL"}
        or host.get("CapAdd") not in (None, [])
        or not _security_options_valid(host.get("SecurityOpt"))
        or host.get("Privileged") is not False
        or host.get("NetworkMode") != network_name
        or host.get("Memory") != ASSISTANT_MEMORY
        or host.get("MemorySwap") != ASSISTANT_MEMORY
        or host.get("NanoCpus") != ASSISTANT_NANO_CPUS
        or host.get("CpusetCpus") != cpuset_cpus
        or host.get("PidsLimit") != ASSISTANT_PIDS
        or host.get("IpcMode") != "private"
        or host.get("CgroupnsMode") != "private"
        or host.get("Tmpfs") != ASSISTANT_TMPFS
        or host.get("Ulimits") != ASSISTANT_ULIMITS
        or host.get("Sysctls") not in (None, {})
        or host.get("AutoRemove") is not False
        or (host.get("RestartPolicy") or {}).get("Name") not in {"", "no"}
        or (host.get("LogConfig") or {}).get("Type") != "none"
        or (host.get("LogConfig") or {}).get("Config") != {}
        or host.get("PortBindings") not in (None, {})
        or host.get("Binds") not in (None, [])
        or host.get("Devices") not in (None, [])
        or host.get("DeviceRequests") not in (None, [])
        or attrs.get("Mounts") not in (None, [])
        or set(networks) != {network_name}
    ):
        return None
    return config, environment
