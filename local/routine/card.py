"""The recovery card of a held Routine run: Rodar and Excluir (ADR-0092 section 7, ADR-0101).

Team verifies a held run automatically; a card exists only when that could not prove what the failed call did. It
shows the call and the failure Team recorded for it, and carries an entity tag: the digest of exactly the state its
answer must still find, the Team incarnation, the run, its Routine and the revision it executed, its held generation and
operation, and the Routine's current revision. An answer names that tag, as an HTTP If-Match names the entity it
expects (RFC 9110 section 13.1.1); Team answers it only when the state it finds in the Team's execution slot has exactly
that tag, refuses it as stale otherwise, and checks the same state again in the answer's own write. Once answered, the
run is no longer held, so no answer applies twice. Team keeps no card in memory.

Rodar sets the held run aside without verifying it and requests one fresh run of the current revision; the call may
already have acted, so it may act again. Excluir is not a card answer: it is the Routine's own deletion, confirmed with
the Supervisor's password and second factor in Admin. Rodar never starts while the held attempt's workload could still
be running, while another run of the Routine is live, or once the Routine is deleted.
"""

import hashlib
import secrets
import time
from dataclasses import dataclass
from http import HTTPStatus

from local import audit as local_audit
from local import errors as local_errors
from local.errors import ApiProblemError as ApiProblem
from local.routine import contracts as routine_contracts
from local.routine import diagnostics as routine_diagnostics
from local.routine import incident as routine_incident
from local.routine import recovery as routine_recovery
from local.routine import state as routine_state
from local.validation import validate_team_id
from protocol.http.v1 import routine_run as http_routine_run
from routine import hold as routine_hold
from routine import plan as routine_plan
from routine import record


@dataclass(frozen=True, slots=True)
class _Tagged:
    """A held run's card state as Team finds it now, and its entity tag."""

    tag: str
    # What the answer's own write checks again: the run's revision and generation, and the Routine's current revision.
    expected: routine_hold.Expected
    opened: routine_incident.OpenedRecovery
    value: record.Run


def _person() -> None:
    if local_audit.human_principal() is None:
        raise local_errors.routine_card_person_required()


def _tagged(self, team_id: str, incident_id: str) -> _Tagged:
    """The unresolved incident's card state now, and the entity tag over exactly what an answer must still find."""
    value = routine_incident.unresolved(self, team_id, incident_id)
    opened = routine_incident.open_recovery(self, team_id, incident_id)
    current = routine_hold.current_revision(routine_state.load(self, team_id), value.routine_id)
    material = [
        opened.recovery.binding.incarnation,
        incident_id,
        value.routine_id,
        value.revision,
        value.generation,
        opened.cursor.operation_id,
        current,
    ]
    tag = hashlib.sha256(routine_plan.canonical(material)).hexdigest()
    return _Tagged(tag, routine_hold.Expected(value.revision, value.generation, current), opened, value)


def _evidence(self, team_id: str, incarnation: str, incident_id: str, operation_id: str | None) -> dict[str, object]:
    """The held operation's latest recorded diagnostic, never one of an earlier operation of the run."""
    try:
        found = self.routine_diagnostics.read(team_id, incarnation, incident_id, int(time.time()))
    except routine_diagnostics.DiagnosticStoreError:
        return {"evidence": "unavailable", "diagnostic": None}
    latest = [item for item in found if operation_id is not None and item.operation_id == operation_id]
    if not latest:
        return {"evidence": "absent", "diagnostic": None}
    return {"evidence": "recorded", "diagnostic": latest[-1].view()}


def open_card(self, team_id: str, incident_id: str) -> dict[str, object]:
    """Open the recovery card of one held run for the authenticated person who will answer it."""
    team_id = validate_team_id(team_id)
    _person()
    tagged = _tagged(self, team_id, incident_id)
    opened, value = tagged.opened, tagged.value
    # The call the hold indexed, the one its incident view names.
    assistant_id, action, position, steps = routine_hold.held_step(value)
    incarnation = opened.recovery.binding.incarnation
    evidence = _evidence(self, team_id, incarnation, incident_id, opened.cursor.operation_id)
    if evidence["diagnostic"] is not None and (
        (evidence["diagnostic"]["assistant_id"], evidence["diagnostic"]["action"], evidence["diagnostic"]["position"])
        != (assistant_id, action, position)
    ):
        evidence = {"evidence": "absent", "diagnostic": None}
    return {
        "team_id": team_id,
        "run_id": incident_id,
        "assistant_id": assistant_id,
        "action": action,
        "position": position,
        "steps": steps,
        **evidence,
        "tag": tagged.tag,
    }


def restartable(self, team_id: str, tagged: _Tagged) -> record.Routine:
    """The Routine a fresh run may start for: listed, idle, and with the held attempt stopped."""
    routine_id = tagged.value.routine_id
    state = routine_state.load(self, team_id)
    current = next((item for item in state.routines if item.routine_id == routine_id), None)
    if current is None or current.deleting:
        raise ApiProblem(HTTPStatus.CONFLICT, "Routine is unavailable", code="routine-not-found")
    if any(item.routine_id == routine_id for item in record.live_runs(state)):
        raise ApiProblem(HTTPStatus.CONFLICT, "another run of this Routine is live", code="routine-busy")
    if not routine_recovery.workload_stopped(self, team_id, tagged.opened):
        raise ApiProblem(
            HTTPStatus.CONFLICT, "the held attempt may still be running", code="routine-workload-unquiesced"
        )
    return current


def _contracts_changed() -> ApiProblem:
    return ApiProblem(HTTPStatus.CONFLICT, "the Routine's Assistants changed", code="routine-contracts-changed")


def _run(self, team_id: str, tagged: _Tagged) -> None:
    """Rodar: set the held run aside and request one fresh run of the Routine's current revision."""
    current = restartable(self, team_id, tagged)
    if current.needs_reconfirm:
        raise _contracts_changed()
    routine_contracts.require_pinned(self, team_id, dict(current.assistants), _contracts_changed())
    now = int(time.time())
    incident_id = tagged.value.run_id
    routine_incident.set_aside(
        self,
        team_id,
        incident_id,
        lambda state: routine_hold.run_incident(state, incident_id, now, tagged.expected),
    )


def answer_card(self, team_id: str, incident_id: str, body: object) -> dict[str, object]:
    """Answer one recovery card with Rodar, only while the held run's state still has the card's tag."""
    team_id = validate_team_id(team_id)
    _person()
    body = http_routine_run.canonical_card_answer_request(body)
    if body is None:
        raise ApiProblem(HTTPStatus.UNPROCESSABLE_ENTITY, "a card answer is its tag", code="invalid-body")
    routine_id = routine_incident.unresolved(self, team_id, incident_id).routine_id
    # Every answer is checked and applied in the Team's execution slot, against the state its card's tag names.
    with self._exclusive_chat_turn(team_id, routine_id):
        tagged = _tagged(self, team_id, incident_id)
        if not secrets.compare_digest(tagged.tag, body["tag"]):
            raise local_errors.routine_card_stale()
        _run(self, team_id, tagged)
    local_audit.record_request("routine-card", result="ok", team_id=team_id, detail=f"{incident_id}:run")
    return {"team_id": team_id, "run_id": incident_id}
