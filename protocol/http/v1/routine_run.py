"""Canonical Team Routine run forms (ADR-0092, ADR-0101): recovery cards, claims, diagnostics, and run steps."""

from __future__ import annotations

import copy
import re

if __package__:
    from . import routine, routine_notice
else:  # The protocol verifier runs every module of this directory flat.
    import routine_notice

    import routine


# A held run's recovery card (ADR-0092 section 7, ADR-0101): exactly Rodar and Excluir, in this order, none
# recommended. Team answers only Rodar; Excluir is the Routine's own confirmed deletion.
CARD_ANSWERS = ("run",)
CARD_SECONDS = 300
NONCE_RE = re.compile(r"[0-9a-f]{32}\Z")
# What an answer did: Rodar set the held run aside and requested one fresh run.
CARD_STATUSES = {"run": "requested"}
# Whether the held call's failure has a diagnostic: recorded (and shown), none kept, or one that could not be read.
CARD_EVIDENCE = ("recorded", "absent", "unavailable")


def _card_evidence(value: dict[str, object]) -> bool:
    """The held operation's latest diagnostic, exactly when one is recorded, and only of the card's own call."""
    evidence, diagnostic = value["evidence"], value["diagnostic"]
    if evidence not in CARD_EVIDENCE or (diagnostic is None) == (evidence == "recorded"):
        return False
    if diagnostic is None:
        return True
    admitted = canonical_diagnostic(diagnostic)
    return admitted is not None and (admitted["assistant_id"], admitted["action"], admitted["position"]) == (
        value["assistant_id"],
        value["action"],
        value["position"],
    )


def canonical_card(value: object) -> dict[str, object] | None:
    """An opened recovery card: the call it stopped at, its failure as recorded, its one-use nonce, and its choices."""
    fields = {"team_id", "incident_id", "routine_id", "revision", "assistant_id", "action", "nonce", "expires_in"}
    if not isinstance(value, dict) or set(value) != fields | {"position", "steps", "evidence", "diagnostic", "choices"}:
        return None
    valid = (
        routine._team(value["team_id"])
        and routine._identity(value["incident_id"], routine.ROUTINE_ID_RE)
        and routine._identity(value["routine_id"], routine.ROUTINE_ID_RE)
        and routine._revision(value["revision"])
        and routine._assistant(value["assistant_id"])
        and routine._action(value["action"])
        and routine.canonical_position(value["position"], value["steps"]) is not None
        and _card_evidence(value)
        and routine._identity(value["nonce"], NONCE_RE)
        and value["expires_in"] == CARD_SECONDS
        and type(value["expires_in"]) is int
        and value["choices"] == list(routine_notice.CARD_CHOICES)
    )
    return copy.deepcopy(value) if valid else None


def canonical_card_answer_request(value: object) -> dict[str, str] | None:
    """A person's answer to one card: its nonce and Rodar; Excluir is never a card answer."""
    if not isinstance(value, dict) or set(value) != {"nonce", "choice"}:
        return None
    valid = routine._identity(value["nonce"], NONCE_RE) and value["choice"] in CARD_ANSWERS
    return {"nonce": value["nonce"], "choice": value["choice"]} if valid else None


def canonical_card_answer(value: object) -> dict[str, object] | None:
    """What an answer did: Rodar requested one fresh run."""
    if not isinstance(value, dict) or set(value) != {"team_id", "incident_id", "choice", "status"}:
        return None
    valid = (
        routine._team(value["team_id"])
        and routine._identity(value["incident_id"], routine.ROUTINE_ID_RE)
        and value["choice"] in CARD_ANSWERS
        and value["status"] == CARD_STATUSES[value["choice"]]
    )
    return copy.deepcopy(value) if valid else None


def canonical_challenge_open(value: object) -> dict[str, str] | None:
    """Opening a frozen run's challenge names the Admin interface language its request copy renders in (ADR-0091)."""
    locale = value.get("locale") if isinstance(value, dict) and set(value) == {"locale"} else None
    return {"locale": locale} if isinstance(locale, str) and locale in routine.LOCALES else None


