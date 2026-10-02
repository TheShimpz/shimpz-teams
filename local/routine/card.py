"""The recovery card of a held Routine run: exactly Verificar, Pular, and Pausar (ADR-0092 section 7).

Opening a card binds it to the authenticated person, the Team incarnation, the Routine, its run and operation, the
revision the run executed, a fresh nonce, and a five-minute expiry. Its answer must match every one of those, once.

Verificar runs the Action's fixed read-only verifier in the Team's execution slot with no model and no provider key;
proven occurrence, or proven absence with the run's one retry left, continues the already-authorized run under a fresh
internal lease. Pular abandons the rest of the run and its dependent steps without replay or fabricated output, keeps
its possible effects unresolved, and permits future cycles. Pausar disables dispatch while the incident stays.
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
from local.routine import incident as routine_incident
from local.routine import recovery as routine_recovery
from local.routine import run as routine_run
from local.routine import state as routine_state
from local.validation import validate_team_id
from protocol.http.v1 import routine as http_routine
from routine import hold as routine_hold
from routine import record

CARD_SECONDS = http_routine.CARD_SECONDS
# A manual verification's registered deadline, after which the watchdog stops it like an overdue run.
VERIFY_SECONDS = 60
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


def _verifiable(self, team_id: str, incident_id: str) -> bool:
    """Whether Verificar can prove anything: nothing is uncertain, absence is proven, or a verifier is declared.

    A policy hold has nothing to verify, so its card recommends Pausar.
    """
    try:
        assessment = routine_recovery.assess(self, team_id, incident_id)
    except ApiProblem:
        return False
    proven = routine_recovery.proven(assessment)
    if proven in {"policy", "unquiesced"}:
        return False
    return proven != "uncertain" or routine_recovery.verifier_request(assessment) is not None


def open_card(self, team_id: str, incident_id: str) -> dict[str, object]:
    """Open the recovery card of one held run for the authenticated person who will answer it."""
    team_id, principal = validate_team_id(team_id), _principal()
    value = _unresolved(self, team_id, incident_id)
    opened = routine_incident.open_recovery(self, team_id, incident_id)
    steps = opened.recovery.plan["steps"]
    step = steps[min(opened.cursor.step, len(steps) - 1)]
    verifiable = _verifiable(self, team_id, incident_id)
    recommended = "verify" if verifiable else "pause"
    card = Card(
        principal,
        opened.recovery.binding.incarnation,
        incident_id,
        value.routine_id,
        value.revision,
        _current_revision(self, team_id, value.routine_id),
        value.generation,
        opened.cursor.operation_id,
        secrets.token_hex(16),
        self.routine_cards.deadline(),
    )
    self.routine_cards.open(team_id, card)
    return {
        "team_id": team_id,
        "incident_id": incident_id,
        "routine_id": value.routine_id,
        "revision": value.revision,
        "assistant_id": step["assistant"],
        "action": step["action"],
        "nonce": card.nonce,
        "expires_in": CARD_SECONDS,
        # The recommended available choice leads; an unverifiable step recommends Pausar.
        "choices": [recommended, *(choice for choice in CHOICES if choice != recommended)],
        "recommended": recommended,
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
        raise _problem(HTTPStatus.CONFLICT, "the recovery card is stale; open it again", "routine-card-stale")
    return routine_hold.Expected(card.revision, card.generation, card.current)


def _verify(self, team_id: str, card: Card, token: str) -> dict[str, object]:
    """Verificar: the fixed verifier with no model, then the already-authorized continuation when evidence allows."""
    # A registered, cancellable recovery lease: Stop and deletion reach the verification and fence the continuation.
    with routine_run.registered(self, team_id, card.incident_id, token, VERIFY_SECONDS):
        verdict = routine_recovery.verify(self, team_id, card.incident_id, token, budgeted=False)
        status = None
        if verdict in {"occurred", "absent", "none"} and not self._chat_cancelled(token):
            opened = routine_incident.open_recovery(self, team_id, card.incident_id)
            if routine_recovery.refusal(opened.cursor) is None and _continuable(self, team_id, card.routine_id):
                status = routine_recovery.continue_run(self, team_id, card.incident_id, token)
    return {"verdict": verdict, "status": status}


def _continuable(self, team_id: str, routine_id: str) -> bool:
    """A deleted, paused, or busy Routine never resumes a run; its incident stays for the person to settle."""
    state = routine_state.load(self, team_id)
    current = next((item for item in state.routines if item.routine_id == routine_id), None)
    return (
        current is not None
        and not current.deleting
        and not current.paused
        and not any(item.routine_id == routine_id for item in state.runs)
    )


def answer_card(self, team_id: str, incident_id: str, body: object) -> dict[str, object]:
    """Answer one open recovery card with exactly one of its choices."""
    team_id, principal = validate_team_id(team_id), _principal()
    body = http_routine.canonical_card_answer_request(body)
    if body is None:
        raise _problem(HTTPStatus.UNPROCESSABLE_ENTITY, "a card answer is its nonce and one choice", "invalid-body")
    routine_id = _unresolved(self, team_id, incident_id).routine_id
    # Every choice is checked and applied in the Team's execution slot, against the state the card was opened on.
    with self._exclusive_chat_turn(team_id, routine_id) as token:
        card = self.routine_cards.take(team_id, incident_id, body["nonce"], principal)
        if card is None:
            raise _problem(HTTPStatus.CONFLICT, "the recovery card expired; open it again", "routine-card-expired")
        expected = _bound(self, team_id, card)
        choice = body["choice"]
        if choice == "verify":
            result = _verify(self, team_id, card, token)
        elif choice == "skip":
            routine_incident.skip(self, team_id, incident_id, expected)
            result = {"verdict": None, "status": "skipped"}
        else:
            routine_incident.pause(self, team_id, incident_id, "person", expected)
            result = {"verdict": None, "status": "paused"}
    local_audit.record_request("routine-card", result="ok", team_id=team_id, detail=f"{incident_id}:{choice}")
    return {"team_id": team_id, "incident_id": incident_id, "choice": choice, **result}
