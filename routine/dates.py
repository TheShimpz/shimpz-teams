"""Dates written inside a recorded text input, which each run writes again for its own date (ADR-0101).

A recorded search such as "AI news 15 October 2026" must search the date of each run, not the recording's. Like the
run-time date expressions of workflow tools, such a value is kept as a template: its literal text and, in place of each
date, a part holding a whole-day offset from the run date and a closed format. A part's format is literal characters
and the tokens ``{d}`` ``{dd}`` ``{M}`` ``{MM}`` ``{yyyy}``, or a month name ``{mmmm}`` (lowercase), ``{Mmmm}``
(capitalized), or ``{MMMM}`` (uppercase) in one interface language.

Recognition reads only whole dates in a closed set of forms: ISO ``2026-10-15``; numeric ``15/10/2026``, ``15.10.2026``
and ``15/10``, day first or month first; ``2026年10月15日`` and ``10月15日``; and a day with a month name, day first
(``15 de outubro de 2026``, ``15. Oktober``) or month first (``October 15, 2026``). A date counts only within
``MAX_OFFSET_DAYS`` of the recording's date; a written form that reads as two dates, two orders, or two languages there
is decided by the person's interface language, else the text stays a literal. A bare day joined to a date by a range
word ("15 a 21/10/2026") is never guessed, so such a text stays a literal too. The template must write the recorded
text again byte for byte on the recording's date, or it is no template.
"""

import datetime
import re
from collections.abc import Callable, Mapping, Sequence

from routine import schedule

MAX_OFFSET_DAYS = 31
MAX_PARTS = 32
MAX_FORMAT_CHARS = 64
LANGUAGES = ("pt", "en", "es", "fr", "de", "ar")
MONTHS = {
    "pt": "janeiro fevereiro março abril maio junho julho agosto setembro outubro novembro dezembro",
    "en": "january february march april may june july august september october november december",
    "es": "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre",
    "fr": "janvier février mars avril mai juin juillet août septembre octobre novembre décembre",
    "de": "januar februar märz april mai juni juli august september oktober november dezember",
    "ar": "يناير فبراير مارس أبريل مايو يونيو يوليو أغسطس سبتمبر أكتوبر نوفمبر ديسمبر",
}
_NAMES = {language: tuple(names.split()) for language, names in MONTHS.items()}
_CASES = {"mmmm": str.lower, "Mmmm": lambda name: name[:1].upper() + name[1:], "MMMM": str.upper}
_TOKEN_RE = re.compile(r"\{(d|dd|M|MM|yyyy|mmmm|Mmmm|MMMM)\}")
_FORMAT_RE = re.compile(r"(?:[^{}]|\{(?:d|dd|M|MM|yyyy|mmmm|Mmmm|MMMM)\})+")
_NAMED = {"mmmm", "Mmmm", "MMMM"}
# Numeric orders: month first is the order only English writes.
_MONTH_FIRST = frozenset({"en"})

_NAME_ALTERNATION = "|".join(
    sorted({re.escape(name) for names in _NAMES.values() for name in names}, key=len, reverse=True)
)
_FORMS = (
    ("iso", re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d-])")),
    ("numeric", re.compile(r"(?<![\d/.])(\d{1,2})([/.])(\d{1,2})\2(\d{4})(?![\d/]|\.\d)")),
    ("short", re.compile(r"(?<![\d/.])(\d{1,2})/(\d{1,2})(?![\d/]|\.\d)")),
    ("cjk", re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日")),
    (
        "day_name",
        re.compile(
            rf"(?<![\w.])(\d{{1,2}})(\.?\s+(?:de\s+)?)({_NAME_ALTERNATION})(?:(,?\s+(?:de\s+)?)(\d{{4}}))?(?!\w)",
            re.IGNORECASE,
        ),
    ),
    (
        "name_day",
        re.compile(rf"(?<!\w)({_NAME_ALTERNATION})(\s+)(\d{{1,2}})(?:(,?\s+)(\d{{4}}))?(?!\w)", re.IGNORECASE),
    ),
)
# A bare day joined to a date by a range word: the day is never guessed.
_RANGE_BEFORE_RE = re.compile(r"(?<![\w/.])\d{1,2}\s*(?:-|–|—|a|até|to|al|au|bis|~|〜|至)\s*\Z", re.IGNORECASE)
_RANGE_AFTER_RE = re.compile(r"\A\s*(?:-|–|—|a|até|to|al|au|bis|~|〜|至)\s*\d", re.IGNORECASE)


def render(parts: Sequence[object], run_date: datetime.date) -> str:
    """The text a template writes for a run on ``run_date``: each date part at its offset from that date."""
    return "".join(part if isinstance(part, str) else _written(part, run_date) for part in parts)


