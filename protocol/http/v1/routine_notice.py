"""Canonical Team Routine notices and the views Admin lists (ADR-0086, ADR-0092, ADR-0101)."""

from __future__ import annotations

import copy
import datetime
import re

if __package__:
    from . import routine
else:  # The protocol verifier runs every module of this directory flat.
    import routine


def _disposed(value: object, steps: int) -> bool:
    """Exactly one admitted disposition of a plan of ``steps`` steps; null is none."""
    admitted = routine.canonical_disposition(value, steps)
    return admitted is not None and admitted == value


def _completed(detail: dict[str, object]) -> bool:
    """A completed run: its plan's summary and its shown result if any."""
    summary = routine.canonical_summary(detail["plan"])
    output = detail["output"]
    return summary is not None and (
        output is None or (routine.canonical_output(output) is not None and output["step"] <= summary["steps"])
    )


def _held_step(detail: dict[str, object]) -> bool:
    """The call a held run stopped at and its position, or all null when the run sealed no plan before it was held."""
    if detail["assistant_id"] is None:
        return detail["action"] is None and detail["position"] is None and detail["steps"] is None
    return (
        routine.valid_assistant(detail["assistant_id"])
        and routine.valid_action(detail["action"])
        and routine.canonical_position(detail["position"], detail["steps"]) is not None
    )


_STEP_FIELDS = {"assistant_id", "action", "position", "steps"}
# What a frozen run waits for: a person's answer to a declared human request, or an Integration.
REQUEST_KINDS = ("human", "integrations")


def _frozen(detail: dict[str, object]) -> bool:
    """The one request a frozen run waits for: its kind, the Assistant Action that asked, and that call's position."""
    return detail["request_kind"] in REQUEST_KINDS and detail["assistant_id"] is not None and _held_step(detail)


def _failed_at(detail: dict[str, object]) -> bool:
    """The call a failed run stopped at, by position, or both null when it failed before any call."""
    if (detail["position"], detail["steps"]) == (None, None):
        return True
    return routine.canonical_position(detail["position"], detail["steps"]) is not None


_COMPLETED_FIELDS = {"plan", "output"}
# Each outcome's exact detail fields and check: denied and stopped name the Actions that completed; held, paused, and
# user-skipped name the call whose effects are unresolved, and user-skipped the card choice that set the run aside.
_DETAILS = {
    "done": (_COMPLETED_FIELDS, _completed),
    "recovered": (_COMPLETED_FIELDS, _completed),
    "held": (_STEP_FIELDS, _held_step),
    "paused": (
        _STEP_FIELDS | {"reason"},
        lambda detail: _held_step(detail) and detail["reason"] in routine.PAUSE_REASONS,
    ),
    "user-skipped": (
        _STEP_FIELDS | {"choice"},
        lambda detail: _held_step(detail) and detail["choice"] in CARD_CHOICES,
    ),
    "skipped": ({"missed"}, lambda detail: type(detail["missed"]) is int and detail["missed"] >= 1),
    "healthy": (
        {"runs"},
        lambda detail: type(detail["runs"]) is int and 1 <= detail["runs"] <= routine.MAX_ROLLUP_RUNS,
    ),
    "scope-changed": ({"assistants"}, routine.scope_changed),
    "frozen": ({"request_kind"} | _STEP_FIELDS, _frozen),
    "failed": (
        {"code", "actions", "position", "steps"},
        lambda detail: (
            routine.matches(detail["code"], routine.ERROR_CODE_RE)
            and routine.valid_actions(detail["actions"])
            and _failed_at(detail)
        ),
    ),
    "denied": ({"actions"}, lambda detail: routine.valid_actions(detail["actions"])),
    "stopped": ({"actions"}, lambda detail: routine.valid_actions(detail["actions"])),
    # A Routine's creation, change, and deletion say nothing beyond the notice's own name (ADR-0101).
    "created": (set(), lambda _detail: True),
    "changed": (set(), lambda _detail: True),
    "deleted": (set(), lambda _detail: True),
    "streak-paused": (
        {"failures"},
        lambda detail: type(detail["failures"]) is int and 1 <= detail["failures"] <= routine.MAX_FAILURE_STREAK,
    ),
}


