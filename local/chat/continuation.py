"""Closed JSON codec for encrypted local Team chat continuations."""

import math
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass

from action import challenges as action_challenges
from action import files as action_files
from action import human as action_human
from action import journal as action_journal
from assistant import action_schema
from chat import orchestrator as chat_orchestrator
from core import canonical_json, identifier
from inference import client as brain_runtime_client
from inference import config as inference_config
from inference import usage as brain_usage
from integrations import challenges as integration_challenges
from local.chat import continuation_store as local_chat_continuation_store
from local.errors import ApiProblemError
from local.validation import validate_team_name
from protocol.assistant.v1.validators import message_catalog as catalog_validator
from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import strict_json
from routine import plan as routine_plan

SCHEMA_VERSION = 7
_RECORDING_RE = re.compile(r"[0-9a-f]{32}\Z")
MAX_INVOKED_ACTIONS = 512
MAX_IDENTITY_ASSISTANTS = http_payload.MAX_CHAT_ASSISTANTS
MAX_IDENTITY_FILES = 8
# A turn's wall-clock admission in epoch milliseconds, within the exact JSON integer range.
MAX_STARTED_MS = 2**53 - 1
# A frozen Routine run's continuation (ADR-0092 amendment, 2026-10-05, scale): what a chat continuation may hold, beside
# the one pending request's resolved input, every step's interrupt id, and the unfinished Action's earlier answers at
# their longest, each control character escaped to six bytes; and the record its store keeps, with its kind, its
# release bindings, and the base64 plaintext.
MAX_ROUTINE_PLAINTEXT_BYTES = (
    local_chat_continuation_store.MAX_PLAINTEXT_BYTES
    + routine_plan.MAX_RESOLVED_INPUT_BYTES
    + routine_plan.MAX_STEPS * 32
    + (action_human.MAX_REQUESTS_PER_ACTION - 1) * (6 * max(action_human.LENGTH_KINDS.values()) + 512)
)
MAX_ROUTINE_BYTES = (
    4 * -(-MAX_ROUTINE_PLAINTEXT_BYTES // 3)
    + local_chat_continuation_store.MAX_BINDINGS * (local_chat_continuation_store.MAX_BINDING_BYTES + 3)
    + 256
)
_IMAGE = re.compile(r"(?:sha256:[0-9a-f]{64}|[^\s\x00-\x1f\x7f]{1,512}@sha256:[0-9a-f]{64})\Z")
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
    requests_used: int = 0
    # The interface language the turn's start pinned (ADR-0091).
    locale: str | None = None
    # What a paused chat turn consumed so far (ADR-0082); a Routine run carries none.
    usage: brain_usage.TurnUsage | None = None
    # The fingerprint of the Action batch a human request paused, which ending the turn removes exactly; an
    # Integration pause holds no batch.
    paused_batch: str | None = None
    # The memory-only recording a new turn may define a Routine from, by id; it carries no message and no secret, and
    # a Team restart leaves it naming nothing (ADR-0101 section 4.1).
    recording: str | None = None
    # The model the turn's start ran on, of its provider, which every resume runs and is metered on; a Routine run,
    # which asks no model, carries none.
    model: str | None = None


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


def _component_id(value: object, label: str, canonical: Callable[[object], str | None]) -> str:
    return identifier.require(canonical, value, ContinuationCodecError, f"{label} is malformed")


def _interrupt_id(value: object) -> str:
    if not isinstance(value, str) or action_journal.SAFE_ID_RE.fullmatch(value) is None:
        raise ContinuationCodecError("continuation interrupt is malformed")
    return value


def _json_value(value: object) -> object:
    # Admission bounds an Action payload's depth and nothing else, so the walk applies that depth and no narrower
    # structure limit. Every value costs at least one encoded byte, so this budget refuses only what the fixed byte
    # limit would refuse anyway while still bounding the walk over an in-memory value.
    budget = [local_chat_continuation_store.MAX_PLAINTEXT_BYTES]

    def visit(item: object, depth: int) -> object:
        budget[0] -= 1
        if budget[0] < 0 or depth > action_schema.MAX_PAYLOAD_DEPTH:
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
                if not isinstance(key, str) or key in result:
                    raise ContinuationCodecError("continuation JSON object is malformed")
                result[key] = visit(nested, depth + 1)
            return result
        raise ContinuationCodecError("continuation contains a non-JSON value")

    return visit(value, 0)


def _turn_payload(turn: brain_runtime_client.RuntimeTurn) -> dict[str, object]:
    # A suspended turn requested Actions, so it never carries a clarification, memory, or Routine change.
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
            "invoked": [{**asdict(item), "inputs": list(item.inputs)} for item in pending.continuation.invoked],
            "round_index": pending.continuation.round_index,
            "file_actions": pending.continuation.file_actions,
        },
        "assistant_ids": list(pending.assistant_ids),
        "file_ids": list(pending.file_ids),
        "provider": pending.provider,
        "identity": identity,
        "transcripts": _transcripts_payload(pending.transcripts),
        "requests_used": _requests_used(pending.requests_used),
        "locale": pending.locale,
        "usage": _usage_payload(pending.usage),
        "paused_batch": pending.paused_batch,
        "recording": pending.recording,
        "model": pending.model,
    }


