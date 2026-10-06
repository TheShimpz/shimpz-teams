"""Local chat start and integration-resume API operations."""

import json
import time
from http import HTTPStatus

from chat import contract as assistant_chat
from chat import knowledge as chat_knowledge
from chat import orchestrator as chat_orchestrator
from chat import progress as chat_progress
from chat import turn as chat_turn_engine
from inference import config as inference_config
from inference import usage as brain_usage
from local import audit as local_audit
from local.chat import pause as local_chat_pause
from local.chat.segment import SegmentRequest as _ChatSegmentRequest
from local.chat.types import PendingLocalChat as _PendingLocalChat
from local.chat.types import ResponseRequest as _ResponseRequest
from local.errors import ApiProblemError as ApiProblem
from local.errors import (
    chat_stopped,
    integration_challenge_expired,
    integration_contract_unavailable,
    team_context_changed,
)
from local.validation import validate_chat_assistant_ids, validate_team_id
from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import progress as http_progress
from routine import schedule as routine_schedule

MAX_CHAT_MESSAGE_CHARS = http_payload.MAX_CHAT_MESSAGE_CHARS


def _pending_chat_continuation(self, team_id: str, locale: str | None = None) -> dict[str, object] | None:
    """The Team's pending challenge; a human one is returned in the chat's interface language when it names one.

    Either challenge is validated against the context the turn left before it is returned; a drifted one ends its turn.
    """
    self._expire_human_challenges()
    # The live challenge is read and validated under one Team lock, so an opening cannot reissue it in between.
    with self._lock(team_id):
        existing_human = self.human_challenges.current(team_id)
        if existing_human is not None:
            # A chat without an interface language keeps the challenge's language but still validates its binding.
            existing_human = self._relocalized_human(existing_human, locale or existing_human.requirement.copy.locale)
        existing_integration = None if existing_human is not None else self.integration_challenges.current(team_id)
        if existing_integration is not None:
            pending, current = local_chat_pause._paused_setup(
                self, team_id, existing_integration.payload.provider, existing_integration
            )
            if self._chat_identity(*current) != pending.identity:
                local_chat_pause._end_drifted_turn(self, team_id, existing_integration)
    if existing_human is not None:
        return self._human_response(existing_human)
    if existing_integration is not None:
        return self._integration_response(existing_integration)
    return None


def _routine_outcome(self, response: _ResponseRequest, terminal: chat_orchestrator.ChatOutcome, body: dict):
    """A recording turn's ``record``: the write that keeps its card, and the terminal fields its reply carries.

    The card is refused, never cut, when the whole terminal line would outgrow its bound (ADR-0101 section 5.2).
    """
    write, fields = self._routine_record(response, terminal.routine)
    line = {"type": "terminal", "status": 200, "body": {**body, **fields}}
    if "routine_proposal" in fields and _line_bytes(line) > http_progress.MAX_LINE_BYTES:
        return (lambda: None), {"routine_refusal": {"code": "routine-proposal-too-large"}}
    return write, fields


