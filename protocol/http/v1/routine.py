"""Canonical Team Routine wire forms (ADR-0086): schedules, timezones, bounds, and the views Admin admits."""

from __future__ import annotations

import copy
import datetime
import json
import re
import unicodedata
from fractions import Fraction

MAX_ROUTINES = 8
MAX_ROUTINE_QUOTE_CHARS = 500
MAX_DAILY_RUNS = 24
MAX_NOTICE_REPLY_CHARS = 16_000
MAX_NOTICE_QUESTION_CHARS = 240
MAX_NOTICE_ACTIONS = 16
MAX_NOTICE_ASSISTANTS = 16
OUTCOMES = frozenset(
    {"done", "failed", "denied", "uncertain", "stopped", "skipped", "scope-changed", "needs-input", "frozen"}
)
# The same identifier grammar as payload.py; protocol modules stay independent, and a Team test pins the equality.
ASSISTANT_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
ACTION_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
SCHEDULE_KINDS = frozenset({"hourly", "daily", "weekly", "monthly"})
ROUTINE_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
# An IANA zone name such as "UTC" or "America/Argentina/Buenos_Aires"; Team also requires that it loads.
TIMEZONE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{0,31}(?:/[A-Za-z0-9][A-Za-z0-9_+-]{0,31}){0,2}\Z")
_TIME_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")
_FIELDS = {
    "hourly": frozenset({"kind", "every"}),
    "daily": frozenset({"kind", "time"}),
    "weekly": frozenset({"kind", "weekday", "time"}),
    "monthly": frozenset({"kind", "day", "time"}),
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
    """The exact schedule, or None: hourly every 1..24 hours, daily, weekly (0 = Monday), or monthly on day 1..28."""
    kind = value.get("kind") if isinstance(value, dict) else None
    if not isinstance(kind, str) or kind not in SCHEDULE_KINDS or set(value) != _FIELDS[kind]:
        return None
    valid = _whole(value["every"], 1, 24) if value["kind"] == "hourly" else _wall_clock(value)
    return dict(value) if valid else None


def canonical_quote(value: object) -> str | None:
    """The user's quoted request: NFC, one line, no control characters, trimmed, 1 to 500 characters."""
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= MAX_ROUTINE_QUOTE_CHARS
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
    """The average runs per day a canonical schedule fires; a Team's Routines may sum to at most MAX_DAILY_RUNS."""
    kind = schedule["kind"]
    if kind == "hourly":
        return Fraction(24, schedule["every"])
    return {"daily": Fraction(1), "weekly": Fraction(1, 7), "monthly": Fraction(1, 28)}[kind]


def _text(value: object, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and value.strip() == value


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


def _frozen(detail: dict[str, object]) -> bool:
    """The one request a frozen run waits for: its kind and the Assistant Action that asked."""
    return (
        detail["request_kind"] in ("human", "integrations")
        and _identity(detail["assistant_id"], ASSISTANT_ID_RE)
        and _identity(detail["action"], ACTION_ID_RE)
    )


# Each outcome's exact detail fields and their check. denied, stopped, and uncertain name the Actions that completed or
# whose effects are unknown; after a restart an uncertain run may not know them, and its notice then says only that.
_DETAILS = {
    "done": ({"reply"}, lambda detail: _text(detail["reply"], MAX_NOTICE_REPLY_CHARS)),
    "needs-input": ({"question"}, lambda detail: _text(detail["question"], MAX_NOTICE_QUESTION_CHARS)),
    "skipped": ({"missed"}, lambda detail: type(detail["missed"]) is int and detail["missed"] >= 1),
    "scope-changed": ({"assistants"}, _scope_changed),
    "frozen": ({"request_kind", "assistant_id", "action"}, _frozen),
    "failed": (
        {"code", "actions"},
        lambda detail: _identity(detail["code"], ERROR_CODE_RE) and _actions(detail["actions"]),
    ),
    "denied": ({"actions"}, lambda detail: _actions(detail["actions"])),
    "stopped": ({"actions"}, lambda detail: _actions(detail["actions"])),
    "uncertain": ({"actions"}, lambda detail: _actions(detail["actions"])),
}


def canonical_notice_detail(outcome: object, detail: object) -> dict[str, object] | None:
    """The exact closed detail for a run outcome, or None; never an Action's raw input or result."""
    if not isinstance(outcome, str) or outcome not in OUTCOMES or not isinstance(detail, dict):
        return None
    fields, valid = _DETAILS[outcome]
    return copy.deepcopy(detail) if set(detail) == fields and valid(detail) else None


def canonical_routine_change(value: object) -> dict[str, object] | None:
    """The exact Routine change a Brain turn proposes, or None: propose with a schedule, or cancel a Routine by id."""
    if not isinstance(value, dict) or set(value) != {"op", "quote", "schedule", "timezone", "routine_id"}:
        return None
    op, timezone, routine_id = value["op"], value["timezone"], value["routine_id"]
    if canonical_quote(value["quote"]) is None:
        return None
    if op == "propose":
        schedule = canonical_schedule(value["schedule"])
        valid = schedule is not None and routine_id is None and (timezone is None or canonical_timezone(timezone))
        return {**value, "schedule": schedule} if valid else None
    valid = (
        op == "cancel"
        and value["schedule"] is None
        and timezone is None
        and isinstance(routine_id, str)
        and ROUTINE_ID_RE.fullmatch(routine_id) is not None
    )
    return dict(value) if valid else None


# Views a Local Team returns to Admin for Routines. Admin admits each only in exactly this closed form.
PROPOSAL_SECONDS = 900
MAX_PREVIEW_RUNS = 3
MAX_NOTICE_BATCH = 1024
# The encoded notice list of one batch, under the Local API's 128 KiB response cap with room for its envelope. A
# notice at its bound, a 16,000-character reply whose every character JSON-escapes to six bytes, is about 96.5 KB.
MAX_NOTICE_BATCH_BYTES = 112 * 1024
RUN_STATUSES = frozenset({"leased", "frozen", "uncertain"})
# The model providers a Local Team can use; a claim names its Team's, so Admin sends that provider's key.
MODEL_PROVIDERS = ("anthropic", "openai")
TEAM_ID_RE = re.compile(r"[a-z0-9_]{1,40}\Z")
LEASE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_INSTANT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_RATE_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:/[1-9][0-9]*)?\Z")
_PROPOSAL_FIELDS = frozenset({"proposal_id", "op", "quote", "schedule", "timezone", "routine_id", "assistant_ids"})
_PREVIEW_FIELDS = frozenset({"timezone", "next_runs", "daily_runs", "max_daily_runs", "fits"})


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


def canonical_proposal(value: object) -> dict[str, object] | None:
    """A chat turn's one-use Routine proposal as the confirmation card shows it, or None."""
    if not isinstance(value, dict) or set(value) != _PROPOSAL_FIELDS | {"expires_in"}:
        return None
    change = canonical_routine_change(
        {field: value[field] for field in ("op", "quote", "schedule", "timezone", "routine_id")}
    )
    expires_in = value["expires_in"]
    valid = (
        change is not None
        and _identity(value["proposal_id"], ROUTINE_ID_RE)
        and _assistant_ids(value["assistant_ids"], minimum=1 if value["op"] == "propose" else 0)
        and type(expires_in) is int
        and 0 <= expires_in <= PROPOSAL_SECONDS
    )
    return copy.deepcopy(value) if valid else None


def canonical_preview(value: object) -> dict[str, object] | None:
    """A proposal with the facts its card shows: the timezone, the next runs, and the Team's daily run budget."""
    if not isinstance(value, dict) or set(value) != _PROPOSAL_FIELDS | {"expires_in"} | _PREVIEW_FIELDS:
        return None
    if canonical_proposal({field: value[field] for field in _PROPOSAL_FIELDS | {"expires_in"}}) is None:
        return None
    runs, rate, fits = value["next_runs"], value["daily_runs"], value["fits"]
    if value["op"] == "cancel":
        facts = (value["timezone"], runs, rate, value["max_daily_runs"], fits)
        return copy.deepcopy(value) if facts == (None, [], None, None, True) else None
    valid = (
        canonical_timezone(value["timezone"]) is not None
        and isinstance(runs, list)
        and 0 < len(runs) <= MAX_PREVIEW_RUNS
        and all(_instant(item) for item in runs)
        and runs == sorted(set(runs))
        and isinstance(rate, str)
        and _RATE_RE.fullmatch(rate) is not None
        and value["max_daily_runs"] == MAX_DAILY_RUNS
        and type(fits) is bool
    )
    return copy.deepcopy(value) if valid else None


def canonical_routine_view(value: object) -> dict[str, object] | None:
    """One confirmed Routine as a Supervisor sees it."""
    fields = {"routine_id", "quote", "schedule", "timezone", "assistant_ids", "next_run_at", "needs_reconfirm"}
    if not isinstance(value, dict) or set(value) != fields | {"deleting"}:
        return None
    valid = (
        _identity(value["routine_id"], ROUTINE_ID_RE)
        and value["quote"] is not None
        and canonical_quote(value["quote"]) == value["quote"]
        and value["schedule"] is not None
        and canonical_schedule(value["schedule"]) == value["schedule"]
        and canonical_timezone(value["timezone"]) is not None
        and _assistant_ids(value["assistant_ids"], minimum=1)
        and _instant(value["next_run_at"])
        and type(value["needs_reconfirm"]) is bool
        and type(value["deleting"]) is bool
    )
    return copy.deepcopy(value) if valid else None


def canonical_run_view(value: object) -> dict[str, object] | None:
    """One live run: a frozen run names its request, an uncertain one its batch and the Actions it may have run."""
    fields = {"run_id", "routine_id", "status", "scheduled_at", "request_kind", "assistant_id", "action"}
    if not isinstance(value, dict) or set(value) != fields | {"batch_fingerprint", "actions"}:
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
        and (frozen if status == "frozen" else request == (None, None, None))
        and (
            _identity(value["batch_fingerprint"], _HEX64_RE)
            if status == "uncertain"
            else isinstance(status, str) and status in RUN_STATUSES and value["batch_fingerprint"] is None
        )
        and _actions(value["actions"])
        and (status == "uncertain" or value["actions"] == [])
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
        and (value["run_id"] is None) == (value["outcome"] in ("skipped", "scope-changed"))
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


def canonical_claim_request(value: object) -> dict[str, object] | None:
    """Admin's claim: the model providers it holds a key for, sorted; only a Team using one of them is claimed."""
    providers = value.get("providers") if isinstance(value, dict) and set(value) == {"providers"} else None
    valid = (
        isinstance(providers, list)
        and 0 < len(providers) <= len(MODEL_PROVIDERS)
        and all(item in MODEL_PROVIDERS for item in providers)
        and providers == sorted(set(providers))
    )
    return {"providers": list(providers)} if valid else None


def canonical_claim(value: object) -> dict[str, object] | None:
    """A claim's answer: no run, or one run with the lease token Admin's routine identity signs for."""
    if not isinstance(value, dict) or set(value) != {"run"}:
        return None
    run = value["run"]
    if run is None:
        return {"run": None}
    fields = {"team_id", "run_id", "routine_id", "lease_token", "lease_expires_at", "provider"}
    valid = (
        isinstance(run, dict)
        and set(run) == fields
        and _identity(run["team_id"], TEAM_ID_RE)
        and _identity(run["run_id"], ROUTINE_ID_RE)
        and _identity(run["routine_id"], ROUTINE_ID_RE)
        and _identity(run["lease_token"], LEASE_TOKEN_RE)
        and type(run["lease_expires_at"]) is int
        and run["lease_expires_at"] > 0
        and run["provider"] in MODEL_PROVIDERS
    )
    return copy.deepcopy(value) if valid else None
