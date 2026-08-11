"""Closed JSON codec for encrypted local Team chat continuations."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass

from action import challenges as action_challenges
from action import human as action_human
from chat import orchestrator as chat_orchestrator
from core import strict_json
from inference import client as brain_runtime_client
from inference import config as inference_config
from integrations import challenges as integration_challenges
from local.chat import continuation_store as local_chat_continuation_store

SCHEMA_VERSION = 2
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 4096
MAX_INVOKED_ACTIONS = 512
MAX_IDENTITY_ASSISTANTS = 16
MAX_IDENTITY_FILES = 8
_FILE_ID = re.compile(r"[0-9a-f]{32}\Z")
_IMAGE = re.compile(r"[^\s\x00-\x1f\x7f]{1,512}@sha256:[0-9a-f]{64}\Z")
_NETWORK_ID = re.compile(r"[^\s\x00-\x1f\x7f]{1,256}\Z")
_CONTAINER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")


class ContinuationCodecError(RuntimeError):
    """A decrypted local continuation violated the closed runtime contract."""


@dataclass(frozen=True, slots=True)
class PendingLocalChat:
    """Secret-free state required to replay one paused local Team turn."""

    continuation: chat_orchestrator.ChatContinuation
    assistant_ids: tuple[str, ...]
    file_ids: tuple[str, ...]
    provider: str
    identity: tuple[object, ...]
    transcripts: tuple[action_human.ActionTranscript, ...] = ()


@dataclass(frozen=True, slots=True)
class DecodedContinuation:
    kind: str
    requirements: tuple[object, ...]
    pending: PendingLocalChat


def _mapping(value: object, fields: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ContinuationCodecError(f"{label} is malformed")
    return value


def _sequence(value: object, maximum: int, label: str) -> list[object]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ContinuationCodecError(f"{label} is malformed")
    return value


def _text(
    value: object,
    maximum: int,
    label: str,
    *,
    optional: bool = False,
) -> str | None:
    if optional and value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or not value.isprintable()
    ):
        raise ContinuationCodecError(f"{label} is malformed")
    return value


def _component_id(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) > 80 or brain_runtime_client.ACTION_ID_RE.fullmatch(value) is None:
        raise ContinuationCodecError(f"{label} is malformed")
    return value


def _interrupt_id(value: object) -> str:
    if not isinstance(value, str) or brain_runtime_client.SAFE_ID_RE.fullmatch(value) is None:
        raise ContinuationCodecError("continuation interrupt is malformed")
    return value


def _json_value(value: object) -> object:
    budget = [MAX_JSON_NODES]

    def visit(item: object, depth: int) -> object:
        budget[0] -= 1
        if budget[0] < 0 or depth > MAX_JSON_DEPTH:
            raise ContinuationCodecError("continuation JSON exceeds its structure limit")
        if item is None or isinstance(item, bool | str):
            return item
        if isinstance(item, int) and not isinstance(item, bool):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ContinuationCodecError("continuation JSON contains a non-finite number")
            return item
        if isinstance(item, list | tuple):
            return [visit(nested, depth + 1) for nested in item]
        if isinstance(item, dict):
            result: dict[str, object] = {}
            for key, nested in item.items():
                if not isinstance(key, str) or len(key) > 128 or key in result:
                    raise ContinuationCodecError("continuation JSON object is malformed")
                result[key] = visit(nested, depth + 1)
            return result
        raise ContinuationCodecError("continuation contains a non-JSON value")

    return visit(value, 0)


def _turn_payload(turn: brain_runtime_client.RuntimeTurn) -> dict[str, object]:
    return {
        "status": turn.status,
        "reply": turn.reply,
        "actions": [
            {
                "interrupt_id": request.interrupt_id,
                "assistant_id": request.assistant_id,
                "action": request.action,
                "input": _json_value(dict(request.input)),
            }
            for request in turn.actions
        ],
    }


def _pending_payload(pending: PendingLocalChat) -> dict[str, object]:
    if not isinstance(pending, PendingLocalChat):
        raise ContinuationCodecError("pending continuation is malformed")
    identity = _identity_payload(pending.identity)
    return {
        "continuation": {
            "turn": _turn_payload(pending.continuation.turn),
            "seen_interrupts": list(pending.continuation.seen_interrupts),
            "invoked": [asdict(item) for item in pending.continuation.invoked],
            "round_index": pending.continuation.round_index,
        },
        "assistant_ids": list(pending.assistant_ids),
        "file_ids": list(pending.file_ids),
        "provider": pending.provider,
        "identity": identity,
        "transcripts": _transcripts_payload(pending.transcripts),
    }


def _transcripts_payload(transcripts: tuple[action_human.ActionTranscript, ...]) -> list[dict[str, object]]:
    if (
        not isinstance(transcripts, tuple)
        or len({item.interrupt_id for item in transcripts}) != len(transcripts)
        or sum(len(item.responses) for item in transcripts) > action_human.MAX_REQUESTS_PER_TURN
    ):
        raise ContinuationCodecError("pending human transcripts are malformed")
    payload: list[dict[str, object]] = []
    for transcript in transcripts:
        if not isinstance(transcript, action_human.ActionTranscript) or any(
            response.secret for response in transcript.responses
        ):
            raise ContinuationCodecError("secret human responses cannot be persisted")
        payload.append(
            {
                "interrupt_id": _interrupt_id(transcript.interrupt_id),
                "responses": [_json_value(response.payload()) for response in transcript.responses],
            }
        )
    return payload


def _identity_payload(identity: tuple[object, ...]) -> dict[str, object]:
    if not isinstance(identity, tuple) or len(identity) != 5:
        raise ContinuationCodecError("continuation Team identity is malformed")
    team_name, network_id, assistants, files, config = identity
    if not isinstance(config, inference_config.InferenceConfig):
        raise ContinuationCodecError("continuation inference identity is malformed")
    if not isinstance(assistants, tuple) or not isinstance(files, list):
        raise ContinuationCodecError("continuation Team identity is malformed")
    return {
        "team_name": team_name,
        "network_id": network_id,
        "assistants": [list(item) if isinstance(item, tuple) else item for item in assistants],
        "files": _json_value(files),
        "inference": {"provider": config.provider, "model": config.model},
    }


def _requirements_payload(kind: str, requirements: tuple[object, ...]) -> list[dict[str, object]]:
    if not requirements:
        raise ContinuationCodecError("continuation requirements are malformed")
    if kind == "integrations" and all(
        isinstance(item, integration_challenges.IntegrationRequirement) for item in requirements
    ):
        return [_json_value(asdict(item)) for item in requirements]  # type: ignore[list-item]
    if kind == "human" and len(requirements) == 1 and isinstance(requirements[0], action_challenges.HumanRequirement):
        requirement = requirements[0]
        return [
            {
                "assistant_id": requirement.assistant_id,
                "assistant_name": requirement.assistant_name,
                "action_id": requirement.action_id,
                "action_summary": requirement.action_summary,
                "interrupt_id": requirement.interrupt_id,
                "request": _json_value(requirement.request.payload()),
                "assistant_version": requirement.assistant_version,
            }
        ]
    raise ContinuationCodecError("continuation requirements are malformed")


def _release_images(pending: PendingLocalChat) -> dict[str, str]:
    identity = _identity_payload(pending.identity)
    images: dict[str, str] = {}
    for raw in identity["assistants"]:
        if not isinstance(raw, list) or len(raw) != 3:
            raise ContinuationCodecError("continuation Assistant identity is malformed")
        assistant = _component_id(raw[0], "continuation Assistant identity")
        image = raw[1]
        if not isinstance(image, str) or _IMAGE.fullmatch(image) is None:
            raise ContinuationCodecError("continuation Assistant release is malformed")
        images[assistant] = image
    return images


def _bindings(kind: str, requirements: tuple[object, ...], pending: PendingLocalChat) -> tuple[str, ...]:
    images = _release_images(pending)
    bindings: set[str] = set()
    if kind == "integrations":
        for requirement in requirements:
            assistant = _component_id(requirement.assistant_id, "continuation binding Assistant")
            image = images.get(assistant)
            if image is None:
                raise ContinuationCodecError("continuation release binding is malformed")
            for action_id in requirement.action_ids:
                action = _component_id(action_id, "continuation binding Action")
                bindings.add(f"{assistant}/{action}/{image}/-")
    elif kind == "human" and len(requirements) == 1:
        requirement = requirements[0]
        assistant = _component_id(requirement.assistant_id, "continuation binding Assistant")
        action = _component_id(requirement.action_id, "continuation binding Action")
        image = images.get(assistant)
        if image is None or not isinstance(requirement.request, action_human.HumanRequest):
            raise ContinuationCodecError("continuation release binding is malformed")
        bindings.add(f"{assistant}/{action}/{image}/{requirement.request.fingerprint}")
    else:
        raise ContinuationCodecError("continuation kind is malformed")
    return tuple(sorted(bindings))


def encode(
    kind: str,
    requirements: tuple[object, ...],
    pending: PendingLocalChat,
) -> tuple[tuple[str, ...], bytes]:
    """Encode one authenticated plaintext payload and its AAD release bindings."""
    body = {
        "schema": SCHEMA_VERSION,
        "kind": kind,
        "requirements": _requirements_payload(kind, requirements),
        "pending": _pending_payload(pending),
    }
    try:
        payload = json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ContinuationCodecError("continuation could not be encoded") from exc
    if not 1 <= len(payload) <= local_chat_continuation_store.MAX_PLAINTEXT_BYTES:
        raise ContinuationCodecError("continuation exceeds its fixed byte limit")
    return _bindings(kind, requirements, pending), payload


def _decode_payload(payload: bytes) -> dict[str, object]:
    try:
        value = strict_json.loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ContinuationCodecError("continuation is not valid JSON") from exc
    return _mapping(value, {"schema", "kind", "requirements", "pending"}, "continuation")


def _action_request(value: object) -> brain_runtime_client.ActionRequest:
    raw = _mapping(
        value,
        {"interrupt_id", "assistant_id", "action", "input"},
        "continuation Action request",
    )
    action_input = _json_value(raw["input"])
    if not isinstance(action_input, dict):
        raise ContinuationCodecError("continuation Action input is malformed")
    return brain_runtime_client.ActionRequest(
        interrupt_id=_interrupt_id(raw["interrupt_id"]),
        assistant_id=_component_id(raw["assistant_id"], "continuation Action Assistant"),
        action=_component_id(raw["action"], "continuation Action"),
        input=action_input,
    )


def _continuation(value: object) -> chat_orchestrator.ChatContinuation:
    raw = _mapping(
        value,
        {"turn", "seen_interrupts", "invoked", "round_index"},
        "Brain continuation",
    )
    turn_value = _mapping(raw["turn"], {"status", "reply", "actions"}, "Brain turn")
    turn = brain_runtime_client.BrainRuntimeClient._parse_turn(
        {
            "status": turn_value["status"],
            "reply": turn_value["reply"],
            "actions": [
                {
                    "interrupt_id": item.interrupt_id,
                    "assistant_id": item.assistant_id,
                    "action": item.action,
                    "input": dict(item.input),
                }
                for item in (
                    _action_request(action)
                    for action in _sequence(
                        turn_value["actions"],
                        brain_runtime_client.MAX_ACTION_REQUESTS,
                        "Brain turn Actions",
                    )
                )
            ],
        }
    )
    seen = tuple(
        _interrupt_id(item)
        for item in _sequence(
            raw["seen_interrupts"],
            brain_runtime_client.MAX_ACTION_REQUESTS * 8,
            "seen Brain interrupts",
        )
    )
    if len(seen) != len(set(seen)):
        raise ContinuationCodecError("seen Brain interrupts are malformed")
    invoked: list[chat_orchestrator.InvokedAction] = []
    for item in _sequence(raw["invoked"], MAX_INVOKED_ACTIONS, "invoked Actions"):
        entry = _mapping(item, {"assistant_id", "action"}, "invoked Action")
        invoked.append(
            chat_orchestrator.InvokedAction(
                _component_id(entry["assistant_id"], "invoked Action Assistant"),
                _component_id(entry["action"], "invoked Action"),
            )
        )
    round_index = raw["round_index"]
    if type(round_index) is not int or not 0 <= round_index < chat_orchestrator.MAX_ACTION_ROUNDS:
        raise ContinuationCodecError("continuation round is malformed")
    return chat_orchestrator.ChatContinuation(turn, seen, tuple(invoked), round_index)


def _identity(value: object) -> tuple[object, ...]:
    raw = _mapping(
        value,
        {"team_name", "network_id", "assistants", "files", "inference"},
        "continuation Team identity",
    )
    team_name = _text(raw["team_name"], 80, "continuation Team name")
    network_id = raw["network_id"]
    if not isinstance(network_id, str) or _NETWORK_ID.fullmatch(network_id) is None:
        raise ContinuationCodecError("continuation network identity is malformed")
    assistants: list[tuple[str, str, str]] = []
    for item in _sequence(raw["assistants"], MAX_IDENTITY_ASSISTANTS, "continuation Assistants"):
        if not isinstance(item, list) or len(item) != 3:
            raise ContinuationCodecError("continuation Assistant identity is malformed")
        assistant = _component_id(item[0], "continuation Assistant identity")
        image = item[1]
        container = item[2]
        if (
            not isinstance(image, str)
            or _IMAGE.fullmatch(image) is None
            or not isinstance(container, str)
            or _CONTAINER_ID.fullmatch(container) is None
        ):
            raise ContinuationCodecError("continuation Assistant identity is malformed")
        assistants.append((assistant, image, container))
    if len({item[0] for item in assistants}) != len(assistants):
        raise ContinuationCodecError("continuation Assistant identity is malformed")
    files: list[dict[str, object]] = []
    for item in _sequence(raw["files"], MAX_IDENTITY_FILES, "continuation files"):
        entry = _mapping(item, {"id", "name", "media_type", "size"}, "continuation file")
        if (
            not isinstance(entry["id"], str)
            or _FILE_ID.fullmatch(entry["id"]) is None
            or _text(entry["name"], 255, "continuation filename") in {".", ".."}
            or not isinstance(entry["media_type"], str)
            or not 1 <= len(entry["media_type"]) <= 127
            or type(entry["size"]) is not int
            or not 0 <= entry["size"] <= 2**53 - 1
        ):
            raise ContinuationCodecError("continuation file is malformed")
        files.append(dict(entry))
    if len({item["id"] for item in files}) != len(files):
        raise ContinuationCodecError("continuation files are malformed")
    inference = _mapping(raw["inference"], {"provider", "model"}, "continuation inference")
    try:
        config = inference_config.normalize(inference["provider"], inference["model"])
    except inference_config.InferenceConfigError as exc:
        raise ContinuationCodecError("continuation inference is malformed") from exc
    return team_name, network_id, tuple(assistants), files, config


def _pending(value: object) -> PendingLocalChat:
    raw = _mapping(
        value,
        {
            "continuation",
            "assistant_ids",
            "file_ids",
            "provider",
            "identity",
            "transcripts",
        },
        "pending continuation",
    )
    assistant_ids = tuple(
        _component_id(item, "pending Assistant") for item in _sequence(raw["assistant_ids"], 16, "pending Assistants")
    )
    if len(assistant_ids) != len(set(assistant_ids)) or tuple(sorted(assistant_ids)) != assistant_ids:
        raise ContinuationCodecError("pending Assistants are malformed")
    file_ids = tuple(
        item
        for item in _sequence(raw["file_ids"], MAX_IDENTITY_FILES, "pending files")
        if isinstance(item, str) and _FILE_ID.fullmatch(item) is not None
    )
    if len(file_ids) != len(raw["file_ids"]) or len(file_ids) != len(set(file_ids)):
        raise ContinuationCodecError("pending files are malformed")
    provider = raw["provider"]
    if not isinstance(provider, str) or provider not in inference_config.PROVIDERS:
        raise ContinuationCodecError("pending provider is malformed")
    identity = _identity(raw["identity"])
    if identity[4].provider != provider:
        raise ContinuationCodecError("pending provider binding is malformed")
    return PendingLocalChat(
        continuation=_continuation(raw["continuation"]),
        assistant_ids=assistant_ids,
        file_ids=file_ids,
        provider=provider,
        identity=identity,
        transcripts=_transcripts(raw["transcripts"]),
    )


def _human_response(value: object, ordinal: int) -> action_human.HumanResponse:
    raw = _mapping(value, {"kind", "ordinal", "fingerprint", "value"}, "human response")
    kind = raw["kind"]
    fingerprint = raw["fingerprint"]
    response_value = _json_value(raw["value"])
    if (
        not isinstance(kind, str)
        or kind == "input:password"
        or type(raw["ordinal"]) is not int
        or raw["ordinal"] != ordinal
        or not isinstance(fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        or ((kind == "approval" or kind in action_human.AUTH_KINDS) and response_value is not True)
    ):
        raise ContinuationCodecError("human response is malformed")
    return action_human.HumanResponse(kind, ordinal, fingerprint, response_value)


def _transcripts(value: object) -> tuple[action_human.ActionTranscript, ...]:
    transcripts: list[action_human.ActionTranscript] = []
    count = 0
    for item in _sequence(value, action_human.MAX_REQUESTS_PER_TURN, "human transcripts"):
        raw = _mapping(item, {"interrupt_id", "responses"}, "human transcript")
        responses = tuple(
            _human_response(response, ordinal)
            for ordinal, response in enumerate(
                _sequence(raw["responses"], action_human.MAX_REQUESTS_PER_ACTION, "human responses")
            )
        )
        count += len(responses)
        transcripts.append(action_human.ActionTranscript(_interrupt_id(raw["interrupt_id"]), responses))
    if count > action_human.MAX_REQUESTS_PER_TURN or len({item.interrupt_id for item in transcripts}) != len(
        transcripts
    ):
        raise ContinuationCodecError("human transcripts are malformed")
    return tuple(transcripts)


def _tuple_text(value: object, maximum: int, label: str) -> tuple[str, ...]:
    result = tuple(str(_text(item, maximum, label)) for item in _sequence(value, 128, label))
    if not result or len(result) != len(set(result)) or tuple(sorted(result)) != result:
        raise ContinuationCodecError(f"{label} is malformed")
    return result


def _integration_requirement(value: object) -> integration_challenges.IntegrationRequirement:
    raw = _mapping(
        value,
        {"assistant_id", "assistant_name", "action_ids", "integrations"},
        "integration requirement",
    )
    integrations: list[tuple[str, str, tuple[str, ...]]] = []
    for item in _sequence(raw["integrations"], 16, "integration requirement integrations"):
        if not isinstance(item, list) or len(item) != 3:
            raise ContinuationCodecError("integration requirement is malformed")
        integrations.append(
            (
                _component_id(item[0], "integration id"),
                _component_id(item[1], "integration provider"),
                _tuple_text(item[2], 128, "integration scopes"),
            )
        )
    if not integrations:
        raise ContinuationCodecError("integration requirement is malformed")
    return integration_challenges.IntegrationRequirement(
        _component_id(raw["assistant_id"], "integration Assistant"),
        str(_text(raw["assistant_name"], 80, "integration Assistant name")),
        _tuple_text(raw["action_ids"], 80, "integration Actions"),
        tuple(integrations),
    )


def _human_requirement(value: object) -> action_challenges.HumanRequirement:
    raw = _mapping(
        value,
        {
            "assistant_id",
            "assistant_name",
            "action_id",
            "action_summary",
            "interrupt_id",
            "request",
            "assistant_version",
        },
        "human requirement",
    )
    request_value = raw["request"]
    if not isinstance(request_value, dict) or not isinstance(request_value.get("kind"), str):
        raise ContinuationCodecError("human requirement request is malformed")
    try:
        request = action_human.validate_request(request_value, (request_value["kind"],))
    except action_human.HumanRequestError as exc:
        raise ContinuationCodecError("human requirement request is malformed") from exc
    return action_challenges.HumanRequirement(
        _component_id(raw["assistant_id"], "human Assistant"),
        str(_text(raw["assistant_name"], 80, "human Assistant name")),
        _component_id(raw["action_id"], "human Action"),
        str(_text(raw["action_summary"], 500, "human Action summary")),
        _interrupt_id(raw["interrupt_id"]),
        request,
        str(_text(raw["assistant_version"], 40, "human Assistant version")),
    )


def decode(
    stored: local_chat_continuation_store.StoredContinuation,
) -> DecodedContinuation:
    """Authenticate structural bindings again after decrypting one record."""
    if not isinstance(stored, local_chat_continuation_store.StoredContinuation):
        raise ContinuationCodecError("stored continuation is malformed")
    body = _decode_payload(stored.payload)
    if body["schema"] != SCHEMA_VERSION or body["kind"] != stored.kind:
        raise ContinuationCodecError("stored continuation contract changed")
    raw_requirements = _sequence(body["requirements"], 64, "continuation requirements")
    if stored.kind == "integrations":
        requirements = tuple(_integration_requirement(item) for item in raw_requirements)
    elif stored.kind == "human" and len(raw_requirements) == 1:
        requirements = (_human_requirement(raw_requirements[0]),)
    else:
        raise ContinuationCodecError("stored continuation kind is malformed")
    if not requirements:
        raise ContinuationCodecError("continuation requirements are malformed")
    pending = _pending(body["pending"])
    if _bindings(stored.kind, requirements, pending) != stored.bindings:
        raise ContinuationCodecError("stored continuation release binding changed")
    return DecodedContinuation(stored.kind, requirements, pending)
