"""Canonical Team Routine wire forms (ADR-0086): schedules, timezones, bounds, and the views Admin admits."""

from __future__ import annotations

import copy
import datetime
import json
import math
import re
import unicodedata
from fractions import Fraction

MAX_ROUTINES = 8
MAX_ROUTINE_QUOTE_CHARS = 500
MAX_ROUTINE_NAME_CHARS = 80
# The ordered Actions of a compiled plan (ADR-0092 section 3).
MAX_ROUTINE_STEPS = 8
# A Team's starts in any rolling 24 hours, and the bound on the sum of its Routines' caps (ADR-0092 section 9).
MAX_DAILY_RUNS = 1000
# A continuous Routine starts its next run this long, at least, after the previous one ended; at most a day.
MIN_CONTINUOUS_GAP_SECONDS = 5
MAX_CONTINUOUS_GAP_SECONDS = 86_400
# The healthy runs a continuous Routine can end in one minute bucket, since each next run starts its gap after.
MAX_ROLLUP_RUNS = 60 // MIN_CONTINUOUS_GAP_SECONDS
MAX_NOTICE_ACTIONS = 16
MAX_NOTICE_ASSISTANTS = 16
OUTCOMES = frozenset(
    {
        "done",
        "healthy",
        "recovered",
        "held",
        "paused",
        "user-skipped",
        "failed",
        "denied",
        "stopped",
        "skipped",
        "scope-changed",
        "frozen",
        "created",
        "changed",
    }
)
# Outcomes of the Routine itself, never of a run: they carry no run id. ``skipped`` reports missed firings; a person
# setting a held run aside through its card's Rodar or Recriar, or by deleting its Routine, is the run outcome
# ``user-skipped``, whose ``choice`` says which. ``healthy`` rolls up a continuous Routine's healthy runs that
# ended in one minute, starting at the notice's instant (ADR-0092 section 9).
ROUTINE_OUTCOMES = ("skipped", "scope-changed", "created", "changed", "healthy")
# Why a held run's Routine was paused (ADR-0092): the recovery decision, a decision that could not be made, the spent
# recovery budget, a Team-detected policy fault such as a secret echo or an invalid frame, or recovery evidence that
# could not be read.
PAUSE_REASONS = ("decided", "unavailable", "exhausted", "policy", "evidence")
# The same identifier grammar as payload.py; protocol modules stay independent, and a Team test pins the equality.
ASSISTANT_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
ACTION_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
# The interface languages: the same closed set as payload.CHAT_LOCALES, pinned equal by a Team test.
LOCALES = frozenset({"ar", "de", "en", "es", "fr", "ja", "pt", "zh"})
SCHEDULE_KINDS = frozenset({"hourly", "daily", "weekly", "monthly", "continuous"})
ROUTINE_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
# An IANA zone name such as "UTC" or "America/Argentina/Buenos_Aires"; Team also requires that it loads.
TIMEZONE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{0,31}(?:/[A-Za-z0-9][A-Za-z0-9_+-]{0,31}){0,2}\Z")
_TIME_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")
_FIELDS = {
    "hourly": frozenset({"kind", "every"}),
    "daily": frozenset({"kind", "time"}),
    "weekly": frozenset({"kind", "weekday", "time"}),
    "monthly": frozenset({"kind", "day", "time"}),
    "continuous": frozenset({"kind", "gap", "cap"}),
}


