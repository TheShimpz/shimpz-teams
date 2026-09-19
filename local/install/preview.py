"""Bounded ephemeral reuse of validated Local Assistant preview icons."""

from __future__ import annotations

import threading
from collections import OrderedDict

from local.install import snapshots

MAX_CACHED_ICONS = snapshots.MAX_CANDIDATES
MAX_CACHED_ICON_BYTES = 8 * 1024 * 1024
MAX_CONCURRENT_MISSES = 2


class PreviewBusyError(RuntimeError):
    """All bounded Local preview extraction slots are occupied."""


class LocalSnapshotPreviewCache:
    """Reuse immutable icon bytes while revalidating the exact staged image."""

    def __init__(self, client, platform: str) -> None:
        self._client = client
        self._platform = platform
        self._lock = threading.Lock()
        self._icons: OrderedDict[str, bytes] = OrderedDict()
        self._cached_bytes = 0
        self._miss_slots = threading.BoundedSemaphore(MAX_CONCURRENT_MISSES)

    def icon(self, image_id: str) -> bytes:
        cached = self._cached(image_id)
        if cached is not None:
            try:
                snapshots.require_candidate(self._client, image_id, platform=self._platform)
            except snapshots.LocalSnapshotAbsentError:
                self._discard(image_id)
                raise
            return cached
        if not self._miss_slots.acquire(blocking=False):
            raise PreviewBusyError("Local Assistant preview capacity is busy")
        try:
            contents = snapshots.preview_icon(self._client, image_id, platform=self._platform)
            self._remember(image_id, contents)
            return contents
        finally:
            self._miss_slots.release()

    def _cached(self, image_id: str) -> bytes | None:
        with self._lock:
            contents = self._icons.get(image_id)
            if contents is not None:
                self._icons.move_to_end(image_id)
            return contents

    def _remember(self, image_id: str, contents: bytes) -> None:
        with self._lock:
            self._cached_bytes -= len(self._icons.pop(image_id, b""))
            self._icons[image_id] = contents
            self._cached_bytes += len(contents)
            while len(self._icons) > MAX_CACHED_ICONS or self._cached_bytes > MAX_CACHED_ICON_BYTES:
                _, evicted = self._icons.popitem(last=False)
                self._cached_bytes -= len(evicted)

    def _discard(self, image_id: str) -> None:
        with self._lock:
            self._cached_bytes -= len(self._icons.pop(image_id, b""))
