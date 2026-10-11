"""The Space's read-only resource signal Team answers the Supervisor, and its closed shape.

Only Team and Admin consume it, so `payload.py` never imports it.
"""

FIELDS = frozenset(
    {
        "services_running",
        "services_total",
        "memory_used_bytes",
        "memory_total_bytes",
        "storage_used_bytes",
        "storage_total_bytes",
    }
)
MAX_SERVICES = 32
# The largest byte count a browser reads exactly (JavaScript's largest safe integer).
MAX_BYTES = 2**53 - 1


def _count(value: object, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum


def _pair(used: object, total: object, *, within: bool) -> bool:
    """Both null, or a used and a positive total byte count; `within` also bounds the use by the total."""
    if used is None and total is None:
        return True
    return _count(used, MAX_BYTES) and _count(total, MAX_BYTES) and total > 0 and (not within or used <= total)


def canonical_space_resources(value: object) -> dict[str, object] | None:
    """Return the exact resource signal, or None when any member breaks its bounds.

    `services_running` counts the Space's long-running services Docker reports running, out of all of them; the
    memory pair is what the Space's services and owned Assistants use against the memory Docker reports, and the
    storage pair is the filesystem holding Team storage. An unknown pair is null, never zero.
    """
    if (
        not isinstance(value, dict)
        or set(value) != FIELDS
        or not _count(value["services_total"], MAX_SERVICES)
        or value["services_total"] < 1
        or not _count(value["services_running"], value["services_total"])
        or not _pair(value["memory_used_bytes"], value["memory_total_bytes"], within=False)
        or not _pair(value["storage_used_bytes"], value["storage_total_bytes"], within=True)
    ):
        return None
    return {field: value[field] for field in sorted(FIELDS)}