def _usage_payload(usage: object) -> dict[str, object] | None:
    if usage is None:
        return None
    if not isinstance(usage, brain_usage.TurnUsage):
        raise ContinuationCodecError("pending turn usage is malformed")
    return _usage_value(
        {
            "started_ms": usage.started_ms,
            "models": [
                {"provider": provider, "model": model, "input_tokens": inputs, "output_tokens": outputs}
                for provider, model, inputs, outputs in usage.models
            ],
        }
    )


def _usage(value: object) -> brain_usage.TurnUsage | None:
    if value is None:
        return None
    raw = _usage_value(value)
    return brain_usage.TurnUsage(
        raw["started_ms"],
        tuple(
            (model["provider"], model["model"], model["input_tokens"], model["output_tokens"])
            for model in raw["models"]
        ),
    )


def _usage_value(value: object) -> dict[str, object]:
    """A turn's start and its models in the closed wire shape, which also admits no models before any call."""
    raw = _mapping(value, {"started_ms", "models"}, "pending turn usage")
    started_ms = raw["started_ms"]
    models = raw["models"]
    if type(started_ms) is not int or not 0 <= started_ms <= MAX_STARTED_MS or not isinstance(models, list):
        raise ContinuationCodecError("pending turn usage is malformed")
    if models and http_payload.canonical_turn_usage({"duration_ms": 0, "models": models}) is None:
        raise ContinuationCodecError("pending turn usage is malformed")
    return raw


def _transcripts_payload(transcripts: tuple[action_human.ActionTranscript, ...]) -> list[dict[str, object]]:
    if (
        not isinstance(transcripts, tuple)
        or len({item.interrupt_id for item in transcripts}) != len(transcripts)
        or sum(len(item.responses) for item in transcripts) > action_human.MAX_REQUESTS_PER_TURN
    ):
        raise ContinuationCodecError("pending human transcripts are malformed")
    payload: list[dict[str, object]] = []
    for transcript in transcripts:
        if not isinstance(transcript, action_human.ActionTranscript):
            raise ContinuationCodecError("pending human transcripts are malformed")
        payload.append(
            {
                "interrupt_id": _interrupt_id(transcript.interrupt_id),
                "responses": [_json_value(response.payload()) for response in transcript.responses],
            }
        )
    return payload


def _requests_used(value: object) -> int:
    if type(value) is not int or not 0 <= value <= action_human.MAX_REQUESTS_PER_TURN:
        raise ContinuationCodecError("human request budget is malformed")
    return value


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
        "inference": {"provider": config.provider, "model": config.model, "effort": config.effort},
    }


def _requirements_payload(kind: str, requirements: tuple[object, ...]) -> list[dict[str, object]]:
    if not requirements:
        raise ContinuationCodecError("continuation requirements are malformed")
    if kind == "integrations" and all(
        isinstance(item, integration_challenges.IntegrationRequirement) for item in requirements
    ):
        return [_json_value(asdict(item)) for item in requirements]
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
                "messages": _json_value(requirement.request.messages()),
                "assistant_version": requirement.assistant_version,
                "copy": {
                    "locale": requirement.copy.locale,
                    "catalog_digest": requirement.copy.catalog_digest,
                    "pack_digest": requirement.copy.pack_digest,
                    "rendered": _json_value(requirement.copy.rendered),
                    "help": requirement.copy.help,
                },
                "help_url": requirement.help_url,
                "help_text": requirement.help_text,
                "purpose": requirement.purpose,
                "purpose_locale": requirement.purpose_locale,
                "file": None if requirement.file is None else _json_value(dict(requirement.file)),
            }
        ]
    raise ContinuationCodecError("continuation requirements are malformed")


