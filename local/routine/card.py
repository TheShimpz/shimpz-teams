"""The recovery card of a held Routine run: Rodar, Recriar, and Excluir (ADR-0092 section 7, amended 2026-10-02).

Team verifies a held run automatically; a card exists only when that could not prove what the failed step did. It
shows the step and the failure Team recorded for it, and binds itself to the authenticated person, the Team
incarnation, the Routine, its run and operation, the revision the run executed, the Routine's sealed creation source, a
fresh nonce, and a five-minute expiry. Its answer must match every one of those, once.

Rodar sets the held run aside without verifying it and requests one fresh run of the current revision; the step may
already have acted, so it may act again. Recriar compiles the Routine's exact creation message from scratch and
replaces the Routine in place as its next revision. Excluir is not a card answer: it is the Routine's own deletion,
confirmed with the Supervisor's password and second factor in Admin. Neither answer starts while the held attempt's
workload could still be running, while another run of the Routine is live, or once the Routine is deleted.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus

from local import audit as local_audit
from local.errors import ApiProblemError as ApiProblem
from local.routine import diagnostics as routine_diagnostics
from local.routine import incident as routine_incident
from local.routine import recovery as routine_recovery
from local.routine import recreate as routine_recreate
from local.routine import source as routine_source
from local.routine import state as routine_state
from local.routine import turn as routine_turn
from local.validation import validate_team_id
from protocol.http.v1 import routine as http_routine
from routine import hold as routine_hold
from routine import record

CARD_SECONDS = http_routine.CARD_SECONDS
CHOICES = http_routine.CARD_CHOICES


@dataclass(frozen=True, slots=True)
class Card:
    principal: str
    incarnation: str
    incident_id: str
    routine_id: str
    revision: int
    # The Routine's current revision when the card opened, 0 once it is deleted; an update since makes it stale.
    current: int
    # The held run's journal generation the card was opened on; a continuation and a new hold since change it.
    generation: str
    operation_id: str | None
    # The commitment of the Routine's sealed creation source, which Recriar compiles; None when it has none.
    source: str | None
    nonce: str
    expires_at: float


class CardBook:
    """The open recovery cards of every Team, each answerable once before it expires."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._cards: dict[tuple[str, str], Card] = {}

    def open(self, team_id: str, card: Card) -> None:
        with self._lock:
            self._cards[(team_id, card.incident_id)] = card

    def take(self, team_id: str, incident_id: str, nonce: object, principal: str) -> Card | None:
        """The exact card this answer names, consumed; None when it is unknown, expired, or someone else's."""
        with self._lock:
            card = self._cards.get((team_id, incident_id))
            if card is None or not secrets.compare_digest(card.nonce, nonce if isinstance(nonce, str) else ""):
                return None
            del self._cards[(team_id, incident_id)]
        return card if card.principal == principal and card.expires_at > self._now() else None

    def deadline(self) -> float:
        return self._now() + CARD_SECONDS

    def drop(self, team_id: str) -> None:
        with self._lock:
            for key in [key for key in self._cards if key[0] == team_id]:
                del self._cards[key]

    def clear(self) -> None:
        with self._lock:
            self._cards.clear()


def _problem(status: HTTPStatus, message: str, code: str) -> ApiProblem:
    return ApiProblem(status, message, code=code)


def _principal() -> str:
    principal = local_audit.human_principal()
    if principal is None:
        raise _problem(HTTPStatus.FORBIDDEN, "a person must answer a recovery card", "routine-card-person-required")
    return principal


def _current_revision(self, team_id: str, routine_id: str) -> int:
    state = routine_state.load(self, team_id)
    current = next((item for item in state.routines if item.routine_id == routine_id), None)
    return 0 if current is None or current.deleting else current.revision


def _unresolved(self, team_id: str, incident_id: str) -> record.Incident:
    try:
        value = routine_hold.incident(routine_state.load(self, team_id), incident_id)
    except record.RoutineStateError as exc:
        raise _problem(HTTPStatus.NOT_FOUND, "Routine incident is unavailable", "routine-incident-unavailable") from exc
    if value.status != "unresolved":
        raise _problem(HTTPStatus.CONFLICT, "Routine incident is not unresolved", "routine-incident-unavailable")
    return value


def _source(self, team_id: str, routine_id: str) -> str | None:
    """The commitment of the Routine's creation source; None when it has none or it cannot be read."""
    try:
        source = routine_source.load(self, team_id, routine_id)
    except ApiProblem:
        return None
    return None if source is None else source.commitment


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
    team_id, principal = validate_team_id(team_id), _principal()
    value = _unresolved(self, team_id, incident_id)
    opened = routine_incident.open_recovery(self, team_id, incident_id)
    steps = opened.recovery.plan["steps"]
    index = min(opened.cursor.step, len(steps) - 1)
    step = steps[index]
    incarnation = opened.recovery.binding.incarnation
    card = Card(
        principal,
        incarnation,
        incident_id,
        value.routine_id,
        value.revision,
        _current_revision(self, team_id, value.routine_id),
        value.generation,
        opened.cursor.operation_id,
        _source(self, team_id, value.routine_id),
        secrets.token_hex(16),
        self.routine_cards.deadline(),
    )
    evidence = _evidence(self, team_id, incarnation, incident_id, opened.cursor.operation_id)
    if evidence["diagnostic"] is not None and (
        (evidence["diagnostic"]["assistant_id"], evidence["diagnostic"]["action"])
        != (step["assistant"], step["action"])
    ):
        evidence = {"evidence": "absent", "diagnostic": None}
    self.routine_cards.open(team_id, card)
    return {
        "team_id": team_id,
        "incident_id": incident_id,
        "routine_id": value.routine_id,
        "revision": value.revision,
        "assistant_id": step["assistant"],
        "action": step["action"],
        "step": index + 1,
        "steps": len(steps),
        **evidence,
        "nonce": card.nonce,
        "expires_in": CARD_SECONDS,
        "choices": list(CHOICES),
    }


