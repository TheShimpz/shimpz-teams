"""Local Routine persistence: one private state file per Team and its sealed run records (ADR-0086, ADR-0092).

A Team's Routines, runs and their incidents, and notices live in one private JSON file that every transition replaces
atomically. A frozen run's secret-free continuation, a run's cursor, its recovery snapshot, and an incident's compact
evidence are each encrypted separately, bound by their AAD to exactly what they belong to; each is written before the
state that relies on it, so a crash leaves at worst an unreferenced one, which recovery removes.
"""

import dataclasses
import hashlib
import hmac
import json
import os
import re
import stat
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from action import human as action_human
from action import journal as action_journal
from core import canonical_json, identifier
from local.chat import continuation as local_chat_continuation
from protocol.http.v1 import identifiers as http_identifiers
from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import routine as http_routine
from protocol.http.v1 import routine_notice as http_routine_notice
from protocol.http.v1 import strict_json
from routine import cursor as routine_cursor
from routine import definition as routine_definition
from routine import plan as routine_plan
from routine import record
from routine import starts as routine_starts
from storage import private_state

ROOT = Path("/var/lib/shimpz-local/routines/state")
KEY_PATH = Path("/var/lib/shimpz-local/routines/key/aes256.key")
SCHEMA = 11
# Every bound below derives from the plan's admission budget and the encoders' worst cases (ADR-0092 amendment,
# 2026-10-05, scale), which tests build through the real encoders. The state file holds every definition at the Team's
# aggregate budget, each Routine's other fields, every undelivered notice, and every run, start, and discard, each at
# its own bound.
# A notice at most: its shown output, its plan summary, its usage, and every other field.
MAX_NOTICE_BYTES = http_routine.MAX_OUTPUT_BYTES + http_routine_notice.MAX_SUMMARY_BYTES + 8 * 1024
_NOTICES_BYTES = (record.MAX_UNDELIVERED_NOTICES + record.MAX_ROUTINE_NOTICES) * MAX_NOTICE_BYTES
_RECORDS_BYTES = routine_starts.MAX_STARTS * 64 + record.MAX_DISCARDS * 256
_ROUTINES_BYTES = routine_plan.TEAM_DEFINITION_BYTES + record.MAX_ROUTINES * 18 * 1024
MAX_STATE_BYTES = _ROUTINES_BYTES + _NOTICES_BYTES + _RECORDS_BYTES + record.MAX_RUNS * 4 * 1024 + 64 * 1024
# The state bytes whose decoded Routine state is kept for reuse across every Team: room for one largest state.
MAX_DECODED_STATE_BYTES = MAX_STATE_BYTES
# Reads of one state file that may race its atomic replace before a failure is taken as real.
UNLOCKED_READ_ATTEMPTS = 3
# The recovery snapshot: the plan, its binding, and the Routine's name.
MAX_RECOVERY_BYTES = routine_plan.MAX_PLAN_BYTES + 8 * 1024
# The incident's compact evidence: its copy of the snapshot and its batch's operation rows.
MAX_INCIDENT_BYTES = MAX_RECOVERY_BYTES + action_journal.MAX_OPERATIONS * 512 + 8 * 1024
_CONTINUATION_NAME_RE = re.compile(r"[0-9a-f]{32}\.continuation\Z")
_CURSOR_NAME_RE = re.compile(r"[0-9a-f]{32}\.cursor\Z")
_RECOVERY_NAME_RE = re.compile(r"[0-9a-f]{32}\.recovery\Z")
_ROUTINE_FIELDS = frozenset(
    {
        "routine_id",
        "name",
        "plan",
        "schedule",
        "timezone_source",
        "assistants",
        "anchor",
        "next_run_at",
        "needs_reconfirm",
        "deleting",
        "gap_started_at",
        "missed",
        "reported_missed",
        "revision",
        "paused",
        "confirmation",
        "permitted",
        "failures",
        "rollup_minute",
        "rollup_runs",
        "rollup_usage",
        "run_requested",
        "output_digest",
    }
)
_RUN_FIELDS = frozenset(
    {
        "run_id",
        "routine_id",
        "status",
        "lease_sha256",
        "lease_key",
        "lease_expires_at",
        "active_seconds_left",
        "request_kind",
        "assistant_id",
        "action",
        "generation",
        "notice_version",
        "requests_used",
        "position",
        "steps",
        "usage",
        "protection_lost",
        "revision",
    }
)
_NOTICE_FIELDS = frozenset(
    {"notice_id", "routine_id", "run_id", "outcome", "created_at", "detail", "version", "name", "usage"}
    | {"protection_lost"}
)
_STATE_FIELDS = frozenset(
    {
        "schema",
        "team_id",
        "routines",
        "runs",
        "notices",
        "served_at",
        "starts",
        "discards",
    }
)


