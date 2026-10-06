"""A recorded Routine's card and its one-tap confirmation, the only standing authority (ADR-0101 section 5).

When a recording turn ends with the chat agent's ``record`` call, Team builds the plan from its own trace of that turn
(``routine/recording.py``), admits it against the Team's exact current contracts and budgets, and keeps a proposal in
memory, one per Team and person, for 15 minutes: the candidate definition, the Team incarnation and person it is bound
to, and the confirmation card the reply carries, which shows every literal complete, every source and selector, the
schedule, the output, and every permitted Action. A recording that cannot become a Routine answers a refusal code
instead; the work the turn already did stands.

Only an authenticated Local Supervisor session confirms or cancels a card. Confirming consumes it first, then rechecks
the person, the incarnation, every pin, the budgets, and a replaced Routine's revision, and commits the Routine with its
confirmation record in one write; a repeated confirmation answers with the Routine that record names. Cancelling, a
newer card, the replaced Routine's deletion, the Team's deletion or reset, and a Team restart drop a card.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus

from local import audit as local_audit
from local.errors import ApiProblemError as ApiProblem
from local.routine import contracts as routine_contracts
from local.routine import state as routine_state
from local.validation import validate_team_id
from protocol.http.v1 import routine as http_routine
from routine import definition as routine_definition
from routine import plan as routine_plan
from routine import record, trace
from routine import recording as routine_recording

PROPOSAL_SECONDS = 15 * 60
# The record call names the Routine and what each run does with its result; when it runs and in which zone are the
# person's own words, which Team reads (ADR-0101).
_OUTCOME_FIELDS = frozenset({"op", "name", "output", "notes", "decide_actions", "replaces", "turn_date"})
_DATE_TEXT = 10


@dataclass(frozen=True, slots=True)
class Proposal:
    """One card a person may confirm once: the candidate Routine and everything it is bound to."""

    proposal_id: str
    team_id: str
    principal: str
    incarnation: str
    # The candidate definition, scheduled, with no confirmation yet.
    routine: record.Routine
    replaces: str | None
    expected_revision: int | None
    view: dict[str, object]
    digest: str
    expires_at: float


class ProposalBook:
    """Every Team's open cards, one per person, each consumed by its first confirmation."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._guard = threading.Lock()
        self._cards: dict[tuple[str, str], Proposal] = {}

    def deadline(self) -> float:
        return self._now() + PROPOSAL_SECONDS

    def put(self, proposal: Proposal) -> None:
        """Keep a card, replacing the person's earlier one."""
        with self._guard:
            self._cards[(proposal.team_id, proposal.principal)] = proposal

    def take(self, team_id: str, proposal_id: str, principal: str) -> Proposal | None:
        """The person's live card with exactly this id, consumed; None when unknown, expired, or someone else's."""
        with self._guard:
            found = self._cards.get((team_id, principal))
            if found is None or found.proposal_id != proposal_id:
                return None
            del self._cards[(team_id, principal)]
        return found if found.expires_at > self._now() else None

    def revoke(self, team_id: str, proposal_id: str, principal: str) -> None:
        """Drop the person's card with this id; an absent card is already revoked."""
        self.take(team_id, proposal_id, principal)

    def drop_routine(self, team_id: str, routine_id: str) -> None:
        """Drop every card that would replace a Routine being deleted."""
        with self._guard:
            for key, card in list(self._cards.items()):
                if key[0] == team_id and card.replaces == routine_id:
                    del self._cards[key]

    def drop(self, team_id: str) -> None:
        with self._guard:
            for key in [key for key in self._cards if key[0] == team_id]:
                del self._cards[key]

    def clear(self) -> None:
        with self._guard:
            self._cards.clear()


