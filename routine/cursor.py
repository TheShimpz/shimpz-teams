"""The cursor of one Routine run, without I/O (ADR-0092 sections 3 and 5, ADR-0101 section 6).

A cursor binds the Team incarnation, the Routine and its revision, the run, the exact plan (whose digest covers every
step's complete pin), the run's one start instant, the current step, that step's logical ``operation_id``, attempts,
and resolved-input commitment, the values later steps selected from completed ones, the shown step's bounded result
and its keyed comparison digest (ADR-0092 amendment, 2026-10-05, output), and the remaining recovery budgets. A
completed step is never run again: the successful prefix is durable even when a later step fails. Restart never
replenishes a budget, and no secret, human response, or complete output is ever held here.

It also records the Team boot its run protection was bound in, and whether that protection was lost, which is never
undone. A run whose plan decides then moves from its ``replay`` phase to its ``decision`` phase and on to ``closed``:
its cursor keeps every replay step's kept result in an accumulator (the decision's input), the sealed candidate that
holds it, the decision's allowance and what it used, every decision call's operation with Team's classification of its
outcome, and the model the decision is bound to.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass

from action import journal as action_journal
from protocol.http.v1 import identifiers as http_identifiers
from protocol.http.v1 import routine as http_routine
from protocol.http.v1 import strict_json
from routine import plan as routine_plan

VERSION = 4
MAX_CURSOR_BYTES = 256 * 1024
# The initial automatic recovery bounds; consumption is persisted before any paid dispatch.
BUDGETS = (
    ("episodes", 1),
    ("model_calls", 4),
    ("output_tokens", 4096),
    ("recovery_seconds", 60),
    ("retries", 1),
    ("verifications", 3),
)
# Continuation segments a run may open after holds; each runs in its own journal generation.
MAX_SEGMENTS = 8
# Team's own classification of the dispatched operation's last failed attempt (ADR-0092 section 6): a handled failure
# envelope, a transport fault after a proven fail-stop, a fault whose workload could not be proven stopped, a policy
# fault (a secret echo, an invalid frame or result, an undeclared request), or any other refusal. Empty when the
# attempt has not failed, or its failure could not be classified.
FAULTS = ("", "handled", "transport", "unquiesced", "policy", "other")
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
# A Docker container id or name: the workload an attempt was dispatched to.
_WORKLOAD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_FIELDS = frozenset(
    {
        "version",
        "boot",
        "protection_lost",
        "phase",
        "accumulator",
        "candidate",
        "reservation",
        "calls",
        "model",
        "incarnation",
        "routine_id",
        "revision",
        "run_id",
        "plan",
        "started_at",
        "step",
        "operation_id",
        "attempts",
        "commitment",
        "selected",
        "budgets",
        "segment",
        "absent",
        "carried",
        "fault",
        "workload",
        "dispatched_at",
        "shown",
    }
)
_SHOWN_FIELDS = frozenset({"step", "output", "digest"})
# Where a run is: replaying its plan's steps, in its decision turn, or with its decision closed (decide plans only).
PHASES = ("replay", "decision", "closed")
# The kept replay results a decision reads, together; past it the accumulator is ``over`` and the decision refused.
MAX_ACCUMULATOR_BYTES = 64 * 1024
# A decision call's operation, as Team classified it: reserved before its RPC, dispatched, then settled.
CALL_STATES = ("reserved", "dispatched", "succeeded", "failed", "stopped")
_CALL_FIELDS = frozenset(
    {
        "operation_id",
        "assistant",
        "action",
        "read_only",
        "commitment",
        "attempts",
        "state",
        "fault",
        "workload",
        "dispatched_at",
        "absent",
    }
)


class CursorError(ValueError):
    """A cursor transition or encoding was refused; ``code`` is the stable reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Binding:
    """What a cursor belongs to: the Team incarnation, the Routine revision, and the run."""

    incarnation: str
    routine_id: str
    revision: int
    run_id: str


