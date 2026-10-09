"""Durable, fail-closed bindings for dynamically installed Assistants."""

import builtins
import fcntl
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from core import canonical_json
from core.container import network as network_policy
from install import lock
from install.contract import ContractValidationError, ContractValidator
from protocol.http.v1 import payload as http_payload
from storage import private_state

_FORMAT_VERSION = 2
_MAX_BINDINGS = 4096
_MAX_FILE_BYTES = 8 * 1024 * 1024
_CONTRACTS = ContractValidator()
_PUBLISHED = "published"
_LOCAL = "local"
_LOCK_UNAVAILABLE = "the dynamic Assistant registry lock is unavailable"
type AssistantProvenance = Literal["published", "local"]
type LocalRecordValidator = Callable[[dict[str, Any]], None]


class DynamicAssistantError(RuntimeError):
    """The dynamic Assistant registry is unavailable or violates its contract."""


class DynamicAssistantConflictError(DynamicAssistantError):
    """A Team already binds this Assistant id to a different artifact."""


class InadmissibleAssistantBindingError(DynamicAssistantError):
    """An intact installed binding that the current Assistant contract refuses; it must be replaced or uninstalled."""


@dataclass(frozen=True, slots=True)
class DynamicAssistantBinding:
    team_id: str
    binding_digest: str
    provenance: AssistantProvenance
    document: dict[str, Any]
    # False for an intact, digest-verified binding admitted under an earlier contract that the current one refuses,
    # such as one staged with an older SDK: it stays owned so it can be listed, replaced, or uninstalled, and every
    # runtime use of it is refused (ADR-0033's 2026-10-08 amendment).
    admissible: bool = True

    @property
    def assistant_id(self) -> str:
        return str(self.document["assistant_id"])

    @property
    def resolution(self) -> dict[str, Any]:
        if self.provenance != _PUBLISHED:
            raise DynamicAssistantError("the Assistant binding is not a publication")
        self.require_admissible()
        return self.document

    @property
    def local_record(self) -> dict[str, Any]:
        if self.provenance != _LOCAL:
            raise DynamicAssistantError("the Assistant binding is not a local snapshot")
        self.require_admissible()
        return self.document

    def require_admissible(self) -> None:
        if not self.admissible:
            raise InadmissibleAssistantBindingError("the installed Assistant must be replaced under the current terms")


class DynamicAssistantStore:
    """One controller-private, atomic registry shared by request threads."""

    def __init__(self, path: Path, *, local_record_validator: LocalRecordValidator | None = None) -> None:
        self._path = path
        self._lock_path = path.with_suffix(f"{path.suffix}.lock")
        self._local_record_validator = local_record_validator

    def put(self, team_id: str, resolution: dict[str, Any]) -> DynamicAssistantBinding:
        binding, _created = self.put_with_status(team_id, resolution)
        return binding

    def put_with_status(
        self,
        team_id: str,
        resolution: dict[str, Any],
    ) -> tuple[DynamicAssistantBinding, bool]:
        binding = binding_from_resolution(team_id, resolution)
        return self._put(binding)

    def put_local(self, team_id: str, record: dict[str, Any]) -> DynamicAssistantBinding:
        binding, _created = self.put_local_with_status(team_id, record)
        return binding

    def put_local_with_status(
        self,
        team_id: str,
        record: dict[str, Any],
    ) -> tuple[DynamicAssistantBinding, bool]:
        binding = binding_from_local_record(team_id, record, self._local_record_validator)
        return self._put(binding)

    def _put(self, binding: DynamicAssistantBinding) -> tuple[DynamicAssistantBinding, bool]:
        with self._exclusive_lock():
            bindings = self._read()
            existing = _find(bindings, binding.team_id, binding.assistant_id)
            if existing is not None:
                if existing == binding:
                    return existing, False
                raise DynamicAssistantConflictError("the Team already binds this Assistant id to another artifact")
            if len(bindings) >= _MAX_BINDINGS:
                raise DynamicAssistantError("the dynamic Assistant registry is full")
            bindings.append(binding)
            self._write(bindings)
        return binding, True

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
        if http_payload.SOURCE_DIGEST_RE.fullmatch(expected_binding_digest) is None:
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

    def delete_if_matches(self, team_id: str, assistant_id: str, expected_binding_digest: str) -> bool:
        _validate_identity(team_id, assistant_id)
        if http_payload.SOURCE_DIGEST_RE.fullmatch(expected_binding_digest) is None:
            raise DynamicAssistantConflictError("the expected Assistant binding digest is invalid")
        with self._exclusive_lock():
            bindings = self._read()
            existing = _find(bindings, team_id, assistant_id)
            if existing is None or existing.binding_digest != expected_binding_digest:
                return False
            bindings.remove(existing)
            self._write(bindings)
        return True

    def _exclusive_lock(self):
        return lock.FileLock(self._lock_path, fcntl.LOCK_EX, DynamicAssistantError, _LOCK_UNAVAILABLE)

    def _shared_lock(self):
        return lock.FileLock(self._lock_path, fcntl.LOCK_SH, DynamicAssistantError, _LOCK_UNAVAILABLE)

    def _read(self) -> builtins.list[DynamicAssistantBinding]:
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

    def _write(self, bindings: builtins.list[DynamicAssistantBinding]) -> None:
        document = {
            "version": _FORMAT_VERSION,
            "bindings": [_encode_binding(binding) for binding in sorted(bindings, key=_binding_key)],
        }
        encoded = canonical_json.encode(document)
        if len(encoded) > _MAX_FILE_BYTES:
            raise DynamicAssistantError("the dynamic Assistant registry is too large")
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            private_state.replace_durably(self._path, encoded)
        except OSError as exc:
            raise DynamicAssistantError("the dynamic Assistant registry cannot be written") from exc


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
    digest = f"sha256:{hashlib.sha256(canonical_json.encode(digest_value)).hexdigest()}"
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
    if not isinstance(document, dict) or provenance not in (_PUBLISHED, _LOCAL):
        raise DynamicAssistantError("the dynamic Assistant registry binding is malformed")
    # Integrity is checked first and stays fatal: a binding whose digest does not match was not written by Team.
    intact = _binding(value["team_id"], provenance, document_name, document)
    if value["binding_digest"] != intact.binding_digest:
        raise DynamicAssistantError("the dynamic Assistant registry binding digest is invalid")
    # Admission is the current contract's verdict on one intact binding, so a refusal marks only that binding.
    try:
        if provenance == _PUBLISHED:
            return binding_from_resolution(value["team_id"], document)
        return binding_from_local_record(value["team_id"], document, local_record_validator)
    except DynamicAssistantError:
        return replace(intact, admissible=False)


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
    if http_payload.canonical_team_id(team_id) is None:
        raise DynamicAssistantError("the Team id is invalid")


def _validate_identity(team_id: object, assistant_id: object) -> None:
    _validate_team_id(team_id)
    if http_payload.canonical_assistant_id(assistant_id) is None:
        raise DynamicAssistantError("the Assistant id is invalid")