def _line_bytes(line: dict[str, object]) -> int:
    """The encoded bytes of one terminal NDJSON line, its newline included."""
    return len(json.dumps(line, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1


def _segment_response(
    self,
    response: _ResponseRequest,
) -> dict[str, object]:
    team_id = response.team_id
    token = response.token
    segment = response.segment

    def pending(suspension: object) -> _PendingLocalChat:
        if not isinstance(suspension, chat_orchestrator.ChatSuspension | chat_orchestrator.ChatHumanSuspension):
            raise AssertionError("invalid local chat suspension")
        return _PendingLocalChat(
            continuation=suspension.continuation,
            assistant_ids=response.assistant_ids,
            file_ids=response.file_ids,
            provider=response.provider,
            identity=segment.identity,
            transcripts=chat_orchestrator.retain_suspension_transcripts(response.transcripts, suspension),
            requests_used=response.requests_used,
            locale=segment.locale,
            usage=None if response.usage is None else response.usage.joined(),
            paused_batch=segment.paused_batch,
            recording=response.recording,
        )

    def save_knowledge(terminal: chat_orchestrator.ChatOutcome) -> None:
        # Saved only as the reply commits, under the Stop guard, in one write; a failed save fails the turn.
        if response.file_ids:
            # A turn that consumed selected files learns nothing: neither a memory the Brain proposed nor the Action
            # path it took becomes lasting knowledge (ADR-0093).
            return
        skill = chat_knowledge.learned_skill(terminal.actions)
        if not terminal.memory and skill is None:
            return
        # The attempt is audited first ("ok" means accepted for saving, not saved): when the audit cannot be written,
        # nothing is touched and the turn fails. A failed save adds an error event when the journal allows it.
        detail = f"attempt:memory={len(terminal.memory)},skill={int(skill is not None)}"
        local_audit.record_request("chat-memory", result="ok", team_id=team_id, detail=detail)
        try:
            self.inference_store.apply_knowledge(team_id, list(terminal.memory), skill)
        except inference_config.InferenceConfigError as exc:
            local_audit.record_request("chat-memory", result="error", team_id=team_id, detail="save-failed")
            raise ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE, "Team memory could not be saved", code="memory-store-failed"
            ) from exc

    def commit(terminal: chat_orchestrator.ChatOutcome, body: dict[str, object]) -> tuple[bool, dict]:
        if terminal.routine is None:
            return self._commit_chat_terminal(team_id, token, lambda: save_knowledge(terminal)), {}
        # A recorded Routine's card is kept with the reply, under the lifecycle lock and the Stop guard: when Stop wins,
        # no card exists (ADR-0101 section 5.1).
        with self._lock(team_id):
            write, fields = _routine_outcome(self, response, terminal, body)
            return self._commit_chat_terminal(team_id, token, lambda: (write(), save_knowledge(terminal))), fields

    def complete(terminal: chat_orchestrator.ChatOutcome) -> dict[str, object]:
        self._delete_chat_continuation(team_id)
        body = chat_turn_engine.terminal_body(team_id, segment.team_name, terminal, response.usage)
        try:
            committed, fields = commit(terminal, body)
        finally:
            # The logical turn ended here, whatever its commit did: nothing more is recorded for it.
            self.routine_recordings.end(team_id, response.recording)
        if not committed:
            raise chat_stopped()
        return {**body, **fields}

    try:
        return chat_turn_engine.dispatch(
            segment.outcome,
            segment.requirement_groups(),
            pending,
            (
                lambda suspension, requirements, state: self._pause_integration(
                    team_id, token, suspension, requirements, state
                ),
                lambda suspension, requirements, state: self._pause_human(
                    team_id,
                    token,
                    suspension,
                    requirements,
                    state,
                ),
            ),
            complete,
        )
    except ValueError as exc:
        raise ApiProblem(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc), code="internal-error") from exc


def _timezone(value: object) -> str | None:
    """The browser's IANA zone, which must load, or None when it named none."""
    if value is None:
        return None
    try:
        routine_schedule.zone(value)
    except routine_schedule.ScheduleError as exc:
        raise ApiProblem(HTTPStatus.UNPROCESSABLE_ENTITY, "timezone is invalid", code="invalid-timezone") from exc
    return value


def _recording(self, team_id: str, identity: dict[str, object], send: tuple[str, list, str | None]) -> str | None:
    """Open the recording of a new turn that may define a Routine, or None.

    Only a person's fresh request without files records, in the Team incarnation it starts in (ADR-0101 section 4.1).
    """
    message, file_ids, timezone = send
    principal = local_audit.human_principal()
    now = int(time.time())
    if principal is None or file_ids or not http_payload.request_identity_fresh(identity["issued_at"], now):
        return None
    incarnation = self.assistant_lifecycle._network(team_id).id
    return self.routine_recordings.start(team_id, (principal, incarnation), message, timezone, now)


