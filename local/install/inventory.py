"""Near-instant, Docker-validated discovery of staged Local Assistants."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import suppress

from docker.errors import DockerException

from local.install import snapshots

MAX_CACHE_AGE_SECONDS = 30.0
REFRESH_WAIT_SECONDS = 12.0
_NANOSECONDS_PER_SECOND = 1_000_000_000
log = logging.getLogger("shimpz-team-local-snapshot-inventory")

CandidateLoader = Callable[[object, str], tuple[snapshots.LocalSnapshotCandidate, ...]]


def _load_candidates(client, platform: str) -> tuple[snapshots.LocalSnapshotCandidate, ...]:
    return snapshots.list_candidates(client, platform=platform)


class LocalSnapshotInventory:
    """Keep one ephemeral candidate projection fresh against Docker image events."""

    def __init__(
        self,
        client,
        platform: str,
        *,
        loader: CandidateLoader = _load_candidates,
        clock_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
        max_age_seconds: float = MAX_CACHE_AGE_SECONDS,
    ) -> None:
        self._client = client
        self._platform = platform
        self._loader = loader
        self._clock_ns = clock_ns
        self._monotonic = monotonic
        self._max_age_seconds = max_age_seconds
        self._condition = threading.Condition()
        self._candidates: tuple[snapshots.LocalSnapshotCandidate, ...] | None = None
        self._cursor_ns = 0
        self._loaded_at = 0.0
        self._refreshing = False

    def warm(self) -> None:
        """Start one best-effort refresh without delaying Team startup."""
        with self._condition:
            if self._candidates is not None or self._refreshing:
                return
            self._start_background_locked()

    def candidates(self) -> tuple[snapshots.LocalSnapshotCandidate, ...]:
        """Return the current projection after a bounded Docker freshness check."""
        while True:
            snapshot = self._snapshot_or_claim_cold_refresh()
            if snapshot is None:
                return self._refresh()
            cached, cursor_ns = snapshot
            changed, next_cursor_ns = self._validate(cursor_ns)
            if changed:
                if self._claim_refresh(cursor_ns):
                    return self._refresh()
                continue
            with self._condition:
                if self._cursor_ns != cursor_ns or self._candidates is None:
                    continue
                self._cursor_ns = next_cursor_ns
                cached = self._candidates
                if self._monotonic() - self._loaded_at >= self._max_age_seconds:
                    self._start_background_locked()
                return cached

    def _snapshot_or_claim_cold_refresh(
        self,
    ) -> tuple[tuple[snapshots.LocalSnapshotCandidate, ...], int] | None:
        with self._condition:
            while self._candidates is None:
                if not self._refreshing:
                    self._refreshing = True
                    return None
                if not self._condition.wait(timeout=REFRESH_WAIT_SECONDS):
                    raise snapshots.LocalSnapshotUnavailableError(
                        "Local Assistant snapshot inventory refresh timed out"
                    )
            return self._candidates, self._cursor_ns

    def _validate(self, cursor_ns: int) -> tuple[bool, int]:
        next_cursor_ns = self._clock_ns()
        if next_cursor_ns <= cursor_ns:
            return True, next_cursor_ns
        stream = None
        try:
            stream = self._client.events(
                since=_docker_timestamp(cursor_ns),
                until=_docker_timestamp(next_cursor_ns),
                filters={"type": "image"},
                decode=True,
            )
            events = tuple(stream)
        except (DockerException, OSError, TypeError, ValueError):
            log.warning("Docker image event validation failed; refreshing directly", exc_info=True)
            return True, next_cursor_ns
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                with suppress(AttributeError, DockerException, OSError, UnboundLocalError):
                    close()
        return bool(events), next_cursor_ns

    def _claim_refresh(self, cursor_ns: int) -> bool:
        with self._condition:
            if self._cursor_ns != cursor_ns:
                return False
            if not self._refreshing:
                self._refreshing = True
                return True
            if not self._condition.wait(timeout=REFRESH_WAIT_SECONDS):
                raise snapshots.LocalSnapshotUnavailableError(
                    "Local Assistant snapshot inventory refresh timed out"
                )
            return False

    def _refresh(self) -> tuple[snapshots.LocalSnapshotCandidate, ...]:
        cursor_ns = self._clock_ns()
        try:
            candidates = self._loader(self._client, self._platform)
        except Exception:
            with self._condition:
                self._refreshing = False
                self._condition.notify_all()
            raise
        with self._condition:
            self._candidates = candidates
            self._cursor_ns = cursor_ns
            self._loaded_at = self._monotonic()
            self._refreshing = False
            self._condition.notify_all()
        return candidates

    def _start_background_locked(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        thread = threading.Thread(
            target=self._background_refresh,
            name="local-snapshot-inventory",
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError:
            self._refreshing = False
            self._condition.notify_all()

    def _background_refresh(self) -> None:
        try:
            self._refresh()
        except (DockerException, OSError, snapshots.LocalSnapshotError):
            log.warning("Local Assistant snapshot inventory background refresh deferred", exc_info=True)


def _docker_timestamp(value_ns: int) -> str:
    seconds, nanoseconds = divmod(value_ns, _NANOSECONDS_PER_SECOND)
    return f"{seconds}.{nanoseconds:09d}"