def _release_images(pending: PendingLocalChat) -> dict[str, str]:
    identity = _identity_payload(pending.identity)
    images: dict[str, str] = {}
    for raw in identity["assistants"]:
        if not isinstance(raw, list) or len(raw) != 3:
            raise ContinuationCodecError("continuation Assistant identity is malformed")
        assistant = _component_id(raw[0], "continuation Assistant identity", http_payload.canonical_assistant_id)
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
            assistant = _component_id(
                requirement.assistant_id, "continuation binding Assistant", http_payload.canonical_assistant_id
            )
            image = images.get(assistant)
            if image is None:
                raise ContinuationCodecError("continuation release binding is malformed")
            for action_id in requirement.action_ids:
                action = _component_id(action_id, "continuation binding Action", http_payload.canonical_action_id)
                bindings.add(f"{assistant}/{action}/{image}/-")
    elif kind == "human" and len(requirements) == 1:
        requirement = requirements[0]
        assistant = _component_id(
            requirement.assistant_id, "continuation binding Assistant", http_payload.canonical_assistant_id
        )
        action = _component_id(requirement.action_id, "continuation binding Action", http_payload.canonical_action_id)
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
    *,
    limit: int = local_chat_continuation_store.MAX_PLAINTEXT_BYTES,
) -> tuple[tuple[str, ...], bytes]:
    """Encode one authenticated plaintext payload and its AAD release bindings, within its store's ``limit``.

    A chat keeps the chat store's bound; a frozen Routine run's store derives its own (``local.routine.store``).
    """
    body = {
        "schema": SCHEMA_VERSION,
        "kind": kind,
        "requirements": _requirements_payload(kind, requirements),
        "pending": _pending_payload(pending),
    }
    try:
        payload = canonical_json.encode(body)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ContinuationCodecError("continuation could not be encoded") from exc
    if not 1 <= len(payload) <= limit:
        raise ContinuationCodecError("continuation exceeds its fixed byte limit")
    bindings = _bindings(kind, requirements, pending)
    # Never persist what a restart cannot restore: an undecodable record would stop Local Team at startup.
    decoded = _decoded(kind, payload, bindings)
    if decoded.requirements != tuple(requirements) or decoded.pending != pending:
        raise ContinuationCodecError("continuation does not round-trip")
    return bindings, payload


def _decode_payload(payload: bytes) -> dict[str, object]:
    try:
        value = strict_json.loads(payload)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
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
        assistant_id=_component_id(
            raw["assistant_id"], "continuation Action Assistant", http_payload.canonical_assistant_id
        ),
        action=_component_id(raw["action"], "continuation Action", http_payload.canonical_action_id),
        input=action_input,
    )