@dataclass(frozen=True, slots=True)
class Call:
    """One decision call's operation and Team's classification of it, recorded before its RPC (ADR-0101 §6.6)."""

    operation_id: str
    assistant: str
    action: str
    read_only: bool
    commitment: str
    attempts: int = 1
    state: str = "reserved"
    fault: str = ""
    workload: str = ""
    dispatched_at: int = 0
    absent: bool = False


@dataclass(frozen=True, slots=True)
class Cursor:
    binding: Binding
    plan: str
    started_at: int
    step: int = 0
    operation_id: str | None = None
    attempts: int = 0
    commitment: str | None = None
    selected: tuple[tuple[str, str, object], ...] = ()
    budgets: tuple[tuple[str, int], ...] = BUDGETS
    # The continuation segment after holds; 0 is the run's own generation.
    segment: int = 0
    # Team-admitted evidence proved the dispatched operation had no business effect, which permits one retry of it.
    absent: bool = False
    # The dispatched operation came from an earlier segment: this segment may only retry it, under the same logical
    # operation, after proven absence.
    carried: bool = False
    # How the dispatched operation's last attempt failed, as Team classified it; see FAULTS.
    fault: str = ""
    # The workload the last attempt was dispatched to, and when, so recovery can prove it stopped since (ADR-0092).
    workload: str = ""
    dispatched_at: int = 0
    # The shown step's result as its notice shows it, and the keyed digest of its safe form, or None when no step
    # shown so far: {"step", "output", "digest"}; it outlives later steps, holds, and continuations.
    shown: dict[str, object] | None = None
    # The Team boot the run's protection was bound in (ADR-0101 section 6.2), and whether it was lost; never regained.
    boot: str = ""
    protection_lost: bool = False
    # A decide plan's phase, its accumulated kept results {"results": [[step, kept]], "over"}, the sealed candidate
    # that holds them, its allowance and what it used, its calls, and the model its decision is bound to.
    phase: str = "replay"
    accumulator: dict[str, object] | None = None
    candidate: str | None = None
    reservation: tuple[int, int] = (0, 0)
    calls: tuple[Call, ...] = ()
    model: dict[str, str] | None = None

    @property
    def generation_suffix(self) -> str:
        return f"s{self.segment}" if self.segment else ""

    def remaining(self, budget: str) -> int:
        return dict(self.budgets)[budget]

    def selections(self) -> dict[routine_plan.Key, object]:
        return {tuple(item[:4]): item[4] for item in self.selected}

    def done(self, plan: routine_plan.Plan) -> bool:
        """Whether the run is complete: every step replayed, and for a decide plan its decision closed."""
        if plan.output["mode"] == "decide":
            return self.phase == "closed"
        return self.step == len(plan.steps)

    def replayed(self, plan: routine_plan.Plan) -> bool:
        """Whether every replay step completed."""
        return self.step == len(plan.steps)


def start(plan: routine_plan.Plan, binding: Binding, started_at: int, boot: str) -> Cursor:
    """A run's first cursor, at its first step with every budget whole, its protection bound in this Team boot."""
    return _checked(Cursor(binding, plan.digest, started_at, boot=boot))


def lose_protection(cursor: Cursor) -> Cursor:
    """Record that the run's protection was lost; it is never regained (ADR-0101 section 6.2)."""
    return _checked(dataclasses.replace(cursor, protection_lost=True))


def dispatch(
    cursor: Cursor,
    plan: routine_plan.Plan,
    operation_id: str,
    commitment: str,
    *,
    workload: str = "",
    dispatched_at: int = 0,
) -> Cursor:
    """Record one dispatch of the current step before its RPC: its logical operation and exact resolved input.

    A repeated dispatch of the same step keeps its logical operation and must carry the same input commitment, so a
    replay or permitted retry never changes the business payload of an operation. The attempt's workload and instant
    are kept with it; an unknown workload can never be proven stopped.
    """
    _same_plan(cursor, plan)
    if cursor.replayed(plan) or not action_journal.valid_operation_id(operation_id):
        raise CursorError("cursor-dispatch-invalid")
    if not isinstance(commitment, str) or _HEX64_RE.fullmatch(commitment) is None:
        raise CursorError("cursor-dispatch-invalid")
    if cursor.operation_id is not None and (cursor.operation_id, cursor.commitment) != (operation_id, commitment):
        raise CursorError("cursor-operation-changed")
    return _checked(
        dataclasses.replace(
            cursor,
            operation_id=operation_id,
            attempts=cursor.attempts + 1,
            commitment=commitment,
            fault="",
            workload=workload,
            dispatched_at=dispatched_at,
        )
    )


