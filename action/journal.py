"""Crash-safe, bounded idempotency journal for Assistant Action side effects.

The journal stores only caller-provided fingerprints, logical operation ids, and bounded Action results. Raw Action
inputs never cross this boundary. Each operation receives its random logical ``operation_id`` in the same durable
commit that prepares it, before any RPC, and keeps it across human-request replay and restart (ADR-0092). An operation
durably enters ``executing`` before its side effect starts; finding it there again is intentionally an uncertain
outcome and fails closed instead of risking a duplicate side effect. Only a successfully decoded human-interaction
suspension may explicitly return it to ``prepared`` for deterministic replay. An operation ends ``completed`` with a
result, or ``no_effect`` when its failure is proven to have had no business effect; either records whether the
execution itself or Team-admitted verifier evidence decided it.

A batch whose turn ended without delivery, and with no uncertain operation, becomes ``ended``: it keeps its completed
receipts and frees its generation. Only the exact same batch may reopen it to replay those receipts; a fresh batch that
repeats none of its interrupts replaces it, and any other batch is refused.

A Routine batch is prepared ``archivable``: it reserves one archive marker before any of its Actions may run. When it
is held for a person, its incident is written first and the batch is then ``archived``: its operation rows go, and the
marker refuses every later prepare, reopen, or replacement of that generation until the settled marker is released.
Markers never count against the active generations a chat needs.

Every commit is synchronous FULL in WAL mode, so an acknowledged transition survives power loss.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from protocol.http.v1 import payload as http_payload

SCHEMA_VERSION = 2
APPLICATION_ID = 0x53484A31  # SHJ1
MAX_GENERATIONS = 1024
# Archive markers per Local profile (ADR-0092); they never count against the active generations.
MAX_ARCHIVED = 4096
MAX_OPERATIONS = 64
# Matches the Assistant RPC frame bound; the RPC boundary refuses any result this journal could not admit.
MAX_RESULT_BYTES = 512 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 4096
# Synchronous FULL syncs the WAL at every commit; this bound keeps the WAL itself small.
WAL_AUTOCHECKPOINT_PAGES = 32

SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_OPERATION_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_STATES = frozenset({"prepared", "executing", "completed", "no_effect"})
_ORIGINS = frozenset({"execution", "verification"})
_OPEN = "open"
_ENDED = "ended"
_ARCHIVED = "archived"
_OPERATION_COLUMNS = (
    "generation",
    "ordinal",
    "interrupt_id",
    "fingerprint",
    "operation_id",
    "attempts",
    "state",
    "origin",
    "evidence",
    "result",
)


class ActionJournalError(RuntimeError):
    """The Action journal could not safely prove the requested transition."""


class ActionJournalConflictError(ActionJournalError):
    """Durable state does not match the caller's immutable batch contract."""


class ActionJournalUncertainError(ActionJournalError):
    """A side effect may already have happened and must not be executed again."""


class ActionJournalCorruptionError(ActionJournalError):
    """The journal or a persisted record violated its closed schema."""


class ActionJournalArchivedError(ActionJournalConflictError):
    """The generation holds an archived batch, which nothing may prepare, reopen, or replace."""


@dataclass(frozen=True, slots=True)
class Operation:
    """Opaque Action identity; the fingerprint commits to the validated request.

    A permitted retry after proven absence names the logical ``operation_id`` it repeats; otherwise the journal mints
    a fresh one when it first prepares the operation.
    """

    interrupt_id: str
    fingerprint: str
    operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class Batch:
    """Immutable handle for one Brain suspension in a Team generation."""

    generation: str
    fingerprint: str
    operations: tuple[Operation, ...]


@dataclass(frozen=True, slots=True)
class Execution:
    """Decision returned before invocation, or a previously committed JSON result, with the logical operation id."""

    execute: bool
    result: object | None = None
    operation_id: str = ""


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """The compact, result-free state of one operation, as an incident keeps it before its batch is archived."""

    interrupt_id: str
    operation_id: str
    state: str
    attempts: int
    origin: str | None


def new_operation_id() -> str:
    """One random RFC 9562 version 4 UUID in canonical lowercase text."""
    return str(uuid.uuid4())


def valid_operation_id(value: object) -> bool:
    return isinstance(value, str) and _OPERATION_ID_RE.fullmatch(value) is not None


