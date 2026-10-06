"""The schedule, output, and timezone a person states in their own words, read with no model (ADR-0101).

This is the one rule source: Team records with it and the Brain mirrors it, so both read the same words alike.

A Routine runs exactly when the person said, so Team reads it from the person's own text, never from the model. The
table is bounded and closed: in each interface language, an interval of seconds, minutes, or hours ("a cada 30
segundos", "every hour"), or a day, weekday, or day of the month with a time ("todo dia às 9h", "every Monday at 9am",
"毎月5日9時"). A sentence that asks something (it ends with a question mark) states nothing, and a negation in any
language rejects every reading it reaches, whichever language reads the same words. One text may state several
schedules, which its reader then asks the person to choose between; a phrase this table does not read states nothing,
so the person is asked again rather than guessed for.

An output choice is read the same way, from a bounded phrase table in the same languages that holds every localized
choice label. A timezone is stated only as an exact, loadable IANA area zone such as "Europe/Lisbon", or "UTC".
"""

from __future__ import annotations

import re
import zoneinfo
from collections.abc import Iterator

if __package__:
    from . import routine as http_routine
else:  # The protocol verifier runs every module of this directory flat.
    import routine as http_routine

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹０１２３４５６７８９：", "01234567890123456789" + "0123456789:")
# A sentence ends at its mark, kept with it; a period after a digit is an ordinal ("am 4."), never an end.
_SENTENCE_END_RE = re.compile(r"(?<=[!?;\n。！？؟])|(?<=(?<!\d)\.)")
_ASKS = ("?", "？", "؟")

