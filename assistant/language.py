"""Verified language packs that travel with one reviewed Assistant artifact (ADR-0091)."""

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from assistant import manifest as assistant_manifest
from protocol.assistant.v1 import message_catalog_validator as catalog_validator

PACK_PATH = "/opt/shimpz/shimpz.pack.json"
MAX_PACK_BYTES = catalog_validator.MAX_PACK_BYTES
# English is the catalog itself; every other interface language comes from the pack.
ENGLISH = "en"
# A Team holds a few small packs in memory; the byte budget bounds a Space of large ones.
DEFAULT_CACHE_ENTRIES = 256
DEFAULT_CACHE_BYTES = 32 * 1024 * 1024
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class LanguagePackError(assistant_manifest.ManifestError):
    """A language pack is missing, modified, incomplete, or does not match its reviewed binding."""


@dataclass(frozen=True, slots=True, eq=False)
class LanguagePack:
    """The English catalog and every admitted translation of one reviewed binding."""

    catalog_digest: str
    pack_digest: str
    messages: Mapping[str, Mapping[str, Any]]
    translations: Mapping[str, Mapping[str, str]]
    size: int = 0

    def template(self, identifier: str, locale: str) -> str:
        """The English template or its admitted translation for one declared message."""
        if locale == ENGLISH:
            return str(self.messages[identifier]["msgid"])
        return self.translations[locale][identifier]


def catalog_digest(machine_contract: Mapping[str, Any]) -> str:
    """The digest of the reviewed contract's canonical catalog."""
    return catalog_validator.catalog_digest(machine_contract["messages"])


def admit_pack(raw: bytes, messages: list[dict[str, Any]], expected_digest: str) -> LanguagePack:
    """Admit exact pack bytes for an already-admitted catalog and its reviewed pack digest."""
    if not isinstance(expected_digest, str) or _DIGEST_RE.fullmatch(expected_digest) is None:
        raise LanguagePackError("Assistant language pack digest is invalid")
    error = catalog_validator.pack_error(raw, messages)
    if error is not None:
        raise LanguagePackError(f"Assistant language pack is invalid ({error})")
    if catalog_validator.pack_digest(raw) != expected_digest:
        raise LanguagePackError("Assistant language pack does not match its reviewed digest")
    pack = json.loads(raw)
    return LanguagePack(
        catalog_digest=catalog_validator.catalog_digest(messages),
        pack_digest=expected_digest,
        messages={message["id"]: message for message in messages},
        translations=pack["locales"],
        size=len(raw),
    )


def read_container_pack(container, messages: list[dict[str, Any]], expected_digest: str) -> LanguagePack:
    """Read and admit the fixed read-only pack from an immutable Assistant root."""
    raw = assistant_manifest.read_container_file(
        container,
        path=PACK_PATH,
        name="shimpz.pack.json",
        maximum=MAX_PACK_BYTES,
    )
    return admit_pack(raw, messages, expected_digest)


class LanguagePackCache:
    """Admit each container generation's pack once and keep it only while that generation is reviewed."""

    def __init__(self, max_entries: int = DEFAULT_CACHE_ENTRIES, max_bytes: int = DEFAULT_CACHE_BYTES) -> None:
        if any(type(value) is not int or value < 1 for value in (max_entries, max_bytes)):
            raise ValueError("Assistant language pack cache bounds must be positive")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._bytes = 0
        self._entries: OrderedDict[str, LanguagePack] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, container, machine_contract: Mapping[str, Any], expected_digest: str) -> LanguagePack:
        """Return the container's pack only when it is valid for exactly this reviewed catalog and digest."""
        container_id = getattr(container, "id", None)
        if (
            not isinstance(container_id, str)
            or not container_id
            or len(container_id) > 256
            or any(not character.isalnum() and character not in {"-", "_", "."} for character in container_id)
        ):
            raise LanguagePackError("Assistant container identity is invalid")
        messages = machine_contract["messages"]
        expected_catalog = catalog_validator.catalog_digest(messages)
        with self._lock:
            pack = self._entries.get(container_id)
            if pack is not None:
                self._entries.move_to_end(container_id)
        if pack is None:
            pack = read_container_pack(container, messages, expected_digest)
            with self._lock:
                self._store(container_id, pack)
        if (pack.catalog_digest, pack.pack_digest) != (expected_catalog, expected_digest):
            raise LanguagePackError("Assistant language pack does not match its reviewed binding")
        return pack

    def _store(self, container_id: str, pack: LanguagePack) -> None:
        previous = self._entries.pop(container_id, None)
        if previous is not None:
            self._bytes -= previous.size
        self._entries[container_id] = pack
        self._bytes += pack.size
        while len(self._entries) > self._max_entries or (self._bytes > self._max_bytes and len(self._entries) > 1):
            _evicted_id, evicted = self._entries.popitem(last=False)
            self._bytes -= evicted.size

    def discard(self, container_id: object) -> None:
        if isinstance(container_id, str):
            with self._lock:
                previous = self._entries.pop(container_id, None)
                if previous is not None:
                    self._bytes -= previous.size