def failed(cursor: Cursor, fault: str) -> Cursor:
    """Record Team's classification of the dispatched operation's failed attempt before the segment unwinds."""
    if cursor.operation_id is None or fault not in FAULTS[1:]:
        raise CursorError("cursor-not-dispatched")
    return _checked(dataclasses.replace(cursor, fault=fault))


def complete(cursor: Cursor, plan: routine_plan.Plan, result: object, shown: dict[str, object] | None = None) -> Cursor:
    """Advance past the current step, keeping only the values later steps select from its result.

    The plan's shown step also keeps ``shown``, its bounded result and comparison digest; no other step may.
    """
    _same_plan(cursor, plan)
    if cursor.replayed(plan) or cursor.operation_id is None:
        raise CursorError("cursor-not-dispatched")
    step_id = plan.steps[cursor.step].step_id
    try:
        chosen = routine_plan.selections(plan, step_id, result)
    except routine_plan.PlanError as exc:
        raise CursorError(exc.code) from exc
    shown_step = plan.shown()
    if (shown is not None) != (shown_step is not None and shown_step.step_id == step_id) or (
        shown is not None
        and (
            not isinstance(shown, dict)
            or shown.get("step") != step_id
            or not isinstance(shown.get("output"), dict)
            # Its output names the same step by position, as the wire does.
            or shown["output"].get("step") != cursor.step + 1
        )
    ):
        raise CursorError("cursor-shown-invalid")
    selected = (*cursor.selected, *((step_id, *key, value) for key, value in sorted(chosen.items())))
    kept = cursor.shown if shown is None else shown
    return _checked(
        dataclasses.replace(
            cursor,
            step=cursor.step + 1,
            operation_id=None,
            attempts=0,
            commitment=None,
            selected=selected,
            absent=False,
            carried=False,
            fault="",
            workload="",
            dispatched_at=0,
            shown=kept,
        )
    )


def proven_absent(cursor: Cursor) -> Cursor:
    """Record Team-admitted proof that the dispatched operation had no business effect; a policy fault never has it."""
    if cursor.operation_id is None:
        raise CursorError("cursor-not-dispatched")
    if cursor.fault == "policy":
        raise CursorError("cursor-policy-hold")
    return _checked(dataclasses.replace(cursor, absent=True))


def retry(cursor: Cursor) -> Cursor:
    """Permit the one retry of the dispatched operation, under the same logical operation, only after proven absence.

    The retry is consumed here, before its dispatch, and the proof is spent with it.
    """
    if cursor.operation_id is None or not cursor.absent:
        raise CursorError("cursor-operation-uncertain")
    return _checked(dataclasses.replace(spend(cursor, "retries", 1), absent=False))


def continued(cursor: Cursor) -> Cursor:
    """The cursor of the run's next continuation segment, after a hold archived the current generation."""
    if cursor.segment >= MAX_SEGMENTS:
        raise CursorError("cursor-segments-exhausted")
    return _checked(dataclasses.replace(cursor, segment=cursor.segment + 1, carried=cursor.operation_id is not None))


def spend(cursor: Cursor, budget: str, amount: int) -> Cursor:
    """Consume part of one recovery budget; exhaustion is refused, never borrowed."""
    remaining = dict(cursor.budgets)
    if budget not in remaining or type(amount) is not int or not 0 < amount <= remaining[budget]:
        raise CursorError("cursor-budget-exhausted")
    remaining[budget] -= amount
    return _checked(dataclasses.replace(cursor, budgets=tuple(sorted(remaining.items()))))