# Each language's phrases; a phrase counts only in a sentence where that language says no negation.
_NEGATIONS = {
    "pt": r"\b(?:não|nunca|nem)\b",
    "en": r"\b(?:not|don't|dont|never|no)\b",
    "es": r"\b(?:no|nunca)\b",
    "fr": r"\b(?:ne|pas|jamais)\b|\bn'",
    "de": r"\b(?:nicht|kein|keine|nie)\b",
    "ja": r"ない|ません",
    "zh": r"不|别",
    "ar": r"(?:^|\s)(?:لا|ليس|لن|لم)(?:\s|$)",
}
_UNIT = {
    "segundo": "second",
    "minuto": "minute",
    "hora": "hour",
    "second": "second",
    "sec": "second",
    "minute": "minute",
    "min": "minute",
    "hour": "hour",
    "hr": "hour",
    "seconde": "second",
    "heure": "hour",
    "sekunde": "second",
    "stunde": "hour",
    "秒": "second",
    "分": "minute",
    "時間": "hour",
    "秒钟": "second",
    "分钟": "minute",
    "小时": "hour",
    "个小时": "hour",
    "ثانية": "second",
    "ثوان": "second",
    "ثواني": "second",
    "دقيقة": "minute",
    "دقائق": "minute",
    "ساعة": "hour",
    "ساعات": "hour",
}
# An interval: (language, pattern with an optional count group and a unit group, or a fixed unit).
_INTERVALS = (
    ("pt", r"\b(?:a\s+)?cada\s+(?:(\d+)\s+)?(segundo|minuto|hora)s?\b"),
    ("pt", r"\bde\s+(\d+)\s+em\s+\d+\s+(segundo|minuto|hora)s?\b"),
    ("pt", r"\bde\s+()(minuto|hora)\s+em\s+(?:minuto|hora)\b"),
    ("pt", r"\btod[ao]\s+()(minuto|hora)\b"),
    ("en", r"\bevery\s+(?:(\d+)\s+)?(second|sec|minute|min|hour|hr)s?\b"),
    ("en", r"\beach\s+()(second|minute|hour)\b"),
    ("en", r"\b()(hour)ly\b"),
    ("es", r"\bcada\s+(?:(\d+)\s+)?(segundo|minuto|hora)s?\b"),
    ("fr", r"\btoutes\s+les\s+(?:(\d+)\s+)?(seconde|minute|heure)s?\b"),
    ("fr", r"\bchaque\s+()(seconde|minute|heure)\b"),
    ("de", r"\balle\s+(\d+)\s+(sekunde|minute|stunde)n?\b"),
    ("de", r"\bjede[nrs]?\s+()(sekunde|minute|stunde)\b"),
    ("de", r"\b()(stünd)lich\b"),
    ("ja", r"(\d+)\s*(秒|分|時間)(?:ごと|おき|毎)"),
    ("ja", r"毎()(時|分)(?!間)"),
    ("zh", r"每(?:隔)?\s*(\d*)\s*(秒钟|秒|分钟|个小时|小时)"),
    ("ar", r"كل\s+(\d*)\s*(ثانية|ثواني|ثوان|دقيقة|دقائق|ساعة|ساعات)"),
)
_FIXED_UNITS = {"stünd": "hour", "時": "hour"}
_DAILY = (
    ("pt", r"\b(?:todo\s+dia|todos\s+os\s+dias|diariamente|cada\s+dia)\b"),
    ("en", r"\b(?:every\s+day|daily|each\s+day)\b"),
    ("es", r"\b(?:todos\s+los\s+d[ií]as|cada\s+d[ií]a|diariamente)\b"),
    ("fr", r"\b(?:tous\s+les\s+jours|chaque\s+jour|quotidiennement)\b"),
    ("de", r"\b(?:jeden\s+tag|täglich)\b"),
    ("ja", r"毎日"),
    ("zh", r"每天|每日"),
    ("ar", r"كل\s+يوم(?!\s+(?:ال)?(?:اثنين|ثلاثاء|أربعاء|خميس|جمعة|سبت|أحد))|يومياً?"),
)
_WEEKDAYS = {
    "segunda": 0,
    "terça": 1,
    "terca": 1,
    "quarta": 2,
    "quinta": 3,
    "sexta": 4,
    "sábado": 5,
    "sabado": 5,
    "domingo": 6,
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    "lunes": 0,
    "martes": 1,
    "miércoles": 2,
    "miercoles": 2,
    "jueves": 3,
    "viernes": 4,
    "lundi": 0,
    "mardi": 1,
    "mercredi": 2,
    "jeudi": 3,
    "vendredi": 4,
    "samedi": 5,
    "dimanche": 6,
    "montag": 0,
    "dienstag": 1,
    "mittwoch": 2,
    "donnerstag": 3,
    "freitag": 4,
    "samstag": 5,
    "sonntag": 6,
    "月": 0,
    "火": 1,
    "水": 2,
    "木": 3,
    "金": 4,
    "土": 5,
    "日": 6,
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "天": 6,
    "اثنين": 0,
    "ثلاثاء": 1,
    "أربعاء": 2,
    "خميس": 3,
    "جمعة": 4,
    "سبت": 5,
    "أحد": 6,
}
_WEEKLY = (
    (
        "pt",
        r"\b(?:tod[ao]s?\s+(?:[ao]s\s+)?|cada\s+)(segunda|terça|terca|quarta|quinta|sexta|sábado|sabado|domingo)s?\b",
    ),
    ("en", r"\b(?:every|each)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"),
    ("es", r"\b(?:todos\s+los|cada)\s+(lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo)s?\b"),
    ("fr", r"\b(?:chaque|tous\s+les)\s+(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)s?\b"),
    ("de", r"\bjede[nr]?\s+(montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)\b"),
    ("ja", r"毎週([月火水木金土日])曜"),
    ("zh", r"每(?:周|星期|礼拜)([一二三四五六日天])"),
    ("ar", r"كل\s+(?:يوم\s+)?(?:ال)?(اثنين|ثلاثاء|أربعاء|خميس|جمعة|سبت|أحد)"),
)
_MONTHLY = (
    ("pt", r"\b(?:todo\s+m[eê]s|cada\s+m[eê]s|mensalmente)\b.*?\bdia\s+(\d{1,2})\b"),
    ("en", r"\b(?:every\s+month|each\s+month|monthly)\b.*?\b(?:on\s+the\s+)?(\d{1,2})(?:st|nd|rd|th)\b"),
    ("es", r"\b(?:cada\s+mes|todos\s+los\s+meses|mensualmente)\b.*?\bd[ií]a\s+(\d{1,2})\b"),
    ("fr", r"\b(?:chaque\s+mois|tous\s+les\s+mois|mensuellement)\b.*?\ble\s+(\d{1,2})\b"),
    ("de", r"\b(?:jeden\s+monat|monatlich)\b.*?\bam\s+(\d{1,2})\b"),
    ("ja", r"毎月(\d{1,2})日"),
    ("zh", r"每月(\d{1,2})[日号號]"),
    ("ar", r"كل\s+شهر.*?(?:يوم|اليوم)\s+(\d{1,2})"),
)
# Times of day, most specific first: hour, optional minute, and an optional half of the day.
_TIMES = (
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)",
    r"(\d{1,2})\s*[:h]\s*(\d{2})()",
    r"(\d{1,2})()\s*(?:h|horas?|hrs?)\b()",
    r"(\d{1,2})(?::(\d{2}))?\s*uhr()",
    r"(\d{1,2})時(?:(\d{1,2})分)?()",
    r"(\d{1,2})[点點](?:(\d{1,2})分)?()",
    r"(?:\bàs|\bas|\bat|\ba\s+las|\ba\s+la|\bà|\bum|الساعة)\s+(\d{1,2})(?::(\d{2}))?()(?!\s*\d)",
)
# What each run does with its result, by language: show it every run, only when it changes, nothing, or use it in
# other Actions. Each interface language's choice labels (``routine.OUTPUT_CHOICES``) are among these phrases. A plain
# "show the result" reads as showing it every run, unless its own clause says only when it changes.
_PT_SHOW = r"\b(?:mostrar|mostre|exibir|exiba)\s+"
_ES_SHOW = r"\b(?:mostrar|muestra|muéstrame)\s+"
_FR_SHOW = r"\b(?:afficher|affiche|montre(?:-moi)?)\s+"
_OUTPUTS = (
    ("show", "pt", _PT_SHOW + r"(?:sempre|em\s+todas\s+as\s+execuções)\b"),
    ("result", "pt", _PT_SHOW + r"os?\s+resultados?\b"),
    ("changes", "pt", r"\b(?:só|somente|apenas)\s+(?:quando|se)\s+(?:mudar|alterar|houver\s+mudança)"),
    ("none", "pt", r"\b(?:não|sem)\s+(?:precisa\s+)?(?:mostrar|exibir)\b"),
    ("chain", "pt", r"\b(?:usar|use)\s+(?:o\s+resultado\s+)?(?:em|para)\s+(?:executar\s+)?outras?\s+ações\b"),
    ("show", "en", r"\b(?:show|display)\s+(?:me\s+)?(?:it\s+)?(?:every\s+(?:run|time)|always)\b"),
    ("result", "en", r"\b(?:show|display)\s+(?:me\s+)?the\s+results?\b"),
    ("changes", "en", r"\bonly\s+(?:(?:when|if)\s+(?:it\s+)?changes|on\s+changes?)\b"),
    ("none", "en", r"\b(?:don't|do\s+not|no\s+need\s+to)\s+show\b|\bshow\s+nothing\b"),
    ("chain", "en", r"\buse\s+(?:it|the\s+result)\s+(?:in|to\s+run)\s+other\s+actions\b"),
    ("show", "es", _ES_SHOW + r"(?:siempre|en\s+cada\s+ejecución)"),
    ("result", "es", _ES_SHOW + r"(?:el\s+resultado|los\s+resultados)"),
    ("changes", "es", r"\bsolo\s+(?:cuando\s+cambie|si\s+cambia)\b"),
    ("none", "es", r"\b(?:no|sin)\s+mostrar\b"),
    ("chain", "es", r"\busar(?:lo)?\s+en\s+otras\s+acciones\b"),
    ("show", "fr", _FR_SHOW + r"(?:toujours|à\s+chaque\s+exécution)"),
    ("result", "fr", _FR_SHOW + r"les?\s+résultats?"),
    ("changes", "fr", r"\b(?:seulement|uniquement)\s+(?:en\s+cas\s+de\s+changement|quand\s+(?:ça|il|elle)\s+change)"),
    ("none", "fr", r"\bne\s+(?:pas|rien)\s+afficher\b|\bsans\s+afficher\b"),
    ("chain", "fr", r"\butiliser\s+dans\s+d'autres\s+actions\b"),
    ("show", "de", r"\b(?:immer|bei\s+jedem\s+lauf)\s+anzeigen\b"),
    ("result", "de", r"\bzeig\s+mir\s+das\s+ergebnis\b"),
    ("changes", "de", r"\bnur\s+(?:bei\s+änderung|wenn\s+(?:es\s+)?sich\s+(?:etwas\s+)?ändert)"),
    ("none", "de", r"\b(?:nicht|nichts)\s+anzeigen\b"),
    ("chain", "de", r"\bin\s+anderen\s+aktionen\s+verwenden\b"),
    ("show", "ja", r"毎回表示"),
    ("result", "ja", r"結果を表示"),
    ("changes", "ja", r"(?:変更|変化)(?:時|したとき)のみ|変わったときだけ"),
    ("none", "ja", r"表示しない|表示不要"),
    ("chain", "ja", r"他のアクションで使"),
    ("show", "zh", r"每次(?:运行都)?显示"),
    ("result", "zh", r"显示结果"),
    ("changes", "zh", r"(?:仅|只)在变化时"),
    ("none", "zh", r"不(?:用)?显示"),
    ("chain", "zh", r"用于其他操作"),
    ("show", "ar", r"اعرض\s+(?:في\s+كل\s+تشغيل|دائم)"),
    ("result", "ar", r"اعرض\s+النتيجة"),
    ("changes", "ar", r"فقط\s+عند\s+التغيير"),
    ("none", "ar", r"لا\s+تعرض|بدون\s+عرض"),
    ("chain", "ar", r"استخدمه\s+في\s+إجراءات\s+أخرى"),
)
# A whole zone token: never part of a longer word, path, or offset ("UTC+3" names no zone), though it may close a
# sentence or sit inside brackets or quotes.
_ZONE_RE = re.compile(
    r"(?<![\w/.:+-])([A-Za-z][A-Za-z_+-]*(?:/[A-Za-z][A-Za-z0-9_+-]*){1,2}|UTC)"
    r"(?=$|[\s,;!?)\]}\"'»”，。！？；、]|[.:](?:\s|$))"
)


