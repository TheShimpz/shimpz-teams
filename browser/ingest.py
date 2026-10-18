"""Bring a finished Browser task's downloads into the Team's files (ADR-0117 §7).

The listing `browser-host files list` answered is parsed strictly. Each complete file within the bounds is read
exactly, typed from its bytes, and stored as an untrusted Team file marked as from the web in the same write; every
other entry gets a closed reason, so the reply can say what was not kept. A Stop ends the ingest before the next file.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from prepare import detect
from storage import files as team_storage

MAX_FILES = 8
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_LISTED = 64
REFUSALS = {"incomplete", "over-limit"}


class ListingError(ValueError):
    """The downloads listing broke its closed shape; nothing of it is trusted."""


class ReadLostError(RuntimeError):
    """A download could not be read exactly; the Browser cannot be trusted for any later file."""


class StoppedError(RuntimeError):
    """The turn was stopped before this download could be stored; no further one is."""


@dataclass(frozen=True, slots=True)
class Ingested:
    """The kept files' references, the named files not kept with their reasons, and how many were unnamed."""

    kept: tuple[dict[str, object], ...]
    lost: tuple[dict[str, str], ...]
    skipped: int


def _entries(listing: Mapping[str, object]) -> tuple[list[tuple[str, int]], list[tuple[str, str]], int]:
    files, refused, skipped = listing.get("files"), listing.get("refused"), listing.get("skipped")
    if not isinstance(files, list) or not isinstance(refused, list) or type(skipped) is not int or skipped < 0:
        raise ListingError("the downloads listing is malformed")
    if len(files) > MAX_LISTED or len(refused) > MAX_LISTED:
        raise ListingError("the downloads listing is oversized")
    named = []
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"name", "size"}:
            raise ListingError("the downloads listing is malformed")
        name, size = entry["name"], entry["size"]
        if not isinstance(name, str) or not name or type(size) is not int or size < 0:
            raise ListingError("the downloads listing is malformed")
        named.append((name, size))
    reasons = []
    for entry in refused:
        if not isinstance(entry, dict) or set(entry) != {"name", "reason", "size"} or entry["reason"] not in REFUSALS:
            raise ListingError("the downloads listing is malformed")
        if not isinstance(entry["name"], str) or not entry["name"]:
            raise ListingError("the downloads listing is malformed")
        reasons.append((entry["name"], entry["reason"]))
    return named, reasons, skipped


def ingest(
    listing: Mapping[str, object],
    read: Callable[[str, int], bytes],
    put: Callable[[str, bytes, str], Mapping[str, object]],
    cancelled: Callable[[], bool],
) -> Ingested:
    """Store at most 8 downloads of at most 25 MiB each, one at a time; give every other one its reason.

    `put` marks each file as from the web as it stores it, and refuses with StoppedError once the turn was stopped; no
    further file is then read or stored.
    """
    named, refused, skipped = _entries(listing)
    kept: list[dict[str, object]] = []
    lost = [{"name": name, "reason": reason} for name, reason in refused]
    for index, (name, size) in enumerate(named):
        if cancelled():
            break
        if index >= MAX_FILES or size > MAX_FILE_BYTES:
            lost.append({"name": name, "reason": "over-limit"})
            continue
        if size == 0:
            lost.append({"name": name, "reason": "empty"})
            continue
        try:
            data = read(name, size)
        except ReadLostError:
            lost.extend({"name": later, "reason": "browser-lost"} for later, _size in named[index:])
            break
        if cancelled():
            # Stop arrived while the file was read: nothing more is stored.
            break
        media_type = detect.detect(name, data).media_type
        try:
            stored = put(name, data, media_type)
        except StoppedError:
            break
        except team_storage.StorageQuotaError:
            lost.append({"name": name, "reason": "quota-exceeded"})
            continue
        except team_storage.StorageInputError:
            lost.append({"name": name, "reason": "name-refused"})
            continue
        except team_storage.StorageError:
            lost.append({"name": name, "reason": "store-unavailable"})
            continue
        kept.append({key: stored[key] for key in ("id", "name", "media_type", "size")})
    return Ingested(tuple(kept), tuple(lost), skipped)
