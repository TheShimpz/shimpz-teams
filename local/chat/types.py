"""Shared local chat bindings and continuation types."""

from dataclasses import dataclass
from http import HTTPStatus

from chat import turn as chat_turn_engine
from local.chat import continuation as local_chat_continuations
from local.errors import ApiProblemError
from local.install.runtime import AssistantSpec
from power import human as power_human


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
    transcripts: tuple[power_human.PowerTranscript, ...] = ()


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