def _continuation(value: object) -> chat_orchestrator.ChatContinuation:
    raw = _mapping(
        value,
        {"turn", "seen_interrupts", "invoked", "round_index", "file_actions"},
        "Brain continuation",
    )
    turn_value = _mapping(raw["turn"], {"status", "reply", "actions"}, "Brain turn")
    turn = brain_runtime_client.BrainRuntimeClient._parse_turn(
        {
            "status": turn_value["status"],
            "reply": turn_value["reply"],
            "clarification": None,
            "memory": [],
            "routine": None,
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
        entry = _mapping(item, {"assistant_id", "action", "inputs", "contract", "learnable"}, "invoked Action")
        inputs = entry["inputs"]
        if (
            not isinstance(inputs, list)
            or any(not isinstance(name, str) or http_payload.SKILL_INPUT_RE.fullmatch(name) is None for name in inputs)
            or inputs != sorted(set(inputs))
            or http_payload.canonical_source_digest(entry["contract"]) is None
            or type(entry["learnable"]) is not bool
        ):
            raise ContinuationCodecError("invoked Action is malformed")
        invoked.append(
            chat_orchestrator.InvokedAction(
                _component_id(entry["assistant_id"], "invoked Action Assistant", http_payload.canonical_assistant_id),
                _component_id(entry["action"], "invoked Action", http_payload.canonical_action_id),
                tuple(inputs),
                entry["contract"],
                entry["learnable"],
            )
        )
    round_index = raw["round_index"]
    if type(round_index) is not int or not 0 <= round_index < chat_orchestrator.MAX_RESUMABLE_ROUNDS:
        raise ContinuationCodecError("continuation round is malformed")
    file_actions = raw["file_actions"]
    if type(file_actions) is not int or not 0 <= file_actions <= action_files.MAX_FILE_ACTIONS_PER_TURN:
        raise ContinuationCodecError("continuation file Actions are malformed")
    return chat_orchestrator.ChatContinuation(turn, seen, tuple(invoked), round_index, file_actions)


def _identity(value: object) -> tuple[object, ...]:
    raw = _mapping(
        value,
        {"team_name", "network_id", "assistants", "files", "inference"},
        "continuation Team identity",
    )
    # Team names and filenames follow their owning validators, which admit every printable Unicode character the Team
    # and its files may carry; both validators keep this codec's 80-character and 255-byte bounds.
    try:
        team_name = validate_team_name(raw["team_name"])
    except ApiProblemError as exc:
        raise ContinuationCodecError("continuation Team name is malformed") from exc
    network_id = raw["network_id"]
    if not isinstance(network_id, str) or _NETWORK_ID.fullmatch(network_id) is None:
        raise ContinuationCodecError("continuation network identity is malformed")
    assistants: list[tuple[str, str, str]] = []
    for item in _sequence(raw["assistants"], MAX_IDENTITY_ASSISTANTS, "continuation Assistants"):
        if not isinstance(item, list) or len(item) != 3:
            raise ContinuationCodecError("continuation Assistant identity is malformed")
        assistant = _component_id(item[0], "continuation Assistant identity", http_payload.canonical_assistant_id)
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
        entry = _mapping(item, {"id", "name", "media_type", "size", "sha256"}, "continuation file")
        if (
            not isinstance(entry["id"], str)
            or http_payload.FILE_ID_RE.fullmatch(entry["id"]) is None
            or http_payload.canonical_filename(entry["name"]) is None
            or not isinstance(entry["media_type"], str)
            or not 1 <= len(entry["media_type"]) <= 127
            or type(entry["size"]) is not int
            or not 0 <= entry["size"] <= 2**53 - 1
            or not isinstance(entry["sha256"], str)
            or http_payload.SHA256_RE.fullmatch(entry["sha256"]) is None
        ):
            raise ContinuationCodecError("continuation file is malformed")
        files.append(dict(entry))
    if len({item["id"] for item in files}) != len(files):
        raise ContinuationCodecError("continuation files are malformed")
    inference = _mapping(raw["inference"], {"provider", "model", "effort"}, "continuation inference")
    if not isinstance(inference["effort"], str):
        raise ContinuationCodecError("continuation inference is malformed")
    try:
        config = inference_config.normalize(inference["provider"], inference["model"], inference["effort"])
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
            "requests_used",
            "locale",
            "usage",
            "paused_batch",
            "recording",
            "model",
        },
        "pending continuation",
    )
    assistant_ids = tuple(
        _component_id(item, "pending Assistant", http_payload.canonical_assistant_id)
        for item in _sequence(raw["assistant_ids"], 16, "pending Assistants")
    )
    if len(assistant_ids) != len(set(assistant_ids)) or tuple(sorted(assistant_ids)) != assistant_ids:
        raise ContinuationCodecError("pending Assistants are malformed")
    file_ids = tuple(
        item
        for item in _sequence(raw["file_ids"], MAX_IDENTITY_FILES, "pending files")
        if isinstance(item, str) and http_payload.FILE_ID_RE.fullmatch(item) is not None
    )
    if len(file_ids) != len(raw["file_ids"]) or len(file_ids) != len(set(file_ids)):
        raise ContinuationCodecError("pending files are malformed")
    provider = raw["provider"]
    if not isinstance(provider, str) or provider not in inference_config.PROVIDERS:
        raise ContinuationCodecError("pending provider is malformed")
    identity = _identity(raw["identity"])
    if identity[4].provider != provider:
        raise ContinuationCodecError("pending provider binding is malformed")
    transcripts = _transcripts(raw["transcripts"])
    requests_used = _requests_used(raw["requests_used"])
    locale = raw["locale"]
    if locale is not None and http_payload.canonical_locale(locale) is None:
        raise ContinuationCodecError("pending locale is malformed")
    if sum(len(item.responses) for item in transcripts) > requests_used:
        raise ContinuationCodecError("human request budget is malformed")
    recording = raw["recording"]
    if recording is not None and (not isinstance(recording, str) or _RECORDING_RE.fullmatch(recording) is None):
        raise ContinuationCodecError("pending recording is malformed")
    model = raw["model"]
    if model is not None and (
        not isinstance(model, str) or model not in inference_config.PROVIDERS[provider]["models"]
    ):
        raise ContinuationCodecError("pending model is malformed")
    return PendingLocalChat(
        continuation=_continuation(raw["continuation"]),
        assistant_ids=assistant_ids,
        file_ids=file_ids,
        provider=provider,
        identity=identity,
        transcripts=transcripts,
        requests_used=requests_used,
        locale=locale,
        usage=_usage(raw["usage"]),
        paused_batch=raw["paused_batch"],
        recording=recording,
        model=model,
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
        or http_payload.SHA256_RE.fullmatch(fingerprint) is None
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


def _ordered(result: tuple[str, ...], label: str) -> tuple[str, ...]:
    if not result or len(result) != len(set(result)) or tuple(sorted(result)) != result:
        raise ContinuationCodecError(f"{label} is malformed")
    return result


def _tuple_text(value: object, maximum: int, label: str) -> tuple[str, ...]:
    return _ordered(tuple(str(_text(item, maximum, label)) for item in _sequence(value, 128, label)), label)


def _action_ids(value: object) -> tuple[str, ...]:
    label = "integration Actions"
    items = _sequence(value, 128, label)
    return _ordered(tuple(_component_id(item, label, http_payload.canonical_action_id) for item in items), label)


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
                _component_id(item[0], "integration id", http_payload.canonical_identifier),
                _component_id(item[1], "integration provider", http_payload.canonical_identifier),
                _tuple_text(item[2], 128, "integration scopes"),
            )
        )
    if not integrations:
        raise ContinuationCodecError("integration requirement is malformed")
    return integration_challenges.IntegrationRequirement(
        _component_id(raw["assistant_id"], "integration Assistant", http_payload.canonical_assistant_id),
        str(_text(raw["assistant_name"], 80, "integration Assistant name")),
        _action_ids(raw["action_ids"]),
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
            "messages",
            "assistant_version",
            "copy",
            "help_url",
            "help_text",
            "purpose",
            "purpose_locale",
            "file",
        },
        "human requirement",
    )
    request = _human_request(raw["request"], raw["messages"])
    copy = _request_copy(raw["copy"], request)
    help_url, help_text, purpose, purpose_locale = (
        raw["help_url"],
        raw["help_text"],
        raw["purpose"],
        raw["purpose_locale"],
    )
    # A Stored Input request keeps its help link, English help text, and rendered help; no other request has any.
    stored = request.kind == "input:password" and request.stored_input is not None
    help_url_valid = (
        (help_url, help_text, copy.help) == (None, None, None)
        if not stored
        else http_payload.canonical_help_url(help_url) is not None
        and http_payload.canonical_stored_input_help(help_text) is not None
        and copy.help is not None
    )
    purpose_valid = (purpose is None and purpose_locale is None) or (
        http_payload.canonical_purpose(purpose) is not None
        and http_payload.canonical_locale(purpose_locale) is not None
    )
    file = raw["file"]
    file_valid = file is None or (
        request.kind in action_human.AUTHORIZATION_KINDS and http_payload.canonical_file_disclosure(file) == file
    )
    if not help_url_valid or not purpose_valid or not file_valid:
        raise ContinuationCodecError("human requirement presentation is malformed")
    return action_challenges.HumanRequirement(
        _component_id(raw["assistant_id"], "human Assistant", http_payload.canonical_assistant_id),
        str(_text(raw["assistant_name"], 80, "human Assistant name")),
        _component_id(raw["action_id"], "human Action", http_payload.canonical_action_id),
        str(_text(raw["action_summary"], 500, "human Action summary")),
        _interrupt_id(raw["interrupt_id"]),
        request,
        str(_text(raw["assistant_version"], 40, "human Assistant version")),
        copy,
        help_url=help_url,
        help_text=help_text,
        purpose=purpose,
        purpose_locale=purpose_locale,
        file=file,
    )