def _read_unlocked(path: Path) -> bytes | None:
    """A state file read without its Team's lock, which the Team is only known from once it is read.

    A writer's atomic replace can unlink the very file a reader just opened, which then fails the ownership contract
    for that one read; reading again sees the replacement, and a file that keeps failing really breaks it.
    """
    for _attempt in range(UNLOCKED_READ_ATTEMPTS - 1):
        try:
            return _PRIVATE.read_private_file(path, MAX_STATE_BYTES, "Routine state")
        except RoutineStoreError:
            continue
    return _PRIVATE.read_private_file(path, MAX_STATE_BYTES, "Routine state")


class RoutineStoreError(RuntimeError):
    """Routine state is invalid or unavailable; callers fail closed."""


_PRIVATE = private_state.PrivateState(
    RoutineStoreError,
    "Routine state is malformed",
    "Routine record is malformed",
    # The largest sealed record, base64-encoded.
    (max(local_chat_continuation.MAX_ROUTINE_BYTES, MAX_INCIDENT_BYTES) * 2) + 128,
)


def _team_id(value: object) -> str:
    return identifier.require(http_payload.canonical_team_id, value, RoutineStoreError, "Routine Team is invalid")


def _run_id(value: object) -> str:
    if not isinstance(value, str) or http_routine.ROUTINE_ID_RE.fullmatch(value) is None:
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


def _requests_used(value: object) -> bool:
    return type(value) is int and 0 <= value <= action_human.MAX_REQUESTS_PER_TURN


def _rollup_runs(value: object) -> int:
    _require(type(value) is int and 0 <= value <= http_routine.MAX_ROLLUP_RUNS)
    return value


