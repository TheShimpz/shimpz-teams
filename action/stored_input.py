"""Encrypted Team custody for persistent Assistant-declared Action inputs."""

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from core import identifier
from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import strict_json
from storage import private_state

STATE_PATH = Path("/var/lib/shimpz-local/assistant-stored-inputs/state/stored-inputs.json")
KEY_PATH = Path("/var/lib/shimpz-local/assistant-stored-inputs/key/aes256.key")
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_VALUE_CHARACTERS = 1024
MAX_VALUE_BYTES = 16 * 1024
MAX_PLAINTEXT_BYTES = MAX_VALUE_BYTES + 160
MAX_STORED_INPUTS_PER_ASSISTANT = 8
MAX_TOTAL_RECORDS = 4096
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
    origin: str


def _team_id(value: object) -> str:
    return identifier.require(http_payload.canonical_team_id, value, StoredInputValidationError, "Team id is invalid")


def _component_id(
    value: object, label: str, canonical: Callable[[object], str | None] = http_payload.canonical_identifier
) -> str:
    return identifier.require(canonical, value, StoredInputValidationError, f"{label} is invalid")


def _assistant_id(value: object) -> str:
    return _component_id(value, "Assistant id", http_payload.canonical_assistant_id)


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


def _origin(value: object) -> str:
    if not isinstance(value, str) or http_payload.SHA256_RE.fullmatch(value) is None:
        raise StoredInputValidationError("Stored Input origin is invalid")
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
    if not isinstance(updated_at, str) or _TIMESTAMP.fullmatch(updated_at) is None:
        raise StoredInputStoreError("Stored Input state record is malformed")
    _PRIVATE_STATE.check_envelope(value.get("envelope"), MAX_PLAINTEXT_BYTES, "Stored Input state record is malformed")
    return value


def _validate_state(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"schema", "teams", "last_generation"} or value.get("schema") != 1:
        raise StoredInputStoreError("Stored Input state has an unsupported shape")
    teams = value.get("teams")
    if not isinstance(teams, dict):
        raise StoredInputStoreError("Stored Input state is malformed")
    last_generation = _PRIVATE_STATE.last_generation(value)
    total = 0
    for raw_team, raw_assistants in teams.items():
        total += _validate_assistants(raw_team, raw_assistants, last_generation)
        if total > MAX_TOTAL_RECORDS:
            raise StoredInputStoreError("Stored Input state exceeds its record limit")
    return value


def _validate_assistants(raw_team: object, raw_assistants: object, last_generation: int) -> int:
    try:
        _team_id(raw_team)
    except StoredInputValidationError as exc:
        raise StoredInputStoreError("Stored Input state is malformed") from exc
    if not isinstance(raw_assistants, dict):
        raise StoredInputStoreError("Stored Input state is malformed")
    count = 0
    for raw_assistant, raw_records in raw_assistants.items():
        try:
            _assistant_id(raw_assistant)
        except StoredInputValidationError as exc:
            raise StoredInputStoreError("Stored Input state is malformed") from exc
        if not isinstance(raw_records, dict) or len(raw_records) > MAX_STORED_INPUTS_PER_ASSISTANT:
            raise StoredInputStoreError("Stored Input state is malformed")
        for raw_stored_input, raw_record in raw_records.items():
            try:
                _component_id(raw_stored_input, "Stored Input id")
            except StoredInputValidationError as exc:
                raise StoredInputStoreError("Stored Input state is malformed") from exc
            if _record_metadata(_validate_record(raw_record))[1] > last_generation:
                raise StoredInputStoreError("Stored Input state is malformed")
            count += 1
    return count


