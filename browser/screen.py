"""Provenance screening of a Browser task's answer (ADR-0117, 2026-10-22 amendment §4).

Team keeps the URLs of the pages the task observed or read and the URLs mentioned in the text it kept, each a bounded,
oldest-evicted set captured at observation time. An answer's cited URLs are extracted as exact tokens; one in neither
set is unscreened. This is screening, not verification: a page that showed a URL does not prove it supports the answer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

MAX_URLS = 2_000
# A URL as a token: an http(s) scheme and every character up to whitespace or a quote or bar an answer puts around it.
URL_RE = re.compile(r"https?://[^\s<>\"'`|]+")
TRAILING = ".,;:!?"
CLOSERS = {")": "(", "]": "[", "}": "{"}


def _token(match: str) -> str:
    """A URL token without the sentence punctuation or unbalanced closing brackets that follow it in prose."""
    while match and (
        match[-1] in TRAILING or (match[-1] in CLOSERS and match.count(match[-1]) > match.count(CLOSERS[match[-1]]))
    ):
        match = match[:-1]
    return match


def _destinations(text: str) -> tuple[list[str], str]:
    """Each Markdown link destination, read up to its balanced closing parenthesis, and the text without them."""
    found: list[str] = []
    rest: list[str] = []
    start = 0
    for match in re.finditer(r"\]\((?=https?://)", text):
        if match.start() < start:
            continue
        depth, end = 1, match.end()
        while end < len(text) and depth and not text[end].isspace():
            depth += {"(": 1, ")": -1}.get(text[end], 0)
            end += 1
        if depth:
            continue
        found.append(text[match.end() : end - 1])
        rest.append(text[start : match.start() + 1])
        start = end
    rest.append(text[start:])
    return found, "".join(rest)


def urls(text: str) -> list[str]:
    """The distinct URLs a text names, in order: a link destination as written, a URL in prose as its token."""
    linked, prose = _destinations(text)
    found = [*linked, *(token for token in map(_token, URL_RE.findall(prose)) if token)]
    return list(dict.fromkeys(found))


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
        return url in self._seen or url in self._mentioned


def _remember(store: dict[str, None], values: Iterable[str]) -> None:
    for value in values:
        store.pop(value, None)
        store[value] = None
        if len(store) > MAX_URLS:
            del store[next(iter(store))]
