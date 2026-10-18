"""Bounded Browser-egress audit: the destination, the Browser's address, and this process's generation.

Every line names the random generation drawn when this process started. Team restarts the process between Browser
tasks and records the generation each task ran on, so a line is attributed to exactly one task (ADR-0117). No path,
query, header, or page content is ever recorded.
"""

import ipaddress
import uuid
from pathlib import Path

from audit_writer import AuditError, AuditWriter

AUDIT = AuditWriter(Path("/var/log/browser-egress/audit.jsonl"), "browser-egress")
GENERATION = uuid.uuid4().hex
REJECTED = "rejected-target"
RESULTS = frozenset({"denied", "error", "ok"})


def _client(decision) -> str:
    """The decision's client address, which must be one IPv4 or IPv6 address."""
    value = decision.fields.get("client")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise AuditError("invalid Browser egress audit event") from exc


def record(decision) -> None:
    """Persist one decision: its result, stable code and reason, destination, client address, and generation."""
    host, _, port = decision.subject.rpartition(":")
    named = decision.subject == REJECTED or (bool(host) and port == "443")
    if decision.result not in RESULTS or not named or set(decision.fields) != {"client"}:
        raise AuditError("invalid Browser egress audit event")
    AUDIT.write(
        {
            "principal_class": "machine",
            "principal_id": "browser",
            "operation": "connect",
            "generation": GENERATION,
            "client": _client(decision),
            "subject": decision.subject,
            "result": decision.result,
            "code": decision.code,
            "reason": decision.reason,
            "probe": decision.probe,
        }
    )