def _whole(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _wall_clock(value: dict[str, object]) -> bool:
    kind = value["kind"]
    return (
        isinstance(value["time"], str)
        and _TIME_RE.fullmatch(value["time"]) is not None
        and (kind != "weekly" or _whole(value["weekday"], 0, 6))
        and (kind != "monthly" or _whole(value["day"], 1, 28))
    )


def canonical_schedule(value: object) -> dict[str, object] | None:
    """The exact schedule, or None.

    Hourly every 1..24 hours, daily, weekly (0 = Monday), monthly on day 1..28, or continuous: ``gap`` seconds (5 to
    86,400) after each run ends, and at most ``cap`` (1 to 1,000) starts in any rolling 24 hours.
    """
    kind = value.get("kind") if isinstance(value, dict) else None
    if not isinstance(kind, str) or kind not in SCHEDULE_KINDS or set(value) != _FIELDS[kind]:
        return None
    if kind == "continuous":
        valid = _whole(value["gap"], MIN_CONTINUOUS_GAP_SECONDS, MAX_CONTINUOUS_GAP_SECONDS) and _whole(
            value["cap"], 1, MAX_DAILY_RUNS
        )
    elif kind == "hourly":
        valid = _whole(value["every"], 1, 24)
    else:
        valid = _wall_clock(value)
    return dict(value) if valid else None


def canonical_quote(value: object) -> str | None:
    """The user's quoted request: NFC, one line, no control characters, trimmed, 1 to 500 characters."""
    return _line(value, MAX_ROUTINE_QUOTE_CHARS)


def canonical_name(value: object) -> str | None:
    """A Routine's short name: NFC, one line, no control characters, trimmed, 1 to 80 characters."""
    return _line(value, MAX_ROUTINE_NAME_CHARS)


def _line(value: object, maximum: int) -> str | None:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= maximum
        or unicodedata.normalize("NFC", value) != value
        or value.strip() != value
        or any(
            unicodedata.category(character)[0] == "C" or unicodedata.category(character) in {"Zl", "Zp"}
            for character in value
        )
    ):
        return None
    return value


def canonical_timezone(value: object) -> str | None:
    return value if isinstance(value, str) and TIMEZONE_RE.fullmatch(value) is not None else None


def daily_rate(schedule: dict[str, object]) -> Fraction:
    """The runs per day a canonical schedule allows, its cap for a continuous one.

    A Team's Routines may sum to at most MAX_DAILY_RUNS.
    """
    kind = schedule["kind"]
    if kind == "continuous":
        return Fraction(schedule["cap"])
    if kind == "hourly":
        return Fraction(24, schedule["every"])
    return {"daily": Fraction(1), "weekly": Fraction(1, 7), "monthly": Fraction(1, 28)}[kind]


def daily_cap(schedule: dict[str, object]) -> int:
    """The most runs a Routine may start in any rolling 24 hours: its cap, or its schedule's whole daily rate."""
    return math.ceil(daily_rate(schedule))


def _actions(value: object) -> bool:
    """Bounded Assistant and Action identities only: a notice never carries an Action's input or result."""
    return (
        isinstance(value, list)
        and len(value) <= MAX_NOTICE_ACTIONS
        and all(
            isinstance(pair, list)
            and len(pair) == 2
            and all(isinstance(part, str) for part in pair)
            and ASSISTANT_ID_RE.fullmatch(pair[0]) is not None
            and ACTION_ID_RE.fullmatch(pair[1]) is not None
            for pair in value
        )
    )


