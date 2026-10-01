"""Bounded ephemeral reuse of validated Local Assistant previews: the icon and the localized summaries."""

from __future__ import annotations

import threading
from collections import OrderedDict

from local.install import snapshots

MAX_CACHED_PREVIEWS = snapshots.MAX_CANDIDATES
MAX_CACHED_PREVIEW_BYTES = 8 * 1024 * 1024
MAX_CONCURRENT_MISSES = 2


class PreviewBusyError(RuntimeError):
    """All bounded Local preview extraction slots are occupied."""


def _size(value: snapshots.SnapshotPreview) -> int:
    return len(value.icon) + sum(len(summary.encode()) for summary in value.summaries.values())


class LocalSnapshotPreviewCache:
    """Reuse one immutable image's validated preview while revalidating the exact staged image."""

    def __init__(self, client, platform: str) -> None:
        self._client = client
        self._platform = platform
        self._lock = threading.Lock()
        self._previews: OrderedDict[str, snapshots.SnapshotPreview] = OrderedDict()
        self._cached_bytes = 0
        self._miss_slots = threading.BoundedSemaphore(MAX_CONCURRENT_MISSES)

    def icon(self, image_id: str) -> bytes:
        return self._preview(image_id).icon

    def summary(self, image_id: str, locale: str) -> str:
        """The image's summary in one closed interface language, read only from its own admitted pack."""
        return self._preview(image_id).summaries[locale]

    def _preview(self, image_id: str) -> snapshots.SnapshotPreview:
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
            value = snapshots.preview(self._client, image_id, platform=self._platform)
            self._remember(image_id, value)
            return value
        finally:
            self._miss_slots.release()

    def _cached(self, image_id: str) -> snapshots.SnapshotPreview | None:
        with self._lock:
            value = self._previews.get(image_id)
            if value is not None:
                self._previews.move_to_end(image_id)
            return value

    def _remember(self, image_id: str, value: snapshots.SnapshotPreview) -> None:
        with self._lock:
            previous = self._previews.pop(image_id, None)
            if previous is not None:
                self._cached_bytes -= _size(previous)
            self._previews[image_id] = value
            self._cached_bytes += _size(value)
            while len(self._previews) > MAX_CACHED_PREVIEWS or self._cached_bytes > MAX_CACHED_PREVIEW_BYTES:
                _, evicted = self._previews.popitem(last=False)
                self._cached_bytes -= _size(evicted)

    def _discard(self, image_id: str) -> None:
        with self._lock:
            previous = self._previews.pop(image_id, None)
            if previous is not None:
                self._cached_bytes -= _size(previous)
