"""Observe a Team's file database in tests: what storage records but never reads back for itself."""

import sqlite3
from contextlib import closing

from storage import files as team_storage


def referenced(storage: team_storage.TeamStorage, team_id: str) -> frozenset[str]:
    """The files the Team's current chat turn references, as its file database durably records them."""
    path = storage.root / team_id / "files.sqlite3"
    if not path.exists():
        return frozenset()
    with closing(sqlite3.connect(path)) as connection:
        return frozenset(row[0] for row in connection.execute("SELECT id FROM files WHERE referenced=1"))