class RefusedError(Exception):
    """A recording that cannot become a Routine; ``code`` is the stable reason the reply carries."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _problem(status: HTTPStatus, message: str, code: str) -> ApiProblem:
    return ApiProblem(status, message, code=code)


def _brain_failed() -> ApiProblem:
    return _problem(HTTPStatus.BAD_GATEWAY, "Brain could not complete the Team turn", "brain-runtime-failed")


def _outcome(value: object) -> dict[str, object]:
    """The chat agent's ``record`` call exactly as Brain reports it; anything else is a Brain contract failure."""
    output = value.get("output") if isinstance(value, dict) else None
    valid = (
        isinstance(value, dict)
        and set(value) == _OUTCOME_FIELDS
        and value["op"] == "record"
        and http_routine.canonical_name(value["name"]) == value["name"]
        and isinstance(output, dict)
        and set(output) == {"mode", "when"}
        and output["mode"] in http_routine.OUTPUT_MODES
        and (output["when"] is None or output["when"] in http_routine.DECISION_WHEN)
        and isinstance(value["notes"], str)
        and isinstance(value["decide_actions"], list)
        and (value["replaces"] is None or http_routine.ROUTINE_ID_RE.fullmatch(str(value["replaces"])) is not None)
        and (
            value["turn_date"] is None
            or (isinstance(value["turn_date"], str) and len(value["turn_date"]) == _DATE_TEXT)
        )
    )
    if not valid:
        raise _brain_failed()
    return value


def chat_routines(self, team_id: str) -> tuple[dict[str, object], ...]:
    """The Team's Routines as data for the Brain: enough to name one and to see its steps; never an input value."""
    state = routine_state.load(self, team_id)
    return tuple(
        {
            "routine_id": item.routine_id,
            "name": item.name,
            "schedule": dict(item.schedule),
            "timezone": item.timezone,
            "timezone_source": item.timezone_source,
            "revision": item.revision,
            "daily_steps": routine_definition.daily_steps(item),
            "output": {"mode": item.plan["output"]["mode"], "when": item.plan["output"]["when"]},
            "steps": [
                {
                    "id": step["id"],
                    "assistant": step["assistant"],
                    "action": step["action"],
                    "inputs": sorted(step["input"]),
                }
                for step in item.plan["steps"]
            ],
        }
        for item in state.routines
        if not item.deleting
    )


def routine_capacity(self, team_id: str) -> int:
    """The daily Action units the Team leaves a new Routine, after every Routine's allocation, paused ones included."""
    return routine_definition.capacity(routine_state.load(self, team_id).routines)


def _recording(self, response: object):
    """The turn's recording span, still the same person's, usable, and still protected."""
    found = self.routine_recordings.get(response.team_id, response.recording)
    if found is None or found.principal != local_audit.human_principal() or found.protection.lost:
        raise RefusedError("routine-recording-unavailable")
    if found.refused:
        raise RefusedError(found.refused)
    return found


def _current(self, response: object, recording) -> tuple[object, ...]:
    """The Team's Assistants exactly as the turn saw them, in the incarnation the recording began in."""
    setup = self._chat_setup(response.team_id, list(response.file_ids), response.provider, response.assistant_ids)
    if self._chat_identity(*setup) != response.segment.identity or setup[1] != recording.incarnation:
        raise RefusedError("team-context-changed")
    return setup[2]


def _replaced(state: record.TeamRoutines, routine_id: str | None, recording) -> record.Routine | None:
    """The Routine a recording replaces, only one the turn was shown and only at the revision it was shown."""
    if routine_id is None:
        return None
    shown = dict(recording.revisions).get(routine_id)
    found = next((item for item in state.routines if item.routine_id == routine_id and not item.deleting), None)
    if found is None or shown is None:
        raise RefusedError("routine-not-found")
    if found.revision != shown:
        raise RefusedError("routine-revision-changed")
    return found


def _recorded(outcome, recording, contracts, existing: record.Routine | None):
    """The recorded plan with its origins, permitted Actions, schedule, and zone, or the question to ask first."""
    mode = outcome["output"]["mode"]
    if mode == "decide" or outcome["notes"] or outcome["decide_actions"]:
        # Decisions come with their own slice; nothing here admits one yet.
        raise RefusedError("routine-recording-invalid")
    choice = routine_recording.Recording(mode, None, ())
    kept = None
    if existing is not None:
        kept = routine_recording.Existing(existing.plan, existing.schedule, existing.timezone, existing.timezone_source)
    try:
        return routine_recording.record(
            recording.sends, choice, recording.protection, contracts, asked=recording.asked, existing=kept
        )
    except routine_recording.RecordingError as exc:
        raise RefusedError(exc.code) from exc


