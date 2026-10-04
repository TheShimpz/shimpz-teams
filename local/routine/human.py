"""A human answers a frozen Routine run: a fresh challenge when the notice opens, then the run resumes (ADR-0086).

Routine challenges live in their own namespace, never beside chat's one pending challenge, so a frozen run never
blocks chat. Expiry or dismissal of a routine challenge leaves the run frozen; denial or an unavailable assurance ends
it denied.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus

from action import challenges as action_challenges
from action import human as action_human
from chat import progress as chat_progress
from core import strict_json
from inference import config as inference_config
from install import bindings
from local.chat import continuation as local_chat_continuations
from local.chat import human as local_chat_human
from local.errors import ApiProblemError as ApiProblem
from local.routine import compiled as routine_compiled
from local.routine import manage as routine_manage
from local.routine import run as routine_run
from local.routine import state as routine_state
from local.routine import store as routine_store
from local.routine import turn as routine_turn
from local.validation import validate_team_id
from routine import record


@dataclass(frozen=True, slots=True)
class _Frozen:
    """One frozen run about to resume: its Team, record, Routine, and decoded continuation."""

    team_id: str
    value: record.Run
    routine: record.Routine
    pending: local_chat_continuations.PendingLocalChat
    # The answered request, whose copy must still come from the Team's current binding (ADR-0091).
    requirement: action_challenges.HumanRequirement | None = None


def _problem(status: HTTPStatus, message: str, code: str) -> ApiProblem:
    return ApiProblem(status, message, code=code)


def _frozen(self, team_id: str, run_id: object) -> tuple[record.Run, record.Routine]:
    state = routine_state.load(self, team_id)
    try:
        value = record.run(state, run_id if isinstance(run_id, str) else "")
        routine = record.routine(state, value.routine_id)
    except record.RoutineStateError as exc:
        raise _problem(HTTPStatus.NOT_FOUND, "Routine run is unavailable", "routine-run-not-found") from exc
    if value.status != "frozen":
        raise _not_frozen()
    return value, routine


def _decoded(self, team_id: str, run_id: str) -> local_chat_continuations.DecodedContinuation:
    try:
        envelope = strict_json.loads(self.routine_store.continuation(team_id, run_id))
        return local_chat_continuations.decode_parts(
            envelope["kind"], base64.b64decode(envelope["payload"], validate=True), tuple(envelope["bindings"])
        )
    except (routine_store.RoutineStoreError, local_chat_continuations.ContinuationCodecError) as exc:
        raise _problem(
            HTTPStatus.SERVICE_UNAVAILABLE, "Routine run state is unavailable", "routine-state-unavailable"
        ) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise _problem(
            HTTPStatus.SERVICE_UNAVAILABLE, "Routine run state is unavailable", "routine-state-unavailable"
        ) from exc


def _not_frozen() -> ApiProblem:
    return _problem(HTTPStatus.CONFLICT, "Routine run is not waiting for a person", "routine-run-not-frozen")


def _expired() -> ApiProblem:
    # An expired or dismissed routine challenge leaves the run frozen, ready to be opened again.
    return _problem(HTTPStatus.CONFLICT, "Action human request expired; open it again", "human-request-expired")


def _end_changed(self, team_id: str, value: record.Run) -> None:
    """End a frozen run whose Team changed, removing everything it held, its challenge included.

    The run must still be frozen when it ends: a replay that resumed it meanwhile owns it now.
    """
    if not routine_manage.end_frozen(
        self, team_id, value.run_id, "failed", {"code": "team-context-changed", "actions": []}
    ):
        raise _not_frozen()
    cancel_routine_challenge(self, team_id, value.run_id)
    routine_manage.settle(self, team_id, value.routine_id)


def _proven_changed(self, team_id: str, pending: local_chat_continuations.PendingLocalChat) -> bool:
    """Whether a Team whose context could not be set up provably changed since the run froze.

    Only an Assistant the Team no longer runs, or a model configuration that is gone or names another provider, is
    proof; a Team whose Assistants or configuration cannot be read now proves nothing.
    """
    try:
        current = routine_turn.current_contracts(self, team_id, pending.assistant_ids)
        provider = self.inference_store.load(team_id).provider
    except inference_config.InferenceConfigMissingError:
        return True
    except routine_turn.ContractsUnavailableError, inference_config.InferenceConfigError:
        return False
    return set(current) != set(pending.assistant_ids) or provider != pending.provider


def _current_context(
    self,
    team_id: str,
    value: record.Run,
    pending: local_chat_continuations.PendingLocalChat,
    requirement: action_challenges.HumanRequirement | None = None,
) -> tuple[object, ...]:
    """The Team must still be exactly as the run left it; otherwise the run ends and nothing replays.

    A human request's copy must also still come from the binding's catalog and pack (ADR-0091). When the Team cannot
    be read now, the run stays frozen and the person may retry. Returns the Team's running Assistants.
    """
    try:
        current = self._chat_setup(team_id, [], pending.provider, pending.assistant_ids)
    except (ApiProblem, bindings.DynamicAssistantError) as exc:
        if not _proven_changed(self, team_id, pending):
            raise routine_turn.context_unavailable() from exc
        current = None
    if (
        current is None
        or self._chat_identity(*current) != pending.identity
        or (requirement is not None and not local_chat_human.copy_binding_current(requirement, current[2]))
    ):
        _end_changed(self, team_id, value)
        raise _problem(HTTPStatus.CONFLICT, "Team capabilities changed; the run ended", "team-context-changed")
    return current[2]


def open_routine_challenge(self, team_id: str, run_id: str, locale: str) -> dict[str, object]:
    """A person opened a frozen run's notice: create a fresh one-use challenge for its exact request.

    Each opening renders the request copy in the Admin interface language from the same binding's pack, so another
    language is always a fresh challenge; a purpose from another language is not shown (ADR-0091).
    """
    team_id = validate_team_id(team_id)
    # The run's frozen read, its challenge's publication, and a Stop or deletion ending it are serialized by the Team
    # lifecycle lock, so a run ended meanwhile is never given a challenge and never displaces another run's.
    with self._lock(team_id):
        value, _routine = _frozen(self, team_id, run_id)
        if value.request_kind != "human":
            return {"team_id": team_id, "run_id": value.run_id, "status": "integrations-required"}
        decoded = _decoded(self, team_id, value.run_id)
        frozen = decoded.requirements[0]
        assistants = _current_context(self, team_id, value, decoded.pending, frozen)
        active = next(item for item in assistants if item.spec.assistant_id == frozen.assistant_id)
        try:
            requirement = action_challenges.relocalize(frozen, self._assistant_language(active), locale)
        except action_challenges.HumanChallengeError as exc:
            raise _problem(
                HTTPStatus.CONFLICT, "Action human request changed; the run stays frozen", "human-request-invalid"
            ) from exc
        # One routine challenge per Team at a time: opening another returns the earlier run to waiting, still frozen.
        self.routine_human_challenges.cancel_team(team_id)
        challenge = self.routine_human_challenges.create(team_id, requirement, (value.run_id, decoded))
    return {**self._human_response(challenge), "run_id": value.run_id}


def current_routine_challenge(self, team_id: str) -> action_challenges.PendingHumanChallenge | None:
    return self.routine_human_challenges.current(team_id)


def cancel_routine_challenge(self, team_id: str, run_id: str) -> None:
    """Drop the Team's routine challenge when it belongs to this run; the run itself is ended by the caller."""
    challenge = self.routine_human_challenges.current(team_id)
    if challenge is not None and challenge.payload[0] == run_id:
        self.routine_human_challenges.cancel_team(team_id)