def _sentences(text: str) -> Iterator[str]:
    """Each sentence a person affirmed: never one that asks."""
    for sentence in _SENTENCE_END_RE.split(text.translate(_DIGITS).casefold()):
        if sentence.strip() and not sentence.rstrip().endswith(_ASKS):
            yield sentence


# Languages whose negation follows what it negates, anywhere in the sentence; elsewhere it comes before.
_TRAILING_NEGATION = frozenset({"ja"})


def _negated(language: str, sentence: str, match: re.Match[str]) -> bool:
    """Whether the sentence negates a reading of this language: by a negation before it, or anywhere when it trails."""
    return any(
        language in _TRAILING_NEGATION or negation.start() < match.start()
        for negation in re.finditer(_NEGATIONS[language], sentence)
    )


def _readings(sentence: str) -> dict[str, list[re.Match[str]]]:
    """Each kind's readings of one sentence that no negation reaches, in any language.

    A reading a language negates also rejects every overlapping reading another language would take of the same words,
    so "não quero a cada 30 segundos" never becomes Spanish "cada 30 segundos".
    """
    tables = {"interval": _INTERVALS, "monthly": _MONTHLY, "weekly": _WEEKLY, "daily": _DAILY}
    found = [
        (kind, language, match)
        for kind, table in tables.items()
        for language, pattern in table
        for match in re.finditer(pattern, sentence)
    ]
    negated = [match.span() for _kind, language, match in found if _negated(language, sentence, match)]
    admitted: dict[str, list[re.Match[str]]] = {kind: [] for kind in tables}
    for kind, _language, match in found:
        if not any(match.start() < end and start < match.end() for start, end in negated):
            admitted[kind].append(match)
    return admitted


