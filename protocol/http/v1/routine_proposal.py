"""Canonical recorded-Routine card forms (ADR-0101): the card, Team's questions, refusals, and answers."""

from __future__ import annotations

import copy
import json

if __package__:
    from . import routine, routine_notice
else:  # The protocol verifier runs every module of this directory flat.
    import routine_notice

    import routine


# The confirmation card of a recorded Routine (ADR-0101, amended 2026-10-15): the recording agent's
# description, which states the schedule and the output in the person's own words, and every step with its effect and
# every literal complete and escaped, every source and selector described completely. It is the one thing a person
# confirms, so nothing in it is paged or cut; a recording whose card does not fit is refused.
MAX_PROPOSAL_BYTES = 160 * 1024
INPUT_ORIGINS = ("request", "assistant", "clock", "step", "selector")
_PROPOSAL_INPUT_FIELDS = frozenset({"member", "origin", "value", "step", "pointer", "where", "item"})


def _card_input(value: object, position: int) -> bool:
    """One input as the card shows it: a literal's complete JSON text, the run date or a dated text, or its source."""
    if not isinstance(value, dict) or set(value) != _PROPOSAL_INPUT_FIELDS or value["origin"] not in INPUT_ORIGINS:
        return False
    origin = value["origin"]
    if not routine.plain(value["member"], routine.MAX_MEMBER_CHARS):
        return False
    if origin in ("request", "assistant"):
        literal = isinstance(value["value"], str) and routine.PLAN_UNSAFE_RE.search(value["value"]) is None
        return literal and all(value[key] is None for key in ("step", "pointer", "where", "item"))
    if origin == "clock":
        # The run date alone, or a text template complete, each of its dates in braces.
        template = value["value"] is None or (
            isinstance(value["value"], str)
            and "{" in value["value"]
            and routine.PLAN_UNSAFE_RE.search(value["value"]) is None
        )
        return template and all(value[key] is None for key in ("step", "pointer", "where", "item"))
    return (
        value["value"] is None
        and (value["where"] is not None) == (origin == "selector")
        and routine.reference(value, position)
    )


def _card_step(value: object, position: int) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"position", "assistant", "action", "read_only", "inputs"}
        and value["position"] == position
        and type(value["position"]) is int
        and routine.valid_assistant(value["assistant"])
        and routine.valid_action(value["action"])
        and type(value["read_only"]) is bool
        and routine.members(value["inputs"], lambda item: _card_input(item, position))
    )


def canonical_proposal(value: object) -> dict[str, object] | None:
    """One recorded Routine's confirmation card, within its byte bound."""
    fields = {"proposal_id", "expires_at", "replaces", "name", "description", "steps"}
    if not isinstance(value, dict) or set(value) != fields:
        return None
    steps = value["steps"]
    valid = (
        routine.matches(value["proposal_id"], routine.ROUTINE_ID_RE)
        and routine_notice.instant(value["expires_at"])
        and routine_notice.optional(value["replaces"], routine.ROUTINE_ID_RE)
        and routine.exact_name(value["name"])
        and routine.exact_description(value["description"])
        and isinstance(steps, list)
        and 0 < len(steps) <= routine.MAX_ROUTINE_STEPS
        and all(_card_step(item, index) for index, item in enumerate(steps, start=1))
        and routine.encoded_bytes(value) <= MAX_PROPOSAL_BYTES
    )
    return copy.deepcopy(value) if valid else None


# What Team asks the person, through the chat, before a recording can become a card (ADR-0101): the recording span
# is kept and the person's answer is an ordinary send in it. Only an ambiguous binding offers targets to choose from;
# an interval over the Team's budget carries the shortest interval that fits, in seconds.
QUESTION_CODES = (
    "routine-schedule-unstated",
    "routine-output-unstated",
    "routine-interval-over-budget",
    "routine-binding-ambiguous",
    "routine-work-rerun",
)
MAX_QUESTION_OPTIONS = 8
MAX_QUESTION_OPTION_CHARS = 120


# A target's exact JSON text: a string within MAX_QUESTION_OPTION_CHARS escapes at most its quotes and backslashes.
MAX_QUESTION_VALUE_CHARS = 2 * MAX_QUESTION_OPTION_CHARS + 2


def _target(text: object) -> bool:
    """A target's exact JSON text: one string or integer, as compact JSON would write it, so no client rounds it."""
    if not isinstance(text, str) or not 0 < len(text) <= MAX_QUESTION_VALUE_CHARS:
        return False
    try:
        decoded = json.loads(text)
    except ValueError:
        return False
    scalar = (
        routine.plain(decoded, MAX_QUESTION_OPTION_CHARS)
        if isinstance(decoded, str)
        else type(decoded) is int and len(text) <= MAX_QUESTION_OPTION_CHARS
    )
    return scalar and json.dumps(decoded, ensure_ascii=False) == text


def _question_option(value: object) -> bool:
    """One target a person may choose: the JSON text of the value its input would take, and its name, if any."""
    if not isinstance(value, dict) or set(value) != {"value", "label"}:
        return False
    label = value["label"]
    return _target(value["value"]) and (label is None or routine.plain(label, MAX_QUESTION_OPTION_CHARS))