def _room(candidate: record.Routine, others: tuple[record.Routine, ...]) -> routine_recording.Question:
    """What to ask when the stated schedule outgrows the Team's daily steps: the shortest interval that fits, if any."""
    fits = routine_definition.capacity(others) // routine_definition.run_units(candidate)
    if fits < 1:
        return routine_recording.Question("routine-no-room")
    shortest = max(http_routine.MIN_CONTINUOUS_GAP_SECONDS, -(-http_routine.DAY_SECONDS // fits))
    return routine_recording.Question("routine-interval-over-budget", value=shortest)


class _AskedError(Exception):
    def __init__(self, question: routine_recording.Question, protected: frozenset[str]) -> None:
        super().__init__(question.code)
        self.question = question
        # What the span protects as it asks, which nothing in the question may hold.
        self.protected = protected


def _candidate(self, response: object, outcome: dict[str, object]) -> tuple:
    """The candidate Routine a recording defines, admitted against the Team's current contracts and budgets."""
    recording = _recording(self, response)
    contracts = routine_contracts.contracts(_current(self, response, recording))
    state = routine_state.load(self, response.team_id)
    existing = _replaced(state, outcome["replaces"], recording)
    recorded = _recorded(outcome, recording, contracts, existing)
    if isinstance(recorded, routine_recording.Question):
        raise _AskedError(recorded, recording.protection.values)
    if not all(item["read_only"] for item in recorded.permitted):
        # Routines that change something come with rehearsal, in their own slice.
        raise RefusedError("routine-mutation-unavailable")
    try:
        routine_plan.admit(recorded.document, contracts)
    except routine_plan.PlanError as exc:
        raise RefusedError(exc.code) from exc
    scopes = dict(response.segment.contracts)
    routine_id = record.new_id() if existing is None else existing.routine_id
    others = tuple(item for item in state.routines if item.routine_id != routine_id)
    candidate = record.Routine(
        routine_id,
        outcome["name"],
        dict(recorded.schedule),
        recorded.timezone,
        tuple(
            sorted((assistant, scopes[assistant]) for assistant in {item["assistant"] for item in recorded.permitted})
        ),
        recorded.document,
        anchor=0,
        next_run_at=0,
        permitted=recorded.permitted,
        timezone_source=recorded.timezone_source,
    )
    try:
        candidate = record.scheduled(candidate, int(time.time()))
    except record.RoutineStateError as exc:
        raise RefusedError(str(exc)) from exc
    refused = routine_definition.over_budget(others, candidate) or record.change_room(state, 1)
    if refused == "routine-step-budget":
        # The person's interval stands; Team asks rather than run it less often than they said (ADR-0101).
        raise _AskedError(_room(candidate, others), recording.protection.values)
    if refused is not None or not routine_definition.fits(candidate):
        raise RefusedError(refused or "routine-too-large")
    if existing is not None and any(item.routine_id == existing.routine_id for item in state.runs):
        raise RefusedError("routine-busy")
    return recording, candidate, recorded, existing


def _escaped_json(value: object) -> str:
    return http_routine.escaped(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _card_input(member: str, source: dict[str, object], origin: str, positions: dict[str, int]) -> dict[str, object]:
    empty = {"value": None, "step": None, "pointer": None, "where": None, "item": None}
    if source["kind"] == "literal":
        return {"member": member, "origin": origin, **empty, "value": _escaped_json(source["value"])}
    if source["kind"] == "run_clock":
        return {"member": member, "origin": "clock", **empty}
    where = source.get("where")
    shown = None
    if where is not None:
        ((key, constant),) = where.items()
        shown = {"member": key, "value_json": http_routine.where_text(constant)}
    return {
        "member": member,
        "origin": origin,
        **empty,
        "step": positions[source["step"]],
        "pointer": source["pointer"],
        "where": shown,
        "item": source.get("item"),
    }


def _next_runs(value: record.Routine) -> list[str]:
    """When the Routine would first run, and for a fixed schedule the two firings after."""
    runs = [value.next_run_at]
    while len(runs) < http_routine.MAX_NEXT_RUNS and not record.continuous(value):
        runs.append(record.next_after(value, runs[-1]))
    return [_instant(item) for item in runs]


def _instant(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(int(epoch), datetime.UTC).isoformat().replace("+00:00", "Z")


def card(proposal_id: str, candidate: record.Routine, recorded, framing: tuple[str | None, bool, int]) -> dict:
    """The confirmation card: every step, every input's complete value or source, the schedule, and the scope."""
    replaces, expires_at = framing
    document = candidate.plan
    positions = {step["id"]: index for index, step in enumerate(document["steps"], start=1)}
    entries = {(item["assistant"], item["action"]): item for item in candidate.permitted}
    steps = [
        {
            "position": positions[step["id"]],
            "assistant": step["assistant"],
            "action": step["action"],
            "read_only": entries[(step["assistant"], step["action"])]["read_only"],
            "inputs": [
                _card_input(member, step["input"][member], recorded.origins[step["id"]][member], positions)
                for member in sorted(step["input"])
            ],
        }
        for step in document["steps"]
    ]
    return {
        "proposal_id": proposal_id,
        "expires_at": _instant(expires_at),
        "replaces": replaces,
        "name": candidate.name,
        "schedule": dict(candidate.schedule),
        "timezone": candidate.timezone,
        "timezone_source": candidate.timezone_source,
        "next_runs": _next_runs(candidate),
        "daily_cap": http_routine.daily_cap(candidate.schedule),
        "output": {"mode": document["output"]["mode"], "when": document["output"]["when"]},
        "steps": steps,
        "permitted": [
            {"assistant": item["assistant"], "action": item["action"], "read_only": item["read_only"]}
            for item in candidate.permitted
        ],
        "decision": None,
        "rehearsal": not all(item["read_only"] for item in candidate.permitted),
    }


def _digest(candidate: record.Routine, view: dict[str, object], expected_revision: int | None) -> str:
    """What a confirmation binds: the exact definition, the card shown, and the revision a replacement saw."""
    bound = {
        "routine_id": candidate.routine_id,
        "name": candidate.name,
        "schedule": candidate.schedule,
        "timezone": candidate.timezone,
        "timezone_source": candidate.timezone_source,
        "assistants": [list(pair) for pair in candidate.assistants],
        "plan": candidate.plan,
        "permitted": list(candidate.permitted),
        "view": view,
        "expected_revision": expected_revision,
    }
    return "sha256:" + hashlib.sha256(routine_plan.canonical(bound)).hexdigest()


def admit(self, response: object, proposed: object) -> tuple[Callable[[], None], dict[str, object]]:
    """A completed turn's ``record`` as the write that keeps its card, and the terminal field the reply carries.

    The caller holds the Team lifecycle lock and runs the write in the reply's commit, under the Stop guard. A card or a
    refusal ends the recording span; a question keeps it, with what it asked, for the person's answer.
    """
    outcome = _outcome(proposed)
    team_id, send_id = response.team_id, response.recording
    try:
        recording, candidate, recorded, existing = _candidate(self, response, outcome)
        proposal_id = record.new_id()
        expires_at = time.time() + PROPOSAL_SECONDS
        replaces = None if existing is None else existing.routine_id
        view = card(proposal_id, candidate, recorded, (replaces, expires_at))
        if http_routine.encoded_bytes(view) > http_routine.MAX_PROPOSAL_BYTES:
            raise RefusedError("routine-proposal-too-large")
        if trace.exposes(view, recording.protection.values):
            raise RefusedError("routine-secret-literal")
    except _AskedError as asking:
        question = asking.question
        if trace.exposes(question.wire(), asking.protected):
            # Nothing the span protects ever reaches the person, a question's targets included.
            return (lambda: self.routine_recordings.finish(team_id, send_id)), {
                "routine_refusal": {"code": "routine-secret-literal"}
            }
        if http_routine.canonical_question(question.wire()) is None:
            raise _problem(
                HTTPStatus.INTERNAL_SERVER_ERROR, "the Routine question is invalid", "internal-error"
            ) from asking
        return (lambda: self.routine_recordings.asked(team_id, send_id, question)), {
            "routine_question": question.wire()
        }
    except RefusedError as exc:
        return (lambda: self.routine_recordings.finish(team_id, send_id)), {"routine_refusal": {"code": exc.code}}
    if http_routine.canonical_proposal(view) != view:
        raise _problem(HTTPStatus.INTERNAL_SERVER_ERROR, "the Routine card is invalid", "internal-error")
    expected = None if existing is None else existing.revision
    proposal = Proposal(
        proposal_id,
        response.team_id,
        recording.principal,
        recording.incarnation,
        candidate,
        replaces,
        expected,
        view,
        _digest(candidate, view, expected),
        self.routine_proposals.deadline(),
    )

    def keep() -> None:
        self.routine_proposals.put(proposal)
        self.routine_recordings.finish(team_id, send_id)

    return keep, {"routine_proposal": view}


def _principal() -> str:
    principal = local_audit.human_principal()
    if principal is None:
        raise _problem(HTTPStatus.FORBIDDEN, "a person must answer a Routine card", "routine-card-person-required")
    return principal


def _answer(team_id: str, proposal_id: str, routine_id: str | None, status: str) -> dict[str, object]:
    return {"team_id": team_id, "proposal_id": proposal_id, "routine_id": routine_id, "status": status}


def _confirmed(state: record.TeamRoutines, proposal_id: str, principal: str, incarnation: str) -> record.Routine | None:
    """The Routine an earlier confirmation of this exact card committed, by its confirmation record."""
    return next(
        (
            item
            for item in state.routines
            if item.confirmation is not None
            and (item.confirmation["proposal_id"], item.confirmation["principal"], item.confirmation["incarnation"])
            == (proposal_id, principal, incarnation)
        ),
        None,
    )


def _changed() -> ApiProblem:
    return _problem(HTTPStatus.CONFLICT, "Team capabilities changed; ask again", "team-context-changed")


def _pinned(self, team_id: str, proposal: Proposal) -> None:
    """Every Assistant the card pinned still runs exactly as it was when the card was made."""
    pinned = dict(proposal.routine.assistants)
    try:
        if routine_contracts.current_contracts(self, team_id, tuple(pinned)) != pinned:
            raise _changed()
    except routine_contracts.ContractsUnavailableError as exc:
        raise routine_contracts.context_unavailable() from exc


def _commit(self, proposal: Proposal, principal: str) -> str:
    """Commit the card's Routine with its confirmation record in one write; the status it answers with."""
    now = int(time.time())
    confirmation = {
        "proposal_id": proposal.proposal_id,
        "proposal_digest": proposal.digest,
        "principal": principal,
        "incarnation": proposal.incarnation,
        "confirmed_at": now,
    }
    value = record.scheduled(dataclasses.replace(proposal.routine, confirmation=confirmation), now)

    def apply(state: record.TeamRoutines) -> tuple[record.TeamRoutines, str]:
        try:
            if proposal.replaces is None:
                return record.create(state, value, now), "created"
            return record.update(state, value, proposal.expected_revision, now), "changed"
        except record.RoutineStateError as exc:
            return state, str(exc)

    outcome = routine_state.update(self, proposal.team_id, apply)
    if outcome not in ("created", "changed"):
        raise _problem(HTTPStatus.CONFLICT, "the Team cannot hold this Routine", outcome)
    return outcome


def confirm(self, team_id: str, proposal_id: str) -> dict[str, object]:
    """Criar rotina: consume the person's card, recheck everything it is bound to, and commit its Routine."""
    team_id, principal = validate_team_id(team_id), _principal()
    with self._lock(team_id):
        proposal = self.routine_proposals.take(team_id, proposal_id, principal)
        incarnation = self.assistant_lifecycle._network(team_id).id
        if proposal is None:
            found = _confirmed(routine_state.load(self, team_id), proposal_id, principal, incarnation)
            if found is None:
                raise _problem(HTTPStatus.CONFLICT, "this Routine card expired; ask again", "routine-proposal-expired")
            return _answer(team_id, proposal_id, found.routine_id, "created" if found.revision == 1 else "changed")
        if proposal.incarnation != incarnation:
            raise _changed()
        _pinned(self, team_id, proposal)
        status = _commit(self, proposal, principal)
    local_audit.record_request(
        "routine-proposal", result="ok", team_id=team_id, detail=f"{status}:{proposal.routine.routine_id}"
    )
    return _answer(team_id, proposal_id, proposal.routine.routine_id, status)


def revoke(self, team_id: str, proposal_id: str) -> dict[str, object]:
    """Cancelar: drop the person's card; one already gone is already revoked."""
    team_id, principal = validate_team_id(team_id), _principal()
    self.routine_proposals.revoke(team_id, proposal_id, principal)
    local_audit.record_request("routine-proposal", result="ok", team_id=team_id, detail=f"revoked:{proposal_id}")
    return _answer(team_id, proposal_id, None, "revoked")