def _human_request(value: object, messages: object) -> action_human.HumanRequest:
    """Re-admit the paused request against exactly the catalog entries it references (ADR-0091).

    Each entry's id is the hash of its template, so the record names its English copy; the binding's full catalog
    and pack digests are compared again before any resume or relocalization.
    """
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str) or not isinstance(messages, list):
        raise ContinuationCodecError("human requirement request is malformed")
    if any(catalog_validator.message_error(message) is not None for message in messages):
        raise ContinuationCodecError("human requirement catalog is malformed")
    catalog = {message["id"]: message for message in messages}
    # The record is Team-authenticated and its Stored Input was admitted against the Action declaration at pause;
    # sealing a submitted value re-checks that declaration, so restore admits exactly the recorded identifier.
    stored_input = value.get("stored_input")
    try:
        request = action_human.validate_request(
            value,
            (value["kind"],),
            (stored_input,) if isinstance(stored_input, str) else (),
            catalog=catalog,
        )
    except action_human.HumanRequestError as exc:
        raise ContinuationCodecError("human requirement request is malformed") from exc
    if request.messages() != messages:
        raise ContinuationCodecError("human requirement catalog is malformed")
    return request


def _request_copy(value: object, request: action_human.HumanRequest) -> action_challenges.RequestCopy:
    raw = _mapping(value, {"locale", "catalog_digest", "pack_digest", "rendered", "help"}, "human request copy")
    if (
        http_payload.canonical_locale(raw["locale"]) is None
        or http_payload.canonical_pack_digest(raw["catalog_digest"]) is None
        or http_payload.canonical_pack_digest(raw["pack_digest"]) is None
        or http_payload.canonical_rendered(raw["rendered"], request.payload()) is None
        or (raw["help"] is not None and http_payload.canonical_stored_input_help(raw["help"]) is None)
    ):
        raise ContinuationCodecError("human request copy is malformed")
    return action_challenges.RequestCopy(
        raw["locale"], raw["catalog_digest"], raw["pack_digest"], raw["rendered"], raw["help"]
    )