# What each run does with its result, as the person chooses it (ADR-0101): show it every run, show it only when it
# changes, or show nothing. Team reads the choice from the person's own words; asked, Admin offers these labels in the
# interface language, and the label the person picks, as Admin composes it, states that choice.
OUTPUT_CHOICES = {
    "ar": {
        "show": "اعرض في كل تشغيل",
        "changes": "اعرض فقط عند التغيير",
        "none": "لا تعرض",
    },
    "de": {
        "show": "Bei jedem Lauf anzeigen",
        "changes": "Nur bei Änderung anzeigen",
        "none": "Nicht anzeigen",
    },
    "en": {
        "show": "Show every run",
        "changes": "Show only when it changes",
        "none": "Don't show",
    },
    "es": {
        "show": "Mostrar en cada ejecución",
        "changes": "Mostrar solo cuando cambie",
        "none": "No mostrar",
    },
    "fr": {
        "show": "Afficher à chaque exécution",
        "changes": "Afficher seulement en cas de changement",
        "none": "Ne pas afficher",
    },
    "ja": {
        "show": "毎回表示する",
        "changes": "変更時のみ表示する",
        "none": "表示しない",
    },
    "pt": {
        "show": "Mostrar em todas as execuções",
        "changes": "Mostrar somente quando mudar",
        "none": "Não mostrar",
    },
    "zh": {"show": "每次运行都显示", "changes": "仅在变化时显示", "none": "不显示"},
}


# The schedules Admin offers, in the interface language, when Team asks how often a Routine runs, and the interval it
# offers when the stated one outgrows the Team's budget, its seconds being the question's value. Each states exactly
# one schedule as Team reads it, and a send that is exactly one of them answering that question is a settings answer:
# the Brain then records again and runs nothing (ADR-0101).
SCHEDULE_CHOICES = {
    "ar": ("كل 30 ثانية", "كل ساعة", "كل يوم الساعة 09:00"),
    "de": ("Alle 30 Sekunden", "Jede Stunde", "Jeden Tag um 09:00"),
    "en": ("Every 30 seconds", "Every hour", "Every day at 09:00"),
    "es": ("Cada 30 segundos", "Cada hora", "Todos los días a las 09:00"),
    "fr": ("Toutes les 30 secondes", "Toutes les heures", "Tous les jours à 09:00"),
    "ja": ("30 秒ごと", "1 時間ごと", "毎日 09:00"),
    "pt": ("A cada 30 segundos", "A cada hora", "Todo dia às 09:00"),
    "zh": ("每 30 秒", "每小时", "每天 09:00"),
}
OVER_BUDGET_ANSWERS = {
    "ar": "كل {seconds} ثانية",
    "de": "Alle {seconds} Sekunden",
    "en": "Every {seconds} seconds",
    "es": "Cada {seconds} segundos",
    "fr": "Toutes les {seconds} secondes",
    "ja": "{seconds} 秒ごと",
    "pt": "A cada {seconds} segundos",
    "zh": "每 {seconds} 秒",
}
# The questions about how often the Routine runs and what each run does with its result, never about its work, each
# with every interface language's fixed answers Admin offers to it; the over-budget answer's "{seconds}" is the
# question's value. An answer to one runs no work unless it changes the work, and then the whole resulting work once.
SETTINGS_ANSWERS = {
    "routine-schedule-unstated": frozenset(label for labels in SCHEDULE_CHOICES.values() for label in labels),
    "routine-interval-over-budget": frozenset(OVER_BUDGET_ANSWERS.values()),
    "routine-output-unstated": frozenset(label for labels in OUTPUT_CHOICES.values() for label in labels.values()),
}
SETTINGS_QUESTION_CODES = frozenset(SETTINGS_ANSWERS)


def canonical_question(value: object) -> dict[str, object] | None:
    """One question Team asks before a card: its code, its targets for an ambiguous binding, and a fitting interval."""
    if not isinstance(value, dict) or set(value) != {"code", "options", "value"} or value["code"] not in QUESTION_CODES:
        return None
    code, options, interval = value["code"], value["options"], value["value"]
    valid = (
        isinstance(options, list)
        and (code == "routine-binding-ambiguous" or not options)
        and len(options) <= MAX_QUESTION_OPTIONS
        and all(_question_option(item) for item in options)
        and len({item["value"] for item in options}) == len(options)
        and (
            routine.whole(interval, routine.MIN_CONTINUOUS_GAP_SECONDS, routine.MAX_CONTINUOUS_GAP_SECONDS)
            if code == "routine-interval-over-budget"
            else interval is None
        )
    )
    return copy.deepcopy(value) if valid else None


def canonical_refusal(value: object) -> dict[str, str] | None:
    """Why a recording made no card: one code, which Admin words in the interface language; nothing was created."""
    if (
        not isinstance(value, dict)
        or set(value) != {"code"}
        or not routine.matches(value["code"], routine.ERROR_CODE_RE)
    ):
        return None
    return {"code": value["code"]}


# A person's answer to a card: confirming it creates or changes the Routine; revoking it drops the card.
PROPOSAL_STATUSES = ("created", "changed", "revoked")


def canonical_proposal_answer(value: object) -> dict[str, object] | None:
    """What an answer did: the Routine it created or changed, or the card it revoked."""
    if not isinstance(value, dict) or set(value) != {"team_id", "proposal_id", "routine_id", "status"}:
        return None
    status = value["status"]
    valid = (
        routine.valid_team(value["team_id"])
        and routine.matches(value["proposal_id"], routine.ROUTINE_ID_RE)
        and status in PROPOSAL_STATUSES
        and (
            value["routine_id"] is None
            if status == "revoked"
            else routine.matches(value["routine_id"], routine.ROUTINE_ID_RE)
        )
    )
    return copy.deepcopy(value) if valid else None
