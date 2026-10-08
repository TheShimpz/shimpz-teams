"""Canonical recorded-Routine card forms (ADR-0101): the card, Team's questions, refusals, and answers."""

from __future__ import annotations

import copy
import json

if __package__:
    from . import routine, routine_notice
else:  # The protocol verifier runs every module of this directory flat.
    import routine_notice

    import routine


# The confirmation card of a recorded Routine (ADR-0101 section 5.2): every literal complete and escaped, every source
# and selector described completely, the schedule, the output, and every permitted Action. It is the one thing a
# person confirms, so nothing in it is paged or cut; a recording whose card does not fit is refused.
MAX_PROPOSAL_BYTES = 160 * 1024
INPUT_ORIGINS = ("request", "assistant", "clock", "step", "selector")
MAX_NEXT_RUNS = 3
_PROPOSAL_INPUT_FIELDS = frozenset({"member", "origin", "value", "step", "pointer", "where", "item"})


def _card_input(value: object, position: int) -> bool:
    """One input as the card shows it: a literal's complete JSON text, the run date, or its source step and path."""
    if not isinstance(value, dict) or set(value) != _PROPOSAL_INPUT_FIELDS or value["origin"] not in INPUT_ORIGINS:
        return False
    origin = value["origin"]
    if not routine._plain(value["member"], routine.MAX_MEMBER_CHARS):
        return False
    if origin in ("request", "assistant"):
        literal = isinstance(value["value"], str) and routine._PLAN_UNSAFE_RE.search(value["value"]) is None
        return literal and all(value[key] is None for key in ("step", "pointer", "where", "item"))
    if origin == "clock":
        return all(value[key] is None for key in ("value", "step", "pointer", "where", "item"))
    return (
        value["value"] is None
        and (value["where"] is not None) == (origin == "selector")
        and routine._reference(value, position)
    )


def _card_step(value: object, position: int) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"position", "assistant", "action", "read_only", "inputs"}
        and value["position"] == position
        and type(value["position"]) is int
        and routine._assistant(value["assistant"])
        and routine._action(value["action"])
        and type(value["read_only"]) is bool
        and routine._members(value["inputs"], lambda item: _card_input(item, position))
    )


def _card_permitted(value: object) -> bool:
    """Every permitted Action, each once, in identity order, with whether its reviewed effect is read-only."""
    if not isinstance(value, list) or len(value) > routine.MAX_PERMITTED:
        return False
    if not all(
        isinstance(item, dict)
        and set(item) == {"assistant", "action", "read_only"}
        and routine._assistant(item["assistant"])
        and routine._action(item["action"])
        and type(item["read_only"]) is bool
        for item in value
    ):
        return False
    identities = [(item["assistant"], item["action"]) for item in value]
    return identities == sorted(set(identities))


def _permits_exactly(permitted: list[dict[str, object]], steps: list[dict[str, object]]) -> bool:
    """The permitted Actions are exactly the steps' Actions, each with the same reviewed effect as its steps."""
    effects = {(item["assistant"], item["action"]): item["read_only"] for item in permitted}
    return set(effects) == {(step["assistant"], step["action"]) for step in steps} and all(
        effects[(step["assistant"], step["action"])] == step["read_only"] for step in steps
    )


def _card_output(value: object, total: int) -> bool:
    """The card's output: its mode; a shown mode shows the last step."""
    if not isinstance(value, dict) or set(value) != {"mode"}:
        return False
    shown = total if value["mode"] in routine.SHOWN_MODES else None
    return routine.canonical_disposition({**value, "step": shown}, total) is not None


def canonical_proposal(value: object) -> dict[str, object] | None:
    """One recorded Routine's confirmation card, within its byte bound."""
    fields = {"proposal_id", "expires_at", "replaces", "name", "schedule", "timezone", "timezone_source", "next_runs"}
    rest = {"daily_cap", "output", "steps", "permitted"}
    if not isinstance(value, dict) or set(value) != fields | rest:
        return None
    steps, runs = value["steps"], value["next_runs"]
    if (
        not isinstance(steps, list)
        or len(steps) > routine.MAX_ROUTINE_STEPS
        or not _card_output(value["output"], len(steps))
    ):
        return None
    valid = (
        routine._identity(value["proposal_id"], routine.ROUTINE_ID_RE)
        and routine_notice._instant(value["expires_at"])
        and routine_notice._optional(value["replaces"], routine.ROUTINE_ID_RE)
        and routine.canonical_name(value["name"]) == value["name"]
        and routine.canonical_schedule(value["schedule"]) == value["schedule"]
        and routine.zoned(value["timezone"], value["timezone_source"])
        and isinstance(runs, list)
        and 1 <= len(runs) <= MAX_NEXT_RUNS
        and all(routine_notice._instant(item) for item in runs)
        and runs == sorted(runs)
        and type(value["daily_cap"]) is int
        and value["daily_cap"] == routine.daily_cap(value["schedule"])
        and all(_card_step(item, index) for index, item in enumerate(steps, start=1))
        and _card_permitted(value["permitted"])
        and _permits_exactly(value["permitted"], steps)
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
    "routine-binding-unsourced",
    "routine-work-split",
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
        routine._plain(decoded, MAX_QUESTION_OPTION_CHARS)
        if isinstance(decoded, str)
        else type(decoded) is int and len(text) <= MAX_QUESTION_OPTION_CHARS
    )
    return scalar and json.dumps(decoded, ensure_ascii=False) == text