# The longest count any schedule can hold: a day in seconds has five digits; a longer one states nothing.
_MAX_COUNT_DIGITS = len(str(http_routine.MAX_CONTINUOUS_GAP_SECONDS))


def _interval(count: str, unit: str) -> dict[str, object] | None:
    if count and len(count) > _MAX_COUNT_DIGITS:
        return None
    every = int(count) if count else 1
    kind = _FIXED_UNITS.get(unit) or _UNIT[unit]
    if kind == "hour" and 1 <= every <= 24:
        return {"kind": "hourly", "every": every}
    gap = every * (60 if kind == "minute" else 3600 if kind == "hour" else 1)
    if not http_routine.MIN_CONTINUOUS_GAP_SECONDS <= gap <= http_routine.MAX_CONTINUOUS_GAP_SECONDS:
        return None
    return {"kind": "continuous", "gap": gap, "cap": http_routine.continuous_cap(gap)}


def _time(hour: str, minute: str, half: str) -> str | None:
    value, minutes = int(hour), int(minute or 0)
    if half.startswith("p") and value < 12:
        value += 12
    elif half.startswith("a") and value == 12:
        value = 0
    if value > 23 or minutes > 59 or (half and not 1 <= int(hour) <= 12):
        return None
    return f"{value:02d}:{minutes:02d}"


