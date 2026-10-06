"""The recovery card of a held Routine run: Rodar and Excluir (ADR-0092 section 7, ADR-0101).

Team verifies a held run automatically; a card exists only when that could not prove what the failed call did. It
shows the call and the failure Team recorded for it, and binds itself to the authenticated person, the Team
incarnation, the Routine, its run and operation, the revision the run executed, a fresh nonce, and a five-minute
expiry. Its answer must match every one of those, once.

Rodar sets the held run aside without verifying it and requests one fresh run of the current revision; the call may
already have acted, so it may act again. Excluir is not a card answer: it is the Routine's own deletion, confirmed with
the Supervisor's password and second factor in Admin. Rodar never starts while the held attempt's workload could still
be running, while another run of the Routine is live, or once the Routine is deleted.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
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
from protocol.http.v1 import routine_notice as http_routine_notice
from protocol.http.v1 import routine_run as http_routine_run
from routine import hold as routine_hold
from routine import record

CARD_SECONDS = http_routine_run.CARD_SECONDS
CHOICES = http_routine_notice.CARD_CHOICES


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
    nonce: str
    expires_at: float


class CardBook:
    """The open recovery cards of every Team, each answerable once before it expires.

    Expired cards are swept whenever one opens or is answered, and a card goes as soon as its incident settles or its
    Routine is deleted, so the book holds only cards someone could still answer.
    """

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._cards: dict[tuple[str, str], Card] = {}

    def _sweep(self) -> None:
        """Drop every expired card; the caller holds the lock."""
        now = self._now()
        for key in [key for key, card in self._cards.items() if card.expires_at <= now]:
            del self._cards[key]

    def open(self, team_id: str, card: Card) -> None:
        with self._lock:
            self._sweep()
            self._cards[(team_id, card.incident_id)] = card

    def take(self, team_id: str, incident_id: str, nonce: object, principal: str) -> Card | None:
        """The exact card this answer names, consumed; None when it is unknown, expired, or someone else's."""
        with self._lock:
            self._sweep()
            card = self._cards.get((team_id, incident_id))
            if card is None or not secrets.compare_digest(card.nonce, nonce if isinstance(nonce, str) else ""):
                return None
            del self._cards[(team_id, incident_id)]
        return card if card.principal == principal else None

    def deadline(self) -> float:
        return self._now() + CARD_SECONDS

    def discard(self, team_id: str, incident_id: str) -> None:
        """Forget the card of an incident that settled; nothing can answer it any more."""
        with self._lock:
            self._cards.pop((team_id, incident_id), None)

    def drop_routine(self, team_id: str, routine_id: str) -> None:
        """Forget every card of a deleted Routine."""
        with self._lock:
            for key, card in list(self._cards.items()):
                if key[0] == team_id and card.routine_id == routine_id:
                    del self._cards[key]

    def drop(self, team_id: str) -> None:
        with self._lock:
            for key in [key for key in self._cards if key[0] == team_id]:
                del self._cards[key]

    def clear(self) -> None:
        with self._lock:
            self._cards.clear()


def _principal() -> str:
    principal = local_audit.human_principal()
    if principal is None:
        raise ApiProblem(
            HTTPStatus.FORBIDDEN, "a person must answer a recovery card", code="routine-card-person-required"
        )
    return principal


def _current_revision(self, team_id: str, routine_id: str) -> int:
    state = routine_state.load(self, team_id)
    current = next((item for item in state.routines if item.routine_id == routine_id), None)
    return 0 if current is None or current.deleting else current.revision


