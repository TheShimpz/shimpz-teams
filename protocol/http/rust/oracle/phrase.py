"""`phrase`: its readers over the vector texts and generated multilingual text.

The generated texts join fragments drawn from every phrase table's words, the vector texts, counts and times in many
digit scripts, negations, zone names, question marks, and sentence ends, so every reader meets each language's
readings, their negations, and their sentence and clause boundaries.
"""

from __future__ import annotations

import re

import phrase
import routine

from .common import arguments, group
from .mutate import SplitMix64

LANGUAGES = (None, "", *sorted(routine.LOCALES))
DIGITS = ("0", "١", "۲", "৩", "४", "５", "𝟔", "𑁨", "௭", "๘", "᠙")
SEPARATORS = (" ", " ", " ", ", ", ". ", "? ", "! ", "; ", "\n", "。", "؟ ", " ou ", " or ", " 或 ", "、", "", "1. ")
ZONES = (
    "Europe/Lisbon",
    "UTC",
    "America/Sao_Paulo",
    "europe/lisbon",
    "Etc/GMT+3",
    "Mars/Base",
    "UTC+3",
    "(Asia/Tokyo)",
)


def _words() -> list[str]:
    """The words and literal runs of every phrase table pattern."""
    sources = [
        *phrase._NEGATIONS.values(),
        *(pattern for _language, pattern in phrase._INTERVALS + phrase._DAILY + phrase._WEEKLY + phrase._MONTHLY),
        *phrase._TIMES,
        *(pattern for _kind, _language, pattern in phrase._OUTPUTS),
        *(pattern for _language, pattern in phrase._ROUTINE_NOUNS),
        *phrase._ASK_PHRASES,
        *phrase._BARE_ASKS,
    ]
    found: set[str] = set()
    for source in sources:
        plain = re.sub(r"\\[bswdBSWD]|\(\?[:!=<]+|[()?*+|^$\\\[\]{}]|\\.", " ", source)
        found.update(word for word in plain.split() if word)
    found.update(phrase._UNIT)
    found.update(phrase._WEEKDAYS)
    return sorted(found)


def _numbers(rng: SplitMix64) -> str:
    digits = "".join(rng.pick(DIGITS) if rng.below(4) == 0 else str(rng.below(10)) for _ in range(rng.below(3) + 1))
    return digits + rng.pick(("", "h", ":30", "am", " p.m.", "時", "点", "日", "号", "st", "th", "h15", " uhr"))


def _fragment(rng: SplitMix64, words: list[str], texts: list[str]) -> str:
    kind = rng.below(10)
    if kind < 4:
        return rng.pick(words)
    if kind < 6:
        return _numbers(rng)
    if kind < 8:
        text = rng.pick(texts)
        return text if rng.below(2) else text.upper()
    if kind == 8:
        return rng.pick(ZONES)
    return rng.pick(("não", "not", "no", "ne pas", "nicht", "ない", "不", "لا", "?", "？", "؟", "."))


def texts() -> list[str]:
    readings = group("routine_phrase")
    texts = [case["text"] for cases in readings.values() for case in cases]
    words = _words()
    rng = SplitMix64(0x9E3779B9)
    generated = []
    for _ in range(6000):
        parts = [_fragment(rng, words, texts) for _ in range(rng.below(7) + 1)]
        joined = parts[0]
        for part in parts[1:]:
            joined += rng.pick(SEPARATORS) + part
        generated.append(joined)
    return [*texts, *generated]


def _language_case(text: str, index: int) -> dict[str, object]:
    return {"text": text, "language": LANGUAGES[index % len(LANGUAGES)]}


def _language(value: object) -> tuple[bool, object]:
    """Whether an argument is a language the readers know: null, empty, or one interface language."""
    return value is None or (isinstance(value, str) and (not value or value in routine.LOCALES)), value


def _with_language(read):
    def call(value):
        found = arguments(value, "text", "language")
        if found is None or not isinstance(found[0], str) or not _language(found[1])[0]:
            return None
        return read(found[0], found[1])

    return call


def _text(read):
    return lambda value: read(value) if isinstance(value, str) else None


ENTRIES = {
    "phrase.stated": (_text(lambda text: list(phrase.stated(text))), texts),
    "phrase.stated(language)": (
        _with_language(lambda text, language: list(phrase.stated(text, language))),
        lambda: [_language_case(text, index) for index, text in enumerate(texts())],
    ),
    "phrase.outputs": (_text(lambda text: list(phrase.outputs(text))), texts),
    "phrase.zones": (_text(lambda text: list(phrase.zones(text))), texts),
    "phrase.team_asks": (_text(phrase.team_asks), texts),
    "phrase.team_asks(language)": (
        _with_language(phrase.team_asks),
        lambda: [_language_case(text, index) for index, text in enumerate(texts())],
    ),
    "phrase.requests_routine": (_text(phrase.requests_routine), texts),
}