def _bound(self, team_id: str, card: Card) -> routine_hold.Expected:
    """The card still names exactly this Team incarnation, incident, revision, generation, operation, and source.

    It is checked in the Team's execution slot, where nothing else moves the cursor, and returns what the card's state
    transition checks again in its own write.
    """
    value = _unresolved(self, team_id, card.incident_id)
    opened = routine_incident.open_recovery(self, team_id, card.incident_id)
    if (
        opened.recovery.binding.incarnation != card.incarnation
        or (value.routine_id, value.revision, value.generation) != (card.routine_id, card.revision, card.generation)
        or _current_revision(self, team_id, card.routine_id) != card.current
        or opened.cursor.operation_id != card.operation_id
        or _source(self, team_id, card.routine_id) != card.source
    ):
        raise _problem(HTTPStatus.CONFLICT, "the recovery card is stale; open it again", "routine-card-stale")
    return routine_hold.Expected(card.revision, card.generation, card.current)


def restartable(self, team_id: str, card: Card) -> record.Routine:
    """The Routine a fresh run or a replacement may start for: listed, idle, and with the held attempt stopped."""
    state = routine_state.load(self, team_id)
    current = next((item for item in state.routines if item.routine_id == card.routine_id), None)
    if current is None or current.deleting or card.current == 0:
        raise _problem(HTTPStatus.CONFLICT, "Routine is unavailable", "routine-not-found")
    if any(item.routine_id == card.routine_id for item in state.runs):
        raise _problem(HTTPStatus.CONFLICT, "another run of this Routine is live", "routine-busy")
    if not routine_recovery.workload_stopped(
        self, team_id, routine_incident.open_recovery(self, team_id, card.incident_id)
    ):
        raise _problem(HTTPStatus.CONFLICT, "the held attempt may still be running", "routine-workload-unquiesced")
    return current


def _contracts_changed() -> ApiProblem:
    return _problem(HTTPStatus.CONFLICT, "the Routine's Assistants changed", "routine-contracts-changed")


def _run(self, team_id: str, card: Card, expected: routine_hold.Expected) -> str:
    """Rodar: set the held run aside and request one fresh run of the Routine's current revision."""
    current = restartable(self, team_id, card)
    if current.needs_reconfirm:
        raise _contracts_changed()
    pinned = dict(current.assistants)
    try:
        if routine_turn.current_contracts(self, team_id, tuple(pinned)) != pinned:
            raise _contracts_changed()
    except routine_turn.ContractsUnavailableError as exc:
        raise routine_turn.context_unavailable() from exc
    now = int(time.time())
    routine_incident.set_aside(
        self, team_id, card.incident_id, lambda state: routine_hold.run_incident(state, card.incident_id, now, expected)
    )
    return "requested"


def _credential(choice: str, credential: tuple[str, str] | None) -> None:
    """Recriar needs the Team's model credential; Rodar runs no model and carries none."""
    if (choice == "recreate") != (credential is not None):
        raise _problem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "only Recriar carries the model credential, and it always does",
            "routine-card-credential-invalid",
        )


def answer_card(
    self, team_id: str, incident_id: str, body: object, credential: tuple[str, str] | None = None
) -> dict[str, object]:
    """Answer one open recovery card with Rodar or Recriar."""
    team_id, principal = validate_team_id(team_id), _principal()
    body = http_routine.canonical_card_answer_request(body)
    if body is None:
        raise _problem(
            HTTPStatus.UNPROCESSABLE_ENTITY, "a card answer is its nonce and Rodar or Recriar", "invalid-body"
        )
    choice = body["choice"]
    _credential(choice, credential)
    routine_id = _unresolved(self, team_id, incident_id).routine_id
    # Every answer is checked and applied in the Team's execution slot, against the state the card was opened on.
    with self._exclusive_chat_turn(team_id, routine_id) as token:
        card = self.routine_cards.take(team_id, incident_id, body["nonce"], principal)
        if card is None:
            raise _problem(HTTPStatus.CONFLICT, "the recovery card expired; open it again", "routine-card-expired")
        expected = _bound(self, team_id, card)
        if choice == "run":
            status = _run(self, team_id, card, expected)
        else:
            current = restartable(self, team_id, card)
            status = routine_recreate.recreate(self, team_id, card, expected, (current, principal, token), credential)
    local_audit.record_request("routine-card", result="ok", team_id=team_id, detail=f"{incident_id}:{choice}")
    return {"team_id": team_id, "incident_id": incident_id, "choice": choice, "status": status}
