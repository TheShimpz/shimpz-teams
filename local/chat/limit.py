"""The Space's chat execution limit (ADR-0119): how many chat turns the Space runs at once.

Team owns it, as one owner-only record in the private inference volume. It is read once and then replaced only by a
committed save, so every admission compares against the same current value; an absent record is the default. A record
that is unreadable or not this store's own fails closed: no chat turn starts until a save replaces it or the Space is
reset.
"""

import threading
from http import HTTPStatus
from pathlib import Path

from local.errors import ApiProblemError as ApiProblem
from protocol.http.v1 import payload as http_payload
from storage import private_state

SCHEMA = 1
DEFAULT = 2
MAX_RECORD_BYTES = 256
NAME = "chat-limit.json"


def _unavailable() -> ApiProblem:
    return ApiProblem(
        HTTPStatus.SERVICE_UNAVAILABLE, "the Space chat limit is unavailable", code="chat-limit-unavailable"
    )


class ChatLimitStore:
    """The current limit, loaded once and replaced only by a committed save."""

    def __init__(self, root: Path) -> None:
        self._path = root / NAME
        self._lock = threading.Lock()
        self._current: int | None = None

    def current(self) -> int:
        with self._lock:
            if self._current is None:
                self._current = self._load()
            return self._current

    def save(self, limit: int) -> int:
        """Durably replace the limit; admissions see it once the record is committed."""
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                private_state.write_json_record(self._path, {"schema": SCHEMA, "limit": limit}, MAX_RECORD_BYTES)
            except (OSError, ValueError) as exc:
                raise _unavailable() from exc
            self._current = limit
            return limit

    def delete(self) -> None:
        """Forget the limit for a Space reset; the next admission reads the default."""
        with self._lock:
            try:
                self._path.unlink(missing_ok=True)
            except OSError as exc:
                raise _unavailable() from exc
            self._current = None

    def _load(self) -> int:
        try:
            value = private_state.read_json_record(self._path, MAX_RECORD_BYTES)
        except (OSError, ValueError) as exc:
            raise _unavailable() from exc
        if value is None:
            return DEFAULT
        if not isinstance(value, dict) or set(value) != {"schema", "limit"} or value["schema"] != SCHEMA:
            raise _unavailable()
        admitted = http_payload.canonical_chat_limit({"limit": value["limit"]})
        if admitted is None or type(value["schema"]) is not int:
            raise _unavailable()
        return admitted["limit"]