def _written(part: dict[str, object], run_date: datetime.date) -> str:
    day = run_date + datetime.timedelta(days=part["days"])

    def token(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in _NAMED:
            return _CASES[name](_NAMES[part["lang"]][day.month - 1])
        value = {"d": day.day, "M": day.month}.get(name[0], day.year)
        return f"{value:02d}" if name in ("dd", "MM") else str(value)

    return _TOKEN_RE.sub(token, part["format"])


def clock_value(source: Mapping[str, object], instant: datetime.datetime, timezone: str) -> object:
    """A plan's run-clock value at a run's start: its date in the plan's timezone, or a template written for it."""
    day = instant.astimezone(schedule.zone(timezone)).date()
    return day.isoformat() if source["format"] == "date" else render(source["parts"], day)


def admitted_clock(source: Mapping[str, object]) -> bool:
    """Whether a run-clock source is the run date alone or a text template with its parts."""
    if source["format"] == "date":
        return "parts" not in source
    return source["format"] == "text" and "parts" in source and admitted(source["parts"])


def admitted(parts: object) -> bool:
    """Whether a template is in its closed shape: text and date parts, at least one date, no two texts adjacent."""
    if not isinstance(parts, list) or not 0 < len(parts) <= MAX_PARTS:
        return False
    previous_text = False
    dated = False
    for part in parts:
        if isinstance(part, str):
            if not part or previous_text:
                return False
            previous_text = True
            continue
        if not _date_part(part):
            return False
        previous_text, dated = False, True
    return dated


def _date_part(part: object) -> bool:
    if not isinstance(part, dict) or set(part) != {"days", "format", "lang"}:
        return False
    days, form, language = part["days"], part["format"], part["lang"]
    if type(days) is not int or not -MAX_OFFSET_DAYS <= days <= MAX_OFFSET_DAYS:
        return False
    if not isinstance(form, str) or len(form) > MAX_FORMAT_CHARS or _FORMAT_RE.fullmatch(form) is None:
        return False
    tokens = set(_TOKEN_RE.findall(form))
    if not tokens:
        return False
    return language in LANGUAGES if tokens & _NAMED else language is None


# How a person reads each token: the date's own fields in capitals, a month name as the word "month" in its case.
_SHOWN_TOKENS = {
    "d": "D",
    "dd": "DD",
    "M": "M",
    "MM": "MM",
    "yyyy": "YYYY",
    "mmmm": "month",
    "Mmmm": "Month",
    "MMMM": "MONTH",
}


def shown(parts: Sequence[object]) -> str:
    """The template as a person reads it, the same whatever the date.

    Its text has every literal brace doubled, and each date is in braces as its format ("D month YYYY"), followed after
    ``|`` by its month-name language and its offset in days.
    """
    written = []
    for part in parts:
        if isinstance(part, str):
            written.append(part.replace("{", "{{").replace("}", "}}"))
            continue
        form = _TOKEN_RE.sub(lambda match: _SHOWN_TOKENS[match.group(1)], part["format"])
        notes = [part["lang"]] if part["lang"] is not None else []
        notes += [f"{part['days']:+d}"] if part["days"] else []
        written.append("{" + "|".join([form, *notes]) + "}")
    return "".join(written)


# --- Recognition ------------------------------------------------------------------------------------------------------


def template(
    text: str, recorded_on: datetime.date, locale: str | None, named: Callable[[datetime.date], bool]
) -> list[object] | None:
    """The template whose run on ``recorded_on`` writes ``text`` exactly, or None when the text holds no date to move.

    ``named`` says whether the person's own words named a date, which then stays as written. Any date form the table
    reads but cannot decide, or a bare day joined to a date by a range word, leaves the whole text a literal.
    """
    found = _found(text, recorded_on, locale)
    if found is None:
        return None
    parts: list[object] = []
    cursor = 0
    for start, end, part, day in found:
        if named(day):
            continue
        if start > cursor:
            parts.append(text[cursor:start])
        parts.append(part)
        cursor = end
    if cursor < len(text):
        parts.append(text[cursor:])
    if not any(isinstance(part, dict) for part in parts) or not admitted(parts):
        return None
    return parts if render(parts, recorded_on) == text else None


def dates(text: str, recorded_on: datetime.date) -> set[datetime.date]:
    """Every date a text may write in a form the table reads near ``recorded_on``, in any order or language.

    It reads the person's own words, so it keeps every reading, an undecided one or one beside a range included: a
    date it may name stays as written.
    """
    return {
        day
        for kind, pattern in _FORMS
        for match in pattern.finditer(text)
        for _part, day, _writer in _readings(kind, match, recorded_on)
    }


def _found(
    text: str, recorded_on: datetime.date, locale: str | None
) -> list[tuple[int, int, dict[str, object], datetime.date]] | None:
    """Each date the text writes, or None when one is undecidable.

    The longest written date wins wherever two would overlap, whatever its form, and the shorter one stays text.
    """
    matches = sorted(
        ((kind, match) for kind, pattern in _FORMS for match in pattern.finditer(text)),
        key=lambda item: (item[1].start() - item[1].end(), item[1].start()),
    )
    spans: list[tuple[int, int, list[tuple[dict[str, object], datetime.date]]]] = []
    taken: list[tuple[int, int]] = []
    for kind, match in matches:
        if any(match.start() < end and start < match.end() for start, end in taken):
            continue
        taken.append(match.span())
        readings = _decided(_readings(kind, match, recorded_on), locale)
        if readings is None:
            return None
        if readings:
            spans.append((match.start(), match.end(), readings))
    found = []
    ordered = sorted(spans, key=lambda item: item[0])
    for position, (start, end, readings) in enumerate(ordered):
        # Only the text between this date and its neighbours counts: a range of two whole dates is two dates.
        before = text[ordered[position - 1][1] if position else 0 : start]
        after = text[end : ordered[position + 1][0] if position + 1 < len(ordered) else len(text)]
        if _RANGE_BEFORE_RE.search(before) or _RANGE_AFTER_RE.search(after):
            return None
        ((part, day),) = readings
        found.append((start, end, part, day))
    return found


def _decided(
    readings: list[tuple[dict[str, object], datetime.date, str | None]], locale: str | None
) -> list[tuple[dict[str, object], datetime.date]] | None:
    """The one reading of a written date: alone, or the one the interface language writes; None when undecided."""
    distinct = {(part["format"], part["lang"]): (part, day, writer) for part, day, writer in readings}
    if len(distinct) > 1:
        distinct = {key: value for key, value in distinct.items() if _writes(value[2], locale)}
        if len(distinct) != 1:
            return None
    return [(part, day) for part, day, _writer in distinct.values()]


def _writes(writer: str | None, locale: str | None) -> bool:
    """Whether the interface language writes a reading: a month name in its own language, numbers in its order."""
    if writer == "dmy":
        return locale is not None and locale not in _MONTH_FIRST
    if writer == "mdy":
        return locale in _MONTH_FIRST
    return writer is None or writer == locale


def _readings(
    kind: str, match: re.Match[str], recorded_on: datetime.date
) -> list[tuple[dict[str, object], datetime.date, str | None]]:
    """Every (part, date, writing language) a match may be within reach of the recording's date."""
    groups = match.groups()
    found: list[tuple[str, int | None, int, int, str | None, str | None]] = []
    if kind == "iso":
        found.append(("{yyyy}-{MM}-{dd}", int(groups[0]), int(groups[1]), int(groups[2]), None, None))
    elif kind in ("numeric", "short"):
        first, second = groups[0], groups[2] if kind == "numeric" else groups[1]
        separator = groups[1] if kind == "numeric" else "/"
        year = int(groups[3]) if kind == "numeric" else None
        day_style, month_style = _padding(first, second)
        tail = f"{separator}{{yyyy}}" if year is not None else ""
        found.append((f"{{{day_style}}}{separator}{{{month_style}}}{tail}", year, int(second), int(first), None, "dmy"))
        found.append((f"{{{month_style}}}{separator}{{{day_style}}}{tail}", year, int(first), int(second), None, "mdy"))
    elif kind == "cjk":
        day_style, month_style = _padding(groups[2], groups[1])
        head = "{yyyy}年" if groups[0] else ""
        year = int(groups[0]) if groups[0] else None
        found.append((f"{head}{{{month_style}}}月{{{day_style}}}日", year, int(groups[1]), int(groups[2]), None, None))
    else:
        if kind == "day_name":
            day_text, joiner, name, year_joiner, year_text = groups
            shape = "{%s}" + joiner + "{%s}" + (year_joiner + "{yyyy}" if year_text else "")
            order = ("day", "name")
        else:
            name, joiner, day_text, year_joiner, year_text = groups
            shape = "{%s}" + joiner + "{%s}" + (year_joiner + "{yyyy}" if year_text else "")
            order = ("name", "day")
        case = next((token for token, cased in _CASES.items() if cased(name.lower()) == name), None)
        day_style = "dd" if day_text.startswith("0") else "d"
        tokens = {"day": day_style, "name": case}
        for language, names in _NAMES.items():
            if case is None or name.lower() not in names:
                continue
            form = shape % (tokens[order[0]], tokens[order[1]])
            month = names.index(name.lower()) + 1
            found.append((form, int(year_text) if year_text else None, month, int(day_text), language, language))
    readings = []
    for form, year, month, day, language, writer in found:
        written = _near(year, month, day, recorded_on)
        if written is not None:
            part = {"days": (written - recorded_on).days, "format": form, "lang": language}
            readings.append((part, written, writer))
    return readings


def _padding(day: str, month: str) -> tuple[str, str]:
    """Two-digit tokens unless either number is written with one digit."""
    return ("d", "M") if len(day) == 1 or len(month) == 1 else ("dd", "MM")


def _near(year: int | None, month: int, day: int, recorded_on: datetime.date) -> datetime.date | None:
    """The date within reach of the recording's date: the written year's, else the nearest year's; None when none is."""
    years = (year,) if year is not None else (recorded_on.year - 1, recorded_on.year, recorded_on.year + 1)
    for candidate in years:
        try:
            written = datetime.date(candidate, month, day)
        except ValueError:
            continue
        if abs((written - recorded_on).days) <= MAX_OFFSET_DAYS:
            return written
    return None