def _question_option(value: object) -> bool:
    """One target a person may choose: the JSON text of the value its input would take, and its name, if any."""
    if not isinstance(value, dict) or set(value) != {"value", "label"}:
        return False
    label = value["label"]
    return _target(value["value"]) and (label is None or routine._plain(label, MAX_QUESTION_OPTION_CHARS))


# What each run does with its result, as the person chooses it (ADR-0101): show it every run, show it only when it
# changes, show nothing, or use it in other Actions, which the recorded work then runs and the Routine shows. Team reads
# the choice from the person's own words; asked, Admin offers these labels in the interface language, and the label
# the person picks, as Admin composes it, states that choice.
OUTPUT_KINDS = ("show", "changes", "none", "chain")
OUTPUT_CHOICES = {
    "ar": {
        "show": "اعرض في كل تشغيل",
        "changes": "اعرض فقط عند التغيير",
        "none": "لا تعرض",
        "chain": "استخدمه في إجراءات أخرى",
    },
    "de": {
        "show": "Bei jedem Lauf anzeigen",
        "changes": "Nur bei Änderung anzeigen",
        "none": "Nicht anzeigen",
        "chain": "In anderen Aktionen verwenden",
    },
    "en": {
        "show": "Show every run",
        "changes": "Show only when it changes",
        "none": "Don't show",
        "chain": "Use it in other Actions",
    },
    "es": {
        "show": "Mostrar en cada ejecución",
        "changes": "Mostrar solo cuando cambie",
        "none": "No mostrar",
        "chain": "Usar en otras acciones",
    },
    "fr": {
        "show": "Afficher à chaque exécution",
        "changes": "Afficher seulement en cas de changement",
        "none": "Ne pas afficher",
        "chain": "Utiliser dans d'autres actions",
    },
    "ja": {
        "show": "毎回表示する",
        "changes": "変更時のみ表示する",
        "none": "表示しない",
        "chain": "他のアクションで使う",
    },
    "pt": {
        "show": "Mostrar em todas as execuções",
        "changes": "Mostrar somente quando mudar",
        "none": "Não mostrar",
        "chain": "Usar em outras ações",
    },
    "zh": {"show": "每次运行都显示", "changes": "仅在变化时显示", "none": "不显示", "chain": "用于其他操作"},
}


# The reply of a send that answers Team's pending question, which Team then records without asking the Brain: one
# fixed text in each interface language, and the English one for a chat without one (ADR-0101).
ANSWER_REPLIES = {
    "ar": "طبّقتُ إجابتك على الروتين.",
    "de": "Ich habe Ihre Antwort auf die Routine angewendet.",
    "en": "I applied your answer to the Routine.",
    "es": "Apliqué tu respuesta a la rutina.",
    "fr": "J'ai appliqué votre réponse à la routine.",
    "ja": "回答をルーティンに反映しました。",
    "pt": "Apliquei sua resposta à rotina.",
    "zh": "已将你的回答应用到例行任务。",
}


def answer_reply(locale: str | None) -> str:
    """The fixed reply of an answer Team records itself, in the chat's interface language or English."""
    return ANSWER_REPLIES[locale or "en"]


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
            routine._whole(interval, routine.MIN_CONTINUOUS_GAP_SECONDS, routine.MAX_CONTINUOUS_GAP_SECONDS)
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
        or not routine._identity(value["code"], routine.ERROR_CODE_RE)
    ):
        return None
    return {"code": value["code"]}


# A person's answer to a card: Criar rotina creates or changes the Routine; Cancelar revokes the card.
PROPOSAL_STATUSES = ("created", "changed", "revoked")


def canonical_proposal_answer(value: object) -> dict[str, object] | None:
    """What an answer did: the Routine it created or changed, or the card it revoked."""
    if not isinstance(value, dict) or set(value) != {"team_id", "proposal_id", "routine_id", "status"}:
        return None
    status = value["status"]
    valid = (
        routine._team(value["team_id"])
        and routine._identity(value["proposal_id"], routine.ROUTINE_ID_RE)
        and status in PROPOSAL_STATUSES
        and (
            value["routine_id"] is None
            if status == "revoked"
            else routine._identity(value["routine_id"], routine.ROUTINE_ID_RE)
        )
    )
    return copy.deepcopy(value) if valid else None
