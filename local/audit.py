"""Small bounded audit journal that deliberately excludes bodies and credentials.

Events are appended before ``record`` returns. A background group commit synchronizes
the first unsynced event within a 50 ms target window; a sudden action loss may discard
that window, while process crashes retain the already-written kernel state.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

AUDIT_PATH = Path("/var/log/shimpz-local/audit.jsonl")
MAX_BYTES = 1024 * 1024
BACKUPS = 2
GROUP_COMMIT_MAX_SECONDS = 0.05
_LOCK = threading.Lock()
_CONDITION = threading.Condition(_LOCK)
_descriptor: int | None = None
_dirty_since: float | None = None
_flush_thread: threading.Thread | None = None
_stopping = False
_failure: RuntimeError | None = None
_OPAQUE_HUMAN_ID = re.compile(r"^[0-9a-f]{32}$")
_MACHINE_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_PRINCIPAL_CLASSES = frozenset({"absent", "human", "machine"})
_RESULTS = frozenset({"denied", "error", "ok"})
_CREDENTIAL_STATES = frozenset(
    {
        "assertion_absent_or_malformed",
        "assertion_present",
        "assertion_rejected",
        "machine_bearer_present",
        "machine_bearer_rejected",
    }
)


@dataclass(frozen=True, slots=True)
class AuditPrincipal:
    principal_id: str | None
    principal_class: str
    credential_state: str | None = None
    trace_id: str | None = None


_REQUEST_PRINCIPAL: ContextVar[AuditPrincipal | None] = ContextVar(
    "local_audit_request_principal",
    default=None,
)


@contextmanager
def bind_request_principal(principal: AuditPrincipal) -> Iterator[None]:
    """Bind attributable authority only for the synchronous request execution."""
    token = _REQUEST_PRINCIPAL.set(principal)
    try:
        yield
    finally:
        _REQUEST_PRINCIPAL.reset(token)


def record_request(
    operation: str,
    *,
    result: str,
    team_id: str | None = None,
    assistant: str | None = None,
    detail: str | None = None,
) -> str:
    """Record an internal security event under the verified request principal."""
    principal = _REQUEST_PRINCIPAL.get()
    if principal is None:
        raise RuntimeError("Local request audit principal is unavailable")
    return record(
        operation,
        result=result,
        principal=principal,
        team_id=team_id,
        assistant=assistant,
        detail=detail,
    )


def _safe_file(path: Path) -> None:
    if not path.exists():
        return
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RuntimeError("the local audit journal has unsafe metadata")


def _rotate(path: Path) -> None:
    if not path.exists() or path.stat().st_size <= MAX_BYTES:
        return
    oldest = path.with_name(f"{path.name}.{BACKUPS}")
    if oldest.exists():
        _safe_file(oldest)
        oldest.unlink()
    for index in range(BACKUPS - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            _safe_file(source)
            source.replace(path.with_name(f"{path.name}.{index + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))


def _raise_failure_locked() -> None:
    if _failure is not None:
        raise _failure
    if _stopping:
        raise RuntimeError("the local audit journal is closing")


def _close_descriptor_locked() -> None:
    global _descriptor
    if _descriptor is not None:
        os.close(_descriptor)
        _descriptor = None


def _sync_locked() -> None:
    global _dirty_since, _failure
    if _descriptor is None or _dirty_since is None:
        return
    try:
        os.fsync(_descriptor)
    except OSError as exc:
        _failure = RuntimeError("the local audit journal could not be synchronized")
        raise _failure from exc
    _dirty_since = None
    _CONDITION.notify_all()


def _open_descriptor_locked() -> int:
    global _descriptor
    if _descriptor is not None:
        _safe_file(AUDIT_PATH)
        path_metadata = AUDIT_PATH.lstat()
        descriptor_metadata = os.fstat(_descriptor)
        if (path_metadata.st_dev, path_metadata.st_ino) != (
            descriptor_metadata.st_dev,
            descriptor_metadata.st_ino,
        ):
            raise RuntimeError("the local audit journal changed while open")
        if descriptor_metadata.st_size <= MAX_BYTES:
            return _descriptor
        _sync_locked()
        _close_descriptor_locked()
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _safe_file(AUDIT_PATH)
    _rotate(AUDIT_PATH)
    _descriptor = os.open(
        AUDIT_PATH,
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    _safe_file(AUDIT_PATH)
    return _descriptor


def _write_all(descriptor: int, encoded: bytes) -> None:
    offset = 0
    while offset < len(encoded):
        written = os.write(descriptor, encoded[offset:])
        if written < 1:
            raise RuntimeError("the local audit journal write was incomplete")
        offset += written


def _flush_worker() -> None:
    global _failure
    with _CONDITION:
        while True:
            if _stopping:
                try:
                    _sync_locked()
                finally:
                    _close_descriptor_locked()
                return
            if _failure is not None:
                _close_descriptor_locked()
                return
            if _dirty_since is None:
                _CONDITION.wait()
                continue
            remaining = _dirty_since + GROUP_COMMIT_MAX_SECONDS - time.monotonic()
            if remaining > 0:
                _CONDITION.wait(timeout=remaining)
                continue
            try:
                _sync_locked()
            except RuntimeError:
                _close_descriptor_locked()
                return


def _ensure_worker_locked() -> None:
    global _flush_thread
    if _flush_thread is None:
        _flush_thread = threading.Thread(
            target=_flush_worker,
            name="local-audit-flush",
            daemon=True,
        )
        _flush_thread.start()


def record(
    operation: str,
    *,
    result: str,
    principal: AuditPrincipal,
    team_id: str | None = None,
    assistant: str | None = None,
    detail: str | None = None,
) -> str:
    """Append one metadata-only event and return its correlation id."""
    principal_id = principal.principal_id
    principal_class = principal.principal_class
    credential_state = principal.credential_state
    if (
        principal_class not in _PRINCIPAL_CLASSES
        or result not in _RESULTS
        or (principal_class == "absent") != (principal_id is None)
        or (
            principal_class == "human"
            and (not isinstance(principal_id, str) or _OPAQUE_HUMAN_ID.fullmatch(principal_id) is None)
        )
        or (
            principal_class == "machine"
            and (not isinstance(principal_id, str) or _MACHINE_ID.fullmatch(principal_id) is None)
        )
        or (credential_state is not None and credential_state not in _CREDENTIAL_STATES)
    ):
        raise ValueError("invalid Local audit principal metadata")
    trace_id = principal.trace_id
    if trace_id is None:
        trace_id = uuid.uuid4().hex
    elif not isinstance(trace_id, str) or _TRACE_ID.fullmatch(trace_id) is None:
        raise ValueError("invalid Local audit trace id")
    event = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "service": "team-local",
        "trace_id": trace_id,
        "operation": operation,
        "result": result,
        "principal_id": principal_id,
        "principal_class": principal_class,
    }
    if team_id is not None:
        event["team_id"] = team_id
    if assistant is not None:
        event["assistant"] = assistant
    if detail is not None:
        event["detail"] = detail
    if credential_state is not None:
        event["credential_state"] = credential_state
    encoded = (json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")

    global _dirty_since
    with _CONDITION:
        _raise_failure_locked()
        try:
            descriptor = _open_descriptor_locked()
            _write_all(descriptor, encoded)
        except OSError as exc:
            raise RuntimeError("the local audit journal could not be written") from exc
        if _dirty_since is None:
            _dirty_since = time.monotonic()
        _ensure_worker_locked()
        _CONDITION.notify()
    return trace_id


def flush() -> None:
    """Synchronize every event acknowledged before this call."""
    with _CONDITION:
        _raise_failure_locked()
        _sync_locked()
        _raise_failure_locked()


def close() -> None:
    """Flush and close the process-local writer, allowing a later clean restart."""
    global _flush_thread, _stopping
    with _CONDITION:
        thread = _flush_thread
        if thread is None:
            _sync_locked()
            _close_descriptor_locked()
            _raise_failure_locked()
            return
        _stopping = True
        _CONDITION.notify_all()
    thread.join()
    with _CONDITION:
        _flush_thread = None
        _stopping = False
        _raise_failure_locked()