def _require_paused_batch(kind: str, value: object) -> None:
    """A human pause names exactly the Action batch it holds; an Integration pause holds none."""
    if kind == "integrations" and value is None:
        return
    if kind == "human" and isinstance(value, str) and http_payload.SHA256_RE.fullmatch(value) is not None:
        return
    raise ContinuationCodecError("continuation paused Action batch is malformed")


def decode(
    stored: local_chat_continuation_store.StoredContinuation,
) -> DecodedContinuation:
    """Authenticate structural bindings again after decrypting one record."""
    if not isinstance(stored, local_chat_continuation_store.StoredContinuation):
        raise ContinuationCodecError("stored continuation is malformed")
    return _decoded(stored.kind, stored.payload, stored.bindings)


def decode_parts(kind: str, payload: bytes, bindings: tuple[str, ...]) -> DecodedContinuation:
    """Decode a continuation another store kept, such as a frozen Routine run's, with its release bindings."""
    return _decoded(kind, payload, bindings)


def _decoded(kind: str, payload: bytes, bindings: tuple[str, ...]) -> DecodedContinuation:
    body = _decode_payload(payload)
    if body["schema"] != SCHEMA_VERSION or body["kind"] != kind:
        raise ContinuationCodecError("stored continuation contract changed")
    raw_requirements = _sequence(body["requirements"], 64, "continuation requirements")
    if kind == "integrations":
        requirements = tuple(_integration_requirement(item) for item in raw_requirements)
    elif kind == "human" and len(raw_requirements) == 1:
        requirements = (_human_requirement(raw_requirements[0]),)
    else:
        raise ContinuationCodecError("stored continuation kind is malformed")
    if not requirements:
        raise ContinuationCodecError("continuation requirements are malformed")
    pending = _pending(body["pending"])
    _require_paused_batch(kind, pending.paused_batch)
    if _bindings(kind, requirements, pending) != bindings:
        raise ContinuationCodecError("stored continuation release binding changed")
    return DecodedContinuation(kind, requirements, pending)
