"""Encrypted Team custody for persistent Assistant-declared Action inputs."""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core import strict_json
from storage import private_state

STATE_PATH = Path("/var/lib/shimpz-local/assistant-stored-inputs/state/stored-inputs.json")
KEY_PATH = Path("/var/lib/shimpz-local/assistant-stored-inputs/key/aes256.key")
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_VALUE_CHARACTERS = 1024
MAX_VALUE_BYTES = 16 * 1024
MAX_PLAINTEXT_BYTES = MAX_VALUE_BYTES + 64
MAX_STORED_INPUTS_PER_ASSISTANT = 8
MAX_TOTAL_RECORDS = 4096
_TEAM_ID = re.compile(r"[a-z0-9_]{1,40}\Z")
_COMPONENT_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
StoredInputStatus = Literal["missing", "stored"]


class StoredInputStoreError(RuntimeError):
    """Stored Input state is invalid, unavailable, or unauthentic."""


class StoredInputValidationError(StoredInputStoreError):
    """A caller supplied invalid Stored Input data."""


class StoredInputMissingError(StoredInputStoreError):
    """The requested Stored Input has not been configured."""


_PRIVATE_STATE = private_state.PrivateState(
    StoredInputStoreError,
    "Stored Input state is malformed",
    "Stored Input envelope is malformed",
    MAX_PLAINTEXT_BYTES * 2 + 128,
)


@dataclass(frozen=True, slots=True)
class StoredInputMetadata:
    """Secret-free declared inventory for one Stored Input."""

    id: str
    kind: str
    label: str
    description: str
    status: StoredInputStatus
    generation: int


@dataclass(frozen=True, slots=True, repr=False)
class StoredInputValue:
    """One decrypted value whose representation never includes its contents."""

    value: str
    generation: int


def _team_id(value: object) -> str:
    if not isinstance(value, str) or _TEAM_ID.fullmatch(value) is None:
        raise StoredInputValidationError("Team id is invalid")
    return value


def _component_id(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) > 64 or _COMPONENT_ID.fullmatch(value) is None:
        raise StoredInputValidationError(f"{label} is invalid")
    return value


def _kind(value: object) -> str:
    if value != "password":
        raise StoredInputValidationError("Stored Input kind is invalid")
    return "password"


def _public_text(value: object, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or not value.isprintable()
    ):
        raise StoredInputValidationError(f"Stored Input {label} is invalid")
    return value


