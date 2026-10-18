"""Which Team files came from the web, and the Supervisor acknowledgements their originals need (ADR-0117 §7).

A Browser download is marked by its SHA-256 here before its bytes become a Team file, so no original from the web
exists without its marker; every file whose digest is marked counts as from the web, an identical earlier upload
included. Team releases such a file's original only after the Supervisor acknowledged it for that file id and digest.
The store lives beside the Team's files database, is bounded, is pruned of digests no file has any more, and is
removed with the Team directory. A store that cannot be read refuses every release.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from pathlib import Path

from storage import files, private_state

MAX_MARKERS = 1_024
# A marker precedes its file, so only a marker this old with no file is an orphan; an ingest takes seconds.
ORPHAN_SECONDS = 3_600
MAX_ACKNOWLEDGEMENTS = 4_096
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PRINCIPAL_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
SCHEMA = """
CREATE TABLE IF NOT EXISTS markers (
    sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64),
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS acknowledgements (
    file_id TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    principal TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (file_id, sha256, principal)
);
"""


class WebFiles:
    """The web markers and acknowledgements of every Team in one file store."""

    def __init__(self, storage: files.TeamStorage, clock: Callable[[], float] = time.time) -> None:
        self._storage = storage
        self._clock = clock

    def _path(self, team_id: str) -> Path:
        path = self._storage.team_directory(team_id) / "web.sqlite3"
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not private_state.file_custody_holds(
                info, uid=os.geteuid(), gid=None, mode=private_state.OWNER_ONLY, size=None
            ):
                raise files.StorageError("Team web store has an unsafe shape")
        else:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            with suppress(FileExistsError):
                os.close(os.open(path, flags, 0o600))
        return path

    @contextmanager
    def _transaction(self, team_id: str) -> Iterator[sqlite3.Connection]:
        try:
            with closing(sqlite3.connect(self._path(team_id), timeout=5, isolation_level=None)) as connection:
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(SCHEMA)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    yield connection
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                connection.execute("COMMIT")
        except (sqlite3.Error, OSError) as exc:
            raise files.StorageError("Team web store is unavailable") from exc

    def _prune(self, team_id: str, connection: sqlite3.Connection) -> None:
        """Drop the acknowledgements of every file that is gone, and every orphan marker past its grace period."""
        current = self._storage.list(team_id)["files"]
        kept = {str(item["sha256"]) for item in current}
        listed = {str(item["id"]) for item in current}
        cutoff = int(self._clock()) - ORPHAN_SECONDS
        for (digest,) in connection.execute("SELECT sha256 FROM markers WHERE created_at <= ?", (cutoff,)).fetchall():
            if digest not in kept:
                connection.execute("DELETE FROM markers WHERE sha256=?", (digest,))
        for file_id, digest in connection.execute("SELECT file_id, sha256 FROM acknowledgements").fetchall():
            if file_id not in listed or digest not in kept:
                connection.execute("DELETE FROM acknowledgements WHERE file_id=? AND sha256=?", (file_id, digest))

    def mark(self, team_id: str, sha256: str) -> None:
        """Mark a digest as from the web before its bytes are stored; refuse once the bounded store is full."""
        digest = _digest(sha256)
        with self._transaction(team_id) as connection:
            self._prune(team_id, connection)
            known = connection.execute("SELECT 1 FROM markers WHERE sha256=?", (digest,)).fetchone()
            count = connection.execute("SELECT COUNT(*) FROM markers").fetchone()[0]
            if known is None and count >= MAX_MARKERS:
                raise files.StorageError("Team web store is full")
            connection.execute(
                "INSERT OR IGNORE INTO markers (sha256, created_at) VALUES (?, ?)", (digest, int(self._clock()))
            )

    def is_web(self, team_id: str, sha256: str) -> bool:
        digest = _digest(sha256)
        with self._transaction(team_id) as connection:
            return connection.execute("SELECT 1 FROM markers WHERE sha256=?", (digest,)).fetchone() is not None

    def acknowledge(self, team_id: str, file_id: str, sha256: str, principal: str) -> None:
        """Record that `principal` accepted the risk of this exact web file's original."""
        digest, who = _digest(sha256), _principal(principal)
        with self._transaction(team_id) as connection:
            if connection.execute("SELECT 1 FROM markers WHERE sha256=?", (digest,)).fetchone() is None:
                raise files.StorageError("the file is not from the web")
            self._prune(team_id, connection)
            count = connection.execute("SELECT COUNT(*) FROM acknowledgements").fetchone()[0]
            if count >= MAX_ACKNOWLEDGEMENTS:
                raise files.StorageError("Team web store is full")
            connection.execute(
                "INSERT OR IGNORE INTO acknowledgements (file_id, sha256, principal, created_at) VALUES (?,?,?,?)",
                (file_id, digest, who, int(self._clock())),
            )

    def acknowledged(self, team_id: str, file_id: str, sha256: str, principal: str) -> bool:
        digest, who = _digest(sha256), _principal(principal)
        with self._transaction(team_id) as connection:
            row = connection.execute(
                "SELECT 1 FROM acknowledgements WHERE file_id=? AND sha256=? AND principal=?", (file_id, digest, who)
            ).fetchone()
        return row is not None


def _digest(value: object) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise files.StorageError("the file digest is invalid")
    return value


def _principal(value: object) -> str:
    if not isinstance(value, str) or not PRINCIPAL_RE.fullmatch(value):
        raise files.StorageError("the acknowledging principal is invalid")
    return value
