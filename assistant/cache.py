"""Bounded cache of reads from immutable Assistant containers, coordinated per container."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable

# Distinct cold Docker archive reads one cache runs at once; further misses wait without holding the map.
MAX_CONCURRENT_READS = 4


class _Flight[T]:
    """One in-progress read that every concurrent miss for the same container waits on."""

    __slots__ = ("done", "error", "value")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.error: BaseException | None = None
        self.value: T | None = None


class ContainerReadCache[T]:
    """A bounded LRU whose lock only guards the map: a cold read never delays a warm hit for another container.

    Concurrent misses for one container share a single read, and its failure, while at most
    ``MAX_CONCURRENT_READS`` distinct reads run. A container discarded while its read runs is not cached.
    """

    def __init__(self, max_entries: int) -> None:
        self._max_entries = max_entries
        self._entries: OrderedDict[str, T] = OrderedDict()
        self._flights: dict[str, _Flight[T]] = {}
        self._lock = threading.Lock()
        self._reads = threading.BoundedSemaphore(MAX_CONCURRENT_READS)

    def get(self, key: str, read: Callable[[], T]) -> T:
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                return self._entries[key]
            flight = self._flights.get(key)
            if flight is None:
                flight = self._flights[key] = _Flight()
                leader = True
            else:
                leader = False
        if not leader:
            flight.done.wait()
            if flight.error is not None:
                raise flight.error
            return flight.value
        try:
            with self._reads:
                flight.value = read()
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            self._land(key, flight)
        return flight.value

    def _land(self, key: str, flight: _Flight[T]) -> None:
        with self._lock:
            if self._flights.get(key) is flight:
                del self._flights[key]
                if flight.error is None:
                    self._entries[key] = flight.value
                    while len(self._entries) > self._max_entries:
                        self._entries.popitem(last=False)
        flight.done.set()

    def discard(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)
            self._flights.pop(key, None)