def _times(sentence: str) -> set[str]:
    """Each time of day a sentence names; a more specific form read first hides the shorter forms inside it."""
    found = set()
    for pattern in _TIMES:
        for match in re.finditer(pattern, sentence):
            time = _time(*match.groups())
            if time is not None:
                found.add(time)
            sentence = sentence[: match.start()] + " " * len(match.group()) + sentence[match.end() :]
    return found


def _calendar(sentence: str, readings: dict[str, list[re.Match[str]]]) -> list[dict[str, object]]:
    """The day, weekday, or monthly schedules one sentence states, one for each time it names."""
    times = _times(sentence)
    days = [
        {"kind": "monthly", "day": int(match.group(1))}
        for match in readings["monthly"]
        if 1 <= int(match.group(1)) <= 28
    ]
    days += [{"kind": "weekly", "weekday": _WEEKDAYS[match.group(1)]} for match in readings["weekly"]]
    if not days and readings["daily"]:
        days = [{"kind": "daily"}]
    return [{**day, "time": time} for day in days for time in sorted(times)]


def stated(text: str) -> tuple[dict[str, object], ...]:
    """Every distinct canonical schedule one person-authored text states affirmatively, in the order first found."""
    found: list[dict[str, object]] = []
    for sentence in _sentences(text):
        readings = _readings(sentence)
        schedules = [_interval(*match.groups()) for match in readings["interval"]]
        schedules += _calendar(sentence, readings)
        for value in schedules:
            if value is not None and http_routine.canonical_schedule(value) == value and value not in found:
                found.append(value)
    return tuple(found)


# What separates two clauses offered as alternatives: a comma or semicolon in any script, or "or" in any language.
_ALTERNATIVE_RE = re.compile(r"[,;，、،；]|\b(?:ou|or|o|oder)\b|または|或|还是|أو")


def _one_clause(sentence: str, first: tuple[int, int], second: tuple[int, int]) -> bool:
    """Whether two readings sit in one clause: nothing between them separates alternatives."""
    between = sentence[min(first[1], second[1]) : max(first[0], second[0])]
    return _ALTERNATIVE_RE.search(between) is None


