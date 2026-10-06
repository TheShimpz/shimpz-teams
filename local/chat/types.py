"""Shared local chat bindings and continuation types."""

from dataclasses import dataclass
from http import HTTPStatus

from action import human as action_human
from chat import turn as chat_turn_engine
from inference import usage as brain_usage
from local.chat import continuation as local_chat_continuations
from local.errors import ApiProblemError
from local.install.runtime import AssistantSpec


@dataclass(frozen=True, slots=True)
class ActiveAssistant:
    spec: AssistantSpec
    container_id: str
    container: object | None = None


PendingLocalChat = local_chat_continuations.PendingLocalChat


@dataclass(frozen=True, slots=True)
class ResponseRequest:
    team_id: str
    token: str
    segment: chat_turn_engine.SegmentResult
    assistant_ids: tuple[str, ...]
    file_ids: tuple[str, ...]
    provider: str
    transcripts: tuple[action_human.ActionTranscript, ...] = ()
    requests_used: int = 0
    # What the turn consumed before this request; a chat turn always has one (ADR-0082).
    usage: brain_usage.TurnUsage | None = None
    # The recording of the logical turn, which a Routine card is built from (ADR-0101).
    recording: str | None = None


def required_active_assistant(
    bindings: dict[str, ActiveAssistant],
    assistant_id: str,
) -> ActiveAssistant:
    active = bindings.get(assistant_id)
    if active is None:
        raise ApiProblemError(
            HTTPStatus.CONFLICT,
            "Brain requested an unavailable Assistant",
            code="assistant-unavailable",
        )
    return active
