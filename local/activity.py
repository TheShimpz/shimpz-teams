"""Content-free Team work activity for scheduled Local release reconciliation.

The server counts in-flight mutating requests and automatic Assistant updates. The CLI runs this module inside the
ownership-verified Team container, where it reads the machine bearer and asks the authenticated loopback route. It
prints exactly ``idle`` or ``busy``; every failure exits nonzero so the caller treats activity as unknown.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path

TOKEN_PATH = Path("/run/shimpz-local/token")
STATES = frozenset({"idle", "busy"})


class Activity:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._active = 0

    @contextlib.contextmanager
    def working(self) -> Iterator[None]:
        with self._guard:
            self._active += 1
        try:
            yield
        finally:
            with self._guard:
                self._active -= 1

    def state(self) -> str:
        with self._guard:
            return "busy" if self._active else "idle"


def _token() -> str | None:
    try:
        token = TOKEN_PATH.read_text(encoding="ascii")
    except OSError, UnicodeError:
        return None
    return token if len(token) == 64 else None


def _state(token: str) -> str | None:
    connection = http.client.HTTPConnection("127.0.0.1", 7077, timeout=3)
    try:
        connection.request("GET", "/v1/activity", headers={"Authorization": f"Bearer {token}"})
        response = connection.getresponse()
        if response.status != 200 or response.getheader("Content-Type") != "application/json":
            return None
        length = int(response.getheader("Content-Length", "0"))
        if not 1 <= length <= 1024:
            return None
        payload = json.loads(response.read(length))
    except OSError, ValueError, json.JSONDecodeError, http.client.HTTPException:
        return None
    finally:
        connection.close()
    if not isinstance(payload, dict) or set(payload) != {"state", "trace_id"} or payload["state"] not in STATES:
        return None
    return payload["state"]


def main() -> int:
    token = _token()
    state = _state(token) if token is not None else None
    if state is None:
        return 1
    print(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