def outputs(text: str) -> tuple[str, ...]:
    """Every distinct output choice one person-authored text states affirmatively, in the order first found.

    A "nothing" reading carries its own negation and stands; any other reading a negation reaches, in any language,
    or that overlaps a "nothing" reading, states nothing. "Show the result" qualified by "only when it changes" in its
    own clause is that one choice, while "show the result, or only when it changes" states both.
    """
    found: list[str] = []
    for sentence in _sentences(text):
        readings = [
            (kind, language, match) for kind, language, pattern in _OUTPUTS for match in re.finditer(pattern, sentence)
        ]
        blocked = [match.span() for kind, _language, match in readings if kind == "none"]
        blocked += [
            match.span() for kind, language, match in readings if kind != "none" and _negated(language, sentence, match)
        ]
        admitted = [
            (kind, match.span())
            for kind, _language, match in readings
            if kind == "none" or not any(match.start() < end and start < match.end() for start, end in blocked)
        ]
        changes = [span for kind, span in admitted if kind == "changes"]
        for kind, span in admitted:
            if kind == "result" and any(_one_clause(sentence, span, other) for other in changes):
                continue
            chosen = "show" if kind == "result" else kind
            if chosen not in found:
                found.append(chosen)
    return tuple(found)


# How a question asks what Team reads from the person's own words: when or how often a Routine runs, how many times
# a day, or what it does with its result. Matched on the casefolded text, by language.
_TEAM_QUESTIONS = (
    ("pt", r"com que frequência|(?:a|de) cada quanto tempo|de quanto em quanto tempo|quantas vezes por"),
    ("pt", r"(?:que|qual(?: o)?) horário|que horas|limite de execuções"),
    ("pt", r"(?:quer|prefere|deseja) receber (?:o|os) resultados?|o que (?:\w+ ){0,4}com (?:o|os) resultados?"),
    ("en", r"how often|how frequently|what time|which schedule|how many times (?:a|per)\b|run limit"),
    ("en", r"(?:like|want) to (?:receive|get|see) the results?|(?:do|happen) (?:with|to) the results?"),
    ("es", r"con qué frecuencia|cada cuánto|a qué hora|cuántas veces al día"),
    ("es", r"(?:quieres|prefieres|desea) recibir (?:el|los) resultados?|qué \w+ con (?:el|los) resultados?"),
    ("fr", r"à quelle fréquence|tous les combien|à quelle heure|combien de fois par jour"),
    ("fr", r"recevoir (?:le|les) résultats?|que faire (?:du|des) résultats?"),
    ("de", r"wie oft|um wie viel uhr|wann soll|wie viele male pro tag"),
    ("de", r"wie möchte\w* \w+ das ergebnis|was soll mit dem ergebnis"),
    ("ja", r"どのくらいの頻度|何時に|頻度|1日に何回|結果をどのように|結果をどう"),
    ("zh", r"多久一次|多长时间一次|频率|几点|每天几次|一天几次|如何接收结果|结果(?:怎么|如何)处理|如何处理结果"),
    ("ar", r"كم مرة|كل كم|في أي وقت|أي ساعة|كيف تريد استلام النتيجة|ماذا (?:أفعل|نفعل) بالنتيجة"),
)
_QUESTION_MARKS = str.maketrans(dict.fromkeys(_ASKS, "."))


def team_asks(text: str) -> bool:
    """Whether a text asks or states what Team reads from the person's words, so the Brain never asks it.

    That is a schedule, an interval, an output choice, or how many times a Routine may run. The Brain's guard calls it
    on one clarification question or option label. A stated reading counts even inside a question; a work or item
    question ("qual zona?") does not.
    """
    plain = text.translate(_QUESTION_MARKS)
    if stated(plain) or outputs(plain):
        return True
    folded = text.translate(_DIGITS).casefold()
    return any(re.search(pattern, folded) for _language, pattern in _TEAM_QUESTIONS)


def _loads(name: str) -> bool:
    """Whether a canonical IANA name loads as a zone, never a path or the host's local zone."""
    if http_routine.canonical_timezone(name) is None:
        return False
    try:
        zoneinfo.ZoneInfo(name)
    except zoneinfo.ZoneInfoNotFoundError, ValueError:
        return False
    return True


def zones(text: str) -> tuple[str, ...]:
    """Every distinct IANA area zone (or UTC) a person wrote exactly, that loads, in the order written."""
    found: list[str] = []
    for match in _ZONE_RE.finditer(text):
        name = match.group(1)
        if not _loads(name):
            continue
        if name not in found:
            found.append(name)
    return tuple(found)
