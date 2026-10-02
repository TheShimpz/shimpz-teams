"""Bounded cache for Assistant guidance stored in `shimpz.toml`."""

from __future__ import annotations

from assistant import cache as assistant_cache
from assistant import manifest as assistant_manifest

GENESIS_PATH = assistant_manifest.MANIFEST_PATH
DEFAULT_CACHE_ENTRIES = 256


class GenesisError(RuntimeError):
    """An immutable Assistant manifest did not expose safe model guidance."""


def read_container_genesis(container) -> str:
    """Read `genesis` from the fixed, read-only Spec v1 manifest."""
    try:
        return assistant_manifest.read_container_manifest_genesis(container)
    except assistant_manifest.ManifestError as exc:
        raise GenesisError("Assistant Genesis is invalid") from exc


class GenesisCache:
    """Read Genesis once per immutable container generation with a bounded LRU."""

    def __init__(self, max_entries: int = DEFAULT_CACHE_ENTRIES) -> None:
        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries < 1:
            raise ValueError("Genesis cache size must be positive")
        self._cache: assistant_cache.ContainerReadCache[str] = assistant_cache.ContainerReadCache(max_entries)

    def get(self, container) -> str:
        container_id = getattr(container, "id", None)
        if (
            not isinstance(container_id, str)
            or not container_id
            or len(container_id) > 256
            or any(not character.isalnum() and character not in {"-", "_", "."} for character in container_id)
        ):
            raise GenesisError("Assistant container identity is invalid")
        return self._cache.get(container_id, lambda: read_container_genesis(container))

    def discard(self, container_id: object) -> None:
        if isinstance(container_id, str):
            self._cache.discard(container_id)