def _unresolved(self, team_id: str, incident_id: str) -> record.Incident:
    try:
        value = routine_hold.incident(routine_state.load(self, team_id), incident_id)
    except record.RoutineStateError as exc:
        raise local_errors.routine_incident_unavailable() from exc
    if value.status != "unresolved":
        raise local_errors.routine_incident_not_unresolved()
    return value


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
    assistant_id, action, position, steps = routine_incident.held_call(opened.cursor, opened.recovery.plan["steps"])
    if not assistant_id:
        raise ApiProblem(HTTPStatus.CONFLICT, "the held run named no call", code="routine-incident-unavailable")
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
        secrets.token_hex(16),
        self.routine_cards.deadline(),
    )
    evidence = _evidence(self, team_id, incarnation, incident_id, opened.cursor.operation_id)
    if evidence["diagnostic"] is not None and (
        (evidence["diagnostic"]["assistant_id"], evidence["diagnostic"]["action"], evidence["diagnostic"]["position"])
        != (assistant_id, action, position)
    ):
        evidence = {"evidence": "absent", "diagnostic": None}
    self.routine_cards.open(team_id, card)
    return {
        "team_id": team_id,
        "incident_id": incident_id,
        "routine_id": value.routine_id,
        "revision": value.revision,
        "assistant_id": assistant_id,
        "action": action,
        "position": position,
        "steps": steps,
        **evidence,
        "nonce": card.nonce,
        "expires_in": CARD_SECONDS,
        "choices": list(CHOICES),
    }


def _bound(self, team_id: str, card: Card) -> routine_hold.Expected:
    """The card still names exactly this Team incarnation, incident, revision, generation, and operation.

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
    ):
        raise local_errors.routine_card_stale()
    return routine_hold.Expected(card.revision, card.generation, card.current)


def restartable(self, team_id: str, card: Card) -> record.Routine:
    """The Routine a fresh run may start for: listed, idle, and with the held attempt stopped."""
    state = routine_state.load(self, team_id)
    current = next((item for item in state.routines if item.routine_id == card.routine_id), None)
    if current is None or current.deleting or card.current == 0:
        raise ApiProblem(HTTPStatus.CONFLICT, "Routine is unavailable", code="routine-not-found")
    if any(item.routine_id == card.routine_id for item in state.runs):
        raise ApiProblem(HTTPStatus.CONFLICT, "another run of this Routine is live", code="routine-busy")
    if not routine_recovery.workload_stopped(
        self, team_id, routine_incident.open_recovery(self, team_id, card.incident_id)
    ):
        raise ApiProblem(
            HTTPStatus.CONFLICT, "the held attempt may still be running", code="routine-workload-unquiesced"
        )
    return current


def _contracts_changed() -> ApiProblem:
    return ApiProblem(HTTPStatus.CONFLICT, "the Routine's Assistants changed", code="routine-contracts-changed")


def _run(self, team_id: str, card: Card, expected: routine_hold.Expected) -> str:
    """Rodar: set the held run aside and request one fresh run of the Routine's current revision."""
    current = restartable(self, team_id, card)
    if current.needs_reconfirm:
        raise _contracts_changed()
    pinned = dict(current.assistants)
    try:
        if routine_contracts.current_contracts(self, team_id, tuple(pinned)) != pinned:
            raise _contracts_changed()
    except routine_contracts.ContractsUnavailableError as exc:
        raise routine_contracts.context_unavailable() from exc
    now = int(time.time())
    routine_incident.set_aside(
        self, team_id, card.incident_id, lambda state: routine_hold.run_incident(state, card.incident_id, now, expected)
    )
    return "requested"


def answer_card(self, team_id: str, incident_id: str, body: object) -> dict[str, object]:
    """Answer one open recovery card with Rodar."""
    team_id, principal = validate_team_id(team_id), _principal()
    body = http_routine_run.canonical_card_answer_request(body)
    if body is None:
        raise ApiProblem(HTTPStatus.UNPROCESSABLE_ENTITY, "a card answer is its nonce and Rodar", code="invalid-body")
    choice = body["choice"]
    routine_id = _unresolved(self, team_id, incident_id).routine_id
    # Every answer is checked and applied in the Team's execution slot, against the state the card was opened on.
    with self._exclusive_chat_turn(team_id, routine_id):
        card = self.routine_cards.take(team_id, incident_id, body["nonce"], principal)
        if card is None:
            raise ApiProblem(
                HTTPStatus.CONFLICT, "the recovery card expired; open it again", code="routine-card-expired"
            )
        status = _run(self, team_id, card, _bound(self, team_id, card))
    local_audit.record_request("routine-card", result="ok", team_id=team_id, detail=f"{incident_id}:{choice}")
    return {"team_id": team_id, "incident_id": incident_id, "choice": choice, "status": status}
