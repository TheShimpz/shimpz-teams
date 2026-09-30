"""Local Routine persistence: one private state file per Team and encrypted frozen continuations (ADR-0086).

A Team's Routines, runs, and notices live in one private JSON file that every transition replaces atomically. A frozen
run's secret-free continuation is encrypted separately; it is written before the state that references it, so a crash
leaves at worst an unreferenced continuation, which recovery removes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core import strict_json
from protocol.http.v1 import routine as http_routine
from routine import record
from storage import private_state

ROOT = Path("/var/lib/shimpz-local/routines/state")
KEY_PATH = Path("/var/lib/shimpz-local/routines/key/aes256.key")
SCHEMA = 1
# Holds the worst case: every Routine, run, and notice at its bound, with 4-byte characters throughout.
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_CONTINUATION_BYTES = 256 * 1024
_TEAM_ID_RE = re.compile(r"[a-z0-9_]{1,40}\Z")
_RUN_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TEAM_DIR_RE = re.compile(r"[0-9a-f]{64}\Z")
_DAY_RE = re.compile(r"(?:\d{4}-\d{2}-\d{2})?\Z")
_CONTINUATION_NAME_RE = re.compile(r"[0-9a-f]{32}\.continuation\Z")
_ROUTINE_FIELDS = frozenset(
    {
        "routine_id",
        "quote",
        "schedule",
        "timezone",
        "assistants",
        "anchor",
        "next_run_at",
        "needs_reconfirm",
        "deleting",
        "gap_started_at",
        "missed",
        "reported_missed",
    }
)
_RUN_FIELDS = frozenset(
    {
        "run_id",
        "routine_id",
        "status",
        "scheduled_at",
        "lease_sha256",
        "lease_key",
        "lease_expires_at",
        "active_seconds_left",
        "request_kind",
        "assistant_id",
        "action",
        "generation",
        "batch",
        "notice_version",
        "held_actions",
    }
)
_NOTICE_FIELDS = frozenset({"notice_id", "routine_id", "run_id", "outcome", "created_at", "detail", "version", "quote"})
_STATE_FIELDS = frozenset(
    {"schema", "team_id", "routines", "runs", "notices", "served_at", "starts_day", "starts", "discards"}
)


class RoutineStoreError(RuntimeError):
    """Routine state is invalid or unavailable; callers fail closed."""


_PRIVATE = private_state.PrivateState(
    RoutineStoreError,
    "Routine state is malformed",
    "Routine continuation is malformed",
    (MAX_CONTINUATION_BYTES * 2) + 128,
)


def _team_id(value: object) -> str:
    if not isinstance(value, str) or _TEAM_ID_RE.fullmatch(value) is None:
        raise RoutineStoreError("Routine Team is invalid")
    return value


def _run_id(value: object) -> str:
    if not isinstance(value, str) or _RUN_ID_RE.fullmatch(value) is None:
        raise RoutineStoreError("Routine run is invalid")
    return value


def _require(condition: bool) -> None:
    if not condition:
        raise RoutineStoreError("Routine state is malformed")


def _instant(value: object) -> int:
    _require(type(value) is int and 0 <= value < 2**40)
    return value


def _count(value: object) -> int:
    _require(type(value) is int and 0 <= value <= record.MAX_COUNTED_MISSES * 1024)
    return value


def _encode(state: record.TeamRoutines, team_id: str) -> bytes:
    def routine_value(item: record.Routine) -> dict[str, object]:
        value = {name: getattr(item, name) for name in _ROUTINE_FIELDS}
        value["assistants"] = [list(pair) for pair in item.assistants]
        return value

    def run_value(item: record.Run) -> dict[str, object]:
        value = {name: getattr(item, name) for name in _RUN_FIELDS}
        value["batch"] = list(item.batch)
        value["held_actions"] = [list(pair) for pair in item.held_actions]
        return value

    payload = {
        "schema": SCHEMA,
        "team_id": team_id,
        "routines": [routine_value(item) for item in state.routines],
        "runs": [run_value(item) for item in state.runs],
        "notices": [{name: getattr(item, name) for name in _NOTICE_FIELDS} for item in state.notices],
        "served_at": state.served_at,
        "starts_day": state.starts_day,
        "starts": state.starts,
        "discards": [list(item) for item in state.discards],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _decode_routine(value: object) -> record.Routine:
    _require(isinstance(value, dict) and set(value) == _ROUTINE_FIELDS)
    schedule = http_routine.canonical_schedule(value["schedule"])
    assistants = value["assistants"]
    _require(
        isinstance(value["routine_id"], str)
        and http_routine.ROUTINE_ID_RE.fullmatch(value["routine_id"]) is not None
        and http_routine.canonical_quote(value["quote"]) is not None
        and schedule is not None
        and http_routine.canonical_timezone(value["timezone"]) is not None
        and isinstance(assistants, list)
        and 0 < len(assistants) <= http_routine.MAX_NOTICE_ASSISTANTS
        and all(
            isinstance(pair, list)
            and len(pair) == 2
            and isinstance(pair[0], str)
            and isinstance(pair[1], str)
            and http_routine.ASSISTANT_ID_RE.fullmatch(pair[0]) is not None
            and _DIGEST_RE.fullmatch(pair[1]) is not None
            for pair in assistants
        )
        and type(value["needs_reconfirm"]) is bool
        and type(value["deleting"]) is bool
    )
    return record.Routine(
        routine_id=value["routine_id"],
        quote=value["quote"],
        schedule=schedule,
        timezone=value["timezone"],
        assistants=tuple((pair[0], pair[1]) for pair in assistants),
        anchor=_instant(value["anchor"]),
        next_run_at=_instant(value["next_run_at"]),
        needs_reconfirm=value["needs_reconfirm"],
        deleting=value["deleting"],
        gap_started_at=_instant(value["gap_started_at"]),
        missed=_count(value["missed"]),
        reported_missed=_count(value["reported_missed"]),
    )


def _decode_run(value: object) -> record.Run:
    _require(isinstance(value, dict) and set(value) == _RUN_FIELDS)
    batch = value["batch"]
    strings = ("run_id", "routine_id", "status", "lease_sha256", "lease_key", "request_kind", "assistant_id", "action")
    _require(all(isinstance(value[name], str) for name in strings) and isinstance(value["generation"], str))
    run_id = value["run_id"]
    _require(
        _RUN_ID_RE.fullmatch(run_id) is not None
        and http_routine.ROUTINE_ID_RE.fullmatch(value["routine_id"]) is not None
        and _generation_of(run_id, value["generation"])
        and isinstance(batch, list)
        and len(batch) == 2
        and all(isinstance(part, str) for part in batch)
        and type(value["active_seconds_left"]) is int
        and value["active_seconds_left"] <= record.ACTIVE_SECONDS
        and type(value["notice_version"]) is int
        and value["notice_version"] >= 0
        and http_routine.canonical_notice_detail("uncertain", {"actions": value["held_actions"]}) is not None
        and (value["status"] == "uncertain" or value["held_actions"] == [])
    )
    unleased = value["lease_sha256"] == "" and value["lease_key"] == "" and value["lease_expires_at"] == 0
    no_request = value["request_kind"] == "" and value["assistant_id"] == "" and value["action"] == ""
    no_batch = batch == ["", ""]
    # Each status admits exactly its own fields, so teardown and recovery never act on a mixed record.
    _require(
        {
            "leased": _HEX64_RE.fullmatch(value["lease_sha256"]) is not None
            and (value["lease_key"] == record.HUMAN_LEASE or _HEX64_RE.fullmatch(value["lease_key"]) is not None)
            and no_request
            and no_batch,
            "frozen": unleased
            and value["request_kind"] in {"human", "integrations"}
            and http_routine.ASSISTANT_ID_RE.fullmatch(value["assistant_id"]) is not None
            and http_routine.ACTION_ID_RE.fullmatch(value["action"]) is not None
            and no_batch,
            "uncertain": unleased
            and no_request
            and value["generation"] != ""
            and batch[0] == value["generation"]
            and _HEX64_RE.fullmatch(batch[1]) is not None,
        }.get(value["status"], False)
    )
    return record.Run(
        run_id=value["run_id"],
        routine_id=value["routine_id"],
        status=value["status"],
        scheduled_at=_instant(value["scheduled_at"]),
        lease_sha256=value["lease_sha256"],
        lease_key=value["lease_key"],
        lease_expires_at=_instant(value["lease_expires_at"]),
        active_seconds_left=value["active_seconds_left"],
        request_kind=value["request_kind"],
        assistant_id=value["assistant_id"],
        action=value["action"],
        generation=value["generation"],
        batch=(batch[0], batch[1]),
        notice_version=value["notice_version"],
        held_actions=tuple((pair[0], pair[1]) for pair in value["held_actions"]),
    )


def _decode_notice(value: object) -> record.Notice:
    _require(isinstance(value, dict) and set(value) == _NOTICE_FIELDS)
    detail = http_routine.canonical_notice_detail(value["outcome"], value["detail"])
    _require(
        detail is not None
        and isinstance(value["notice_id"], str)
        and _RUN_ID_RE.fullmatch(value["notice_id"]) is not None
        and isinstance(value["routine_id"], str)
        and http_routine.ROUTINE_ID_RE.fullmatch(value["routine_id"]) is not None
        and isinstance(value["run_id"], str)
        and (value["run_id"] == "" or _RUN_ID_RE.fullmatch(value["run_id"]) is not None)
        and type(value["version"]) is int
        and value["version"] >= 1
        and http_routine.canonical_quote(value["quote"]) is not None
    )
    return record.Notice(
        value["notice_id"],
        value["routine_id"],
        value["run_id"],
        value["outcome"],
        _instant(value["created_at"]),
        detail,
        value["version"],
        value["quote"],
    )


def _generation_of(run_id: str, generation: object) -> bool:
    return isinstance(generation, str) and (
        generation == "" or re.fullmatch(rf"[0-9a-f]{{64}}:routine:{run_id}", generation) is not None
    )


def _decode_discard(value: object) -> tuple[str, str]:
    _require(
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and _RUN_ID_RE.fullmatch(value[0]) is not None
        and _generation_of(value[0], value[1])
    )
    return value[0], value[1]


def _decode(payload: bytes, team_id: str) -> record.TeamRoutines:
    try:
        value = strict_json.loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RoutineStoreError("Routine state is not valid JSON") from exc
    _require(
        isinstance(value, dict)
        and set(value) == _STATE_FIELDS
        and value["schema"] == SCHEMA
        and value["team_id"] == team_id
        and isinstance(value["routines"], list)
        and len(value["routines"]) <= record.MAX_ROUTINES
        and isinstance(value["runs"], list)
        and len(value["runs"]) <= record.MAX_ROUTINES
        and isinstance(value["notices"], list)
        and len(value["notices"]) <= record.MAX_UNDELIVERED_NOTICES + record.MAX_ROUTINES
        and isinstance(value["starts_day"], str)
        and _DAY_RE.fullmatch(value["starts_day"]) is not None
        and type(value["starts"]) is int
        and 0 <= value["starts"] <= record.MAX_DAILY_STARTS
        and isinstance(value["discards"], list)
        and len(value["discards"]) <= record.MAX_DISCARDS
    )
    state = record.TeamRoutines(
        routines=tuple(_decode_routine(item) for item in value["routines"]),
        runs=tuple(_decode_run(item) for item in value["runs"]),
        notices=tuple(_decode_notice(item) for item in value["notices"]),
        served_at=_instant(value["served_at"]),
        starts_day=value["starts_day"],
        starts=value["starts"],
        discards=tuple(_decode_discard(item) for item in value["discards"]),
    )
    identifiers = [item.routine_id for item in state.routines]
    _require(
        len(set(identifiers)) == len(identifiers)
        and len({item.run_id for item in state.runs}) == len(state.runs)
        and len({item.notice_id for item in state.notices}) == len(state.notices)
        and len({item[0] for item in state.discards}) == len(state.discards)
        and all(item.routine_id in identifiers for item in state.runs)
    )
    return state


def _aad(team_id: str, run_id: str) -> bytes:
    return json.dumps(["shimpz-local-routine-continuation-v1", team_id, run_id], separators=(",", ":")).encode()


class RoutineStore:
    """Every Local Team's Routine state, changed only through serialized, atomic read-modify-write transitions."""

    def __init__(self, root: Path = ROOT, key_path: Path = KEY_PATH) -> None:
        self.root = Path(root)
        self.key_path = Path(key_path)
        if self.key_path.parent == self.root or self.root in self.key_path.parents:
            raise RoutineStoreError("Routine keyring must be separate from Routine state")
        self._guard = threading.Lock()
        self._key_lock = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}
        # A Space reset closes the store and bumps the epoch, so a write that began before it can never land after.
        self._closed = False
        self._epoch = 0

    def lock(self, team_id: object) -> threading.RLock:
        team = _team_id(team_id)
        with self._guard:
            return self._locks.setdefault(team, threading.RLock())

    def _team_dir(self, team_id: str) -> Path:
        return self.root / hashlib.sha256(team_id.encode()).hexdigest()

    def load(self, team_id: object) -> record.TeamRoutines:
        team = _team_id(team_id)
        payload = _PRIVATE.read_private_file(self._team_dir(team) / "state.json", MAX_STATE_BYTES, "Routine state")
        return record.TeamRoutines() if payload is None else _decode(payload, team)

    def _save(self, team: str, state: record.TeamRoutines) -> None:
        payload = _encode(state, team)
        _decode(payload, team)
        if len(payload) > MAX_STATE_BYTES:
            raise RoutineStoreError("Routine state exceeds its fixed byte limit")
        _PRIVATE.atomic_write(self._team_dir(team) / "state.json", payload, "Routine state")

    def _writable(self, epoch: int) -> None:
        with self._guard:
            if self._closed or self._epoch != epoch:
                raise RoutineStoreError("Routine state is being reset")

    def _current_epoch(self) -> int:
        with self._guard:
            return self._epoch

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Hold every Team's Routine lock with writes refused, as a Space reset needs; the epoch then moves on."""
        with self._guard:
            self._closed = True
            self._epoch += 1
            locks = list(self._locks.values())
        try:
            with ExitStack() as stack:
                for lock in locks:
                    stack.enter_context(lock)
                yield
        finally:
            with self._guard:
                self._closed = False

    def update[T](self, team_id: object, change: Callable[[record.TeamRoutines], tuple[record.TeamRoutines, T]]) -> T:
        """Apply one transition under the Team's lock and persist it before returning its result."""
        team = _team_id(team_id)
        epoch = self._current_epoch()
        with self.lock(team):
            self._writable(epoch)
            before = self.load(team)
            after, result = change(before)
            if after != before:
                self._save(team, after)
            return result

    def teams(self) -> tuple[str, ...]:
        """Every Team with Routine state, read from the state files themselves."""
        teams = []
        for name in self._owned_directories():
            directory = self.root / name
            payload = _PRIVATE.read_private_file(directory / "state.json", MAX_STATE_BYTES, "Routine state")
            if payload is None:
                continue
            try:
                team = _team_id(strict_json.loads(payload).get("team_id"))
            except (UnicodeDecodeError, ValueError, AttributeError) as exc:
                raise RoutineStoreError("Routine state is malformed") from exc
            _require(directory == self._team_dir(team))
            teams.append(team)
        return tuple(teams)

    def put_continuation(self, team_id: object, run_id: object, payload: object) -> None:
        team, run = _team_id(team_id), _run_id(run_id)
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_CONTINUATION_BYTES:
            raise RoutineStoreError("Routine continuation is invalid")
        epoch = self._current_epoch()
        with self.lock(team):
            self._writable(epoch)
            self._write_continuation(team, run, payload)

    def _write_continuation(self, team: str, run: str, payload: bytes) -> None:
        nonce = os.urandom(12)
        # One keyring for every Team: concurrent first writers must not each create a different key.
        with self._key_lock:
            key = _PRIVATE.key(self.key_path, "Routine keyring", allow_create=True)
        envelope = {
            "algorithm": "AES-256-GCM",
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(AESGCM(key).encrypt(nonce, payload, _aad(team, run))).decode("ascii"),
        }
        encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("ascii")
        _PRIVATE.atomic_write(self._team_dir(team) / f"{run}.continuation", encoded, "Routine continuation")

    def continuation(self, team_id: object, run_id: object) -> bytes:
        team, run = _team_id(team_id), _run_id(run_id)
        raw = _PRIVATE.read_private_file(
            self._team_dir(team) / f"{run}.continuation", MAX_CONTINUATION_BYTES * 2 + 256, "Routine continuation"
        )
        if raw is None:
            raise RoutineStoreError("Routine continuation is unavailable")
        try:
            envelope = strict_json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise RoutineStoreError("Routine continuation is malformed") from exc
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"algorithm", "nonce", "ciphertext"}
            or envelope["algorithm"] != "AES-256-GCM"
        ):
            raise RoutineStoreError("Routine continuation is malformed")
        try:
            return AESGCM(_PRIVATE.key(self.key_path, "Routine keyring")).decrypt(
                _PRIVATE.decode_part(envelope["nonce"], expected=12),
                _PRIVATE.decode_part(envelope["ciphertext"], minimum=17, maximum=MAX_CONTINUATION_BYTES + 16),
                _aad(team, run),
            )
        except InvalidTag as exc:
            raise RoutineStoreError("Routine continuation authentication failed") from exc

    def delete_continuation(self, team_id: object, run_id: object) -> None:
        team, run = _team_id(team_id), _run_id(run_id)
        try:
            (self._team_dir(team) / f"{run}.continuation").unlink(missing_ok=True)
        except OSError as exc:
            raise RoutineStoreError("Routine continuation could not be removed") from exc

    def continuations(self, team_id: object) -> tuple[str, ...]:
        """The run ids with a stored continuation, including any a crash left unreferenced."""
        team = _team_id(team_id)
        try:
            names = [path.name for path in self._team_dir(team).iterdir()]
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise RoutineStoreError("Routine continuations could not be listed") from exc
        return tuple(
            sorted(name.removesuffix(".continuation") for name in names if _CONTINUATION_NAME_RE.fullmatch(name))
        )

    def delete(self, team_id: object) -> None:
        """Remove the Team's Routine state and continuations; an absent Team is already clean."""
        team = _team_id(team_id)
        with self.lock(team):
            self._remove(self._team_dir(team).name)

    def delete_all(self) -> None:
        """Remove every Team's Routine state, including a Team no network names now, and the keyring."""
        for name in self._owned_directories():
            self._remove(name)
        try:
            self.key_path.unlink(missing_ok=True)
        except OSError as exc:
            raise RoutineStoreError("Routine keyring could not be removed") from exc

    def _owned_directories(self) -> list[str]:
        """The Team directories under the root; a symlink or file where one belongs fails closed."""
        try:
            with os.scandir(self.root) as entries:
                owned = [entry for entry in entries if _TEAM_DIR_RE.fullmatch(entry.name)]
                if any(not entry.is_dir(follow_symlinks=False) for entry in owned):
                    raise RoutineStoreError("Routine state failed its ownership contract")
                return sorted(entry.name for entry in owned)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise RoutineStoreError("Routine state could not be listed") from exc

    def _remove(self, name: str) -> None:
        """Delete one Team directory through verified handles, never following a link out of Routine storage."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            root = os.open(self.root, flags)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RoutineStoreError("Routine state could not be listed") from exc
        try:
            try:
                directory = os.open(name, flags, dir_fd=root)
            except FileNotFoundError:
                return
            try:
                with os.scandir(directory) as entries:
                    names = [entry.name for entry in entries]
                for entry in names:
                    if not stat.S_ISREG(os.stat(entry, dir_fd=directory, follow_symlinks=False).st_mode):
                        raise RoutineStoreError("Routine state failed its ownership contract")
                    os.unlink(entry, dir_fd=directory)
            finally:
                os.close(directory)
            os.rmdir(name, dir_fd=root)
        except OSError as exc:
            raise RoutineStoreError("Routine state could not be removed") from exc
        finally:
            os.close(root)