def refund(cursor: Cursor, budget: str, amount: int) -> Cursor:
    """Return part of a reservation that was not used; never beyond the budget's initial bound."""
    remaining = dict(cursor.budgets)
    if budget not in remaining or type(amount) is not int or amount <= 0:
        raise CursorError("cursor-budget-invalid")
    remaining[budget] += amount
    return _checked(dataclasses.replace(cursor, budgets=tuple(sorted(remaining.items()))))


def _same_plan(cursor: Cursor, plan: routine_plan.Plan) -> None:
    if cursor.plan != plan.digest:
        raise CursorError("cursor-plan-changed")


def _document(cursor: Cursor) -> dict[str, object]:
    binding = cursor.binding
    return {
        "version": VERSION,
        "incarnation": binding.incarnation,
        "routine_id": binding.routine_id,
        "revision": binding.revision,
        "run_id": binding.run_id,
        "plan": cursor.plan,
        "started_at": cursor.started_at,
        "step": cursor.step,
        "operation_id": cursor.operation_id,
        "attempts": cursor.attempts,
        "commitment": cursor.commitment,
        "selected": [list(item) for item in cursor.selected],
        "budgets": dict(cursor.budgets),
        "segment": cursor.segment,
        "absent": cursor.absent,
        "carried": cursor.carried,
        "fault": cursor.fault,
        "workload": cursor.workload,
        "dispatched_at": cursor.dispatched_at,
        "shown": cursor.shown,
        "boot": cursor.boot,
        "protection_lost": cursor.protection_lost,
        "phase": cursor.phase,
        "accumulator": cursor.accumulator,
        "candidate": cursor.candidate,
        "reservation": {"allowance": cursor.reservation[0], "used": cursor.reservation[1]},
        "calls": [_call_document(call) for call in cursor.calls],
        "model": cursor.model,
    }


def _call_document(call: Call) -> dict[str, object]:
    return {name: getattr(call, name) for name in sorted(_CALL_FIELDS)}


def encode(cursor: Cursor) -> bytes:
    """The canonical plaintext a store seals; at most 256 KiB, with its selections within their own bound."""
    return routine_plan.canonical(_document(_checked(cursor)))


def decode(raw: bytes, binding: Binding) -> Cursor:
    """Admit one opened cursor only for exactly the binding its seal was opened under."""
    try:
        value = strict_json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise CursorError("cursor-invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value) != _FIELDS
        or type(value["version"]) is not int
        or value["version"] != VERSION
    ):
        raise CursorError("cursor-invalid")
    selected, budgets, reservation = value["selected"], value["budgets"], value["reservation"]
    if not isinstance(selected, list) or not all(isinstance(item, list) and len(item) == 5 for item in selected):
        raise CursorError("cursor-invalid")
    if not isinstance(budgets, dict) or not isinstance(reservation, dict) or set(reservation) != {"allowance", "used"}:
        raise CursorError("cursor-invalid")
    calls = value["calls"]
    if not isinstance(calls, list) or not all(isinstance(item, dict) and set(item) == _CALL_FIELDS for item in calls):
        raise CursorError("cursor-invalid")
    cursor = Cursor(
        Binding(value["incarnation"], value["routine_id"], value["revision"], value["run_id"]),
        value["plan"],
        value["started_at"],
        value["step"],
        value["operation_id"],
        value["attempts"],
        value["commitment"],
        tuple(tuple(item) for item in selected),
        tuple(sorted(budgets.items())),
        value["segment"],
        value["absent"],
        value["carried"],
        value["fault"],
        value["workload"],
        value["dispatched_at"],
        value["shown"],
        value["boot"],
        value["protection_lost"],
        value["phase"],
        value["accumulator"],
        value["candidate"],
        (reservation["allowance"], reservation["used"]),
        tuple(Call(**item) for item in calls),
        value["model"],
    )
    if cursor.binding != binding or routine_plan.canonical(_document(cursor)) != raw:
        raise CursorError("cursor-invalid")
    return _checked(cursor)


