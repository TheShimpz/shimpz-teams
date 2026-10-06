"""Encrypted, short-lived local integration continuations."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from integrations import challenge_store as integration_challenge_store
from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import strict_json
from storage import private_state

STATE_PATH = Path("/var/lib/shimpz-local/chat-continuations/state/continuations.json")
KEY_PATH = Path("/var/lib/shimpz-local/chat-continuations/key/aes256.key")
SCHEMA_VERSION = 1
MAX_CONTINUATIONS = 32
MAX_PLAINTEXT_BYTES = 256 * 1024
MAX_STATE_BYTES = 12 * 1024 * 1024
MAX_BINDINGS = 64
MAX_BINDING_BYTES = 640
MAX_TTL_SECONDS = 900
_KINDS = frozenset({"human", "integrations"})


class ContinuationStoreError(RuntimeError):
    """Encrypted continuation state is invalid or unavailable."""


class ContinuationNotFoundError(ContinuationStoreError):
    """The continuation is absent, expired, consumed, or owned by another challenge."""


_PRIVATE = private_state.PrivateState(
    ContinuationStoreError,
    "continuation state is malformed",
    "continuation envelope is malformed",
    (MAX_PLAINTEXT_BYTES * 2) + 128,
)


@dataclass(frozen=True, slots=True, repr=False)
class StoredContinuation:
    team_id: str
    kind: str
    challenge_id: str
    expires_at: int
    generation: int
    bindings: tuple[str, ...]
    payload: bytes


def _team_id(value: object) -> str:
    if not isinstance(value, str) or http_payload.TEAM_ID_RE.fullmatch(value) is None:
        raise ContinuationStoreError("continuation Team is invalid")
    return value


def _kind(value: object) -> str:
    if not isinstance(value, str) or value not in _KINDS:
        raise ContinuationStoreError("continuation kind is invalid")
    return value


def _challenge_id(value: object) -> str:
    if not isinstance(value, str) or integration_challenge_store.CHALLENGE_ID_RE.fullmatch(value) is None:
        raise ContinuationStoreError("continuation challenge is invalid")
    return value


def _bindings(value: object) -> tuple[str, ...]:
    if not isinstance(value, Iterable) or isinstance(value, str | bytes | Mapping):
        raise ContinuationStoreError("continuation bindings are invalid")
    result: list[str] = []
    for item in value:
        if (
            len(result) == MAX_BINDINGS
            or not isinstance(item, str)
            or not item
            or item != item.strip()
            or not item.isprintable()
        ):
            raise ContinuationStoreError("continuation bindings are invalid")
        try:
            encoded = item.encode("utf-8")
        except UnicodeError as exc:
            raise ContinuationStoreError("continuation bindings are invalid") from exc
        if len(encoded) > MAX_BINDING_BYTES:
            raise ContinuationStoreError("continuation bindings are invalid")
        result.append(item)
    if not result or len(set(result)) != len(result):
        raise ContinuationStoreError("continuation bindings are invalid")
    return tuple(sorted(result))


def _aad(
    team_id: str,
    kind: str,
    challenge_id: str,
    expires_at: int,
    generation: int,
    bindings: tuple[str, ...],
) -> bytes:
    return json.dumps(
        [
            "shimpz-local-chat-continuation-v1",
            team_id,
            kind,
            challenge_id,
            expires_at,
            generation,
            list(bindings),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def _empty_state() -> dict[str, object]:
    return {"schema": SCHEMA_VERSION, "records": {}}


def _record(value: object, expected_team: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "team_id",
        "kind",
        "challenge_id",
        "expires_at",
        "generation",
        "bindings",
        "envelope",
    }:
        raise ContinuationStoreError("continuation record is malformed")
    team = _team_id(value["team_id"])
    kind = _kind(value["kind"])
    challenge = _challenge_id(value["challenge_id"])
    expires_at = value["expires_at"]
    generation = value["generation"]
    bindings = _bindings(value["bindings"])
    if (
        team != expected_team
        or type(expires_at) is not int
        or not 1 <= expires_at < 2**63
        or type(generation) is not int
        or not 1 <= generation <= 2**31 - 1
    ):
        raise ContinuationStoreError("continuation record is malformed")
    _PRIVATE.check_envelope(value["envelope"], MAX_PLAINTEXT_BYTES, "continuation record is malformed")
    value["kind"] = kind
    value["challenge_id"] = challenge
    value["bindings"] = list(bindings)
    return value


def _decode_json(payload: bytes) -> object:
    try:
        return strict_json.loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ContinuationStoreError("continuation state is not valid JSON") from exc


def _state(value: object) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "records"}
        or value["schema"] != SCHEMA_VERSION
        or not isinstance(value["records"], dict)
        or len(value["records"]) > MAX_CONTINUATIONS
    ):
        raise ContinuationStoreError("continuation state has an unsupported shape")
    for team, record in value["records"].items():
        _team_id(team)
        _record(record, team)
    return value


class EncryptedContinuationStore:
    """Atomically keep at most one encrypted continuation per Team."""

    def __init__(
        self,
        state_path: Path = STATE_PATH,
        key_path: Path = KEY_PATH,
        *,
        now: Callable[[], float] = time.time,
        capacity: int = MAX_CONTINUATIONS,
    ) -> None:
        self.state_path, self.key_path = _PRIVATE.separate_paths(state_path, key_path, "continuation")
        if not callable(now) or type(capacity) is not int or not 1 <= capacity <= MAX_CONTINUATIONS:
            raise ValueError("continuation store configuration is invalid")
        self._now = now
        self._capacity = capacity
        self._lock = threading.RLock()

    def _read_state(self) -> dict[str, object]:
        payload = _PRIVATE.read_private_file(self.state_path, MAX_STATE_BYTES, "continuation state")
        return _empty_state() if payload is None else _state(_decode_json(payload))

    def _write_state(self, state: Mapping[str, object]) -> None:
        _PRIVATE.write_json(self.state_path, _state(dict(state)), MAX_STATE_BYTES, "continuation state")

    def _key(self, *, allow_create: bool = False) -> bytes:
        return _PRIVATE.key(self.key_path, "continuation keyring", allow_create=allow_create)

    def put(
        self,
        team_id: object,
        kind: object,
        challenge_id: object,
        expires_at: object,
        bindings: object,
        payload: object,
    ) -> StoredContinuation:
        team = _team_id(team_id)
        suspension_kind = _kind(kind)
        challenge = _challenge_id(challenge_id)
        canonical_bindings = _bindings(bindings)
        now = int(self._now())
        if (
            type(expires_at) is not int
            or not now < expires_at <= now + MAX_TTL_SECONDS
            or not isinstance(payload, bytes)
            or not 1 <= len(payload) <= MAX_PLAINTEXT_BYTES
        ):
            raise ContinuationStoreError("continuation payload is invalid")
        with self._lock:
            state = self._read_state()
            records = state["records"]
            if not isinstance(records, dict):
                raise ContinuationStoreError("continuation state is malformed")
            previous = records.get(team)
            if previous is None and len(records) >= self._capacity:
                raise ContinuationStoreError("continuation capacity reached")
            generation = int(previous["generation"]) + 1 if isinstance(previous, dict) else 1
            aad = _aad(team, suspension_kind, challenge, expires_at, generation, canonical_bindings)
            records[team] = {
                "team_id": team,
                "kind": suspension_kind,
                "challenge_id": challenge,
                "expires_at": expires_at,
                "generation": generation,
                "bindings": list(canonical_bindings),
                "envelope": private_state.seal(self._key(allow_create=not records), payload, aad),
            }
            self._write_state(state)
            return StoredContinuation(
                team,
                suspension_kind,
                challenge,
                expires_at,
                generation,
                canonical_bindings,
                payload,
            )

    def _resolved(self, team: str, raw: object) -> StoredContinuation:
        record = _record(raw, team)
        kind = str(record["kind"])
        challenge = str(record["challenge_id"])
        expires_at = int(record["expires_at"])
        generation = int(record["generation"])
        bindings = tuple(record["bindings"])
        plaintext = _PRIVATE.open_envelope(
            self._key(),
            record["envelope"],
            _aad(team, kind, challenge, expires_at, generation, bindings),
            MAX_PLAINTEXT_BYTES,
            "continuation envelope authentication failed",
        )
        if not 1 <= len(plaintext) <= MAX_PLAINTEXT_BYTES:
            raise ContinuationStoreError("decrypted continuation is malformed")
        return StoredContinuation(
            team,
            kind,
            challenge,
            expires_at,
            generation,
            bindings,
            plaintext,
        )

    def active(self) -> tuple[StoredContinuation, ...]:
        with self._lock:
            state = self._read_state()
            records = state["records"]
            if not isinstance(records, dict):
                raise ContinuationStoreError("continuation state is malformed")
            now = int(self._now())
            expired = [team for team, item in records.items() if int(item["expires_at"]) <= now]
            for team in expired:
                records.pop(team)
            if expired:
                self._write_state(state)
            return tuple(self._resolved(team, records[team]) for team in sorted(records))

    def drain_expired(self) -> tuple[StoredContinuation, ...]:
        """Atomically remove and return expired continuations for dependent cleanup."""
        with self._lock:
            state = self._read_state()
            records = state["records"]
            if not isinstance(records, dict):
                raise ContinuationStoreError("continuation state is malformed")
            now = int(self._now())
            teams = tuple(sorted(team for team, item in records.items() if int(item["expires_at"]) <= now))
            expired = tuple(self._resolved(team, records[team]) for team in teams)
            for team in teams:
                records.pop(team)
            if teams:
                self._write_state(state)
            return expired

    def current(self, team_id: object) -> StoredContinuation | None:
        team = _team_id(team_id)
        return next((item for item in self.active() if item.team_id == team), None)

    def delete(self, team_id: object, challenge_id: object | None = None) -> bool:
        team = _team_id(team_id)
        expected = _challenge_id(challenge_id) if challenge_id is not None else None
        with self._lock:
            state = self._read_state()
            records = state["records"]
            if not isinstance(records, dict):
                raise ContinuationStoreError("continuation state is malformed")
            raw = records.get(team)
            if raw is None:
                return False
            record = _record(raw, team)
            if expected is not None and record["challenge_id"] != expected:
                raise ContinuationNotFoundError("continuation is unavailable")
            records.pop(team)
            self._write_state(state)
            return True

    def clear(self) -> int:
        with self._lock:
            state = self._read_state()
            records = state["records"]
            if not isinstance(records, dict):
                raise ContinuationStoreError("continuation state is malformed")
            removed = len(records)
            if removed:
                records.clear()
                self._write_state(state)
            return removed