def canonical_claim_request(value: object) -> dict[str, object] | None:
    """Admin's claim says only whether it can take a long run now (it holds one at most); no model key gates it."""
    if not isinstance(value, dict) or set(value) != {"long"} or type(value["long"]) is not bool:
        return None
    return {"long": value["long"]}


# A run's active time grows with its units to a ceiling, and past 600 s it is long (ADR-0092, scale; ADR-0101 §6.4).
SHORT_ACTIVE_SECONDS = 600
MAX_ACTIVE_SECONDS = 7200


def active_seconds(units: int) -> int:
    """The active execution time a run of ``units`` units may spend: 600 s for eight, 7,200 at most."""
    return min(360 + 30 * units, MAX_ACTIVE_SECONDS)


# How a claimed run was scheduled: by its firings, or continuously after the previous run ended (ADR-0092).
RUN_MODES = ("scheduled", "continuous")


def run_mode(schedule: dict[str, object]) -> str:
    return "continuous" if schedule["kind"] == "continuous" else "scheduled"


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
    fields = {"team_id", "run_id", "routine_id", "lease_token", "lease_expires_at", "provider", "active_seconds"}
    valid = (
        hint is None
        and isinstance(run, dict)
        and set(run) == fields | {"revision", "plan_digest", "mode"}
        and type(run["active_seconds"]) is int
        and 0 < run["active_seconds"] <= MAX_ACTIVE_SECONDS
        and routine._team(run["team_id"])
        and routine._identity(run["run_id"], routine.ROUTINE_ID_RE)
        and routine._identity(run["routine_id"], routine.ROUTINE_ID_RE)
        and routine._identity(run["lease_token"], routine_notice.LEASE_TOKEN_RE)
        and type(run["lease_expires_at"]) is int
        and run["lease_expires_at"] > 0
        and run["provider"] in routine.MODEL_PROVIDERS
        and routine._revision(run["revision"])
        and routine._identity(run["plan_digest"], routine.PLAN_DIGEST_RE)
        and run["mode"] in RUN_MODES
    )
    return copy.deepcopy(value) if valid else None