def _identity(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _scope_changed(detail: dict[str, object]) -> bool:
    assistants = detail["assistants"]
    return (
        isinstance(assistants, list)
        and 0 < len(assistants) <= MAX_NOTICE_ASSISTANTS
        and all(_identity(item, ASSISTANT_ID_RE) for item in assistants)
    )


# A Routine's plan as a Supervisor inspects it (ADR-0092): each step's Action, every input's source, and the Stored
# Inputs its Action uses by name only. A literal shows as a bounded preview of its JSON text; plan admission already
# refuses a literal where a secret belongs, and a Stored Input's value never enters a plan.
MAX_PREVIEW_CHARS = 120
MAX_STEP_INPUTS = 64
MAX_STEP_STORED_INPUTS = 8
MAX_MEMBER_CHARS = 128
MAX_POINTER_CHARS = 256
CLOCK_FORMATS = frozenset({"date", "time", "datetime", "epoch_seconds"})
STEP_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_POINTER_RE = re.compile(r"(?:/(?:[^/~]|~[01])*)*\Z")
_PLAN_UNSAFE_RE = re.compile(r"[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]")
_INPUT_FIELDS = {
    "literal": frozenset({"member", "source", "value"}),
    "run_clock": frozenset({"member", "source", "value"}),
    "step_output": frozenset({"member", "source", "step", "pointer"}),
}


def literal_preview(value: object) -> str:
    """A literal's JSON text, with every control or invisible character escaped, cut to 120 characters."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    text = _PLAN_UNSAFE_RE.sub(lambda match: f"\\u{ord(match.group()):04x}", text)
    return text if len(text) <= MAX_PREVIEW_CHARS else text[: MAX_PREVIEW_CHARS - 1] + "…"


def _plain(value: object, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and _PLAN_UNSAFE_RE.search(value) is None


def _input(value: object, earlier: tuple[str, ...]) -> bool:
    source = value.get("source") if isinstance(value, dict) else None
    if not isinstance(source, str) or source not in _INPUT_FIELDS or set(value) != _INPUT_FIELDS[source]:
        return False
    if not _plain(value["member"], MAX_MEMBER_CHARS):
        return False
    if source == "literal":
        return _plain(value["value"], MAX_PREVIEW_CHARS)
    if source == "run_clock":
        return isinstance(value["value"], str) and value["value"] in CLOCK_FORMATS
    pointer = value["pointer"]
    return (
        value["step"] in earlier
        and isinstance(pointer, str)
        and len(pointer) <= MAX_POINTER_CHARS
        and _POINTER_RE.fullmatch(pointer) is not None
        and _PLAN_UNSAFE_RE.search(pointer) is None
    )


def _step(value: object, earlier: tuple[str, ...]) -> bool:
    if not isinstance(value, dict) or set(value) != {"id", "assistant", "action", "inputs", "stored_inputs"}:
        return False
    inputs, stored = value["inputs"], value["stored_inputs"]
    return (
        _identity(value["id"], STEP_ID_RE)
        and value["id"] not in earlier
        and _identity(value["assistant"], ASSISTANT_ID_RE)
        and _identity(value["action"], ACTION_ID_RE)
        and isinstance(inputs, list)
        and len(inputs) <= MAX_STEP_INPUTS
        and all(_input(item, earlier) for item in inputs)
        and [item["member"] for item in inputs] == sorted({item["member"] for item in inputs})
        and isinstance(stored, list)
        and len(stored) <= MAX_STEP_STORED_INPUTS
        and all(_identity(item, ASSISTANT_ID_RE) for item in stored)
        and stored == sorted(set(stored))
    )


def canonical_steps(value: object) -> list[dict[str, object]] | None:
    """A Routine plan's safe projection: one to eight ordered steps, each referring only to earlier ones."""
    if not isinstance(value, list) or not 0 < len(value) <= MAX_ROUTINE_STEPS:
        return None
    earlier: tuple[str, ...] = ()
    for step in value:
        if not _step(step, earlier):
            return None
        earlier = (*earlier, step["id"])
    return copy.deepcopy(value)


def _defined(detail: dict[str, object]) -> bool:
    """What a created or changed Routine does: its name, its plan's safe projection, and when."""
    return (
        canonical_name(detail["name"]) == detail["name"]
        and canonical_steps(detail["steps"]) is not None
        and canonical_schedule(detail["schedule"]) == detail["schedule"]
        and canonical_timezone(detail["timezone"]) is not None
    )


def _frozen(detail: dict[str, object]) -> bool:
    """The one request a frozen run waits for: its kind and the Assistant Action that asked."""
    return (
        detail["request_kind"] in ("human", "integrations")
        and _identity(detail["assistant_id"], ASSISTANT_ID_RE)
        and _identity(detail["action"], ACTION_ID_RE)
    )


def _completed(detail: dict[str, object]) -> bool:
    """The ordered Assistant Actions a completed run carried out; never their input or result."""
    return _actions(detail["actions"]) and 0 < len(detail["actions"]) <= MAX_ROUTINE_STEPS


def _step_pair(assistant_id: object, action: object) -> bool:
    if assistant_id is None and action is None:
        return True
    return _identity(assistant_id, ASSISTANT_ID_RE) and _identity(action, ACTION_ID_RE)


def _held_step(detail: dict[str, object]) -> bool:
    """The step a held run stopped at, or both null when the run sealed no plan before it was held."""
    return _step_pair(detail["assistant_id"], detail["action"])


_STEP_FIELDS = {"assistant_id", "action"}

# Each outcome's exact detail fields and their check. denied and stopped name the Actions that completed; held,
# paused, and user-skipped name the step whose possible effects are unresolved, and user-skipped the card choice that
# set the run aside.
_DETAILS = {
    "done": ({"actions"}, _completed),
    "recovered": ({"actions"}, _completed),
    "held": (_STEP_FIELDS, _held_step),
    "paused": (_STEP_FIELDS | {"reason"}, lambda detail: _held_step(detail) and detail["reason"] in PAUSE_REASONS),
    "user-skipped": (
        _STEP_FIELDS | {"choice"},
        lambda detail: _held_step(detail) and detail["choice"] in CARD_CHOICES,
    ),
    "skipped": ({"missed"}, lambda detail: type(detail["missed"]) is int and detail["missed"] >= 1),
    "healthy": ({"runs"}, lambda detail: type(detail["runs"]) is int and 1 <= detail["runs"] <= MAX_ROLLUP_RUNS),
    "scope-changed": ({"assistants"}, _scope_changed),
    "frozen": ({"request_kind", "assistant_id", "action"}, _frozen),
    "failed": (
        {"code", "actions"},
        lambda detail: _identity(detail["code"], ERROR_CODE_RE) and _actions(detail["actions"]),
    ),
    "denied": ({"actions"}, lambda detail: _actions(detail["actions"])),
    "stopped": ({"actions"}, lambda detail: _actions(detail["actions"])),
    "created": ({"name", "steps", "schedule", "timezone"}, _defined),
    "changed": ({"name", "steps", "schedule", "timezone"}, _defined),
}


def canonical_notice_detail(outcome: object, detail: object) -> dict[str, object] | None:
    """The exact closed detail for a run outcome, or None; never an Action's raw input or result."""
    if not isinstance(outcome, str) or outcome not in OUTCOMES or not isinstance(detail, dict):
        return None
    fields, valid = _DETAILS[outcome]
    return copy.deepcopy(detail) if set(detail) == fields and valid(detail) else None


# Views a Local Team returns to Admin for Routines. Admin admits each only in exactly this closed form.
MAX_NOTICE_BATCH = 1024
# The encoded notice list of one batch, under the Local API's 128 KiB response cap with room for its envelope. The
# largest notice, a created or changed Routine's projection of a plan admitted within 64 KiB, fits alone.
MAX_NOTICE_BATCH_BYTES = 112 * 1024
RUN_STATUSES = frozenset({"leased", "frozen", "held"})
# The model providers a Local Team can use; a claim names its Team's, so Admin sends that provider's key.
MODEL_PROVIDERS = ("anthropic", "openai")
TEAM_ID_RE = re.compile(r"[a-z0-9_]{1,40}\Z")
LEASE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_INSTANT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")


def _instant(value: object) -> bool:
    """A real UTC instant in whole seconds, written ``YYYY-MM-DDTHH:MM:SSZ``."""
    if not isinstance(value, str) or _INSTANT_RE.fullmatch(value) is None:
        return False
    try:
        datetime.datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def encoded_bytes(value: object) -> int:
    """The UTF-8 size of a value in the Local API's JSON encoding."""
    return len(json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8"))


def _assistant_ids(value: object, *, minimum: int) -> bool:
    return (
        isinstance(value, list)
        and minimum <= len(value) <= MAX_NOTICE_ASSISTANTS
        and all(_identity(item, ASSISTANT_ID_RE) for item in value)
        and value == sorted(set(value))
    )


def _optional(value: object, pattern: re.Pattern[str]) -> bool:
    return value is None or _identity(value, pattern)


def canonical_routine_view(value: object) -> dict[str, object] | None:
    """One Routine as a Supervisor sees it, with its name and its plan's safe projection."""
    fields = {"routine_id", "name", "quote", "schedule", "timezone", "assistant_ids", "next_run_at", "needs_reconfirm"}
    if not isinstance(value, dict) or set(value) != fields | {"deleting", "paused", "steps"}:
        return None
    valid = (
        _identity(value["routine_id"], ROUTINE_ID_RE)
        and canonical_name(value["name"]) == value["name"]
        and canonical_steps(value["steps"]) is not None
        and value["quote"] is not None
        and canonical_quote(value["quote"]) == value["quote"]
        and value["schedule"] is not None
        and canonical_schedule(value["schedule"]) == value["schedule"]
        and canonical_timezone(value["timezone"]) is not None
        and _assistant_ids(value["assistant_ids"], minimum=1)
        and _instant(value["next_run_at"])
        and type(value["needs_reconfirm"]) is bool
        and type(value["deleting"]) is bool
        and type(value["paused"]) is bool
    )
    return copy.deepcopy(value) if valid else None


def canonical_run_view(value: object) -> dict[str, object] | None:
    """One live run: a frozen run names the request it waits for; a leased or held one only that it is live."""
    fields = {"run_id", "routine_id", "status", "scheduled_at", "request_kind", "assistant_id", "action"}
    if not isinstance(value, dict) or set(value) != fields:
        return None
    status = value["status"]
    request = (value["request_kind"], value["assistant_id"], value["action"])
    frozen = (
        request[0] in ("human", "integrations")
        and _identity(request[1], ASSISTANT_ID_RE)
        and _identity(request[2], ACTION_ID_RE)
    )
    valid = (
        _identity(value["run_id"], ROUTINE_ID_RE)
        and _identity(value["routine_id"], ROUTINE_ID_RE)
        and _instant(value["scheduled_at"])
        and isinstance(status, str)
        and status in RUN_STATUSES
        and (frozen if status == "frozen" else request == (None, None, None))
    )
    return copy.deepcopy(value) if valid else None


def canonical_incident_view(value: object) -> dict[str, object] | None:
    """One unresolved incident of a held run, which a recovery card settles; it outlives a deleted Routine."""
    fields = {"incident_id", "routine_id", "quote", "created_at", "assistant_id", "action"}
    if not isinstance(value, dict) or set(value) != fields:
        return None
    valid = (
        _identity(value["incident_id"], ROUTINE_ID_RE)
        and _identity(value["routine_id"], ROUTINE_ID_RE)
        and value["quote"] is not None
        and canonical_quote(value["quote"]) == value["quote"]
        and _instant(value["created_at"])
        and _step_pair(value["assistant_id"], value["action"])
    )
    return copy.deepcopy(value) if valid else None


# The unresolved incidents a Team holds at most, which its Routine list carries (ADR-0092).
MAX_UNRESOLVED_INCIDENTS = 32
# A held run's recovery card (ADR-0092 section 7, amended 2026-10-02): exactly Rodar, Recriar, and Excluir, in this
# order, none recommended. Team answers only Rodar and Recriar; Excluir is the Routine's own confirmed deletion.
CARD_CHOICES = ("run", "recreate", "delete")
CARD_ANSWERS = ("run", "recreate")
CARD_SECONDS = 300
NONCE_RE = re.compile(r"[0-9a-f]{32}\Z")
# What an answer did: Rodar set the held run aside and requested one fresh run; Recriar replaced the Routine in place.
CARD_STATUSES = {"run": "requested", "recreate": "recreated"}
# Whether the held step's failure has a diagnostic: recorded (and shown), none kept, or one that could not be read.
CARD_EVIDENCE = ("recorded", "absent", "unavailable")
MAX_PLAN_STEPS = 8


def _card_step(value: dict[str, object]) -> bool:
    step, steps = value["step"], value["steps"]
    return type(step) is int and type(steps) is int and 1 <= step <= steps <= MAX_PLAN_STEPS


def _card_evidence(value: dict[str, object]) -> bool:
    """The held operation's latest diagnostic, exactly when one is recorded, and only of the card's own step."""
    evidence, diagnostic = value["evidence"], value["diagnostic"]
    if evidence not in CARD_EVIDENCE or (diagnostic is None) == (evidence == "recorded"):
        return False
    if diagnostic is None:
        return True
    admitted = canonical_diagnostic(diagnostic)
    return admitted is not None and (admitted["assistant_id"], admitted["action"]) == (
        value["assistant_id"],
        value["action"],
    )


def canonical_card(value: object) -> dict[str, object] | None:
    """An opened recovery card: the step it stopped at, its failure as recorded, its one-use nonce, and its choices."""
    fields = {"team_id", "incident_id", "routine_id", "revision", "assistant_id", "action", "nonce", "expires_in"}
    if not isinstance(value, dict) or set(value) != fields | {"step", "steps", "evidence", "diagnostic", "choices"}:
        return None
    valid = (
        _identity(value["team_id"], TEAM_ID_RE)
        and _identity(value["incident_id"], ROUTINE_ID_RE)
        and _identity(value["routine_id"], ROUTINE_ID_RE)
        and type(value["revision"]) is int
        and 1 <= value["revision"] < 2**31
        and _identity(value["assistant_id"], ASSISTANT_ID_RE)
        and _identity(value["action"], ACTION_ID_RE)
        and _card_step(value)
        and _card_evidence(value)
        and _identity(value["nonce"], NONCE_RE)
        and value["expires_in"] == CARD_SECONDS
        and type(value["expires_in"]) is int
        and value["choices"] == list(CARD_CHOICES)
    )
    return copy.deepcopy(value) if valid else None


def canonical_card_answer_request(value: object) -> dict[str, str] | None:
    """A person's answer to one card: its nonce and Rodar or Recriar; Excluir is never a card answer."""
    if not isinstance(value, dict) or set(value) != {"nonce", "choice"}:
        return None
    valid = _identity(value["nonce"], NONCE_RE) and value["choice"] in CARD_ANSWERS
    return {"nonce": value["nonce"], "choice": value["choice"]} if valid else None


def canonical_card_answer(value: object) -> dict[str, object] | None:
    """What an answer did: Rodar requested one fresh run, or Recriar replaced the Routine."""
    if not isinstance(value, dict) or set(value) != {"team_id", "incident_id", "choice", "status"}:
        return None
    valid = (
        _identity(value["team_id"], TEAM_ID_RE)
        and _identity(value["incident_id"], ROUTINE_ID_RE)
        and value["choice"] in CARD_ANSWERS
        and value["status"] == CARD_STATUSES[value["choice"]]
    )
    return copy.deepcopy(value) if valid else None


def canonical_notice(value: object) -> dict[str, object] | None:
    """One undelivered run or Routine outcome for Admin to write to its Team's transcript."""
    fields = {"team_id", "notice_id", "version", "routine_id", "quote", "run_id", "outcome", "created_at"}
    if not isinstance(value, dict) or set(value) != fields | {"detail"}:
        return None
    valid = (
        _identity(value["team_id"], TEAM_ID_RE)
        and _identity(value["notice_id"], ROUTINE_ID_RE)
        and type(value["version"]) is int
        and value["version"] >= 1
        and _identity(value["routine_id"], ROUTINE_ID_RE)
        and value["quote"] is not None
        and canonical_quote(value["quote"]) == value["quote"]
        and _optional(value["run_id"], ROUTINE_ID_RE)
        and (value["run_id"] is None) == (value["outcome"] in ROUTINE_OUTCOMES)
        # A run's one notice is keyed by its run id.
        and value["run_id"] in (None, value["notice_id"])
        and _instant(value["created_at"])
        and canonical_notice_detail(value["outcome"], value["detail"]) is not None
    )
    return copy.deepcopy(value) if valid else None


def canonical_notice_batch(value: object) -> dict[str, object] | None:
    """A bounded batch of notices; while ``more`` is true Admin acknowledges it and asks again."""
    if not isinstance(value, dict) or set(value) != {"notices", "more"} or type(value["more"]) is not bool:
        return None
    notices = value["notices"]
    if not isinstance(notices, list) or len(notices) > MAX_NOTICE_BATCH or (value["more"] and not notices):
        return None
    admitted = [canonical_notice(item) for item in notices]
    if None in admitted or encoded_bytes(admitted) > MAX_NOTICE_BATCH_BYTES:
        return None
    keys = {(item["team_id"], item["notice_id"]) for item in admitted}
    return {"notices": admitted, "more": value["more"]} if len(keys) == len(admitted) else None


def canonical_challenge_open(value: object) -> dict[str, str] | None:
    """Opening a frozen run's challenge names the Admin interface language its request copy renders in (ADR-0091)."""
    locale = value.get("locale") if isinstance(value, dict) and set(value) == {"locale"} else None
    return {"locale": locale} if isinstance(locale, str) and locale in LOCALES else None


def canonical_claim_request(value: object) -> dict[str, object] | None:
    """Admin's claim is exactly an empty object: no model key gates it, because a healthy run needs none."""
    return {} if value == {} else None


PLAN_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
# How a claimed run was scheduled: by its firings, or continuously after the previous run ended (ADR-0092).
RUN_MODES = ("scheduled", "continuous")


def run_mode(schedule: dict[str, object]) -> str:
    return "continuous" if schedule["kind"] == "continuous" else "scheduled"


def _revision(value: object) -> bool:
    return type(value) is int and 1 <= value < 2**31


def canonical_claim(value: object) -> dict[str, object] | None:
    """A claim's answer: one run with the lease token Admin's routine identity signs for, or none and a wake hint.

    A run names the Routine revision and plan digest it was claimed at, which its segment request binds. With no run,
    ``next_due_at`` is the earliest epoch second a Routine Admin can run becomes due, or null; Admin still reconciles
    on its own interval, since a hint can be missed.
    """
    if not isinstance(value, dict) or set(value) != {"run", "next_due_at"}:
        return None
    run, hint = value["run"], value["next_due_at"]
    if run is None:
        return {"run": None, "next_due_at": hint} if hint is None or (type(hint) is int and hint > 0) else None
    fields = {"team_id", "run_id", "routine_id", "lease_token", "lease_expires_at", "provider"}
    valid = (
        hint is None
        and isinstance(run, dict)
        and set(run) == fields | {"revision", "plan_digest", "mode"}
        and _identity(run["team_id"], TEAM_ID_RE)
        and _identity(run["run_id"], ROUTINE_ID_RE)
        and _identity(run["routine_id"], ROUTINE_ID_RE)
        and _identity(run["lease_token"], LEASE_TOKEN_RE)
        and type(run["lease_expires_at"]) is int
        and run["lease_expires_at"] > 0
        and run["provider"] in MODEL_PROVIDERS
        and _revision(run["revision"])
        and _identity(run["plan_digest"], PLAN_DIGEST_RE)
        and run["mode"] in RUN_MODES
    )
    return copy.deepcopy(value) if valid else None


def canonical_segment_request(value: object) -> dict[str, object] | None:
    """A leased run's segment request: exactly the revision and plan digest its claim named, under the signature."""
    if not isinstance(value, dict) or set(value) != {"revision", "plan_digest", "mode"}:
        return None
    valid = (
        _revision(value["revision"]) and _identity(value["plan_digest"], PLAN_DIGEST_RE) and value["mode"] in RUN_MODES
    )
    return (
        {"revision": value["revision"], "plan_digest": value["plan_digest"], "mode": value["mode"]} if valid else None
    )


# Per-execution diagnostics (ADR-0092 section 8): one Team-sanitized handled failure, or one safe transport condition,
# per attempt of one logical operation of a Routine run. Text members are literal evidence that Admin renders escaped,
# never as Markdown or HTML, and they are never effect proof or authority.
MAX_RUN_DIAGNOSTICS = 32
MAX_DIAGNOSTIC_ATTEMPTS = 64
MAX_DIAGNOSTIC_TEXT_BYTES = 2048
OPERATION_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
ERROR_TYPE_RE = re.compile(r"[!-~]{1,128}\Z")
PROVIDER_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z")
# The closed safe transport conditions; raw child output is never reflected.
CONDITION_RE = re.compile(
    r"(?:exit-status:-?[0-9]{1,10}|stderr-output|timeout|frame-invalid|exit-unavailable|transport-failed)\Z"
)
# Tab and line feed only; every other control, bidi override or isolate, and zero-width formatting character is refused.
_UNSAFE_TEXT_RE = re.compile(r"[\u0000-\u0008\u000b-\u001f\u007f-\u009f​-‏‪-‮⁠-⁯﻿]")
_FAILURE_FIELDS = frozenset(
    {"error_type", "message", "provider", "http_status", "response_excerpt", "redacted", "truncated"}
)
_DIAGNOSTIC_FIELDS = frozenset(
    {"operation_id", "attempt", "assistant_id", "action", "recorded_at", "failure", "condition"}
)


def _diagnostic_text(value: object) -> bool:
    if not isinstance(value, str) or _UNSAFE_TEXT_RE.search(value) is not None:
        return False
    try:
        return len(value.encode("utf-8")) <= MAX_DIAGNOSTIC_TEXT_BYTES
    except UnicodeEncodeError:
        return False


def canonical_failure(value: object) -> dict[str, object] | None:
    """One sanitized handled failure: the real type, message, provider, status, excerpt, and both flags."""
    if not isinstance(value, dict) or set(value) != _FAILURE_FIELDS:
        return None
    provider, status, excerpt = value["provider"], value["http_status"], value["response_excerpt"]
    valid = (
        _identity(value["error_type"], ERROR_TYPE_RE)
        and _diagnostic_text(value["message"])
        and (provider is None or (_identity(provider, PROVIDER_RE) and len(provider) <= 253))
        and (status is None or (type(status) is int and 100 <= status <= 599))
        and (excerpt is None or _diagnostic_text(excerpt))
        and type(value["redacted"]) is bool
        and type(value["truncated"]) is bool
    )
    return copy.deepcopy(value) if valid else None


def canonical_diagnostic(value: object) -> dict[str, object] | None:
    """One attempt's diagnostic: exactly one of a sanitized failure or a safe transport condition."""
    if not isinstance(value, dict) or set(value) != _DIAGNOSTIC_FIELDS:
        return None
    failure, condition = value["failure"], value["condition"]
    valid = (
        _identity(value["operation_id"], OPERATION_ID_RE)
        and type(value["attempt"]) is int
        and 1 <= value["attempt"] <= MAX_DIAGNOSTIC_ATTEMPTS
        and _identity(value["assistant_id"], ASSISTANT_ID_RE)
        and _identity(value["action"], ACTION_ID_RE)
        and _instant(value["recorded_at"])
        and (failure is None) != (condition is None)
        and (failure is None or canonical_failure(failure) is not None)
        and (condition is None or _identity(condition, CONDITION_RE))
    )
    return copy.deepcopy(value) if valid else None


def canonical_diagnostics(value: object) -> dict[str, object] | None:
    """A run's diagnostics, oldest first, each attempt of each operation at most once."""
    if not isinstance(value, dict) or set(value) != {"team_id", "run_id", "diagnostics"}:
        return None
    entries = value["diagnostics"]
    if (
        not _identity(value["team_id"], TEAM_ID_RE)
        or not _identity(value["run_id"], ROUTINE_ID_RE)
        or not isinstance(entries, list)
        or len(entries) > MAX_RUN_DIAGNOSTICS
    ):
        return None
    admitted = [canonical_diagnostic(item) for item in entries]
    if None in admitted:
        return None
    keys = [(item["recorded_at"], item["operation_id"], item["attempt"]) for item in admitted]
    unique = len({(item["operation_id"], item["attempt"]) for item in admitted}) == len(admitted)
    return {**value, "diagnostics": admitted} if unique and keys == sorted(keys) else None
