"""Bounded ephemeral reuse of validated Local Assistant previews: the icon and the localized Assistant pages."""

import json
import threading
from collections import OrderedDict
from concurrent.futures import Future

from local.install import snapshots

MAX_CACHED_PREVIEWS = snapshots.MAX_CANDIDATES
MAX_CACHED_PREVIEW_BYTES = 8 * 1024 * 1024
MAX_CONCURRENT_MISSES = 2


class PreviewBusyError(RuntimeError):
    """All bounded Local preview extraction slots are occupied."""


def _size(value: snapshots.SnapshotPreview) -> int:
    """The icon's bytes and every language's page as compact UTF-8 JSON, which bounds all the text it holds."""
    pages = (json.dumps(dict(page), ensure_ascii=False, separators=(",", ":")) for page in value.details.values())
    return len(value.icon) + sum(len(page.encode()) for page in pages)


class LocalSnapshotPreviewCache:
    """Reuse one immutable image's validated preview while revalidating the exact staged image.

    Concurrent misses for one image join a single extraction that holds one bounded slot, so concurrent icon, summary,
    and details requests extract an image's preview once and share its outcome.
    """

    def __init__(self, client, platform: str) -> None:
        self._client = client
        self._platform = platform
        self._lock = threading.Lock()
        self._previews: OrderedDict[str, snapshots.SnapshotPreview] = OrderedDict()
        self._cached_bytes = 0
        self._miss_slots = threading.BoundedSemaphore(MAX_CONCURRENT_MISSES)
        self._extractions: dict[str, Future[snapshots.SnapshotPreview]] = {}

    def icon(self, image_id: str) -> bytes:
        return self._preview(image_id).icon

    def summary(self, image_id: str, locale: str) -> str:
        """The image's summary in one closed interface language, read only from its own admitted pack."""
        return str(self._preview(image_id).details[locale]["summary"])

    def details(self, image_id: str, locale: str) -> dict[str, object]:
        """A copy of the image's Assistant page in one closed interface language, from its own admitted pack."""
        return json.loads(json.dumps(dict(self._preview(image_id).details[locale])))

    def _preview(self, image_id: str) -> snapshots.SnapshotPreview:
        with self._lock:
            cached = self._previews.get(image_id)
            if cached is not None:
                self._previews.move_to_end(image_id)
                extraction, owned = None, False
            else:
                extraction = self._extractions.get(image_id)
                owned = extraction is None
                if owned:
                    if not self._miss_slots.acquire(blocking=False):
                        raise PreviewBusyError("Local Assistant preview capacity is busy")
                    extraction = self._extractions[image_id] = Future()
        if extraction is None:
            return self._revalidated(image_id, cached)
        if not owned:
            return extraction.result()
        return self._extract(image_id, extraction)

    def _revalidated(self, image_id: str, cached: snapshots.SnapshotPreview) -> snapshots.SnapshotPreview:
        try:
            snapshots.require_candidate(self._client, image_id, platform=self._platform)
        except snapshots.LocalSnapshotAbsentError:
            self._discard(image_id)
            raise
        return cached

    def _extract(self, image_id: str, extraction: Future[snapshots.SnapshotPreview]) -> snapshots.SnapshotPreview:
        """Extract in this caller's slot, and hand the outcome to every request that joined this extraction."""
        try:
            value = snapshots.preview(self._client, image_id, platform=self._platform)
            self._remember(image_id, value)
        except BaseException as exc:
            extraction.set_exception(exc)
            raise
        finally:
            with self._lock:
                del self._extractions[image_id]
            self._miss_slots.release()
        extraction.set_result(value)
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