def _checked(cursor: Cursor) -> Cursor:
    """Refuse any cursor outside its closed shape and bounds."""
    dispatched = cursor.operation_id is not None
    valid = (
        binding_valid(cursor.binding)
        and isinstance(cursor.plan, str)
        and _DIGEST_RE.fullmatch(cursor.plan) is not None
        and type(cursor.started_at) is int
        and cursor.started_at >= 0
        and type(cursor.step) is int
        and 0 <= cursor.step <= routine_plan.MAX_STEPS
        and (not dispatched or action_journal.valid_operation_id(cursor.operation_id))
        and type(cursor.attempts) is int
        and (cursor.attempts >= 1 if dispatched else cursor.attempts == 0)
        and (
            isinstance(cursor.commitment, str) and _HEX64_RE.fullmatch(cursor.commitment) is not None
            if dispatched
            else cursor.commitment is None
        )
        and _budgets_valid(cursor.budgets)
        and _selections_valid(cursor.selected)
        and type(cursor.segment) is int
        and 0 <= cursor.segment <= MAX_SEGMENTS
        and type(cursor.absent) is bool
        and type(cursor.carried) is bool
        and (dispatched or not (cursor.absent or cursor.carried))
        and isinstance(cursor.fault, str)
        and cursor.fault in FAULTS
        and (dispatched or cursor.fault == "")
        and not (cursor.absent and cursor.fault == "policy")
        and isinstance(cursor.workload, str)
        and (cursor.workload == "" or _WORKLOAD_RE.fullmatch(cursor.workload) is not None)
        and type(cursor.dispatched_at) is int
        and cursor.dispatched_at >= 0
        and (dispatched or (cursor.workload, cursor.dispatched_at) == ("", 0))
        and _shown_valid(cursor.shown)
        and isinstance(cursor.boot, str)
        and _ID_RE.fullmatch(cursor.boot) is not None
        and type(cursor.protection_lost) is bool
        and _decision_valid(cursor)
    )
    if not valid:
        raise CursorError("cursor-invalid")
    if len(routine_plan.canonical(_document(cursor))) > MAX_CURSOR_BYTES:
        raise CursorError("cursor-too-large")
    return cursor


def _decision_valid(cursor: Cursor) -> bool:
    """A decide plan's phase and its parts: replay holds no decision call, and only a decision holds a candidate."""
    allowance, used = cursor.reservation if isinstance(cursor.reservation, tuple) else (None, None)
    return (
        cursor.phase in PHASES
        and _accumulator_valid(cursor.accumulator)
        and (cursor.phase == "replay" or cursor.accumulator is not None)
        and (cursor.candidate is None or (isinstance(cursor.candidate, str) and _ID_RE.fullmatch(cursor.candidate)))
        and (cursor.phase != "replay" or (cursor.candidate is None and cursor.calls == () and cursor.model is None))
        and type(allowance) is int
        and type(used) is int
        and 0 <= used <= allowance <= http_routine.MAX_ALLOWANCE
        and isinstance(cursor.calls, tuple)
        and len(cursor.calls) <= used
        and all(_call_valid(call) for call in cursor.calls)
        and len({call.operation_id for call in cursor.calls}) == len(cursor.calls)
        and (cursor.model is None or http_routine.canonical_model(cursor.model) == cursor.model)
    )


def _accumulator_valid(value: object) -> bool:
    """None, or the kept replay results by step with whether they outgrew the decision's input bound."""
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {"results", "over"} or type(value["over"]) is not bool:
        return False
    results = value["results"]
    return (
        isinstance(results, list)
        and len(results) <= routine_plan.MAX_STEPS
        and all(
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], str)
            and routine_plan.STEP_ID_RE.fullmatch(item[0]) is not None
            and _kept_valid(item[1])
            for item in results
        )
        and len({item[0] for item in results}) == len(results)
        and (value["over"] or len(routine_plan.canonical(results)) <= MAX_ACCUMULATOR_BYTES)
        and (not value["over"] or results == [])
    )