def _positive_limit(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _safe_id(value: object, name: str) -> str:
    if not isinstance(value, str) or SAFE_ID_RE.fullmatch(value) is None:
        raise ActionJournalConflictError(f"{name} is invalid")
    return value


def _operation(value: object) -> Operation:
    if not isinstance(value, Operation):
        raise ActionJournalConflictError("operation is invalid")
    _safe_id(value.interrupt_id, "operation interrupt id")
    if not isinstance(value.fingerprint, str) or http_payload.SHA256_RE.fullmatch(value.fingerprint) is None:
        raise ActionJournalConflictError("operation fingerprint is invalid")
    if value.operation_id is not None and not valid_operation_id(value.operation_id):
        raise ActionJournalConflictError("operation id is invalid")
    return value


def _walk_json(value: object, *, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [MAX_JSON_NODES]
    budget[0] -= 1
    if budget[0] < 0 or depth > MAX_JSON_DEPTH:
        raise ActionJournalConflictError("Action result exceeds the JSON structure limit")
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ActionJournalConflictError("Action result contains a non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _walk_json(item, depth=depth + 1, budget=budget)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ActionJournalConflictError("Action result object keys must be strings")
            _walk_json(item, depth=depth + 1, budget=budget)
        return
    raise ActionJournalConflictError("Action result must contain only JSON values")


def _canonical_result(value: object, max_bytes: int) -> bytes:
    _walk_json(value)
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ActionJournalConflictError("Action result is not canonical JSON") from exc
    if len(encoded) > max_bytes:
        raise ActionJournalConflictError("Action result exceeds the durable size limit")
    return encoded


def require_durable_result(value: object) -> None:
    """Refuse, before any follow-up side effect, a result the journal could not persist."""
    _canonical_result(value, MAX_RESULT_BYTES)


class ActionJournal:
    """Serialize durable Action transitions through one private SQLite database."""

    def __init__(
        self,
        path: Path,
        *,
        max_generations: int = MAX_GENERATIONS,
        max_operations: int = MAX_OPERATIONS,
        max_result_bytes: int = MAX_RESULT_BYTES,
        max_archived: int = MAX_ARCHIVED,
    ) -> None:
        self.path = Path(path)
        self.max_generations = _positive_limit(max_generations, "max_generations")
        self.max_archived = _positive_limit(max_archived, "max_archived")
        self.max_operations = _positive_limit(max_operations, "max_operations")
        self.max_result_bytes = _positive_limit(max_result_bytes, "max_result_bytes")
        self._guard = threading.RLock()
        self._closed = False
        self._validated_batches: dict[
            tuple[str, str],
            dict[str, tuple[int, str]],
        ] = {}
        self._validated_results: dict[tuple[str, str], bytes] = {}
        initialize = self._prepare_file()
        try:
            self._connection = sqlite3.connect(
                self.path,
                isolation_level=None,
                check_same_thread=False,
                timeout=5.0,
            )
            self._configure()
            if initialize:
                self._create_schema()
            self._validate_schema()
            self.path.chmod(0o600)
        except (OSError, sqlite3.Error, ActionJournalError) as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            if isinstance(exc, ActionJournalError):
                raise
            raise ActionJournalCorruptionError("Action journal could not be opened safely") from exc

    def _prepare_file(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            parent = self.path.parent.lstat()
            if not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode) or parent.st_uid != os.geteuid():
                raise ActionJournalCorruptionError("Action journal parent is not a private directory")
            self.path.parent.chmod(0o700)
            try:
                metadata = self.path.lstat()
            except FileNotFoundError:
                descriptor = os.open(
                    self.path,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
                try:
                    metadata = os.fstat(descriptor)
                    self._validate_file_metadata(metadata)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                directory = os.open(
                    self.path.parent,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                return True
            self._validate_file_metadata(metadata)
            if metadata.st_mode & 0o077:
                raise ActionJournalCorruptionError("Action journal file permissions are not private")
        except OSError as exc:
            raise ActionJournalCorruptionError("Action journal private path is unavailable") from exc
        else:
            return metadata.st_size == 0

    @staticmethod
    def _validate_file_metadata(metadata: os.stat_result) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise ActionJournalCorruptionError("Action journal path has unsafe ownership or links")

    def _configure(self) -> None:
        self._connection.execute("PRAGMA trusted_schema = OFF")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()
        if mode != ("wal",):
            raise ActionJournalCorruptionError("Action journal could not enable its durable mode")
        self._connection.execute("PRAGMA synchronous = FULL")
        checkpoint = self._connection.execute(f"PRAGMA wal_autocheckpoint = {WAL_AUTOCHECKPOINT_PAGES}").fetchone()
        synchronous = self._connection.execute("PRAGMA synchronous").fetchone()
        if checkpoint != (WAL_AUTOCHECKPOINT_PAGES,) or synchronous != (2,):
            raise ActionJournalCorruptionError("Action journal durability policy could not be applied")

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE batches (
                generation TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                operation_count INTEGER NOT NULL CHECK (operation_count > 0),
                state TEXT NOT NULL CHECK (state IN ('open', 'ended', 'archived')),
                archivable INTEGER NOT NULL CHECK (archivable IN (0, 1)),
                CHECK (state != 'archived' OR archivable = 1)
            ) WITHOUT ROWID;
            CREATE TABLE operations (
                generation TEXT NOT NULL,
                ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                interrupt_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                attempts INTEGER NOT NULL CHECK (attempts >= 0),
                state TEXT NOT NULL CHECK (state IN ('prepared', 'executing', 'completed', 'no_effect')),
                origin TEXT CHECK (origin IN ('execution', 'verification')),
                evidence TEXT,
                result BLOB,
                PRIMARY KEY (generation, interrupt_id),
                UNIQUE (generation, ordinal),
                UNIQUE (generation, operation_id),
                FOREIGN KEY (generation) REFERENCES batches(generation) ON DELETE CASCADE,
                CHECK ((state IN ('prepared', 'executing') AND origin IS NULL AND result IS NULL) OR
                       (state = 'completed' AND origin IS NOT NULL AND result IS NOT NULL) OR
                       (state = 'no_effect' AND origin IS NOT NULL AND result IS NULL)),
                CHECK ((origin IS 'verification') = (evidence IS NOT NULL))
            ) WITHOUT ROWID;
            PRAGMA application_id = 1397246513;
            PRAGMA user_version = 2;
            COMMIT;
            """
        )

    def _validate_schema(self) -> None:
        try:
            check = self._connection.execute("PRAGMA quick_check").fetchall()
            application_id = self._connection.execute("PRAGMA application_id").fetchone()
            version = self._connection.execute("PRAGMA user_version").fetchone()
            tables = {
                row[0]
                for row in self._connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            batch_columns = [row[1] for row in self._connection.execute("PRAGMA table_info(batches)")]
            operation_columns = [row[1] for row in self._connection.execute("PRAGMA table_info(operations)")]
            foreign_keys = self._connection.execute("PRAGMA foreign_key_check").fetchall()
        except sqlite3.Error as exc:
            raise ActionJournalCorruptionError("Action journal integrity could not be verified") from exc
        if (
            check != [("ok",)]
            or application_id != (APPLICATION_ID,)
            or version != (SCHEMA_VERSION,)
            or tables != {"batches", "operations"}
            or batch_columns != ["generation", "fingerprint", "operation_count", "state", "archivable"]
            or operation_columns != list(_OPERATION_COLUMNS)
            or foreign_keys
        ):
            raise ActionJournalCorruptionError("Action journal schema or contents are invalid")

    def _transaction(self) -> None:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise ActionJournalError("Action journal transaction could not start") from exc

    def _commit(self) -> None:
        try:
            self._connection.execute("COMMIT")
        except sqlite3.Error as exc:
            raise ActionJournalError("Action journal transaction could not commit") from exc

    def _rollback(self) -> None:
        with suppress(sqlite3.Error):
            self._connection.execute("ROLLBACK")

    @contextmanager
    def _writing(self, failure: str) -> Iterator[None]:
        """One serialized write transaction, committed on normal exit and rolled back on any refusal."""
        with self._guard:
            self._ensure_open()
            self._transaction()
            try:
                yield
                self._commit()
            except (sqlite3.Error, ActionJournalError) as exc:
                self._rollback()
                if isinstance(exc, ActionJournalError):
                    raise
                raise ActionJournalError(failure) from exc

    def _ensure_open(self) -> None:
        if self._closed:
            raise ActionJournalError("Action journal is closed")

    def _batch(self, generation: object, thread_id: object, operations: Sequence[Operation]) -> Batch:
        safe_generation = _safe_id(generation, "generation")
        safe_thread = _safe_id(thread_id, "thread id")
        if isinstance(operations, (str, bytes)):
            raise ActionJournalConflictError("operations are invalid")
        try:
            selected = tuple(_operation(item) for item in operations)
        except TypeError as exc:
            raise ActionJournalConflictError("operations are invalid") from exc
        if not selected or len(selected) > self.max_operations:
            raise ActionJournalConflictError("Action batch exceeds the operation count limit")
        if len({item.interrupt_id for item in selected}) != len(selected):
            raise ActionJournalConflictError("Action batch repeats an interrupt id")
        supplied = [item.operation_id for item in selected if item.operation_id is not None]
        if len(set(supplied)) != len(supplied):
            raise ActionJournalConflictError("Action batch repeats an operation id")
        payload = json.dumps(
            {
                "generation": safe_generation,
                "operations": [
                    [item.interrupt_id, item.fingerprint, *(() if item.operation_id is None else (item.operation_id,))]
                    for item in selected
                ],
                "thread": hashlib.sha256(safe_thread.encode("utf-8")).hexdigest(),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return Batch(safe_generation, hashlib.sha256(payload).hexdigest(), selected)

    @staticmethod
    def _validate_handle(batch: object) -> Batch:
        if not isinstance(batch, Batch):
            raise ActionJournalConflictError("Action batch handle is invalid")
        _safe_id(batch.generation, "generation")
        if not isinstance(batch.fingerprint, str) or http_payload.SHA256_RE.fullmatch(batch.fingerprint) is None:
            raise ActionJournalConflictError("Action batch fingerprint is invalid")
        if not isinstance(batch.operations, tuple) or not batch.operations:
            raise ActionJournalConflictError("Action batch operations are invalid")
        for operation in batch.operations:
            _operation(operation)
        if len({item.interrupt_id for item in batch.operations}) != len(batch.operations):
            raise ActionJournalConflictError("Action batch repeats an interrupt id")
        return batch

    def _load_batch(self, batch: Batch) -> list[tuple[object, ...]]:
        try:
            row = self._connection.execute(
                "SELECT fingerprint, operation_count, state FROM batches WHERE generation = ?",
                (batch.generation,),
            ).fetchone()
            operations = self._connection.execute(
                """SELECT ordinal, interrupt_id, fingerprint, state, result, operation_id, attempts, origin
                   FROM operations WHERE generation = ? ORDER BY ordinal""",
                (batch.generation,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ActionJournalCorruptionError("Action journal batch could not be read") from exc
        if row is None:
            raise ActionJournalConflictError("Action batch is no longer current")
        fingerprint, operation_count, state = row
        if fingerprint == batch.fingerprint and state != _OPEN:
            raise ActionJournalConflictError("Action batch has ended")
        expected = [(item.interrupt_id, item.fingerprint) for item in batch.operations]
        actual = [(row[1], row[2]) for row in operations]
        if (
            fingerprint != batch.fingerprint
            or type(operation_count) is not int
            or operation_count != len(batch.operations)
            or actual != expected
            or [row[0] for row in operations] != list(range(len(operations)))
            or any(row[3] not in _STATES or not valid_operation_id(row[5]) for row in operations)
            or any(
                item.operation_id not in {None, row[5]} for item, row in zip(batch.operations, operations, strict=True)
            )
        ):
            raise ActionJournalConflictError("Action batch changed or is corrupt")
        self._validated_batches[(batch.generation, batch.fingerprint)] = {
            operation.interrupt_id: (ordinal, operation.fingerprint)
            for ordinal, operation in enumerate(batch.operations)
        }
        return operations

    def _load_operation(self, batch: Batch, operation: Operation) -> tuple[object, ...]:
        key = (batch.generation, batch.fingerprint)
        expected = self._validated_batches.get(key)
        if expected is None:
            self._load_batch(batch)
            expected = self._validated_batches[key]
        try:
            row = self._connection.execute(
                """SELECT b.fingerprint, b.operation_count, b.state,
                          o.ordinal, o.interrupt_id, o.fingerprint, o.state, o.result, o.operation_id, o.origin,
                          o.evidence
                   FROM batches AS b
                   JOIN operations AS o ON o.generation = b.generation
                   WHERE b.generation = ? AND o.interrupt_id = ?""",
                (batch.generation, operation.interrupt_id),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ActionJournalCorruptionError("Action journal operation could not be read") from exc
        identity = expected.get(operation.interrupt_id)
        if (
            row is None
            or identity is None
            or row[:3] != (batch.fingerprint, len(batch.operations), _OPEN)
            or row[3:6] != (identity[0], operation.interrupt_id, identity[1])
            or row[6] not in _STATES
        ):
            raise ActionJournalConflictError("Action operation changed, ended, or is corrupt")
        return row[3:]

    def _forget_generation(self, generation: str) -> None:
        self._validated_batches = {key: value for key, value in self._validated_batches.items() if key[0] != generation}
        self._validated_results = {key: value for key, value in self._validated_results.items() if key[0] != generation}

    def prepare_batch(
        self,
        generation: str,
        thread_id: str,
        operations: Sequence[Operation],
        *,
        archivable: bool = False,
    ) -> Batch:
        """Durably prepare a batch and mint each new operation's id; an ``archivable`` batch reserves its marker."""
        batch = self._batch(generation, thread_id, operations)
        with self._writing("Action batch could not be prepared"):
            row = self._connection.execute(
                "SELECT fingerprint, state, archivable FROM batches WHERE generation = ?",
                (batch.generation,),
            ).fetchone()
            if row is not None and row[1] == _ARCHIVED:
                raise ActionJournalArchivedError("Action generation is archived")
            if row is None:
                self._reserve_generation(archivable=archivable)
                self._insert_batch(batch, archivable=archivable)
            elif row[0] == batch.fingerprint:
                if row[2] != int(archivable):
                    raise ActionJournalConflictError("Action batch changed its archive reservation")
                self._reopen(batch, row[1])
            elif row[1] == _OPEN:
                raise ActionJournalConflictError("another Action batch is pending for this generation")
            else:
                self._retire_ended(batch)
                self._reserve_generation(archivable=archivable)
                self._insert_batch(batch, archivable=archivable)
        return batch

    def _scalar(self, statement: str) -> int:
        count = self._connection.execute(statement).fetchone()
        if count is None or type(count[0]) is not int:
            raise ActionJournalCorruptionError("Action journal capacity is invalid")
        return count[0]

    def _reserve_generation(self, *, archivable: bool) -> None:
        """Admit one more active generation; an archivable one also needs a free archive marker it can hold.

        Archived markers are not active, so they never take a generation a chat needs; every archivable batch, live or
        archived, holds one marker, so archiving a held batch can never fail for capacity.
        """
        if self._scalar("SELECT COUNT(*) FROM batches WHERE state != 'archived'") >= self.max_generations:
            raise ActionJournalConflictError("Action journal generation capacity is exhausted")
        if archivable and self._scalar("SELECT COUNT(*) FROM batches WHERE archivable = 1") >= self.max_archived:
            raise ActionJournalConflictError("Action journal archive capacity is exhausted")

    def _reopen(self, batch: Batch, state: object) -> None:
        """Resume the exact same batch; an ended one returns to replay its kept receipts."""
        if state == _ENDED:
            self._connection.execute(
                "UPDATE batches SET state = 'open' WHERE generation = ? AND fingerprint = ? AND state = 'ended'",
                (batch.generation, batch.fingerprint),
            )
            if self._connection.execute("SELECT changes()").fetchone() != (1,):
                raise ActionJournalConflictError("ended Action batch changed before replay")
        self._load_batch(batch)

    def _retire_ended(self, batch: Batch) -> None:
        """Free an ended generation for a fresh batch; repeating one of its interrupts is a refused replay."""
        ended = {
            row[0]
            for row in self._connection.execute(
                "SELECT interrupt_id FROM operations WHERE generation = ?",
                (batch.generation,),
            ).fetchall()
        }
        if not ended.isdisjoint(operation.interrupt_id for operation in batch.operations):
            raise ActionJournalConflictError("Action batch repeats an ended Action interrupt")
        self._connection.execute(
            "DELETE FROM batches WHERE generation = ? AND state = 'ended'",
            (batch.generation,),
        )
        if self._connection.execute("SELECT changes()").fetchone() != (1,):
            raise ActionJournalConflictError("ended Action batch changed before replacement")
        self._forget_generation(batch.generation)

    def _insert_batch(self, batch: Batch, *, archivable: bool) -> None:
        self._connection.execute(
            "INSERT INTO batches VALUES (?, ?, ?, 'open', ?)",
            (batch.generation, batch.fingerprint, len(batch.operations), int(archivable)),
        )
        self._connection.executemany(
            "INSERT INTO operations VALUES (?, ?, ?, ?, ?, 0, 'prepared', NULL, NULL, NULL)",
            [
                (
                    batch.generation,
                    ordinal,
                    operation.interrupt_id,
                    operation.fingerprint,
                    operation.operation_id or new_operation_id(),
                )
                for ordinal, operation in enumerate(batch.operations)
            ],
        )

    def begin(self, batch: Batch, operation: Operation) -> Execution:
        batch = self._validate_handle(batch)
        operation = _operation(operation)
        if operation not in batch.operations:
            raise ActionJournalConflictError("operation does not belong to this Action batch")
        with self._writing("Action execution could not begin"):
            persisted = self._load_operation(batch, operation)
            state, raw_result, operation_id = persisted[3], persisted[4], persisted[5]
            if state == "executing":
                raise ActionJournalUncertainError(
                    "Action execution outcome is uncertain; refusing a duplicate side effect"
                )
            if state == "no_effect":
                raise ActionJournalConflictError("Action operation already ended without effect")
            if state == "completed":
                return Execution(False, self._validated_result(batch, operation.interrupt_id, raw_result), operation_id)
            if raw_result is not None:
                raise ActionJournalCorruptionError("Action operation has an invalid durable state")
            self._connection.execute(
                """UPDATE operations SET state = 'executing', attempts = attempts + 1
                   WHERE generation = ? AND interrupt_id = ? AND state = 'prepared'""",
                (batch.generation, operation.interrupt_id),
            )
            self._require_changed("Action operation changed before execution")
            return Execution(True, None, operation_id)

    def _require_changed(self, message: str) -> None:
        if self._connection.execute("SELECT changes()").fetchone() != (1,):
            raise ActionJournalConflictError(message)

    def _decode_result(self, raw: object) -> object:
        if not isinstance(raw, bytes) or len(raw) > self.max_result_bytes:
            raise ActionJournalCorruptionError("cached Action result is invalid")
        try:
            result = json.loads(raw)
            if _canonical_result(result, self.max_result_bytes) != raw:
                raise ValueError("result is not canonical")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, ActionJournalError) as exc:
            raise ActionJournalCorruptionError("cached Action result is invalid") from exc
        return result

    def _validated_result(self, batch: Batch, interrupt_id: str, raw: object) -> object:
        if not isinstance(raw, bytes) or len(raw) > self.max_result_bytes:
            raise ActionJournalCorruptionError("cached Action result is invalid")
        key = (batch.generation, interrupt_id)
        digest = hashlib.sha256(raw).digest()
        if self._validated_results.get(key) == digest:
            try:
                return json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                raise ActionJournalCorruptionError("cached Action result is invalid") from exc
        result = self._decode_result(raw)
        self._validated_results[key] = digest
        return result

    def _remember_result(self, batch: Batch, interrupt_id: str, encoded: bytes) -> None:
        self._validated_results[(batch.generation, interrupt_id)] = hashlib.sha256(encoded).digest()

    def complete(self, batch: Batch, operation: Operation, result: object) -> None:
        """The execution itself returned this result."""
        self._settle(batch, operation, "completed", "execution", None, result)

    def fail_without_effect(self, batch: Batch, operation: Operation) -> None:
        """A handled failure of an Action whose pinned reviewed declaration is ``read_only`` had no business effect."""
        self._settle(batch, operation, "no_effect", "execution", None, None)

    def resolve_verified(self, batch: Batch, operation: Operation, evidence: str, result: object | None) -> None:
        """Team-admitted verifier evidence resolved an uncertain operation: occurred with ``result``, else absent.

        ``evidence`` is the digest that binds the verification to this exact operation. Nothing else, including a
        model's assertion, may move an uncertain operation to an outcome.
        """
        if not isinstance(evidence, str) or http_payload.SHA256_RE.fullmatch(evidence) is None:
            raise ActionJournalConflictError("Action verification evidence is invalid")
        self._settle(
            batch, operation, "completed" if result is not None else "no_effect", "verification", evidence, result
        )

    def _settle(
        self,
        batch: Batch,
        operation: Operation,
        state: str,
        origin: str,
        evidence: str | None,
        result: object | None,
    ) -> None:
        batch = self._validate_handle(batch)
        operation = _operation(operation)
        if operation not in batch.operations:
            raise ActionJournalConflictError("operation does not belong to this Action batch")
        encoded = None if result is None else _canonical_result(result, self.max_result_bytes)
        with self._writing("Action result could not be committed"):
            persisted = self._load_operation(batch, operation)
            current, existing = persisted[3], persisted[4]
            if (current, persisted[6]) == (state, origin):
                if (existing, persisted[7]) != (encoded, evidence):
                    raise ActionJournalConflictError("Action result changed after completion")
            elif current != "executing" or existing is not None:
                raise ActionJournalConflictError("Action operation was not executing")
            else:
                self._connection.execute(
                    """UPDATE operations SET state = ?, origin = ?, evidence = ?, result = ?
                       WHERE generation = ? AND interrupt_id = ? AND state = 'executing'""",
                    (state, origin, evidence, encoded, batch.generation, operation.interrupt_id),
                )
                self._require_changed("Action operation changed before completion")
        self._remember(batch, operation.interrupt_id, encoded)

    def _remember(self, batch: Batch, interrupt_id: str, encoded: bytes | None) -> None:
        if encoded is not None:
            self._remember_result(batch, interrupt_id, encoded)

    def suspend(self, batch: Batch, operation: Operation) -> None:
        """Return only one proven human-request suspension to deterministic replay; its operation id is kept."""
        self._reprepare(batch, operation, "suspension")

    def not_dispatched(self, batch: Batch, operation: Operation) -> None:
        """Return an attempt Team refused before its workload process started to prepared; nothing ran.

        Only Team's own pre-dispatch refusal may settle an attempt this way; any dispatch or inspection ambiguity
        stays uncertain.
        """
        self._reprepare(batch, operation, "dispatch refusal")

    def _reprepare(self, batch: Batch, operation: Operation, settlement: str) -> None:
        batch = self._validate_handle(batch)
        operation = _operation(operation)
        if operation not in batch.operations:
            raise ActionJournalConflictError("operation does not belong to this Action batch")
        with self._writing(f"Action {settlement} could not be committed"):
            persisted = self._load_operation(batch, operation)
            if persisted[3:5] != ("executing", None):
                raise ActionJournalConflictError("Action operation was not executing")
            self._connection.execute(
                """UPDATE operations SET state = 'prepared'
                   WHERE generation = ? AND interrupt_id = ? AND state = 'executing' AND result IS NULL""",
                (batch.generation, operation.interrupt_id),
            )
            self._require_changed(f"Action operation changed before {settlement}")

    def _current(self, batch: Batch, replaced: str) -> bool:
        """Whether the exact batch is still the generation's; absent is False, and another batch is refused."""
        row = self._connection.execute(
            "SELECT fingerprint FROM batches WHERE generation = ?",
            (batch.generation,),
        ).fetchone()
        if row is not None and row != (batch.fingerprint,):
            raise ActionJournalConflictError(f"a newer Action batch replaced this {replaced} handle")
        return row is not None

    def _delete(self, batch: Batch, message: str) -> None:
        self._connection.execute(
            "DELETE FROM batches WHERE generation = ? AND fingerprint = ?",
            (batch.generation, batch.fingerprint),
        )
        self._require_changed(message)

    def delivered(self, batch: Batch) -> None:
        batch = self._validate_handle(batch)
        with self._writing("Action batch delivery could not be committed"):
            if not self._current(batch, "delivery"):
                return
            operations = self._load_batch(batch)
            if any(row[3] != "completed" for row in operations):
                raise ActionJournalConflictError("Action batch cannot be delivered before every result exists")
            for operation in operations:
                self._validated_result(batch, str(operation[1]), operation[4])
            self._delete(batch, "Action batch changed before delivery")
        self._forget_generation(batch.generation)

    def abandon_uncertain(self, batch: Batch) -> bool:
        """End one handled terminal attempt only when its exact batch is uncertain."""
        batch = self._validate_handle(batch)
        with self._writing("uncertain Action batch could not be abandoned"):
            if not self._current(batch, "abandonment") or all(
                operation[3] != "executing" for operation in self._load_batch(batch)
            ):
                return False
            self._delete(batch, "Action batch changed before terminal abandonment")
        self._forget_generation(batch.generation)
        return True

    def snapshot(self, generation: str, fingerprint: str) -> tuple[OperationRecord, ...]:
        """The compact state of one live batch's operations, which its incident records before it is archived."""
        safe_generation = _safe_id(generation, "generation")
        with self._guard:
            self._ensure_open()
            try:
                row = self._connection.execute(
                    "SELECT fingerprint, state FROM batches WHERE generation = ?", (safe_generation,)
                ).fetchone()
                rows = self._connection.execute(
                    """SELECT interrupt_id, operation_id, state, attempts, origin
                       FROM operations WHERE generation = ? ORDER BY ordinal""",
                    (safe_generation,),
                ).fetchall()
            except sqlite3.Error as exc:
                raise ActionJournalError("Action journal state could not be read") from exc
        if row is None or row[0] != fingerprint:
            raise ActionJournalConflictError("Action batch is no longer current")
        if row[1] == _ARCHIVED:
            raise ActionJournalArchivedError("Action generation is archived")
        return tuple(OperationRecord(*item) for item in rows)

    def archive(self, generation: str, fingerprint: str) -> None:
        """Mark a held batch archived and remove its operation rows in one commit; an archived one stays as it is.

        Its incident must already be durable, since afterwards only the marker remains. The marker was reserved when
        the batch was prepared, so archiving never fails for capacity.
        """
        safe_generation = _safe_id(generation, "generation")
        with self._writing("Action batch could not be archived"):
            row = self._connection.execute(
                "SELECT fingerprint, state, archivable FROM batches WHERE generation = ?", (safe_generation,)
            ).fetchone()
            if row is None or row[0] != fingerprint:
                raise ActionJournalConflictError("Action batch is no longer current")
            if row[1] == _ARCHIVED:
                return
            if row[2] != 1:
                raise ActionJournalConflictError("Action batch holds no archive reservation")
            self._connection.execute(
                "UPDATE batches SET state = 'archived' WHERE generation = ? AND fingerprint = ?",
                (safe_generation, fingerprint),
            )
            self._require_changed("Action batch changed before it was archived")
            self._connection.execute("DELETE FROM operations WHERE generation = ?", (safe_generation,))
        self._forget_generation(safe_generation)

    def release_archive(self, generation: str, fingerprint: str) -> bool:
        """Remove a settled archive marker once no resumable state needs it; False when it is already gone."""
        safe_generation = _safe_id(generation, "generation")
        with self._writing("Action archive could not be released"):
            row = self._connection.execute(
                "SELECT fingerprint, state FROM batches WHERE generation = ?", (safe_generation,)
            ).fetchone()
            if row is None:
                return False
            if row != (fingerprint, _ARCHIVED):
                raise ActionJournalConflictError("Action generation holds no such archived batch")
            self._connection.execute("DELETE FROM batches WHERE generation = ?", (safe_generation,))
        return True

    def discard(self, generation: str) -> None:
        """Remove an ended run's live batch, keeping an archive marker that unresolved evidence still needs."""
        safe_generation = _safe_id(generation, "generation")
        with self._writing("Action generation could not be discarded"):
            self._connection.execute(
                "DELETE FROM batches WHERE generation = ? AND state != 'archived'", (safe_generation,)
            )
        self._forget_generation(safe_generation)

    def purge(self, generation: str) -> None:
        """Remove everything a generation holds, archive marker included, as deleting its Team does."""
        safe_generation = _safe_id(generation, "generation")
        with self._writing("Action generation could not be purged"):
            self._connection.execute("DELETE FROM batches WHERE generation = ?", (safe_generation,))
        self._forget_generation(safe_generation)

    def current_batch(self, generation: str) -> tuple[str, str] | None:
        """The fingerprint and state (open, ended, or archived) of a generation's batch, or None when it holds none."""
        safe_generation = _safe_id(generation, "generation")
        with self._guard:
            self._ensure_open()
            try:
                row = self._connection.execute(
                    "SELECT fingerprint, state FROM batches WHERE generation = ?", (safe_generation,)
                ).fetchone()
            except sqlite3.Error as exc:
                raise ActionJournalError("Action journal state could not be read") from exc
        return None if row is None else (str(row[0]), str(row[1]))

    def uncertain_fingerprint(self, generation: str) -> str | None:
        """The fingerprint of a generation's batch when one of its operations may have acted, else None."""
        safe_generation = _safe_id(generation, "generation")
        with self._guard:
            self._ensure_open()
            try:
                row = self._connection.execute(
                    """SELECT b.fingerprint FROM batches AS b
                       JOIN operations AS o ON o.generation = b.generation
                       WHERE b.generation = ? AND o.state = 'executing' LIMIT 1""",
                    (safe_generation,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise ActionJournalError("Action journal state could not be read") from exc
        return None if row is None else str(row[0])

    def end(self, batch: Batch) -> bool:
        """End one exact undelivered batch at a terminal turn unless one of its operations may have acted."""
        batch = self._validate_handle(batch)
        with self._writing("Action batch could not be ended"):
            row = self._connection.execute(
                "SELECT fingerprint, state FROM batches WHERE generation = ?",
                (batch.generation,),
            ).fetchone()
            if row is None or row == (batch.fingerprint, _ENDED):
                return False
            if row[0] != batch.fingerprint:
                raise ActionJournalConflictError("a newer Action batch replaced this ending handle")
            ended = self._end_settled(batch.generation, (row[3] for row in self._load_batch(batch)))
        if ended:
            self._forget_generation(batch.generation)
        return ended

    def end_settled(self, generation: str) -> bool:
        """End a generation's undelivered batch before a fresh turn unless one of its operations may have acted."""
        safe_generation = _safe_id(generation, "generation")
        with self._writing("settled Action generation could not be ended"):
            operations = self._connection.execute(
                """SELECT o.state FROM batches AS b
                   JOIN operations AS o ON o.generation = b.generation
                   WHERE b.generation = ? AND b.state = 'open'""",
                (safe_generation,),
            ).fetchall()
            ended = bool(operations) and self._end_settled(safe_generation, (row[0] for row in operations))
        if ended:
            self._forget_generation(safe_generation)
        return ended

    def _end_settled(self, generation: str, states: Iterable[object]) -> bool:
        if "executing" in states:
            return False
        self._connection.execute(
            "UPDATE batches SET state = 'ended' WHERE generation = ? AND state = 'open'",
            (generation,),
        )
        self._require_changed("Action batch changed before it ended")
        return True

    def close(self) -> None:
        with self._guard:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> ActionJournal:
        self._ensure_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