def canonical_notice_detail(outcome: object, detail: object) -> dict[str, object] | None:
    """The exact closed detail for a run outcome, or None; never an Action's raw input or result."""
    if not isinstance(outcome, str) or outcome not in routine.OUTCOMES or not isinstance(detail, dict):
        return None
    fields, valid = _DETAILS[outcome]
    return copy.deepcopy(detail) if set(detail) == fields and valid(detail) else None


# Views a Local Team returns to Admin for Routines. Admin admits each only in exactly this closed form.
MAX_NOTICE_BATCH = 1024
# The encoded notice list of one batch, under the Local API's 128 KiB response cap with room for its envelope. The
# largest notice, a completed run's shown output of at most MAX_OUTPUT_BYTES beside its plan summary, fits many times.
MAX_NOTICE_BATCH_BYTES = 112 * 1024
RUN_STATUSES = frozenset({"leased", "frozen", "held"})
# A Routine's state (ADR-0101 section 5.5): it runs, or a person paused it.
ROUTINE_STATES = ("active", "paused")
LEASE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_INSTANT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")


def instant(value: object) -> bool:
    """A real UTC instant in whole seconds, written ``YYYY-MM-DDTHH:MM:SSZ``."""
    if not isinstance(value, str) or _INSTANT_RE.fullmatch(value) is None:
        return False
    try:
        datetime.datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def optional(value: object, pattern: re.Pattern[str]) -> bool:
    return value is None or routine.matches(value, pattern)


def canonical_routine_view(value: object) -> dict[str, object] | None:
    """One Routine as a Supervisor sees it: its plan summary (steps are paged), disposition, and state."""
    fields = {"routine_id", "name", "schedule", "timezone", "timezone_source", "next_run_at"}
    if not isinstance(value, dict) or set(value) != fields | {"needs_reconfirm", "state", "deleting", "plan", "output"}:
        return None
    summary = routine.canonical_summary(value["plan"])
    valid = (
        routine.matches(value["routine_id"], routine.ROUTINE_ID_RE)
        and routine.canonical_name(value["name"]) == value["name"]
        and summary is not None
        and _disposed(value["output"], summary["steps"])
        and value["schedule"] is not None
        and routine.canonical_schedule(value["schedule"]) == value["schedule"]
        and routine.zoned(value["timezone"], value["timezone_source"])
        and instant(value["next_run_at"])
        and type(value["needs_reconfirm"]) is bool
        and type(value["deleting"]) is bool
        and value["state"] in ROUTINE_STATES
    )
    return copy.deepcopy(value) if valid else None


def canonical_run_view(value: object) -> dict[str, object] | None:
    """One live run: a frozen run names the request it waits for, and its call; any other only that it is live."""
    fields = {"run_id", "routine_id", "status", "scheduled_at", "request_kind", "assistant_id", "action"}
    if not isinstance(value, dict) or set(value) != fields | {"position", "steps"}:
        return None
    status = value["status"]
    request = (value["request_kind"], value["assistant_id"], value["action"], value["position"], value["steps"])
    frozen = (
        request[0] in REQUEST_KINDS
        and routine.valid_assistant(request[1])
        and routine.valid_action(request[2])
        and routine.canonical_position(request[3], request[4]) is not None
    )
    valid = (
        routine.matches(value["run_id"], routine.ROUTINE_ID_RE)
        and routine.matches(value["routine_id"], routine.ROUTINE_ID_RE)
        and instant(value["scheduled_at"])
        and isinstance(status, str)
        and status in RUN_STATUSES
        and (frozen if status == "frozen" else request == (None, None, None, None, None))
    )
    return copy.deepcopy(value) if valid else None


