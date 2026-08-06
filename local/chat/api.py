"""Local chat start and integration-resume API operations."""

from http import HTTPStatus

from chat import orchestrator as chat_orchestrator
from chat import progress as chat_progress
from chat import turn as chat_turn_engine
from local.chat.segment import SegmentRequest as _ChatSegmentRequest
from local.chat.types import PendingLocalChat as _PendingLocalChat
from local.chat.types import ResponseRequest as _ResponseRequest
from local.errors import ApiProblemError as ApiProblem
from local.validation import validate_chat_assistant_ids, validate_team_id

MAX_CHAT_MESSAGE_CHARS = 16_000


def _pending_chat_continuation(self, team_id: str) -> dict[str, object] | None:
    self._expire_human_challenges()
    existing_human = self.human_challenges.current(team_id)
    if existing_human is not None:
        return self._human_response(existing_human)
    existing_integration = self.integration_challenges.current(team_id)
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
            transcripts=response.transcripts,
        )

    def complete(terminal: chat_orchestrator.ChatOutcome) -> dict[str, object]:
        self._delete_chat_continuation(team_id)
        if not self._commit_chat_terminal(team_id, token):
            raise ApiProblem(HTTPStatus.CONFLICT, "chat turn stopped", code="chat-stopped")
        return {"team_id": team_id, "team_name": segment.team_name, "reply": terminal.reply}

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


def chat(
    self,
    team_id: str,
    body: object,
    provider: str,
    api_key: str,
    progress: chat_progress.Reporter | None = None,
) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    if not isinstance(body, dict) or set(body) != {"message", "files", "assistant_ids"}:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Team chat requires only message, files, and assistant_ids",
            code="invalid-body",
        )
    message = body["message"]
    file_ids = body["files"]
    assistant_ids = validate_chat_assistant_ids(body["assistant_ids"])
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_CHAT_MESSAGE_CHARS or "\0" in message:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "message must be non-empty and within its size limit",
            code="invalid-message",
        )
    pending = self._pending_chat_continuation(team_id)
    if pending is not None:
        return pending
    with self._exclusive_chat_turn(team_id) as token:
        pending = self._pending_chat_continuation(team_id)
        if pending is not None:
            return pending
        segment = self._run_chat_segment(
            _ChatSegmentRequest(
                team_id=team_id,
                file_ids=file_ids,
                assistant_ids=assistant_ids,
                provider=provider,
                api_key=api_key,
                token=token,
                message=message,
                progress=progress or chat_progress.Reporter(),
            )
        )
        return self._segment_response(
            _ResponseRequest(team_id, token, segment, assistant_ids, tuple(file_ids), provider)
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

            def inspect(pending: object) -> chat_turn_engine.IntegrationResumeContext:
                if not isinstance(pending, _PendingLocalChat):
                    raise AssertionError("invalid local integration continuation")
                current = self._chat_setup(team_id, list(pending.file_ids), provider, pending.assistant_ids)
                bindings = {active.spec.assistant_id: active for active in current[2]}
                return chat_turn_engine.IntegrationResumeContext(
                    self._chat_identity(*current),
                    bindings,
                    pending.continuation.turn.powers,
                )

            admission = chat_turn_engine.admit_integration_resume(
                chat_turn_engine.IntegrationResumeStrategy(
                    store=self.integration_challenges,
                    team_id=team_id,
                    challenge_id=challenge_id,
                    pending_valid=lambda pending: (
                        isinstance(pending, _PendingLocalChat) and pending.provider == provider
                    ),
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
                    cancel_extra=lambda: self.oauth_pkce.cancel_team(team_id),
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
            )
        )