def canonical_segment_request(value: object) -> dict[str, object] | None:
    """A leased run's segment request: exactly the revision and plan digest its claim named, under the signature."""
    if not isinstance(value, dict) or set(value) != {"revision", "plan_digest", "mode"}:
        return None
    # A request body reaches only the fixed-length digest pattern, never the shared identifier patterns.
    plan_digest = value["plan_digest"]
    valid = (
        routine._revision(value["revision"])
        and isinstance(plan_digest, str)
        and routine.PLAN_DIGEST_RE.fullmatch(plan_digest) is not None
        and value["mode"] in RUN_MODES
    )
    return {"revision": value["revision"], "plan_digest": plan_digest, "mode": value["mode"]} if valid else None


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
_UNSAFE_TEXT_RE = re.compile(r"[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
_FAILURE_FIELDS = frozenset(
    {"error_type", "message", "provider", "http_status", "response_excerpt", "redacted", "truncated"}
)
_DIAGNOSTIC_FIELDS = frozenset(
    {"operation_id", "attempt", "assistant_id", "action", "position", "recorded_at", "failure", "condition"}
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
        routine._identity(value["error_type"], ERROR_TYPE_RE)
        and _diagnostic_text(value["message"])
        and (provider is None or (routine._identity(provider, PROVIDER_RE) and len(provider) <= 253))
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
        routine._identity(value["operation_id"], OPERATION_ID_RE)
        and type(value["attempt"]) is int
        and 1 <= value["attempt"] <= MAX_DIAGNOSTIC_ATTEMPTS
        and routine._assistant(value["assistant_id"])
        and routine._action(value["action"])
        and routine.canonical_position(value["position"], routine.MAX_ROUTINE_STEPS) is not None
        and routine_notice._instant(value["recorded_at"])
        and (failure is None) != (condition is None)
        and (failure is None or canonical_failure(failure) is not None)
        and (condition is None or routine._identity(condition, CONDITION_RE))
    )
    return copy.deepcopy(value) if valid else None


def canonical_diagnostics(value: object) -> dict[str, object] | None:
    """A run's diagnostics, oldest first, each attempt of each operation at most once."""
    if not isinstance(value, dict) or set(value) != {"team_id", "run_id", "diagnostics"}:
        return None
    entries = value["diagnostics"]
    if (
        not routine._team(value["team_id"])
        or not routine._identity(value["run_id"], routine.ROUTINE_ID_RE)
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


# What one run did, step by step (ADR-0092 amendment, 2026-10-05, scale; ADR-0101 section 7): each replay step's
# status, attempt, duration, and inputs as redacted previews (null when a source's secrecy is unknown). A missing step
# is ``not_run`` only when the run's terminal record proves it, else ``unavailable``. Pages bind the run's revision and
# one records snapshot.
RUN_STEP_STATUSES = ("done", "recovered", "failed", "stopped", "waiting")
RUN_STEP_GAPS = ("not_run", "unavailable")
RUN_INPUT_SOURCES = frozenset({"literal", "run_clock", "step_output"})
SNAPSHOT_RE = re.compile(r"[0-9a-f]{32}\Z")
RUN_STEP_FIELDS = frozenset(
    {"position", "status", "assistant_id", "action", "attempt", "duration_ms", "recorded_at", "inputs"}
)


def _run_input(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"member", "source", "value"}
        and routine._plain(value["member"], routine.MAX_MEMBER_CHARS)
        and isinstance(value["source"], str)
        and value["source"] in RUN_INPUT_SOURCES
        and (value["value"] is None or routine._plain(value["value"], routine.MAX_PREVIEW_CHARS))
    )


def canonical_run_step(value: object, position: dict[str, object], steps: int) -> dict[str, object] | None:
    """One entry at ``position``: what its attempt did, or only that it never ran or cannot be shown."""
    if not isinstance(value, dict) or set(value) != RUN_STEP_FIELDS or value.get("position") != position:
        return None
    status, duration = value["status"], value["duration_ms"]
    if status in RUN_STEP_GAPS:
        valid = all(value[key] is None for key in RUN_STEP_FIELDS - {"position", "status"})
    else:
        valid = (
            status in RUN_STEP_STATUSES
            and routine._assistant(value["assistant_id"])
            and routine._action(value["action"])
            and type(value["attempt"]) is int
            and 1 <= value["attempt"] <= MAX_DIAGNOSTIC_ATTEMPTS
            and (duration is None or (type(duration) is int and 0 <= duration < 2**53))
            and (status != "recovered" or duration is None)
            and routine_notice._instant(value["recorded_at"])
            and (value["inputs"] is None or routine._members(value["inputs"], _run_input))
        )
    valid = (
        valid
        and routine.canonical_position(value["position"], steps) is not None
        and routine.encoded_bytes(value) <= routine.MAX_STEP_VIEW_BYTES
    )
    return copy.deepcopy(value) if valid else None


def run_position(index: int) -> dict[str, object]:
    """The position of a run page's ``index``-th entry (1-based): its replay step."""
    return {"phase": "replay", "step": index}


def canonical_run_steps(value: object) -> dict[str, object] | None:
    """One page of a run's entries from ``offset``: whole consecutive steps of its own revision's run.

    ``total`` is the revision's step count.
    """
    fields = {"team_id", "run_id", "routine_id", "revision", "plan_digest", "total", "snapshot", "ended"}
    if not isinstance(value, dict) or set(value) != fields | {"offset", "steps", "next"}:
        return None
    total = value["total"]
    valid = (
        routine._team(value["team_id"])
        and routine._identity(value["run_id"], routine.ROUTINE_ID_RE)
        and routine._identity(value["routine_id"], routine.ROUTINE_ID_RE)
        and routine._revision(value["revision"])
        and routine._identity(value["plan_digest"], routine.PLAN_DIGEST_RE)
        and routine._identity(value["snapshot"], SNAPSHOT_RE)
        and type(value["ended"]) is bool
        and routine._paged(value, lambda item, index: canonical_run_step(item, run_position(index), total))
    )
    return copy.deepcopy(value) if valid else None