def _encode(state: record.TeamRoutines, team_id: str) -> bytes:
    def routine_value(item: record.Routine) -> dict[str, object]:
        value = {name: getattr(item, name) for name in _ROUTINE_FIELDS}
        value["assistants"] = [list(pair) for pair in item.assistants]
        value["permitted"] = list(item.permitted)
        return value

    def run_value(item: record.Run) -> dict[str, object]:
        return {name: getattr(item, name) for name in _RUN_FIELDS}

    payload = {
        "schema": SCHEMA,
        "team_id": team_id,
        "routines": [routine_value(item) for item in state.routines],
        "runs": [run_value(item) for item in state.runs],
        "notices": [{name: getattr(item, name) for name in _NOTICE_FIELDS} for item in state.notices],
        "served_at": state.served_at,
        "starts": [list(item) for item in state.starts],
        "discards": [list(item) for item in state.discards],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _decode_routine(value: object) -> record.Routine:
    _require(isinstance(value, dict) and set(value) == _ROUTINE_FIELDS)
    assistants, permitted = value["assistants"], value["permitted"]
    _require(
        isinstance(assistants, list)
        and all(isinstance(pair, list) and len(pair) == 2 for pair in assistants)
        and isinstance(permitted, list)
        and isinstance(value["output_digest"], str)
        and (value["output_digest"] == "" or http_payload.SHA256_RE.fullmatch(value["output_digest"]) is not None)
        and (value["rollup_usage"] is None or http_routine.canonical_run_usage(value["rollup_usage"]) is not None)
    )
    decoded = record.Routine(
        **({name: value[name] for name in _ROUTINE_FIELDS} | {"assistants": (), "permitted": ()}),
    )
    decoded = dataclasses.replace(
        decoded,
        assistants=tuple((pair[0], pair[1]) for pair in assistants),
        permitted=tuple(permitted),
        anchor=_instant(value["anchor"]),
        next_run_at=_instant(value["next_run_at"]),
        gap_started_at=_instant(value["gap_started_at"]),
        missed=_count(value["missed"]),
        reported_missed=_count(value["reported_missed"]),
        failures=_count(value["failures"]),
        rollup_minute=_instant(value["rollup_minute"]),
        rollup_runs=_rollup_runs(value["rollup_runs"]),
        run_requested=_instant(value["run_requested"]),
    )
    _require(
        type(decoded.needs_reconfirm) is bool
        and type(decoded.deleting) is bool
        and record.definition_valid(decoded)
        and routine_definition.definition_bytes(decoded) <= routine_plan.MAX_DEFINITION_BYTES
    )
    return decoded


def _decode_run(value: object) -> record.Run:
    _require(isinstance(value, dict) and set(value) == _RUN_FIELDS)
    strings = ("run_id", "routine_id", "status", "lease_sha256", "lease_key", "request_kind", "assistant_id", "action")
    _require(all(isinstance(value[name], str) for name in strings) and isinstance(value["generation"], str))
    run_id = value["run_id"]
    _require(
        http_routine.ROUTINE_ID_RE.fullmatch(run_id) is not None
        and http_routine.ROUTINE_ID_RE.fullmatch(value["routine_id"]) is not None
        and _generation_of(run_id, value["generation"])
        and type(value["active_seconds_left"]) is int
        and value["active_seconds_left"] <= record.ACTIVE_SECONDS
        and type(value["notice_version"]) is int
        and value["notice_version"] >= 0
        and _requests_used(value["requests_used"])
        and type(value["revision"]) is int
        and 1 <= value["revision"] < 2**31
        and type(value["steps"]) is int
    )
    _require(
        http_routine.canonical_run_usage(value["usage"]) == value["usage"] and type(value["protection_lost"]) is bool
    )
    lease_expires_at = _instant(value["lease_expires_at"])
    _require(_STATUSES.get(value["status"], lambda _value: False)(value))
    return record.Run(**({name: value[name] for name in _RUN_FIELDS} | {"lease_expires_at": lease_expires_at}))


def _no_call(value: dict[str, object]) -> bool:
    return (value["assistant_id"], value["action"], value["position"], value["steps"]) == ("", "", None, 0)


def _no_request(value: dict[str, object]) -> bool:
    return value["request_kind"] == "" and _no_call(value)


def _unleased(value: dict[str, object]) -> bool:
    return value["lease_sha256"] == "" and value["lease_key"] == "" and value["lease_expires_at"] == 0


def _action(value: dict[str, object]) -> bool:
    """A named Assistant Action."""
    return (
        http_identifiers.canonical_assistant_id(value["assistant_id"]) is not None
        and http_identifiers.canonical_action_id(value["action"]) is not None
    )


def _incident(value: dict[str, object]) -> bool:
    """A held run's incident: unleased, of its held generation, at its held call by position, or none sealed."""
    return (
        _unleased(value)
        and value["generation"] != ""
        and value["request_kind"] == ""
        and (
            _no_call(value)
            or (_action(value) and http_routine.canonical_position(value["position"], value["steps"]) is not None)
        )
    )


# Each status admits exactly its own fields, so teardown and recovery never act on a mixed record.
_STATUSES = {
    "leased": lambda value: (
        http_payload.SHA256_RE.fullmatch(value["lease_sha256"]) is not None
        and (
            value["lease_key"] == record.HUMAN_LEASE or http_payload.SHA256_RE.fullmatch(value["lease_key"]) is not None
        )
        and _no_request(value)
    ),
    "frozen": lambda value: (
        _unleased(value)
        and value["request_kind"] in http_routine_notice.REQUEST_KINDS
        and _action(value)
        and (value["position"], value["steps"]) == (None, 0)
    ),
    "held": lambda value: _unleased(value) and _no_request(value) and value["generation"] != "",
    "unresolved": _incident,
    "skipped": _incident,
}


def _decode_notice(value: object) -> record.Notice:
    _require(isinstance(value, dict) and set(value) == _NOTICE_FIELDS and isinstance(value["run_id"], str))
    # A stored notice is exactly its wire form for some Team, with the empty run id of a Routine outcome as null.
    wire = {
        **{name: value[name] for name in _NOTICE_FIELDS},
        "team_id": "stored",
        "run_id": value["run_id"] or None,
        "created_at": "1970-01-01T00:00:00Z",
    }
    _require(http_routine_notice.canonical_notice(wire) == wire)
    return record.Notice(
        value["notice_id"],
        value["routine_id"],
        value["run_id"],
        value["outcome"],
        _instant(value["created_at"]),
        value["detail"],
        value["version"],
        value["name"],
        value["usage"],
        value["protection_lost"],
    )


def _generation_of(run_id: str, generation: object) -> bool:
    """None yet, or exactly this run's own or continuation generation in a Team network; never a verification's."""
    grammar = record.generation_grammar(r"[0-9a-f]{64}", re.escape(run_id), "s1")
    return isinstance(generation, str) and (generation == "" or re.fullmatch(grammar, generation) is not None)


def _decode_discard(value: object) -> tuple[str, str]:
    _require(
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and http_routine.ROUTINE_ID_RE.fullmatch(value[0]) is not None
        and _generation_of(value[0], value[1])
    )
    return value[0], value[1]


def _decode_start(value: object) -> tuple[str, int, int]:
    """One start: its Routine, instant, and the business steps it reserved."""
    _require(
        isinstance(value, list)
        and len(value) == 3
        and isinstance(value[0], str)
        and http_routine.ROUTINE_ID_RE.fullmatch(value[0]) is not None
        and type(value[2]) is int
        and 1 <= value[2] <= routine_plan.MAX_STEPS
    )
    return value[0], _instant(value[1]), value[2]


def _decode(payload: bytes, team_id: str) -> record.TeamRoutines:
    try:
        value = strict_json.loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RoutineStoreError("Routine state is not valid JSON") from exc
    _require(
        isinstance(value, dict)
        and set(value) == _STATE_FIELDS
        and type(value["schema"]) is int
        and value["schema"] == SCHEMA
        and value["team_id"] == team_id
        and isinstance(value["routines"], list)
        and len(value["routines"]) <= record.MAX_ROUTINES
        and isinstance(value["runs"], list)
        and len(value["runs"]) <= record.MAX_RUNS
        and isinstance(value["notices"], list)
        and len(value["notices"]) <= record.MAX_UNDELIVERED_NOTICES + record.MAX_ROUTINE_NOTICES
        and isinstance(value["starts"], list)
        and len(value["starts"]) <= routine_starts.MAX_STARTS
        and isinstance(value["discards"], list)
        and len(value["discards"]) <= record.MAX_DISCARDS
    )
    state = record.TeamRoutines(
        routines=tuple(_decode_routine(item) for item in value["routines"]),
        runs=tuple(_decode_run(item) for item in value["runs"]),
        notices=tuple(_decode_notice(item) for item in value["notices"]),
        served_at=_instant(value["served_at"]),
        starts=tuple(_decode_start(item) for item in value["starts"]),
        discards=tuple(_decode_discard(item) for item in value["discards"]),
    )
    identifiers = [item.routine_id for item in state.routines]
    _require(
        len(set(identifiers)) == len(identifiers)
        and [at for _routine_id, at, _steps in state.starts] == sorted(at for _routine_id, at, _steps in state.starts)
        and sum(routine_definition.definition_bytes(item) for item in state.routines)
        <= routine_plan.TEAM_DEFINITION_BYTES
        and len({item.run_id for item in state.runs}) == len(state.runs)
        and len({item.notice_id for item in state.notices}) == len(state.notices)
        and len(set(state.discards)) == len(state.discards)
        and len(record.live_runs(state)) <= record.MAX_ROUTINES
        and sum(item.status == "unresolved" for item in state.runs) <= record.MAX_UNRESOLVED_INCIDENTS
        # No run record outlives its Routine: a deletion completes only once each of its incidents was released.
        and all(item.routine_id in identifiers for item in state.runs)
    )
    return state


def _aad(team_id: str, run_id: str) -> bytes:
    return json.dumps(["shimpz-local-routine-continuation-v1", team_id, run_id], separators=(",", ":")).encode()


def _cursor_aad(team_id: str, binding: routine_cursor.Binding) -> bytes:
    """A cursor opens only for its Team incarnation, Routine revision, and run."""
    return json.dumps(
        [
            "shimpz-local-routine-cursor-v1",
            team_id,
            binding.incarnation,
            binding.routine_id,
            binding.revision,
            binding.run_id,
        ],
        separators=(",", ":"),
    ).encode()


def _recovery_aad(team_id: str, run_id: str) -> bytes:
    return json.dumps(["shimpz-local-routine-recovery-v1", team_id, run_id], separators=(",", ":")).encode()


def _incident_aad(team_id: str, incident_id: str) -> bytes:
    return json.dumps(["shimpz-local-routine-incident-v1", team_id, incident_id], separators=(",", ":")).encode()


class RoutineStore:
    """Every Local Team's Routine state, changed only through serialized, atomic read-modify-write transitions."""

    def __init__(self, root: Path = ROOT, key_path: Path = KEY_PATH) -> None:
        self.root = Path(root)
        self.key_path = Path(key_path)
        if self.key_path.parent == self.root or self.root in self.key_path.parents:
            raise RoutineStoreError("Routine keyring must be separate from Routine state")
        self._guard = threading.Lock()
        self._key_lock = threading.Lock()
        # Held weakly, as Team chat locks are: a Team's lock lives only while a holder or waiter references it, so a
        # deleted Team leaves nothing behind and everyone contending on one Team shares the same lock.
        self._locks: weakref.WeakValueDictionary[str, threading.RLock] = weakref.WeakValueDictionary()
        # A Space reset closes the store and bumps the epoch, so a write that began before it can never land after.
        self._closed = False
        self._epoch = 0
        # The state each Team's file last decoded to, beside its exact bytes, least recently read first: a read finding
        # the same bytes reuses it, since a large state takes tens of milliseconds to decode and one run reads it many
        # times. Only MAX_DECODED_STATE_BYTES of state bytes are kept across every Team.
        self._decoded: OrderedDict[str, tuple[bytes, record.TeamRoutines]] = OrderedDict()

    def lock(self, team_id: object) -> threading.RLock:
        return self._directory_lock(self._team_dir(_team_id(team_id)).name)

    def _directory_lock(self, name: str) -> threading.RLock:
        """One Team directory's lock, keyed by its name, so a sweep can lock a directory it cannot name a Team of."""
        with self._guard:
            lock = self._locks.get(name)
            if lock is None:
                lock = threading.RLock()
                self._locks[name] = lock
            return lock

    def _team_dir(self, team_id: str) -> Path:
        return self.root / hashlib.sha256(team_id.encode()).hexdigest()

    def load(self, team_id: object) -> record.TeamRoutines:
        """The Team's current state, read under its lock so a concurrent atomic replace is never seen half-done."""
        team = _team_id(team_id)
        with self.lock(team):
            payload = _PRIVATE.read_private_file(self._team_dir(team) / "state.json", MAX_STATE_BYTES, "Routine state")
            if payload is None:
                self._forget(team)
                return record.TeamRoutines()
            with self._guard:
                kept = self._decoded.get(team)
                if kept is not None and kept[0] == payload:
                    self._decoded.move_to_end(team)
                    return kept[1]
            state = _decode(payload, team)
            self._keep(team, payload, state)
            return state

    def _keep(self, team: str, payload: bytes, state: record.TeamRoutines) -> None:
        """Keep one Team's decoded state, leaving out the least recently read Teams beyond the byte budget."""
        with self._guard:
            self._decoded[team] = (payload, state)
            self._decoded.move_to_end(team)
            while sum(len(kept) for kept, _state in self._decoded.values()) > MAX_DECODED_STATE_BYTES:
                self._decoded.popitem(last=False)

    def _forget(self, team: str) -> None:
        with self._guard:
            self._decoded.pop(team, None)

    def _save(self, team: str, state: record.TeamRoutines) -> None:
        payload = _encode(state, team)
        if len(payload) > MAX_STATE_BYTES:
            raise RoutineStoreError("Routine state exceeds its fixed byte limit")
        # Decoding what was encoded checks it before it replaces the file, and is the next read's state.
        self._forget(team)
        decoded = _decode(payload, team)
        _PRIVATE.atomic_write(self._team_dir(team) / "state.json", payload, "Routine state")
        self._keep(team, payload, decoded)

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
            payload = _read_unlocked(directory / "state.json")
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
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= local_chat_continuation.MAX_ROUTINE_BYTES:
            raise RoutineStoreError("Routine continuation is invalid")
        self._sealed_write(team, f"{run}.continuation", payload, _aad(team, run), "Routine continuation")

    def _sealed_write(self, team: str, name: str, payload: bytes, aad: bytes, label: str, *, maximum: int = 0) -> None:
        """Seal one record under the Team lock; with ``maximum``, it is write-once.

        A write-once record accepts only its exact first bytes again, idempotently; different bytes are refused and
        the sealed record is left as it was.
        """
        epoch = self._current_epoch()
        with self.lock(team):
            self._writable(epoch)
            if maximum:
                sealed = self._sealed_read(team, name, aad, label, maximum)
                if sealed is not None:
                    if sealed != payload:
                        raise RoutineStoreError(f"{label} is immutable")
                    return
            # One keyring for every Team: concurrent first writers must not each create a different key.
            with self._key_lock:
                key = _PRIVATE.key(self.key_path, "Routine keyring", allow_create=True)
            envelope = private_state.seal(key, payload, aad)
            _PRIVATE.atomic_write(self._team_dir(team) / name, canonical_json.encode(envelope), label)

    def _sealed_read(self, team: str, name: str, aad: bytes, label: str, maximum: int) -> bytes | None:
        """One sealed record, or None when absent; one that cannot be read, opened, or authenticated fails closed."""
        raw = _PRIVATE.read_private_file(self._team_dir(team) / name, maximum * 2 + 256, label)
        if raw is None:
            return None
        try:
            envelope = strict_json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise RoutineStoreError(f"{label} is malformed") from exc
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"algorithm", "nonce", "ciphertext"}
            or envelope["algorithm"] != "AES-256-GCM"
        ):
            raise RoutineStoreError(f"{label} is malformed")
        key = _PRIVATE.key(self.key_path, "Routine keyring")
        return _PRIVATE.open_envelope(key, envelope, aad, maximum, f"{label} authentication failed")

    def _sealed_delete(self, team: str, name: str, label: str) -> None:
        try:
            (self._team_dir(team) / name).unlink(missing_ok=True)
        except OSError as exc:
            raise RoutineStoreError(f"{label} could not be removed") from exc

    def continuation(self, team_id: object, run_id: object) -> bytes:
        team, run = _team_id(team_id), _run_id(run_id)
        payload = self._sealed_read(
            team,
            f"{run}.continuation",
            _aad(team, run),
            "Routine continuation",
            local_chat_continuation.MAX_ROUTINE_BYTES,
        )
        if payload is None:
            raise RoutineStoreError("Routine continuation is unavailable")
        return payload

    def delete_continuation(self, team_id: object, run_id: object) -> None:
        team, run = _team_id(team_id), _run_id(run_id)
        self._sealed_delete(team, f"{run}.continuation", "Routine continuation")

    def put_cursor(self, team_id: object, cursor: routine_cursor.Cursor) -> None:
        """Seal one replayed run's cursor under exactly its binding (ADR-0092); a sealed loss of protection stays."""
        team = _team_id(team_id)
        with self.lock(team):
            sealed = self.cursor(team, cursor.binding) if not cursor.protection_lost else None
            if sealed is not None and sealed.protection_lost:
                cursor = routine_cursor.lose_protection(cursor)
            self._seal_cursor(team, cursor)

    def lose_cursor(self, team_id: object, binding: routine_cursor.Binding) -> None:
        """Mark the run's latest sealed cursor as having lost its protection, keeping its progress (ADR-0101)."""
        team = _team_id(team_id)
        with self.lock(team):
            sealed = self.cursor(team, binding)
            if sealed is not None and not sealed.protection_lost:
                self._seal_cursor(team, routine_cursor.lose_protection(sealed))

    def _seal_cursor(self, team: str, cursor: routine_cursor.Cursor) -> None:
        try:
            payload = routine_cursor.encode(cursor)
        except routine_cursor.CursorError as exc:
            raise RoutineStoreError("Routine cursor is invalid") from exc
        binding = cursor.binding
        self._sealed_write(
            team, f"{_run_id(binding.run_id)}.cursor", payload, _cursor_aad(team, binding), "Routine cursor"
        )

    def cursor(self, team_id: object, binding: routine_cursor.Binding) -> routine_cursor.Cursor | None:
        """The run's cursor when one was sealed under exactly this binding; another binding never opens it."""
        team = _team_id(team_id)
        name = f"{_run_id(binding.run_id)}.cursor"
        payload = self._sealed_read(
            team, name, _cursor_aad(team, binding), "Routine cursor", routine_cursor.MAX_CURSOR_BYTES
        )
        if payload is None:
            return None
        try:
            return routine_cursor.decode(payload, binding)
        except routine_cursor.CursorError as exc:
            raise RoutineStoreError("Routine cursor is malformed") from exc

    def output_digest(self, team_id: object, binding: routine_cursor.Binding, step: str, material: bytes) -> str:
        """The keyed digest a ``changes`` Routine compares one run's safe result by (ADR-0092 amendment, 2026-10-05).

        It is bound to the Team, its incarnation, the Routine revision, and the shown step, under a key derived apart
        from the sealing key, so neither a known Routine id nor a guessed result can be checked against it offline.
        """
        team = _team_id(team_id)
        with self._key_lock:
            key = _PRIVATE.key(self.key_path, "Routine keyring", allow_create=True)
        derived = HKDF(hashes.SHA256(), 32, salt=None, info=b"shimpz-routine-output-v1").derive(key)
        # A JSON header holds no raw line break and names the material's length, so the framing is unambiguous.
        header = [team, binding.incarnation, binding.routine_id, binding.revision, step, len(material)]
        framed = json.dumps(header, separators=(",", ":")).encode() + b"\n" + material
        return hmac.new(derived, framed, hashlib.sha256).hexdigest()

    def delete_cursor(self, team_id: object, run_id: object) -> None:
        self._sealed_delete(_team_id(team_id), f"{_run_id(run_id)}.cursor", "Routine cursor")

    def put_recovery(self, team_id: object, run_id: object, payload: object) -> None:
        """Seal one replayed run's immutable recovery snapshot before its first dispatch (ADR-0092).

        It is write-once: resealing the exact canonical bytes succeeds, and any other snapshot for the run is refused.
        """
        team, run = _team_id(team_id), _run_id(run_id)
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_RECOVERY_BYTES:
            raise RoutineStoreError("Routine recovery snapshot is invalid")
        self._sealed_write(
            team,
            f"{run}.recovery",
            payload,
            _recovery_aad(team, run),
            "Routine recovery snapshot",
            maximum=MAX_RECOVERY_BYTES,
        )

    def recovery(self, team_id: object, run_id: object) -> bytes | None:
        team, run = _team_id(team_id), _run_id(run_id)
        return self._sealed_read(
            team, f"{run}.recovery", _recovery_aad(team, run), "Routine recovery snapshot", MAX_RECOVERY_BYTES
        )

    def delete_recovery(self, team_id: object, run_id: object) -> None:
        self._sealed_delete(_team_id(team_id), f"{_run_id(run_id)}.recovery", "Routine recovery snapshot")

    def recoveries(self, team_id: object) -> tuple[str, ...]:
        """The run ids with a stored recovery snapshot, including any a crash left unreferenced."""
        return self._sealed_names(_team_id(team_id), _RECOVERY_NAME_RE, ".recovery", "recovery snapshots")

    def put_incident(self, team_id: object, incident_id: object, payload: object) -> None:
        """Seal one incident's compact safety evidence; it is durable before its batch is archived."""
        team, incident = _team_id(team_id), _run_id(incident_id)
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_INCIDENT_BYTES:
            raise RoutineStoreError("Routine incident is invalid")
        self._sealed_write(team, f"{incident}.incident", payload, _incident_aad(team, incident), "Routine incident")

    def incident(self, team_id: object, incident_id: object) -> bytes | None:
        team, incident = _team_id(team_id), _run_id(incident_id)
        return self._sealed_read(
            team, f"{incident}.incident", _incident_aad(team, incident), "Routine incident", MAX_INCIDENT_BYTES
        )

    def delete_incident(self, team_id: object, incident_id: object) -> None:
        self._sealed_delete(_team_id(team_id), f"{_run_id(incident_id)}.incident", "Routine incident")

    def continuations(self, team_id: object) -> tuple[str, ...]:
        """The run ids with a stored continuation, including any a crash left unreferenced."""
        return self._sealed_names(_team_id(team_id), _CONTINUATION_NAME_RE, ".continuation", "continuations")

    def cursors(self, team_id: object) -> tuple[str, ...]:
        """The run ids with a stored cursor, including any a crash left unreferenced."""
        return self._sealed_names(_team_id(team_id), _CURSOR_NAME_RE, ".cursor", "cursors")

    def _sealed_names(self, team: str, pattern: re.Pattern[str], suffix: str, label: str) -> tuple[str, ...]:
        try:
            names = [path.name for path in self._team_dir(team).iterdir()]
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise RoutineStoreError(f"Routine {label} could not be listed") from exc
        return tuple(sorted(name.removesuffix(suffix) for name in names if pattern.fullmatch(name)))

    def delete(self, team_id: object) -> None:
        """Remove the Team's Routine state and continuations; an absent Team is already clean."""
        team = _team_id(team_id)
        with self.lock(team):
            self._remove(self._team_dir(team).name)
            self._forget(team)

    def delete_all(self) -> None:
        """Remove every Team's Routine state, including a Team no network names now, and the keyring."""
        for name in self._owned_directories():
            self._remove(name)
        with self._guard:
            self._decoded.clear()
        try:
            self.key_path.unlink(missing_ok=True)
        except OSError as exc:
            raise RoutineStoreError("Routine keyring could not be removed") from exc

    def _owned_directories(self) -> list[str]:
        """The Team directories under the root; a symlink or file where one belongs fails closed."""
        try:
            with os.scandir(self.root) as entries:
                owned = [entry for entry in entries if http_payload.SHA256_RE.fullmatch(entry.name)]
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
