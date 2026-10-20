"""A Team's Sign-ins: sealed Chrome profiles a person signed in with (ADR-0117, Sign-ins amendment items 1, 5, 6, 11).

Each Sign-in's capsule is kept as numbered chunk files, each sealed (AES-256-GCM) under a keyring apart from the state
and bound to the Team, its incarnation (the Team network's id), the Sign-in, its generation, and the chunk's place, so a
chunk never moves between capsules. Its record (site, label, times, chunk count, writer image) is sealed too, bound to
the Team and the Sign-in, so neither the capsule nor its metadata can be altered without the key. A Sign-in lives 30
days at most and 7 days unused; an expired one is deleted when read. A Save fences against Forget, the Team's deletion,
and a Space reset: a commit whose Team was cleared meanwhile, or that its caller no longer admits, keeps nothing.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from local.validation import validate_team_id
from protocol.http.v1 import sign_in as sign_in_protocol
from storage import private_state

STATE_ROOT = Path("/var/lib/shimpz-local/browser-sign-ins/state")
KEY_PATH = Path("/var/lib/shimpz-local/browser-sign-ins/key/aes256.key")
SCHEMA = 1
MAX_SIGN_INS = sign_in_protocol.MAX_SIGN_INS
MAX_STATE_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 4096
CHUNK_BYTES = 1024 * 1024
MAX_CHUNKS = 160
LIFETIME_SECONDS = 30 * 24 * 3600
IDLE_SECONDS = 7 * 24 * 3600
_RECORD_KEYS = frozenset(
    {
        "team_id",
        "sign_in_id",
        "incarnation",
        "generation",
        "site",
        "label",
        "created_at",
        "last_used_at",
        "expires_at",
        "chunks",
        "writer",
    }
)


class SignInError(RuntimeError):
    """The Sign-ins could not be read or kept as their contract requires."""


_PRIVATE = private_state.PrivateState(
    SignInError,
    "the Sign-ins state is malformed",
    "a Sign-in record is malformed",
    MAX_RECORD_BYTES * 2 + 128,
)


def _record_aad(team_id: str, sign_in_id: str) -> bytes:
    return "\0".join(("shimpz-sign-in-record", team_id, sign_in_id)).encode()


def _chunk_aad(record: dict[str, object], index: int) -> bytes:
    parts = ("shimpz-sign-in-capsule", record["team_id"], record["incarnation"], record["sign_in_id"])
    return "\0".join((*parts, str(record["generation"]), str(index))).encode()


class SignInStore:
    """Every Team's Sign-ins on this Local."""

    def __init__(
        self,
        state_root: Path = STATE_ROOT,
        key_path: Path = KEY_PATH,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        index, self._key_path = _PRIVATE.separate_paths(state_root / "index.json", key_path, "Sign-ins")
        self._root = index.parent
        self._index = index
        self._now = now
        self._lock = threading.RLock()
        # Each Team's fence: a Save commits only if no Forget-all, Team deletion, or reset cleared that Team meanwhile.
        self._fences: dict[str, int] = {}

    # State

    def _key(self, *, create: bool = False) -> bytes:
        return _PRIVATE.key(self._key_path, "Sign-ins keyring", allow_create=create)

    def _state(self) -> dict[str, dict[str, object]]:
        payload = _PRIVATE.read_private_file(self._index, MAX_STATE_BYTES, "Sign-ins state")
        if payload is None:
            return {}
        try:
            value = json.loads(payload)
        except ValueError as exc:
            raise SignInError("the Sign-ins state is malformed") from exc
        if not isinstance(value, dict) or set(value) != {"schema", "teams"} or value["schema"] != SCHEMA:
            raise SignInError("the Sign-ins state is malformed")
        teams = value["teams"]
        if not isinstance(teams, dict) or not all(isinstance(records, dict) for records in teams.values()):
            raise SignInError("the Sign-ins state is malformed")
        return teams

    def _write(self, teams: dict[str, dict[str, object]]) -> None:
        kept = {team: records for team, records in teams.items() if records}
        _PRIVATE.write_json(self._index, {"schema": SCHEMA, "teams": kept}, MAX_STATE_BYTES, "Sign-ins state")

    def _open(self, team_id: str, sign_in_id: str, envelope: object) -> dict[str, object]:
        _PRIVATE.check_identified_envelope(envelope, MAX_RECORD_BYTES, "a Sign-in record is malformed")
        plaintext = _PRIVATE.open_identified_envelope(
            self._key(), envelope, _record_aad(team_id, sign_in_id), MAX_RECORD_BYTES, "a Sign-in record is invalid"
        )
        try:
            record = json.loads(plaintext)
        except ValueError as exc:
            raise SignInError("a Sign-in record is invalid") from exc
        if (
            not isinstance(record, dict)
            or set(record) != _RECORD_KEYS
            or record["team_id"] != team_id
            or record["sign_in_id"] != sign_in_id
        ):
            raise SignInError("a Sign-in record is invalid")
        return record

    def _seal(self, record: dict[str, object]) -> dict[str, str]:
        plaintext = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        return private_state.seal_identified(
            self._key(create=True), plaintext, _record_aad(str(record["team_id"]), str(record["sign_in_id"]))
        )

    def _capsule_dir(self, sign_in_id: str, generation: int) -> Path:
        return self._root / "capsules" / f"{sign_in_id}-{generation}"

    def _expired(self, record: dict[str, object]) -> bool:
        now = self._now()
        return now >= float(record["expires_at"]) or now >= float(record["last_used_at"]) + IDLE_SECONDS

    def _drop(self, teams: dict[str, dict[str, object]], team_id: str, sign_in_id: str, generation: int) -> None:
        teams.get(team_id, {}).pop(sign_in_id, None)
        self._write(teams)
        shutil.rmtree(self._capsule_dir(sign_in_id, generation), ignore_errors=True)

    # Reading

    def list(self, team_id: str) -> list[dict[str, object]]:
        """The Team's current Sign-ins, newest first; an expired one is deleted as it is read."""
        team_id = validate_team_id(team_id)
        with self._lock:
            teams = self._state()
            listed = []
            for sign_in_id, envelope in sorted(teams.get(team_id, {}).items()):
                record = self._open(team_id, sign_in_id, envelope)
                if self._expired(record):
                    self._drop(teams, team_id, sign_in_id, int(record["generation"]))
                    continue
                listed.append(_public(record))
            return sorted(listed, key=lambda item: item["created_at"], reverse=True)

    # Saving

    def draft(self, team_id: str, incarnation: str) -> Draft:
        """A new Sign-in's capsule, sealed chunk by chunk as it arrives; nothing is kept until it commits."""
        team_id = validate_team_id(team_id)
        with self._lock:
            if len(self._state().get(team_id, {})) >= MAX_SIGN_INS:
                raise SignInError("the Team keeps as many Sign-ins as it may")
            return Draft(self, team_id, incarnation, self._fences.get(team_id, 0))

    def capsule(self, team_id: str, sign_in_id: str) -> Iterator[bytes]:
        """The Sign-in's capsule, chunk by chunk, each authenticated before it is handed out."""
        team_id = validate_team_id(team_id)
        with self._lock:
            envelope = self._state().get(team_id, {}).get(sign_in_id)
            if envelope is None:
                raise SignInError("the Sign-in is absent")
            record = self._open(team_id, sign_in_id, envelope)
        cipher = AESGCM(self._key())
        directory = self._capsule_dir(sign_in_id, int(record["generation"]))
        for index in range(int(record["chunks"])):
            try:
                sealed = (directory / f"{index:05d}.chunk").read_bytes()
                yield cipher.decrypt(sealed[:12], sealed[12:], _chunk_aad(record, index))
            except (OSError, InvalidTag) as exc:
                raise SignInError("the Sign-in's capsule is unreadable or not its own") from exc

    # Forgetting

    def forget(self, team_id: str, sign_in_id: str) -> bool:
        """Delete one Sign-in; whether it existed. An absent one is no failure."""
        team_id = validate_team_id(team_id)
        if not sign_in_protocol.ID_RE.fullmatch(sign_in_id):
            raise SignInError("the Sign-in id is not admissible")
        with self._lock:
            teams = self._state()
            envelope = teams.get(team_id, {}).get(sign_in_id)
            if envelope is None:
                return False
            record = self._open(team_id, sign_in_id, envelope)
            self._drop(teams, team_id, sign_in_id, int(record["generation"]))
            return True

    def delete_team(self, team_id: str) -> bool:
        """Delete every Sign-in of a deleted Team and fence its Saves under way; prove none is left."""
        team_id = validate_team_id(team_id)
        with self._lock:
            self._fences[team_id] = self._fences.get(team_id, 0) + 1
            teams = self._state()
            records = teams.pop(team_id, {})
            for sign_in_id, envelope in records.items():
                record = self._open(team_id, sign_in_id, envelope)
                shutil.rmtree(self._capsule_dir(sign_in_id, int(record["generation"])), ignore_errors=True)
            self._write(teams)
            return team_id not in self._state()

    def delete_all(self) -> None:
        """A Space reset: every Sign-in and every capsule is deleted, and every Save under way fenced."""
        with self._lock:
            for team_id in set(self._fences) | set(self._state()):
                self._fences[team_id] = self._fences.get(team_id, 0) + 1
            shutil.rmtree(self._root / "capsules", ignore_errors=True)
            self._index.unlink(missing_ok=True)
            if (self._root / "capsules").exists() or self._index.exists():
                raise SignInError("the Sign-ins were not all deleted")


class Draft:
    """One Sign-in being saved: its chunks sealed as they arrive, its record written only by `commit`."""

    def __init__(self, store: SignInStore, team_id: str, incarnation: str, fence: int) -> None:
        self._store = store
        self._fence = fence
        now = store._now()
        self.record: dict[str, object] = {
            "team_id": team_id,
            "sign_in_id": secrets.token_hex(16),
            "incarnation": incarnation,
            "generation": 1,
            "site": "",
            "label": "",
            "created_at": now,
            "last_used_at": now,
            "expires_at": now + LIFETIME_SECONDS,
            "chunks": 0,
            "writer": "",
        }
        self._directory = store._capsule_dir(str(self.record["sign_in_id"]), 1)
        self._cipher: AESGCM | None = None

    def add(self, chunk: bytes) -> None:
        """Seal one more chunk of the capsule."""
        count = int(self.record["chunks"])
        if not chunk or len(chunk) > CHUNK_BYTES or count >= MAX_CHUNKS:
            raise SignInError("the capsule is not within its bounds")
        if self._cipher is None:
            self._cipher = AESGCM(self._store._key(create=True))
            self._directory.mkdir(parents=True, mode=0o700)
        nonce = os.urandom(12)
        sealed = nonce + self._cipher.encrypt(nonce, chunk, _chunk_aad(self.record, count))
        private_state.replace_durably(self._directory / f"{count:05d}.chunk", sealed)
        self.record["chunks"] = count + 1

    def commit(self, site: object, label: object, writer: str, admitted: Callable[[], bool]) -> dict[str, object]:
        """Keep the Sign-in, only if `admitted()` holds and no clear of its Team came since the draft began."""
        site, label = sign_in_protocol.canonical_site(site), sign_in_protocol.canonical_label(label)
        if site is None or label is None or not self.record["chunks"]:
            raise SignInError("the Sign-in's site, label, or capsule is not admissible")
        self.record.update(site=site, label=label, writer=writer)
        store, team_id = self._store, str(self.record["team_id"])
        with store._lock:
            if store._fences.get(team_id, 0) != self._fence or not admitted():
                raise SignInError("the Sign-in was revoked before it was kept")
            private_state.fsync_directory(self._directory)
            teams = store._state()
            teams.setdefault(team_id, {})[str(self.record["sign_in_id"])] = store._seal(self.record)
            store._write(teams)
        return _public(self.record)

    def discard(self) -> None:
        shutil.rmtree(self._directory, ignore_errors=True)


def _public(record: dict[str, object]) -> dict[str, object]:
    """What a Supervisor sees of one Sign-in: never the capsule or the incarnation."""
    expires = min(float(record["expires_at"]), float(record["last_used_at"]) + IDLE_SECONDS)
    return {
        "sign_in_id": record["sign_in_id"],
        "site": record["site"],
        "label": record["label"],
        "created_at": _iso(float(record["created_at"])),
        "last_used_at": _iso(float(record["last_used_at"])),
        "expires_at": _iso(expires),
    }


def _iso(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))