def _kept_valid(value: object) -> bool:
    """One kept result: its exact JSON and the sorted pointers it withholds."""
    return (
        isinstance(value, dict)
        and set(value) == {"value", "withheld"}
        and isinstance(value["withheld"], list)
        and all(routine_plan.pointer_tokens(item) is not None for item in value["withheld"])
        and value["withheld"] == sorted(set(value["withheld"]))
    )


def _call_valid(call: object) -> bool:
    """One decision call: reserved without a workload, dispatched with one, a failure classified, absent after one."""
    if not isinstance(call, Call):
        return False
    dispatched = call.state != "reserved"
    return (
        action_journal.valid_operation_id(call.operation_id)
        and http_identifiers.canonical_assistant_id(call.assistant) is not None
        and http_identifiers.canonical_action_id(call.action) is not None
        and type(call.read_only) is bool
        and isinstance(call.commitment, str)
        and _HEX64_RE.fullmatch(call.commitment) is not None
        and type(call.attempts) is int
        and 1 <= call.attempts <= http_routine.MAX_DIAGNOSTIC_ATTEMPTS
        and call.state in CALL_STATES
        and call.fault in FAULTS
        and (call.fault != "") == (call.state == "failed")
        and isinstance(call.workload, str)
        and (call.workload == "" or _WORKLOAD_RE.fullmatch(call.workload) is not None)
        and type(call.dispatched_at) is int
        and call.dispatched_at >= 0
        and ((call.workload, call.dispatched_at) == ("", 0)) == (not dispatched)
        and (not dispatched or call.dispatched_at > 0)
        and type(call.absent) is bool
        and (not call.absent or (call.state == "failed" and call.fault != "policy"))
    )


def binding_valid(binding: object) -> bool:
    """Whether a binding names one Team incarnation, one Routine revision, and one run."""
    return (
        isinstance(binding, Binding)
        and isinstance(binding.incarnation, str)
        and _HEX64_RE.fullmatch(binding.incarnation) is not None
        and isinstance(binding.routine_id, str)
        and _ID_RE.fullmatch(binding.routine_id) is not None
        and type(binding.revision) is int
        and 1 <= binding.revision < 2**31
        and isinstance(binding.run_id, str)
        and _ID_RE.fullmatch(binding.run_id) is not None
    )


def _shown_valid(shown: object) -> bool:
    """No shown result, or one step's shown output with its keyed digest, or no digest when it was too large."""
    if shown is None:
        return True
    if not isinstance(shown, dict) or set(shown) != _SHOWN_FIELDS:
        return False
    output, digest = http_routine.canonical_output(shown["output"]), shown["digest"]
    return (
        output is not None
        and isinstance(shown["step"], str)
        and routine_plan.STEP_ID_RE.fullmatch(shown["step"]) is not None
        and output["state"] in ("shown", "unavailable")
        and (digest is None or (isinstance(digest, str) and _HEX64_RE.fullmatch(digest) is not None))
        and (digest is None or output["state"] == "shown")
    )


def _budgets_valid(budgets: object) -> bool:
    limits = dict(BUDGETS)
    return (
        isinstance(budgets, tuple)
        and [name for name, _amount in budgets] == sorted(limits)
        and all(type(amount) is int and 0 <= amount <= limits[name] for name, amount in budgets)
    )


def _selections_valid(selected: tuple[tuple[object, ...], ...]) -> bool:
    keys = [tuple(item[:4]) for item in selected]
    return (
        all(len(item) == 5 for item in selected)
        and all(
            isinstance(step, str)
            and routine_plan.STEP_ID_RE.fullmatch(step) is not None
            and routine_plan.pointer_tokens(pointer) is not None
            and isinstance(where, str)
            and isinstance(item, str)
            and (where != "" or item == "")
            and routine_plan.pointer_tokens(item) is not None
            for step, pointer, where, item in keys
        )
        and len(set(keys)) == len(keys)
        and routine_plan.retained_within(dict(zip(keys, (item[4] for item in selected), strict=True)))
    )