def _aad(
    reference: tuple[str, str, str],
    record: Mapping[str, object],
) -> bytes:
    kind, generation = _record_metadata(record)
    return json.dumps(
        [
            "shimpz-stored-input-v1",
            *reference,
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
            raw_kind = raw_spec.kind
            raw_label = raw_spec.label
            raw_description = raw_spec.description
        except AttributeError, TypeError:
            if not isinstance(raw_spec, Mapping) or not {"kind", "label", "description"} <= set(raw_spec):
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


_POLICY = private_state.RecordPolicy(
    private=_PRIVATE_STATE,
    label="Stored Input",
    record_label="Stored Input id",
    maximum_state_bytes=MAX_STATE_BYTES,
    records_per_assistant=MAX_STORED_INPUTS_PER_ASSISTANT,
    validation_error=StoredInputValidationError,
    team_id=_team_id,
    assistant_id=_assistant_id,
    component_id=_component_id,
    decode_state=_strict_json,
    validate_state=_validate_state,
)


class StoredInputStore(private_state.RecordStore):
    """File-backed AES-GCM storage isolated from OAuth and continuation state."""

    def __init__(self, state_path: Path = STATE_PATH, key_path: Path = KEY_PATH) -> None:
        super().__init__(state_path, key_path, _POLICY)

    @staticmethod
    def _plaintext(value: str, origin: str) -> bytes:
        payload = json.dumps(
            {"origin": origin, "value": value},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_PLAINTEXT_BYTES:
            raise StoredInputValidationError("Stored Input value is invalid")
        return payload

    def _sealed_record(
        self,
        reference: tuple[str, str, str],
        kind: str,
        value: str,
        origin: str,
        generation: int,
        key: bytes,
    ) -> dict[str, object]:
        record: dict[str, object] = {
            "kind": kind,
            "generation": generation,
            "updated_at": private_state.timestamp(),
            "envelope": {},
        }
        record["envelope"] = private_state.seal(key, self._plaintext(value, origin), _aad(reference, record))
        return record

    def seal(
        self,
        team_id: object,
        assistant_id: object,
        stored_input_id: object,
        kind: object,
        value: object,
        origin: object,
    ) -> int:
        """Encrypt a successfully consumed value and atomically advance its generation."""
        team = _team_id(team_id)
        assistant = _assistant_id(assistant_id)
        stored_input = _component_id(stored_input_id, "Stored Input id")
        canonical_kind = _kind(kind)
        canonical_value = _secret_value(value)
        canonical_origin = _origin(origin)
        with self._lock:
            state = self._read_state_for_update()
            key = self._key(allow_create=not _PRIVATE_STATE.has_records(state))
            records = _PRIVATE_STATE.records(state, team, assistant, create=True)
            if stored_input not in records and len(records) >= MAX_STORED_INPUTS_PER_ASSISTANT:
                raise StoredInputStoreError("Stored Input capacity reached")
            generation = _PRIVATE_STATE.advance_generation(state)
            records[stored_input] = self._sealed_record(
                (team, assistant, stored_input),
                canonical_kind,
                canonical_value,
                canonical_origin,
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
        assistant = _assistant_id(assistant_id)
        stored_input = _component_id(stored_input_id, "Stored Input id")
        _kind(kind)  # Validate the caller contract before any state lookup.
        with self._lock:
            records = _PRIVATE_STATE.records(self._read_state(), team, assistant, create=False)
            if stored_input not in records:
                raise StoredInputMissingError("Stored Input is not configured")
            record = _validate_record(records[stored_input])
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
        plaintext = _PRIVATE_STATE.open_envelope(
            self._key(),
            validated["envelope"],
            _aad((team, assistant, stored_input), validated),
            MAX_PLAINTEXT_BYTES,
            "Stored Input envelope authentication failed",
        )
        value, origin = self._decrypted_value(plaintext)
        return StoredInputValue(value, generation, origin)

    @staticmethod
    def _decrypted_value(plaintext: bytes) -> tuple[str, str]:
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise StoredInputStoreError("decrypted Stored Input is malformed")
        decoded = _strict_json(plaintext)
        if not isinstance(decoded, dict) or set(decoded) != {"origin", "value"}:
            raise StoredInputStoreError("decrypted Stored Input is malformed")
        try:
            return _secret_value(decoded["value"]), _origin(decoded["origin"])
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
        assistant = _assistant_id(assistant_id)
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
                _stored_kind, generation = _record_metadata(validated)
                result.append(StoredInputMetadata(stored_input, kind, label, description, "stored", generation))
            return tuple(result)

    def inventory(self, team_id: object, assistants: Iterable[object]) -> dict[str, object]:
        """Project authenticated declared identifiers and status without values or generations."""
        team = _team_id(team_id)
        inventory: list[dict[str, str]] = []
        seen: set[str] = set()
        for spec in assistants:
            assistant = _assistant_id(getattr(spec, "assistant_id", None))
            if assistant in seen:
                raise StoredInputValidationError("Stored Input Assistant inventory is ambiguous")
            seen.add(assistant)
            declarations = getattr(spec, "stored_inputs", None)
            inventory.extend(
                {
                    "assistant_id": assistant,
                    "stored_input_id": metadata.id,
                    "status": metadata.status,
                }
                for metadata in self.metadata(team, assistant, declarations)
            )
        inventory.sort(key=lambda item: (item["assistant_id"], item["stored_input_id"]))
        return {"team_id": team, "stored_inputs": inventory}

    def delete(self, team_id: object, assistant_id: object, stored_input_id: object) -> bool:
        team = _team_id(team_id)
        assistant = _assistant_id(assistant_id)
        stored_input = _component_id(stored_input_id, "Stored Input id")
        with self._lock:
            state = self._read_state_for_update()
            records = _PRIVATE_STATE.records(state, team, assistant, create=False)
            if records.pop(stored_input, None) is None:
                return False
            _PRIVATE_STATE.prune_empty_records(state, team, assistant)
            self._write_state(state)
            return True