def _body(body: object) -> tuple[object, str, object | None]:
    decision = body.get("decision") if isinstance(body, dict) else None
    expected = {"challenge_id", "decision", "value"} if decision == "submit" else {"challenge_id", "decision"}
    if decision not in {"submit", "deny"} or set(body) != expected:
        raise _problem(HTTPStatus.UNPROCESSABLE_ENTITY, "Action human response is invalid", "invalid-body")
    return body["challenge_id"], decision, body.get("value")


def _challenge(self, team_id: str, challenge_id: object, run_id: str) -> action_challenges.PendingHumanChallenge:
    """Read, without consuming, the run's own live challenge."""
    try:
        challenge = self.routine_human_challenges.get(team_id, challenge_id)
    except action_challenges.HumanChallengeNotFoundError as exc:
        raise _expired() from exc
    if challenge.payload[0] != run_id:
        raise _problem(HTTPStatus.CONFLICT, "Action human request belongs to another run", "human-request-expired")
    return challenge


def _consume(self, team_id: str, challenge_id: str, commit: Callable[[], None]) -> None:
    """Consume the challenge exactly when ``commit`` succeeds; when it raises, the challenge stays answerable."""
    try:
        self.routine_human_challenges.claim_after(team_id, challenge_id, lambda _challenge: commit())
    except action_challenges.HumanChallengeNotFoundError as exc:
        raise _expired() from exc


def _deny(self, team_id: str, value: record.Run, routine: record.Routine, challenge_id: str) -> dict[str, object]:
    """End the frozen run denied in the Team's execution slot, consuming its challenge only once the run ends."""

    def end() -> None:
        if not routine_manage.end_frozen(self, team_id, value.run_id, "denied", {"actions": []}):
            raise _not_frozen()

    with self._exclusive_chat_turn(team_id, routine.routine_id), self._lock(team_id):
        _consume(self, team_id, challenge_id, end)
    routine_manage.settle(self, team_id, value.routine_id)
    return {"team_id": team_id, "run_id": value.run_id, "status": "denied"}