def canonical_incident_view(value: object) -> dict[str, object] | None:
    """One unresolved incident of a held run, its held call by position; it lasts until a person settles it."""
    fields = {"incident_id", "routine_id", "name", "created_at", "assistant_id", "action", "position", "steps"}
    if not isinstance(value, dict) or set(value) != fields:
        return None
    valid = (
        routine.matches(value["incident_id"], routine.ROUTINE_ID_RE)
        and routine.matches(value["routine_id"], routine.ROUTINE_ID_RE)
        and routine.canonical_name(value["name"]) == value["name"]
        and instant(value["created_at"])
        and _held_step(value)
    )
    return copy.deepcopy(value) if valid else None


# The unresolved incidents a Team holds at most, which its Routine list carries (ADR-0092).
MAX_UNRESOLVED_INCIDENTS = 32
# A plan summary encoded at most: 16 runs of a Local Team's Assistant id (<= 40 chars), Action id (<= 128), and count.
MAX_SUMMARY_BYTES = 4 * 1024
# A Team's whole Routine list, encoded: the one response above the Local API's 128 KiB cap. Beside its summary, a
# Routine view holds at most 8 KiB, a run view 1 KiB, an incident view 4 KiB, and the envelope 4 KiB, for a Local
# Team's identifiers; steps are paged, never listed.
MAX_ROUTINE_LIST_BYTES = (
    routine.MAX_ROUTINES * (MAX_SUMMARY_BYTES + 8 * 1024 + 1024) + MAX_UNRESOLVED_INCIDENTS * 4 * 1024 + 4 * 1024
)
CARD_CHOICES = ("run", "delete")


def canonical_notice(value: object) -> dict[str, object] | None:
    """One undelivered run or Routine outcome for Admin to write to its Team's transcript.

    Every version names the Routine as it was when Team wrote that version, so a row keeps its own title after the
    Routine is renamed or deleted. A run notice carries the run's usage; a Routine outcome carries none, except the
    healthy rollup, which carries its runs' summed duration. ``protection_lost`` says the run lost the protection of its
    secret values, so nothing it produced after the loss was shown anywhere (ADR-0101 section 6.2); a Routine outcome
    never says so.
    """
    fields = {"team_id", "notice_id", "version", "routine_id", "name", "run_id", "outcome", "created_at"}
    if not isinstance(value, dict) or set(value) != fields | {"detail", "usage", "protection_lost"}:
        return None
    outcome = value["outcome"]
    used = outcome not in routine.ROUTINE_OUTCOMES or outcome == "healthy"
    valid = (
        routine.valid_team(value["team_id"])
        and routine.matches(value["notice_id"], routine.ROUTINE_ID_RE)
        and type(value["version"]) is int
        and value["version"] >= 1
        and routine.matches(value["routine_id"], routine.ROUTINE_ID_RE)
        and routine.canonical_name(value["name"]) == value["name"]
        and optional(value["run_id"], routine.ROUTINE_ID_RE)
        and (value["run_id"] is None) == (outcome in routine.ROUTINE_OUTCOMES)
        # A run's one notice is keyed by its run id.
        and value["run_id"] in (None, value["notice_id"])
        and instant(value["created_at"])
        and canonical_notice_detail(outcome, value["detail"]) is not None
        and (routine.canonical_run_usage(value["usage"]) is not None if used else value["usage"] is None)
        and type(value["protection_lost"]) is bool
        and (outcome not in routine.ROUTINE_OUTCOMES or value["protection_lost"] is False)
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
    if None in admitted or routine.encoded_bytes(admitted) > MAX_NOTICE_BATCH_BYTES:
        return None
    keys = {(item["team_id"], item["notice_id"]) for item in admitted}
    return {"notices": admitted, "more": value["more"]} if len(keys) == len(admitted) else None
