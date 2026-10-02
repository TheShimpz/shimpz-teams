"""Pure request and identity validation for the local Team controller."""

import hashlib
import re
from http import HTTPStatus

from inference import config as inference_config
from local.errors import ApiProblemError

TEAM_ID_RE = re.compile(r"[a-z0-9_]{1,40}")
ASSISTANT_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
SPACE_ID_RE = re.compile(r"[a-z0-9][a-z0-9]*(?:-[a-z0-9]+)*")
DOCKER_ID_RE = re.compile(r"[0-9a-f]{12,64}")
MAX_TEAM_ID_LENGTH = 40
MAX_ASSISTANT_ID_LENGTH = 40
MAX_SPACE_ID_LENGTH = 48
MAX_CHAT_ASSISTANTS = 16
MIN_API_KEY_BYTES = 16
MAX_API_KEY_BYTES = 8 * 1024


def validate_team_id(value: str) -> str:
    if len(value) > MAX_TEAM_ID_LENGTH or TEAM_ID_RE.fullmatch(value) is None:
        raise ApiProblemError(HTTPStatus.UNPROCESSABLE_ENTITY, "invalid Team id", code="invalid-team-id")
    return value


def validate_team_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 80
        or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ApiProblemError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Team name must contain 1 to 80 trimmed characters",
            code="invalid-team-name",
        )
    return value


def validate_assistant_id(value: object) -> str:
    if not isinstance(value, str) or len(value) > MAX_ASSISTANT_ID_LENGTH or ASSISTANT_ID_RE.fullmatch(value) is None:
        raise ApiProblemError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "invalid Assistant id",
            code="invalid-assistant-id",
        )
    return value


def validate_chat_assistant_ids(value: object) -> tuple[str, ...]:
    """Return one explicit, bounded Assistant scope; empty means Brain-only."""
    if not isinstance(value, list) or len(value) > MAX_CHAT_ASSISTANTS:
        raise ApiProblemError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            f"assistant_ids must contain at most {MAX_CHAT_ASSISTANTS} ids",
            code="invalid-assistants",
        )
    try:
        assistant_ids = tuple(validate_assistant_id(item) for item in value)
    except ApiProblemError:
        raise ApiProblemError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "assistant_ids contains an invalid id",
            code="invalid-assistants",
        ) from None
    if len(set(assistant_ids)) != len(assistant_ids):
        raise ApiProblemError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "assistant_ids must not contain duplicate ids",
            code="invalid-assistants",
        )
    return tuple(sorted(assistant_ids))


DECISION_OPERATION = "chat-intent-route"
# Requests that carry the Team's model credential, whose digest the Supervisor assertion must bind.
MODEL_BOUND_OPERATIONS = frozenset(
    {
        "assistant-action-labels",
        "chat",
        "chat-capability-plan",
        "chat-intent-route",
        "chat-human-submit",
        "chat-integration-submit",
        "routine-run",
        "routine-human-submit",
        "routine-integration-submit",
    }
)


def credential_binding(provider: str, key: str) -> dict[str, str]:
    return {"provider": provider, "key_sha256": hashlib.sha256(key.encode("ascii")).hexdigest()}


def validate_decision_credential_header(values: list[str], operation: str) -> str | None:
    """Admit at most one TypeSafe key, and only on intent classification requests."""
    if not values:
        return None
    key = values[0]
    if (
        operation != DECISION_OPERATION
        or len(values) != 1
        or not isinstance(key, str)
        or not key.isascii()
        or not MIN_API_KEY_BYTES <= len(key) <= MAX_API_KEY_BYTES
        or any(not 33 <= ord(character) <= 126 for character in key)
    ):
        raise ApiProblemError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "a private decision credential is accepted only on intent classification",
            code="invalid-decision-credential",
        )
    return key


def decision_binding(key: str | None) -> dict[str, str] | None:
    return None if key is None else credential_binding("typesafe", key)


def validate_model_credential_headers(
    providers: list[str],
    api_keys: list[str],
) -> tuple[str, str]:
    """Validate the private Admin hand-off without copying a secret into an error."""
    if len(providers) != 1 or providers[0] not in inference_config.PROVIDERS or len(api_keys) != 1:
        raise ApiProblemError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "one private model credential is required",
            code="invalid-model-credential",
        )
    api_key = api_keys[0]
    if not isinstance(api_key, str) or api_key.strip() != api_key or not api_key.isascii():
        raise ApiProblemError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "private model credential is invalid",
            code="invalid-model-credential",
        )
    encoded = api_key.encode("ascii")
    if not MIN_API_KEY_BYTES <= len(encoded) <= MAX_API_KEY_BYTES or any(not 33 <= byte <= 126 for byte in encoded):
        raise ApiProblemError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "private model credential is invalid",
            code="invalid-model-credential",
        )
    return providers[0], api_key


def validate_space_id(value: str) -> str:
    if len(value) > MAX_SPACE_ID_LENGTH or SPACE_ID_RE.fullmatch(value) is None:
        raise RuntimeError("SHIMPZ_SPACE_ID must be a lowercase, dash-separated identifier")
    return value


def space_prefix(space_id: str) -> str:
    return hashlib.sha256(space_id.encode("ascii")).hexdigest()[:12]


def brain_thread_id(space_id: str, team_id: str, network_id: str) -> str:
    """Bind local conversation state to one immutable Team network generation."""
    if (
        not isinstance(space_id, str)
        or len(space_id) > MAX_SPACE_ID_LENGTH
        or SPACE_ID_RE.fullmatch(space_id) is None
        or not isinstance(team_id, str)
        or len(team_id) > MAX_TEAM_ID_LENGTH
        or TEAM_ID_RE.fullmatch(team_id) is None
        or not isinstance(network_id, str)
        or DOCKER_ID_RE.fullmatch(network_id) is None
    ):
        raise ApiProblemError(
            HTTPStatus.CONFLICT,
            "Team identity failed its persisted contract",
            code="ownership-conflict",
        )
    return f"local:{space_id}:{team_id}:{network_id}:default"


def routine_thread_id(space_id: str, team_id: str, network_id: str, run_id: str) -> str:
    """One Routine run's journal thread label, beside the Team's conversation in the same network generation."""
    if not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
        raise ApiProblemError(
            HTTPStatus.CONFLICT,
            "Team identity failed its persisted contract",
            code="ownership-conflict",
        )
    return brain_thread_id(space_id, team_id, network_id).removesuffix(":default") + f":routine-{run_id}"


def half_cpu_set(processors: int) -> str:
    if isinstance(processors, bool) or not isinstance(processors, int) or processors < 1:
        raise RuntimeError("the Docker daemon reported an invalid CPU count")
    available = max(1, processors // 2)
    return "0" if available == 1 else f"0-{available - 1}"