def resume_routine_human(
    self,
    team_id: str,
    run_id: str,
    body: object,
    provider: str,
    api_key: str,
    progress: chat_progress.Reporter | None = None,
) -> dict[str, object]:
    """Validate one exact answer to a frozen run's challenge, then replay the run from its continuation.

    The challenge is consumed only once the replay holds the Team's execution slot and the run resumed, so an answer
    refused for a busy Team, an unreadable context, or a run that cannot resume can be sent again.
    """
    team_id = validate_team_id(team_id)
    challenge_id, decision, answer = _body(body)
    value, routine = _frozen(self, team_id, run_id)
    challenge = _challenge(self, team_id, challenge_id, value.run_id)
    if decision == "deny":
        return _deny(self, team_id, value, routine, challenge.id)
    pending = challenge.payload[1].pending
    # Without a key Admin holds, the replay goes on with the provider the run froze with; only recovery needs a key.
    provider = provider or pending.provider
    if pending.provider != provider:
        raise _problem(HTTPStatus.CONFLICT, "configured model provider changed; retry", "inference-provider-mismatch")
    try:
        admission = action_human.append_response(
            pending.transcripts,
            challenge.requirement.interrupt_id,
            challenge.requirement.request,
            answer,
            pending.requests_used,
        )
    except action_human.HumanRequestError as exc:
        raise _problem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Action human response does not match its request",
            "invalid-human-response",
        ) from exc
    frozen = _Frozen(team_id, value, routine, pending, challenge.requirement)
    return _replay(self, frozen, (provider, api_key), progress, (admission, challenge.id))


def resume_routine_integrations(
    self, team_id: str, run_id: str, provider: str, api_key: str, progress: chat_progress.Reporter | None = None
) -> dict[str, object]:
    """After the person connected the Integration, replay the run; a still-missing one freezes it again."""
    team_id = validate_team_id(team_id)
    value, routine = _frozen(self, team_id, run_id)
    if value.request_kind != "integrations":
        raise _problem(HTTPStatus.CONFLICT, "Routine run is waiting for an answer", "routine-run-not-frozen")
    pending = _decoded(self, team_id, value.run_id).pending
    provider = provider or pending.provider
    if pending.provider != provider:
        raise _problem(HTTPStatus.CONFLICT, "configured model provider changed; retry", "inference-provider-mismatch")
    return _replay(self, _Frozen(team_id, value, routine, pending), (provider, api_key), progress, None)


def _thaw(state: record.TeamRoutines, run_id: str, now: int) -> tuple[record.TeamRoutines, str | None]:
    """Resume the run only while it is still frozen and its Routine is not being deleted."""
    try:
        return record.thaw(state, run_id, now)
    except record.RoutineStateError:
        return state, None


def _resume(self, team_id: str, run_id: str, challenge_id: str | None) -> record.Lease:
    """Thaw the run; an answered run consumes its challenge in the same step, so a run that stays frozen keeps it."""
    now = int(time.time())
    thawed: list[str] = []

    def thaw() -> None:
        state_token = routine_state.update(self, team_id, lambda state: _thaw(state, run_id, now))
        if state_token is None:
            raise _not_frozen()
        thawed.append(state_token)

    if challenge_id is None:
        thaw()
    else:
        _consume(self, team_id, challenge_id, thaw)
    return record.lease_of(thawed[0], record.HUMAN_LEASE)


def _replay(
    self,
    frozen: _Frozen,
    credentials: tuple[str, str],
    progress: chat_progress.Reporter | None,
    answered: tuple[action_human.HumanResponseAdmission, str] | None,
) -> dict[str, object]:
    """Thaw the run under an internal lease and continue it, with no model, in its own generation.

    The Team's execution slot is held before its lock and before anything is consumed: a concurrent chat refuses the
    replay while the run stays frozen and its challenge stays answerable. Only a hold of the replayed run may use the
    model key, for its one automatic recovery.
    """
    provider, api_key = credentials
    team_id, value, routine, pending = frozen.team_id, frozen.value, frozen.routine, frozen.pending
    admission, challenge_id = answered if answered is not None else (None, None)
    transcripts = pending.transcripts if admission is None else admission.transcripts
    requests_used = pending.requests_used if admission is None else admission.requests_used
    with (
        self._exclusive_chat_turn(team_id, routine.routine_id) as token,
        routine_run.registered(self, team_id, value.run_id, token, value.active_seconds_left),
    ):
        with self._lock(team_id):
            _current_context(self, team_id, value, pending, frozen.requirement)
            lease = _resume(self, team_id, value.run_id, challenge_id)
        routine_state.call(lambda: self.routine_store.delete_continuation(team_id, value.run_id))
        run = routine_run._Run(team_id, value.run_id, lease, token, provider, routine, transcripts, requests_used)
        outcome = routine_compiled.execute(self, run, value, progress, pending)
        if outcome == "held":
            outcome = self._recover_routine_run(run, api_key, progress)
    routine_run._after_run(self, team_id, value.run_id, routine.routine_id, outcome)
    return {"team_id": team_id, "run_id": value.run_id, "status": outcome}
