"""Refining a recorded Routine from its chat: the selected Routine's context and the refinement's card (ADR-0122).

A recording send that selects one Routine (``routine.reference``) shows the Brain that Routine's bounded definition and
its latest run's outcome. When the chat agent then calls ``refine`` on it, Team builds the next revision from that
Routine's plan, the agent's edits, and the calls the current send traced (``routine.refine``), admits it against the
Team's exact current contracts and budgets, and keeps a card like a recording's (``local.routine.proposal``): one per
Team and person, for 15 minutes, which only the person confirms. The card shows each change beside the complete
resulting plan. It is refused for a revision that is no longer current or while a run of the Routine is live; its
confirmation, which checks both again, keeps the Routine's scheduling state as it is then. The Actions the send ran
stay done whatever the person answers.
"""

import dataclasses
import time
from collections.abc import Callable
from http import HTTPStatus

from inference import usage as brain_usage
from local import errors as local_errors
from local.errors import ApiProblemError as ApiProblem
from local.routine import contracts as routine_contracts
from local.routine import diagnostics as routine_diagnostics
from local.routine import proposal as routine_proposal
from local.routine import state as routine_state
from protocol.http.v1 import routine as http_routine
from protocol.http.v1 import routine_context as http_routine_context
from protocol.http.v1 import routine_proposal as http_routine_proposal
from routine import compose as routine_compose
from routine import definition as routine_definition
from routine import record, trace
from routine import recording as routine_recording
from routine import refine as routine_refine


def chat_selected(self, team_id: str, recording) -> tuple[dict[str, object], str] | None:
    """The selected Routine as its recording send's Brain sees it, and its zone; None when it cannot be shown.

    Only a Routine the Team still lists, as the protocol admits its definition within its bound (anything larger is
    not sent); its latest run's outcome is the evidence Team still holds of it, never a reconstruction.
    """
    found = next(
        (item for item in routine_state.load(self, team_id).routines if item.routine_id == recording.selected),
        None,
    )
    if found is None or found.deleting:
        return None
    last_run = routine_diagnostics.last_run(self, team_id, found.routine_id, int(time.time()))
    view = http_routine_context.canonical_selected_routine(routine_definition.selected(found, last_run))
    return None if view is None else (view, found.timezone)


def _outcome(recording, proposed: object) -> dict[str, object]:
    """The agent's ``refine`` as Brain reports it, of the Routine the send selected at the revision it was shown.

    Anything else is a Brain contract failure.
    """
    outcome = http_routine_context.canonical_refine(proposed)
    shown = dict(recording.revisions)
    if (
        outcome is None
        or outcome["routine_id"] != recording.selected
        or shown.get(outcome["routine_id"]) != outcome["base_revision"]
    ):
        raise local_errors.brain_turn_failed()
    return outcome


def _base(state: record.TeamRoutines, outcome: dict[str, object]) -> record.Routine:
    """The Routine the card refines, only at the revision the agent edited and while none of its runs is live."""
    found = next(
        (item for item in state.routines if item.routine_id == outcome["routine_id"] and not item.deleting), None
    )
    if found is None:
        raise routine_proposal.RefusedError("routine-not-found")
    if found.revision != outcome["base_revision"]:
        raise routine_proposal.RefusedError("routine-revision-changed")
    if any(item.routine_id == found.routine_id for item in record.live_runs(state)):
        raise routine_proposal.RefusedError("routine-busy")
    return found


def _refined(recording, base: record.Routine, outcome: dict[str, object], contracts) -> routine_refine.Refined:
    """The refined plan, built from the base plan, the edits, and the current send's traced calls."""
    sends = recording.sends
    try:
        calls = routine_recording.span_calls(sends, contracts)
        texts = [line for send in sends for line in (*routine_recording.segment_lines(send.person), *send.window)]
        known = routine_recording.known_texts(texts, recording.protection)
        zone = (base.timezone, base.timezone_source)
        context = routine_recording.Context(sends, calls, known, zone, None, contracts)
        current = [call for call in calls if call.send == len(sends) - 1]
        return routine_refine.refine(base.plan, outcome["edits"], context, current)
    except routine_recording.RecordingError as exc:
        raise routine_proposal.RefusedError(exc.code) from exc


