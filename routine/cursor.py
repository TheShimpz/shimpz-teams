"""The compiled cursor of one Routine run, without I/O (ADR-0092 sections 3 and 5).

A cursor binds the Team incarnation, the Routine and its revision, the run, the exact plan (whose digest covers every
step's complete pin), the run's one start instant, the current step, that step's logical ``operation_id``, attempts,
and resolved-input commitment, the values later steps selected from completed ones, and the remaining recovery
budgets. A completed step is never run again: the successful prefix is durable even when a later step fails. Restart
never replenishes a budget, and no secret, human response, or complete output is ever held here.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass

from action import journal as action_journal
from core import strict_json
from routine import plan as routine_plan

VERSION = 1
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
_FIELDS = frozenset(
    {
        "version",
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

    @property
    def generation_suffix(self) -> str:
        return f"s{self.segment}" if self.segment else ""

    def remaining(self, budget: str) -> int:
        return dict(self.budgets)[budget]

    def selections(self) -> dict[tuple[str, str], object]:
        return {(step, pointer): value for step, pointer, value in self.selected}

    def done(self, plan: routine_plan.Plan) -> bool:
        return self.step == len(plan.steps)


def start(plan: routine_plan.Plan, binding: Binding, started_at: int) -> Cursor:
    """A run's first cursor, at its first step with every budget whole."""
    return _checked(Cursor(binding, plan.digest, started_at))


def dispatch(cursor: Cursor, plan: routine_plan.Plan, operation_id: str, commitment: str) -> Cursor:
    """Record one dispatch of the current step before its RPC: its logical operation and exact resolved input.

    A repeated dispatch of the same step keeps its logical operation and must carry the same input commitment, so a
    replay or permitted retry never changes the business payload of an operation.
    """
    _same_plan(cursor, plan)
    if cursor.done(plan) or not action_journal.valid_operation_id(operation_id):
        raise CursorError("cursor-dispatch-invalid")
    if not isinstance(commitment, str) or _HEX64_RE.fullmatch(commitment) is None:
        raise CursorError("cursor-dispatch-invalid")
    if cursor.operation_id is not None and (cursor.operation_id, cursor.commitment) != (operation_id, commitment):
        raise CursorError("cursor-operation-changed")
    return _checked(
        dataclasses.replace(
            cursor, operation_id=operation_id, attempts=cursor.attempts + 1, commitment=commitment, fault=""
        )
    )


def failed(cursor: Cursor, fault: str) -> Cursor:
    """Record Team's classification of the dispatched operation's failed attempt before the segment unwinds."""
    if cursor.operation_id is None or fault not in FAULTS[1:]:
        raise CursorError("cursor-not-dispatched")
    return _checked(dataclasses.replace(cursor, fault=fault))


def complete(cursor: Cursor, plan: routine_plan.Plan, result: object) -> Cursor:
    """Advance past the current step, keeping only the values later steps select from its result."""
    _same_plan(cursor, plan)
    if cursor.done(plan) or cursor.operation_id is None:
        raise CursorError("cursor-not-dispatched")
    step_id = plan.steps[cursor.step].step_id
    try:
        chosen = routine_plan.selections(plan, step_id, result)
    except routine_plan.PlanError as exc:
        raise CursorError(exc.code) from exc
    selected = (*cursor.selected, *((step_id, pointer, value) for pointer, value in sorted(chosen.items())))
    advanced = Cursor(cursor.binding, cursor.plan, cursor.started_at, cursor.step + 1, selected=selected)
    return _checked(dataclasses.replace(advanced, budgets=cursor.budgets, segment=cursor.segment))


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
        "selected": [[step, pointer, value] for step, pointer, value in cursor.selected],
        "budgets": dict(cursor.budgets),
        "segment": cursor.segment,
        "absent": cursor.absent,
        "carried": cursor.carried,
        "fault": cursor.fault,
    }


def encode(cursor: Cursor) -> bytes:
    """The canonical plaintext a store seals; at most 256 KiB, with its selections within their own bound."""
    return routine_plan.canonical(_document(_checked(cursor)))


def decode(raw: bytes, binding: Binding) -> Cursor:
    """Admit one opened cursor only for exactly the binding its seal was opened under."""
    try:
        value = strict_json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise CursorError("cursor-invalid") from exc
    if not isinstance(value, dict) or set(value) != _FIELDS or value["version"] != VERSION:
        raise CursorError("cursor-invalid")
    selected, budgets = value["selected"], value["budgets"]
    if not isinstance(selected, list) or not all(isinstance(item, list) and len(item) == 3 for item in selected):
        raise CursorError("cursor-invalid")
    if not isinstance(budgets, dict):
        raise CursorError("cursor-invalid")
    cursor = Cursor(
        Binding(value["incarnation"], value["routine_id"], value["revision"], value["run_id"]),
        value["plan"],
        value["started_at"],
        value["step"],
        value["operation_id"],
        value["attempts"],
        value["commitment"],
        tuple((item[0], item[1], item[2]) for item in selected),
        tuple(sorted(budgets.items())),
        value["segment"],
        value["absent"],
        value["carried"],
        value["fault"],
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
    )
    if not valid:
        raise CursorError("cursor-invalid")
    if len(routine_plan.canonical(_document(cursor))) > MAX_CURSOR_BYTES:
        raise CursorError("cursor-too-large")
    return cursor


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


def _budgets_valid(budgets: object) -> bool:
    limits = dict(BUDGETS)
    return (
        isinstance(budgets, tuple)
        and [name for name, _amount in budgets] == sorted(limits)
        and all(type(amount) is int and 0 <= amount <= limits[name] for name, amount in budgets)
    )


def _selections_valid(selected: tuple[tuple[str, str, object], ...]) -> bool:
    keys = [(step, pointer) for step, pointer, _value in selected]
    return (
        all(
            isinstance(step, str)
            and routine_plan.STEP_ID_RE.fullmatch(step) is not None
            and routine_plan.pointer_tokens(pointer) is not None
            for step, pointer in keys
        )
        and len(set(keys)) == len(keys)
        and routine_plan.retained_within(dict(zip(keys, (value for *_key, value in selected), strict=True)))
    )
