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


def main() -> int:
    connection = None
    try:
        token = TOKEN_PATH.read_text(encoding="ascii")
        if len(token) != 64:
            return 1
        connection = http.client.HTTPConnection("127.0.0.1", 7077, timeout=3)
        connection.request("GET", "/v1/activity", headers={"Authorization": f"Bearer {token}"})
        response = connection.getresponse()
        if response.status != 200 or response.getheader("Content-Type") != "application/json":
            return 1
        length = int(response.getheader("Content-Length", "0"))
        if not 1 <= length <= 1024:
            return 1
        payload = json.loads(response.read(length))
    except OSError, UnicodeError, ValueError, json.JSONDecodeError, http.client.HTTPException:
        return 1
    finally:
        if connection is not None:
            connection.close()
    if not isinstance(payload, dict) or set(payload) != {"state", "trace_id"} or payload["state"] not in STATES:
        return 1
    print(payload["state"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
