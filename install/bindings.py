"""Durable, fail-closed bindings for dynamically installed Assistants."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.container import network as network_policy
from install.contract import ContractValidationError, ContractValidator

_FORMAT_VERSION = 2
_MAX_BINDINGS = 4096
_MAX_FILE_BYTES = 8 * 1024 * 1024
_TEAM_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_ASSISTANT_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTRACTS = ContractValidator()
_PUBLISHED = "published"
_LOCAL = "local"
type AssistantProvenance = Literal["published", "local"]
type LocalRecordValidator = Callable[[dict[str, Any]], None]


class DynamicAssistantError(RuntimeError):
    """The dynamic Assistant registry is unavailable or violates its contract."""


class DynamicAssistantConflictError(DynamicAssistantError):
    """A Team already binds this Assistant id to a different artifact."""


@dataclass(frozen=True, slots=True)
class DynamicAssistantBinding:
    team_id: str
    binding_digest: str
    provenance: AssistantProvenance
    document: dict[str, Any]

    @property
    def assistant_id(self) -> str:
        return str(self.document["assistant_id"])

    @property
    def resolution(self) -> dict[str, Any]:
        if self.provenance != _PUBLISHED:
            raise DynamicAssistantError("the Assistant binding is not a publication")
        return self.document

    @property
    def local_record(self) -> dict[str, Any]:
        if self.provenance != _LOCAL:
            raise DynamicAssistantError("the Assistant binding is not a local snapshot")
        return self.document


class DynamicAssistantStore:
    """One controller-private, atomic registry shared by request threads."""

    def __init__(self, path: Path, *, local_record_validator: LocalRecordValidator | None = None) -> None:
        self._path = path
        self._lock_path = path.with_suffix(f"{path.suffix}.lock")
        self._local_record_validator = local_record_validator

    def put(self, team_id: str, resolution: dict[str, Any]) -> DynamicAssistantBinding:
        binding = binding_from_resolution(team_id, resolution)
        return self._put(binding)

    def put_local(self, team_id: str, record: dict[str, Any]) -> DynamicAssistantBinding:
        binding = binding_from_local_record(team_id, record, self._local_record_validator)
        return self._put(binding)

    def _put(self, binding: DynamicAssistantBinding) -> DynamicAssistantBinding:
        with self._exclusive_lock():
            bindings = self._read()
            existing = _find(bindings, binding.team_id, binding.assistant_id)
            if existing is not None:
                if existing == binding:
                    return existing
                raise DynamicAssistantConflictError("the Team already binds this Assistant id to another artifact")
            if len(bindings) >= _MAX_BINDINGS:
                raise DynamicAssistantError("the dynamic Assistant registry is full")
            bindings.append(binding)
            self._write(bindings)
        return binding

    def get(self, team_id: str, assistant_id: str) -> DynamicAssistantBinding | None:
        _validate_identity(team_id, assistant_id)
        with self._shared_lock():
            return _find(self._read(), team_id, assistant_id)

    def replace(
        self,
        team_id: str,
        expected_binding_digest: str,
        resolution: dict[str, Any],
    ) -> DynamicAssistantBinding:
        replacement = binding_from_resolution(team_id, resolution)
        return self._replace(replacement, expected_binding_digest)

    def replace_local(
        self,
        team_id: str,
        expected_binding_digest: str,
        record: dict[str, Any],
    ) -> DynamicAssistantBinding:
        replacement = binding_from_local_record(team_id, record, self._local_record_validator)
        return self._replace(replacement, expected_binding_digest)

    def _replace(
        self,
        replacement: DynamicAssistantBinding,
        expected_binding_digest: str,
    ) -> DynamicAssistantBinding:
        if _DIGEST_RE.fullmatch(expected_binding_digest) is None:
            raise DynamicAssistantConflictError("the expected Assistant binding digest is invalid")
        with self._exclusive_lock():
            bindings = self._read()
            existing = _find(bindings, replacement.team_id, replacement.assistant_id)
            if existing is None or existing.binding_digest != expected_binding_digest:
                raise DynamicAssistantConflictError("the Assistant binding changed before replacement")
            if existing.provenance != replacement.provenance:
                raise DynamicAssistantConflictError("the Assistant binding provenance cannot be replaced")
            if existing == replacement:
                return existing
            bindings[bindings.index(existing)] = replacement
            self._write(bindings)
        return replacement

    def list(self, team_id: str) -> tuple[DynamicAssistantBinding, ...]:
        _validate_team_id(team_id)
        with self._shared_lock():
            bindings = tuple(binding for binding in self._read() if binding.team_id == team_id)
        return tuple(sorted(bindings, key=lambda binding: binding.assistant_id))

    def snapshot(self) -> tuple[DynamicAssistantBinding, ...]:
        """Read and validate one immutable point-in-time view of every binding."""
        with self._shared_lock():
            return tuple(self._read())

    def delete(self, team_id: str, assistant_id: str) -> bool:
        _validate_identity(team_id, assistant_id)
        with self._exclusive_lock():
            bindings = self._read()
            retained = [
                binding for binding in bindings if (binding.team_id, binding.assistant_id) != (team_id, assistant_id)
            ]
            if len(retained) == len(bindings):
                return False
            self._write(retained)
        return True

    def _exclusive_lock(self):
        return _FileLock(self._lock_path, fcntl.LOCK_EX)

    def _shared_lock(self):
        return _FileLock(self._lock_path, fcntl.LOCK_SH)

    def _read(self) -> list[DynamicAssistantBinding]:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise DynamicAssistantError("the dynamic Assistant registry cannot be read") from exc
        if len(raw) > _MAX_FILE_BYTES:
            raise DynamicAssistantError("the dynamic Assistant registry is too large")
        try:
            document = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DynamicAssistantError("the dynamic Assistant registry is malformed") from exc
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "bindings"}
            or document["version"] != _FORMAT_VERSION
            or not isinstance(document["bindings"], list)
            or len(document["bindings"]) > _MAX_BINDINGS
        ):
            raise DynamicAssistantError("the dynamic Assistant registry is malformed")
        bindings = [_decode_binding(value, self._local_record_validator) for value in document["bindings"]]
        identities = [(binding.team_id, binding.assistant_id) for binding in bindings]
        if len(identities) != len(set(identities)):
            raise DynamicAssistantError("the dynamic Assistant registry contains duplicate bindings")
        return bindings

    def _write(self, bindings: list[DynamicAssistantBinding]) -> None:
        document = {
            "version": _FORMAT_VERSION,
            "bindings": [_encode_binding(binding) for binding in sorted(bindings, key=_binding_key)],
        }
        encoded = _canonical_bytes(document)
        if len(encoded) > _MAX_FILE_BYTES:
            raise DynamicAssistantError("the dynamic Assistant registry is too large")
        temporary: Path | None = None
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                temporary.chmod(0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self._path)
            temporary = None
            directory_fd = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise DynamicAssistantError("the dynamic Assistant registry cannot be written") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class _FileLock:
    def __init__(self, path: Path, operation: int) -> None:
        self._path = path
        self._operation = operation
        self._stream = None

    def __enter__(self) -> None:
        descriptor = -1
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                self._path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            self._stream = os.fdopen(descriptor, "rb+")
            descriptor = -1
            fcntl.flock(self._stream, self._operation)
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            raise DynamicAssistantError("the dynamic Assistant registry lock is unavailable") from exc
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            raise

    def __exit__(self, *_args: object) -> None:
        stream = self._stream
        if stream is None:
            raise DynamicAssistantError("the dynamic Assistant registry lock is unavailable")
        try:
            fcntl.flock(stream, fcntl.LOCK_UN)
        finally:
            stream.close()


def binding_from_resolution(
    team_id: str,
    resolution: dict[str, Any],
) -> DynamicAssistantBinding:
    _validate_team_id(team_id)
    try:
        _CONTRACTS.validate("resolve-response.schema.json", resolution)
    except ContractValidationError as exc:
        raise DynamicAssistantError("the dynamic Assistant resolution is invalid") from exc
    return _binding(team_id, _PUBLISHED, "resolution", resolution)


def binding_from_local_record(
    team_id: str,
    record: dict[str, Any],
    validator: LocalRecordValidator | None,
) -> DynamicAssistantBinding:
    _validate_team_id(team_id)
    if validator is None:
        raise DynamicAssistantError("local Assistant bindings are unavailable in this profile")
    if not isinstance(record, dict):
        raise DynamicAssistantError("the local Assistant record is invalid")
    validator(record)
    return _binding(team_id, _LOCAL, "local_record", record)


def _binding(
    team_id: str,
    provenance: AssistantProvenance,
    document_name: str,
    document: dict[str, Any],
) -> DynamicAssistantBinding:
    assistant_id = document.get("assistant_id")
    _validate_identity(team_id, assistant_id)
    if assistant_id in network_policy.RESERVED_SERVICE_ALIASES:
        raise DynamicAssistantError("the Assistant id is reserved for Team infrastructure")
    digest_value = {
        "version": _FORMAT_VERSION,
        "team_id": team_id,
        "provenance": provenance,
        document_name: document,
    }
    digest = f"sha256:{hashlib.sha256(_canonical_bytes(digest_value)).hexdigest()}"
    return DynamicAssistantBinding(team_id, digest, provenance, document)


def _decode_binding(
    value: object,
    local_record_validator: LocalRecordValidator | None = None,
) -> DynamicAssistantBinding:
    if not isinstance(value, dict) or not isinstance(value.get("provenance"), str):
        raise DynamicAssistantError("the dynamic Assistant registry binding is malformed")
    provenance = value["provenance"]
    document_name = "resolution" if provenance == _PUBLISHED else "local_record"
    if set(value) != {"team_id", "binding_digest", "provenance", document_name}:
        raise DynamicAssistantError("the dynamic Assistant registry binding is malformed")
    document = value[document_name]
    if not isinstance(document, dict):
        raise DynamicAssistantError("the dynamic Assistant registry binding is malformed")
    if provenance == _PUBLISHED:
        expected = binding_from_resolution(value["team_id"], document)
    elif provenance == _LOCAL:
        expected = binding_from_local_record(value["team_id"], document, local_record_validator)
    else:
        raise DynamicAssistantError("the dynamic Assistant registry binding is malformed")
    if value["binding_digest"] != expected.binding_digest:
        raise DynamicAssistantError("the dynamic Assistant registry binding digest is invalid")
    return expected


def _encode_binding(binding: DynamicAssistantBinding) -> dict[str, object]:
    if binding.provenance == _PUBLISHED:
        document_name = "resolution"
    elif binding.provenance == _LOCAL:
        document_name = "local_record"
    else:
        raise DynamicAssistantError("the dynamic Assistant registry binding is malformed")
    return {
        "team_id": binding.team_id,
        "binding_digest": binding.binding_digest,
        "provenance": binding.provenance,
        document_name: binding.document,
    }


def _binding_key(binding: DynamicAssistantBinding) -> tuple[str, str]:
    return binding.team_id, binding.assistant_id


def _find(
    bindings: list[DynamicAssistantBinding],
    team_id: str,
    assistant_id: str,
) -> DynamicAssistantBinding | None:
    return next(
        (binding for binding in bindings if binding.team_id == team_id and binding.assistant_id == assistant_id),
        None,
    )


def _validate_team_id(team_id: object) -> None:
    if not isinstance(team_id, str) or _TEAM_ID_RE.fullmatch(team_id) is None:
        raise DynamicAssistantError("the Team id is invalid")


def _validate_identity(team_id: object, assistant_id: object) -> None:
    _validate_team_id(team_id)
    if not isinstance(assistant_id, str) or len(assistant_id) > 40 or _ASSISTANT_ID_RE.fullmatch(assistant_id) is None:
        raise DynamicAssistantError("the Assistant id is invalid")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