def chat(
    self,
    team_id: str,
    body: object,
    provider: str,
    api_key: str,
    progress: chat_progress.Reporter | None = None,
) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    if not isinstance(body, dict) or set(body) != http_payload.LOCAL_CHAT_BODY_FIELDS:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Team chat requires only message, files, assistant_ids, conversation, locale, request, and timezone",
            code="invalid-body",
        )
    identity = http_payload.canonical_request_identity(body["request"])
    if identity is None:
        raise ApiProblem(HTTPStatus.UNPROCESSABLE_ENTITY, "request identity is invalid", code="invalid-request")
    timezone = _timezone(body["timezone"])
    locale = body["locale"]
    if locale is not None and http_payload.canonical_locale(locale) is None:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "locale must be one interface language or null",
            code="invalid-locale",
        )
    message = body["message"]
    file_ids = body["files"]
    assistant_ids = validate_chat_assistant_ids(body["assistant_ids"])
    try:
        conversation = assistant_chat.conversation_window(body["conversation"])
    except ValueError as exc:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "conversation must be a bounded committed history window",
            code="invalid-conversation",
        ) from exc
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_CHAT_MESSAGE_CHARS or "\0" in message:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "message must be non-empty and within its size limit",
            code="invalid-message",
        )
    pending = self._pending_chat_continuation(team_id, locale)
    if pending is not None:
        return pending
    with self._exclusive_chat_turn(team_id) as token:
        pending = self._pending_chat_continuation(team_id, locale)
        if pending is not None:
            return pending
        # The turn is admitted: its duration runs from here to its terminal, across every resume.
        usage = brain_usage.TurnUsage.start()
        recording = _recording(self, team_id, identity, (message, file_ids, timezone))
        try:
            segment = self._run_chat_segment(
                _ChatSegmentRequest(
                    team_id=team_id,
                    file_ids=file_ids,
                    assistant_ids=assistant_ids,
                    provider=provider,
                    api_key=api_key,
                    token=token,
                    message=message,
                    conversation=conversation,
                    locale=locale,
                    progress=progress or chat_progress.Reporter(),
                    recording=recording,
                )
            )
            return self._segment_response(
                _ResponseRequest(
                    team_id,
                    token,
                    segment,
                    assistant_ids,
                    tuple(file_ids),
                    provider,
                    usage=usage,
                    recording=recording,
                )
            )
        except BaseException:
            # A failed turn records nothing more; a paused one keeps its recording for the answer.
            self.routine_recordings.end(team_id, recording)
            raise


def resume_chat_integrations(
    self,
    team_id: str,
    body: object,
    provider: str,
    api_key: str,
    progress: chat_progress.Reporter | None = None,
) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    if not isinstance(body, dict) or set(body) != {"challenge_id"}:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Assistant integration resume requires only challenge_id",
            code="invalid-body",
        )
    challenge_id = body["challenge_id"]

    with self._exclusive_chat_turn(team_id) as token:
        with self._lock(team_id):

            def inspect(challenge: object) -> chat_turn_engine.IntegrationResumeContext:
                pending, current = local_chat_pause._paused_setup(self, team_id, provider, challenge)
                bindings = {active.spec.assistant_id: active for active in current[2]}
                return chat_turn_engine.IntegrationResumeContext(
                    self._chat_identity(*current),
                    bindings,
                    pending.continuation.turn.actions,
                )

            admission = chat_turn_engine.admit_integration_resume(
                chat_turn_engine.IntegrationResumeStrategy(
                    store=self.integration_challenges,
                    team_id=team_id,
                    challenge_id=challenge_id,
                    pending_valid=lambda pending: isinstance(pending, _PendingLocalChat),
                    pending_identity=lambda pending: pending.identity,
                    inspect=inspect,
                    integration_store=self.assistant_integrations,
                    challenge_response=self._integration_response,
                    expired_error=integration_challenge_expired,
                    context_error=team_context_changed,
                    contract_error=integration_contract_unavailable,
                    end_drifted=lambda challenge: local_chat_pause._end_drifted_turn(self, team_id, challenge),
                )
            )
            if admission.response is not None:
                return admission.response
            pending = admission.pending
            if not isinstance(pending, _PendingLocalChat):
                raise AssertionError("shared integration resume returned invalid state")
        return local_chat_pause._continue_paused(self, team_id, token, pending, pending, (provider, api_key), progress)
