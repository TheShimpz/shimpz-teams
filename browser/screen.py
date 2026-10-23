"""Provenance screening of a Browser task's answer (ADR-0117, 2026-10-22 amendment §4).

Team keeps the URLs of the pages the task observed or read and the URLs mentioned in the text it kept, each a bounded,
oldest-evicted set captured at observation time. An answer's cited URLs are extracted as exact tokens; one in neither
set is unscreened. This is screening, not verification: a page that showed a URL does not prove it supports the answer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

MAX_URLS = 2_000
# A URL as a token: an http(s) scheme and every character up to whitespace or a delimiter an answer puts around it.
URL_RE = re.compile(r"https?://[^\s<>\"'`|()\[\]{}]+")
TRAILING = ".,;:!?"


def urls(text: str) -> list[str]:
    """The distinct URLs a text names, in order, each without trailing sentence punctuation."""
    return list(dict.fromkeys(match.rstrip(TRAILING) for match in URL_RE.findall(text)))


class Screen:
    def __init__(self) -> None:
        self._seen: dict[str, None] = {}
        self._mentioned: dict[str, None] = {}

    def observed(self, url: str, text: str) -> None:
        """Record a page the task observed or read, and the URLs its kept text mentions."""
        _remember(self._seen, (url,))
        _remember(self._mentioned, urls(text))

    def unscreened(self, answer: str) -> list[str]:
        """The answer's cited URLs that no page of the task showed, as its address or in its kept text."""
        return [url for url in urls(answer) if not self._known(url)]

    def _known(self, url: str) -> bool:
        stripped = url.rstrip("/")
        return any(
            candidate in store
            for store in (self._seen, self._mentioned)
            for candidate in (url, stripped, stripped + "/")
        )


def _remember(store: dict[str, None], values: Iterable[str]) -> None:
    for value in values:
        store.pop(value, None)
        store[value] = None
        if len(store) > MAX_URLS:
            del store[next(iter(store))]