def _candidate(self, response: object, recording, outcome: dict[str, object]):
    """The refined Routine, admitted against the Team's current contracts and budgets, with its base and plan."""
    contracts = routine_contracts.contracts(routine_proposal.turn_assistants(self, response, recording))
    state = routine_state.load(self, response.team_id)
    base = _base(state, outcome)
    refined = _refined(recording, base, outcome, contracts)
    actions = [(step["assistant"], step["action"]) for step in refined.document["steps"]]
    permitted = routine_compose.permitted(actions, contracts)
    scopes = dict(response.segment.contracts)
    candidate = dataclasses.replace(
        base,
        description=outcome["description"],
        plan=refined.document,
        permitted=permitted,
        assistants=tuple(sorted((assistant, scopes[assistant]) for assistant in {item[0] for item in actions})),
        confirmation=None,
    )
    others = tuple(item for item in state.routines if item.routine_id != base.routine_id)
    refused = routine_definition.over_budget(others, candidate) or record.change_room(state)
    if refused is not None or not routine_definition.fits(candidate):
        raise routine_proposal.RefusedError(refused or "routine-too-large")
    return base, candidate, refined


def card(proposal_id: str, base: record.Routine, candidate: record.Routine, refined, expires_at: float) -> dict:
    """The refinement's card: the recording card of the resulting Routine, each step's change, and what went."""
    recorded = routine_recording.Recorded(
        candidate.plan, refined.origins, candidate.permitted, candidate.schedule, candidate.timezone_source
    )
    shown = routine_proposal.card(proposal_id, candidate, recorded, (base.routine_id, expires_at))
    effects = {(item["assistant"], item["action"]): item["read_only"] for item in base.permitted}
    steps = base.plan["steps"]
    removed = [
        {
            "position": position,
            "assistant": steps[position - 1]["assistant"],
            "action": steps[position - 1]["action"],
            "read_only": effects[(steps[position - 1]["assistant"], steps[position - 1]["action"])],
            "change": change,
        }
        for position, change in refined.removed
    ]
    return {
        "proposal_id": proposal_id,
        "expires_at": shown["expires_at"],
        "routine_id": base.routine_id,
        "base_revision": base.revision,
        "name": candidate.name,
        "description": candidate.description,
        "schedule": shown["schedule"],
        "timezone": shown["timezone"],
        "timezone_source": shown["timezone_source"],
        "output": shown["output"],
        "last_changed": refined.last_changed,
        "steps": [
            {**step, "change": change, "base": at}
            for step, (change, at) in zip(shown["steps"], refined.changes, strict=True)
        ],
        "removed": removed,
    }


def admit(
    self, response: object, proposed: object, spent: brain_usage.Spent | None
) -> tuple[Callable[[], None], dict[str, object]]:
    """A completed turn's ``refine`` as the write that keeps its card, and the terminal field the reply carries.

    As for a recording (``proposal.admit``), the caller holds the Team lifecycle lock and runs the write in the reply's
    commit; a card or a refusal ends the recording span. A refinement asks no question: what cannot become a card is
    refused with a code Admin words.
    """
    team_id, send_id = response.team_id, response.recording
    try:
        recording = routine_proposal.live_recording(self, response)
        outcome = _outcome(recording, proposed)
        base, candidate, refined = _candidate(self, response, recording, outcome)
        proposal_id = record.new_id()
        view = card(proposal_id, base, candidate, refined, time.time() + routine_proposal.PROPOSAL_SECONDS)
        if http_routine.encoded_bytes(view) > http_routine_proposal.MAX_PROPOSAL_BYTES:
            raise routine_proposal.RefusedError("routine-proposal-too-large")
        if trace.exposes(view, recording.protection.values):
            raise routine_proposal.RefusedError("routine-secret-literal")
        if not routine_proposal.description_agrees(view):
            raise routine_proposal.RefusedError("routine-description-conflict")
    except routine_proposal.RefusedError as exc:
        return (lambda: self.routine_recordings.finish(team_id, send_id)), {"routine_refusal": {"code": exc.code}}
    if http_routine_proposal.canonical_refinement(view) != view:
        raise ApiProblem(HTTPStatus.INTERNAL_SERVER_ERROR, "the Routine card is invalid", code="internal-error")
    proposal = routine_proposal.Proposal(
        proposal_id,
        team_id,
        recording.principal,
        recording.incarnation,
        candidate,
        base.routine_id,
        base.revision,
        routine_proposal.bound_digest(candidate, view, base.revision),
        self.routine_proposals.deadline(),
        (None, []),
        refines=True,
    )

    def keep() -> None:
        self.routine_proposals.put(proposal)
        self.routine_recordings.finish(team_id, send_id)

    return keep, {"routine_refinement": view}
