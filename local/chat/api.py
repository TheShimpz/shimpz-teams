"""Local chat start and integration-resume API operations."""

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
from local.routine import lineage as routine_lineage
from local.routine import question as routine_question
from local.validation import validate_chat_assistant_ids, validate_team_id
from protocol.http.v1 import payload as http_payload
from routine import schedule as routine_schedule
from routine.request import Request as RoutineRequest

MAX_CHAT_MESSAGE_CHARS = 16_000


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

    def commit(terminal: chat_orchestrator.ChatOutcome) -> bool:
        if terminal.routine is None or terminal.clarification is not None:
            # A Routine question changes nothing until a bound answer selects one of its options.
            return self._commit_chat_terminal(team_id, token, lambda: save_knowledge(terminal))
        # A compiled Routine change commits with the reply, in one write under the lifecycle lock and the Stop guard;
        # when Stop wins, nothing is created (ADR-0092).
        with self._lock(team_id):
            write = self._routine_change(response, terminal.routine)
            return self._commit_chat_terminal(team_id, token, lambda: (write(), save_knowledge(terminal)))

    def complete(terminal: chat_orchestrator.ChatOutcome) -> dict[str, object]:
        self._delete_chat_continuation(team_id)
        question = None
        if terminal.routine is not None and terminal.clarification is not None:
            # Every option's Routine is admitted before the question is shown; only a bound answer commits one.
            question = self._routine_question(response, terminal.routine, terminal.clarification)
        if not commit(terminal):
            raise ApiProblem(HTTPStatus.CONFLICT, "chat turn stopped", code="chat-stopped")
        if question is not None:
            self.routine_lineage.record(team_id, question)
        body: dict[str, object] = {
            "team_id": team_id,
            "team_name": segment.team_name,
            "reply": terminal.reply,
            "clarification": terminal.clarification,
        }
        usage = None if response.usage is None else response.usage.joined().wire()
        if usage is not None:
            body["usage"] = usage
        return chat_turn_engine.with_restricted_actions(body, terminal)

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
        principal = local_audit.human_principal()
        bound = None if principal is None or file_ids else self.routine_lineage.bound(team_id, principal, message)
        composed = routine_lineage.composed(message)
        # Every admitted send is recorded once; a send with files or a composed answer is a barrier (ADR-0092). None:
        # this identity cannot change a Routine (reused with another message or person, or no room to freeze its run).
        earlier = (
            ()
            if principal is None
            else self.routine_recent.admit(team_id, principal, identity, message, not file_ids and not composed)
        )
        bound = None if earlier is None else bound
        # A message that answers a clarification may change a Routine only through the question it is bound to.
        routine_request = (
            None
            if principal is None or earlier is None or (bound is None and composed)
            else RoutineRequest(
                principal, message, identity["issued_at"], identity["nonce"], timezone, locale, earlier=earlier
            )
        )
        if bound is not None:
            return routine_question.answer(self, team_id, token, routine_request, bound)
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
                routine_request=routine_request,
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
                routine_request=routine_request,
            )
        )


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
                    expired_error=lambda: ApiProblem(
                        HTTPStatus.CONFLICT,
                        "Assistant integration request expired; retry the message",
                        code="assistant-integration-challenge-expired",
                    ),
                    context_error=lambda: ApiProblem(
                        HTTPStatus.CONFLICT,
                        "Team capabilities changed; retry",
                        code="team-context-changed",
                    ),
                    contract_error=lambda: ApiProblem(
                        HTTPStatus.CONFLICT,
                        "Assistant integration contract is unavailable",
                        code="assistant-integration-contract-invalid",
                    ),
                    end_drifted=lambda challenge: local_chat_pause._end_drifted_turn(self, team_id, challenge),
                )
            )
            if admission.response is not None:
                return admission.response
            pending = admission.pending
            if not isinstance(pending, _PendingLocalChat):
                raise AssertionError("shared integration resume returned invalid state")
        segment = self._run_chat_segment(
            _ChatSegmentRequest(
                team_id=team_id,
                file_ids=list(pending.file_ids),
                assistant_ids=pending.assistant_ids,
                provider=provider,
                api_key=api_key,
                token=token,
                continuation=pending.continuation,
                expected_identity=pending.identity,
                transcripts=pending.transcripts,
                requests_used=pending.requests_used,
                locale=pending.locale,
                progress=progress or chat_progress.Reporter(),
            )
        )
        return self._segment_response(
            _ResponseRequest(
                team_id,
                token,
                segment,
                pending.assistant_ids,
                pending.file_ids,
                provider,
                pending.transcripts,
                pending.requests_used,
                usage=pending.usage,
            )
        )