def _secret_value(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_VALUE_CHARACTERS:
        raise StoredInputValidationError("Stored Input value is invalid")
    if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        raise StoredInputValidationError("Stored Input value is invalid")
    return value


def _strict_json(payload: bytes) -> object:
    try:
        return strict_json.loads(payload)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise StoredInputStoreError("Stored Input state is not valid JSON") from exc


def _record_metadata(record: Mapping[str, object]) -> tuple[str, int]:
    try:
        kind = _kind(record.get("kind"))
    except StoredInputValidationError as exc:
        raise StoredInputStoreError("Stored Input state record is malformed") from exc
    generation = record.get("generation")
    if type(generation) is not int or generation < 1:
        raise StoredInputStoreError("Stored Input state record is malformed")
    return kind, generation


def _validate_record(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"kind", "generation", "updated_at", "envelope"}:
        raise StoredInputStoreError("Stored Input state record is malformed")
    _record_metadata(value)
    updated_at = value.get("updated_at")
    envelope = value.get("envelope")
    if (
        not isinstance(updated_at, str)
        or _TIMESTAMP.fullmatch(updated_at) is None
        or not isinstance(envelope, dict)
        or set(envelope) != {"algorithm", "nonce", "ciphertext"}
        or envelope.get("algorithm") != "AES-256-GCM"
    ):
        raise StoredInputStoreError("Stored Input state record is malformed")
    _PRIVATE_STATE.decode_part(envelope.get("nonce"), expected=12)
    _PRIVATE_STATE.decode_part(envelope.get("ciphertext"), minimum=17, maximum=MAX_PLAINTEXT_BYTES + 16)
    return value


def _validate_state(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"schema", "teams"} or value.get("schema") != 1:
        raise StoredInputStoreError("Stored Input state has an unsupported shape")
    teams = value.get("teams")
    if not isinstance(teams, dict):
        raise StoredInputStoreError("Stored Input state is malformed")
    total = 0
    for raw_team, raw_assistants in teams.items():
        total += _validate_assistants(raw_team, raw_assistants)
        if total > MAX_TOTAL_RECORDS:
            raise StoredInputStoreError("Stored Input state exceeds its record limit")
    return value


def _validate_assistants(raw_team: object, raw_assistants: object) -> int:
    try:
        _team_id(raw_team)
    except StoredInputValidationError as exc:
        raise StoredInputStoreError("Stored Input state is malformed") from exc
    if not isinstance(raw_assistants, dict):
        raise StoredInputStoreError("Stored Input state is malformed")
    count = 0
    for raw_assistant, raw_records in raw_assistants.items():
        try:
            _component_id(raw_assistant, "Assistant id")
        except StoredInputValidationError as exc:
            raise StoredInputStoreError("Stored Input state is malformed") from exc
        if not isinstance(raw_records, dict) or len(raw_records) > MAX_STORED_INPUTS_PER_ASSISTANT:
            raise StoredInputStoreError("Stored Input state is malformed")
        for raw_stored_input, raw_record in raw_records.items():
            try:
                _component_id(raw_stored_input, "Stored Input id")
            except StoredInputValidationError as exc:
                raise StoredInputStoreError("Stored Input state is malformed") from exc
            _validate_record(raw_record)
            count += 1
    return count


def _aad(
    team_id: str,
    assistant_id: str,
    stored_input_id: str,
    record: Mapping[str, object],
) -> bytes:
    kind, generation = _record_metadata(record)
    return json.dumps(
        [
            "shimpz-stored-input-v1",
            team_id,
            assistant_id,
            stored_input_id,
            kind,
            generation,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def _declarations(value: object) -> dict[str, tuple[str, str, str]]:
    if not isinstance(value, Mapping) or len(value) > MAX_STORED_INPUTS_PER_ASSISTANT:
        raise StoredInputValidationError("Stored Input declarations are invalid")
    declarations: dict[str, tuple[str, str, str]] = {}
    for raw_id, raw_spec in value.items():
        stored_input_id = _component_id(raw_id, "Stored Input id")
        try:
            raw_kind = raw_spec.kind  # type: ignore[attr-defined]
            raw_label = raw_spec.label  # type: ignore[attr-defined]
            raw_description = raw_spec.description  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            if not isinstance(raw_spec, Mapping) or set(raw_spec) != {"kind", "label", "description"}:
                raise StoredInputValidationError("Stored Input declarations are invalid") from None
            raw_kind = raw_spec.get("kind")
            raw_label = raw_spec.get("label")
            raw_description = raw_spec.get("description")
        declarations[stored_input_id] = (
            _kind(raw_kind),
            _public_text(raw_label, "label", 80),
            _public_text(raw_description, "description", 500),
        )
    return declarations


def _declared_ids(value: object) -> tuple[str, ...]:
    values: Iterable[object]
    if isinstance(value, Mapping):
        values = value.keys()
    elif isinstance(value, Iterable) and not isinstance(value, str | bytes):
        values = value
    else:
        raise StoredInputValidationError("Stored Input ids are invalid")
    declared: list[str] = []
    seen: set[str] = set()
    for raw_id in values:
        if len(declared) == MAX_STORED_INPUTS_PER_ASSISTANT:
            raise StoredInputValidationError("Stored Input ids are invalid")
        stored_input_id = _component_id(raw_id, "Stored Input id")
        if stored_input_id in seen:
            raise StoredInputValidationError("Stored Input ids are invalid")
        declared.append(stored_input_id)
        seen.add(stored_input_id)
    return tuple(declared)


class StoredInputStore:
    """File-backed AES-GCM storage isolated from OAuth and continuation state."""

    def __init__(self, state_path: Path = STATE_PATH, key_path: Path = KEY_PATH) -> None:
        self.state_path = Path(state_path)
        self.key_path = Path(key_path)
        if not self.state_path.is_absolute() or not self.key_path.is_absolute():
            raise StoredInputStoreError("Stored Input state and key paths must be absolute")
        try:
            state_parent = self.state_path.parent.resolve()
            key_parent = self.key_path.parent.resolve()
        except OSError as exc:
            raise StoredInputStoreError("Stored Input storage paths are unavailable") from exc
        if state_parent == key_parent:
            raise StoredInputStoreError("Stored Input keyring must be separate from encrypted state")
        self._lock = threading.RLock()
        self._state_cache_identity: private_state.PrivateFileIdentity | None = None
        self._state_cache: dict[str, object] | None = None

    def _read_state(self) -> dict[str, object]:
        snapshot = _PRIVATE_STATE.read_private_file_if_changed(
            self.state_path,
            MAX_STATE_BYTES,
            "Stored Input state",
            self._state_cache_identity,
            cache_initialized=self._state_cache is not None,
        )
        if snapshot.unchanged:
            if self._state_cache is None:
                raise StoredInputStoreError("Stored Input state cache is unavailable")
            return self._state_cache
        state = (
            private_state.empty_state()
            if snapshot.payload is None
            else _validate_state(_strict_json(snapshot.payload))
        )
        self._state_cache_identity = snapshot.identity
        self._state_cache = state
        return state

    def _read_state_for_update(self) -> dict[str, object]:
        return copy.deepcopy(self._read_state())

    def _drop_state_cache(self) -> None:
        self._state_cache_identity = None
        self._state_cache = None

    def _write_state(self, state: Mapping[str, object]) -> None:
        try:
            payload = json.dumps(
                _validate_state(dict(state)),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(payload) > MAX_STATE_BYTES:
                raise StoredInputStoreError("Stored Input state exceeds its fixed byte limit")
            _PRIVATE_STATE.atomic_write(self.state_path, payload, "Stored Input state")
        finally:
            self._drop_state_cache()

    def _key(self, *, allow_create: bool = False) -> bytes:
        return _PRIVATE_STATE.key(self.key_path, "Stored Input keyring", allow_create=allow_create)

    @staticmethod
    def _plaintext(value: str) -> bytes:
        payload = json.dumps(
            {"value": value},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_PLAINTEXT_BYTES:
            raise StoredInputValidationError("Stored Input value is invalid")
        return payload

    def _sealed_record(
        self,
        team: str,
        assistant: str,
        stored_input: str,
        kind: str,
        value: str,
        generation: int,
        key: bytes,
    ) -> dict[str, object]:
        record: dict[str, object] = {
            "kind": kind,
            "generation": generation,
            "updated_at": private_state.timestamp(),
            "envelope": {},
        }
        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(
            nonce,
            self._plaintext(value),
            _aad(team, assistant, stored_input, record),
        )
        record["envelope"] = {
            "algorithm": "AES-256-GCM",
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        return record

    def seal(
        self,
        team_id: object,
        assistant_id: object,
        stored_input_id: object,
        kind: object,
        value: object,
    ) -> int:
        """Encrypt a successfully consumed value and atomically advance its generation."""
        team = _team_id(team_id)
        assistant = _component_id(assistant_id, "Assistant id")
        stored_input = _component_id(stored_input_id, "Stored Input id")
        canonical_kind = _kind(kind)
        canonical_value = _secret_value(value)
        with self._lock:
            state = self._read_state_for_update()
            key = self._key(allow_create=not _PRIVATE_STATE.has_records(state))
            records = _PRIVATE_STATE.records(state, team, assistant, create=True)
            if stored_input not in records and len(records) >= MAX_STORED_INPUTS_PER_ASSISTANT:
                raise StoredInputStoreError("Stored Input capacity reached")
            previous = records.get(stored_input)
            generation = int(previous.get("generation", 0)) + 1 if isinstance(previous, dict) else 1
            records[stored_input] = self._sealed_record(
                team,
                assistant,
                stored_input,
                canonical_kind,
                canonical_value,
                generation,
                key,
            )
            self._write_state(state)
        return generation

    def resolve(
        self,
        team_id: object,
        assistant_id: object,
        stored_input_id: object,
        kind: object,
    ) -> StoredInputValue:
        """Decrypt one exact declared value without exposing any inventory peers."""
        team = _team_id(team_id)
        assistant = _component_id(assistant_id, "Assistant id")
        stored_input = _component_id(stored_input_id, "Stored Input id")
        canonical_kind = _kind(kind)
        with self._lock:
            records = _PRIVATE_STATE.records(self._read_state(), team, assistant, create=False)
            if stored_input not in records:
                raise StoredInputMissingError("Stored Input is not configured")
            record = _validate_record(records[stored_input])
            stored_kind, _generation = _record_metadata(record)
            if stored_kind != canonical_kind:
                raise StoredInputMissingError("Stored Input declaration changed")
            return self._resolve_record(team, assistant, stored_input, record)

    def _resolve_record(
        self,
        team: str,
        assistant: str,
        stored_input: str,
        record: Mapping[str, object],
    ) -> StoredInputValue:
        validated = _validate_record(record)
        _kind_value, generation = _record_metadata(validated)
        envelope = validated["envelope"]
        try:
            plaintext = AESGCM(self._key()).decrypt(
                _PRIVATE_STATE.decode_part(envelope.get("nonce"), expected=12),
                _PRIVATE_STATE.decode_part(envelope.get("ciphertext")),
                _aad(team, assistant, stored_input, validated),
            )
        except InvalidTag as exc:
            raise StoredInputStoreError("Stored Input envelope authentication failed") from exc
        return StoredInputValue(self._decrypted_value(plaintext), generation)

    @staticmethod
    def _decrypted_value(plaintext: bytes) -> str:
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise StoredInputStoreError("decrypted Stored Input is malformed")
        decoded = _strict_json(plaintext)
        if not isinstance(decoded, dict) or set(decoded) != {"value"}:
            raise StoredInputStoreError("decrypted Stored Input is malformed")
        try:
            return _secret_value(decoded["value"])
        except StoredInputValidationError as exc:
            raise StoredInputStoreError("decrypted Stored Input is malformed") from exc

    def metadata(
        self,
        team_id: object,
        assistant_id: object,
        declarations: object,
    ) -> tuple[StoredInputMetadata, ...]:
        """Return declared status without decrypting or returning any value."""
        team = _team_id(team_id)
        assistant = _component_id(assistant_id, "Assistant id")
        declared = _declarations(declarations)
        with self._lock:
            records = _PRIVATE_STATE.records(self._read_state(), team, assistant, create=False)
            result: list[StoredInputMetadata] = []
            for stored_input, (kind, label, description) in declared.items():
                record = records.get(stored_input)
                if record is None:
                    result.append(StoredInputMetadata(stored_input, kind, label, description, "missing", 0))
                    continue
                validated = _validate_record(record)
                stored_kind, generation = _record_metadata(validated)
                status: StoredInputStatus = "missing"
                if stored_kind == kind:
                    self._resolve_record(team, assistant, stored_input, validated)
                    status = "stored"
                result.append(StoredInputMetadata(stored_input, kind, label, description, status, generation))
            return tuple(result)

    def retain_declared(self, team_id: object, assistant_id: object, declared_ids: object) -> bool:
        """Discard values removed from the current Assistant contract."""
        team = _team_id(team_id)
        assistant = _component_id(assistant_id, "Assistant id")
        declared = set(_declared_ids(declared_ids))
        with self._lock:
            state = self._read_state_for_update()
            records = _PRIVATE_STATE.records(state, team, assistant, create=False)
            undeclared = set(records) - declared
            if not undeclared:
                return False
            for stored_input in undeclared:
                records.pop(stored_input)
            _PRIVATE_STATE.prune_empty_records(state, team, assistant)
            self._write_state(state)
            return True

    def delete(self, team_id: object, assistant_id: object, stored_input_id: object) -> bool:
        team = _team_id(team_id)
        assistant = _component_id(assistant_id, "Assistant id")
        stored_input = _component_id(stored_input_id, "Stored Input id")
        with self._lock:
            state = self._read_state_for_update()
            records = _PRIVATE_STATE.records(state, team, assistant, create=False)
            if records.pop(stored_input, None) is None:
                return False
            _PRIVATE_STATE.prune_empty_records(state, team, assistant)
            self._write_state(state)
            return True

    def delete_assistant(self, team_id: object, assistant_id: object) -> bool:
        team = _team_id(team_id)
        assistant = _component_id(assistant_id, "Assistant id")
        with self._lock:
            state = self._read_state_for_update()
            removed = _PRIVATE_STATE.delete_assistant(state, team, assistant)
            if removed:
                self._write_state(state)
            return removed

    def delete_team(self, team_id: object) -> bool:
        team = _team_id(team_id)
        with self._lock:
            state = self._read_state_for_update()
            removed = _PRIVATE_STATE.delete_team(state, team)
            if removed:
                self._write_state(state)
            return removed

    def delete_all(self) -> bool:
        """Atomically purge all Stored Inputs during an owned Space reset."""
        with self._lock:
            state = self._read_state_for_update()
            if not _PRIVATE_STATE.has_records(state):
                return False
            self._write_state(private_state.empty_state())
            return True
