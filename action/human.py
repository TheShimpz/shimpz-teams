"""Closed validation for one reviewed Action human-request suspension."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass

MAX_REQUESTS_PER_ACTION = 8
MAX_REQUESTS_PER_TURN = 16
LENGTH_KINDS = {
    "input:text": 4096,
    "input:textarea": 16_000,
    "input:password": 1024,
    "input:phone": 64,
}
CHOICE_KINDS = frozenset({"input:select", "input:choice"})
AUTH_KINDS = frozenset(
    {
        "auth:password",
        "auth:totp",
        "auth:passkey",
    }
)
AUTHORIZATION_KINDS = frozenset({"approval", *AUTH_KINDS})
_BASE_FIELDS = frozenset({"kind", "ordinal", "title", "description"})


class HumanRequestError(ValueError):
    """An Assistant human request violated its reviewed closed contract."""


@dataclass(frozen=True, slots=True)
class HumanRequest:
    """One canonical request safe to bind into a Team-owned challenge."""

    kind: str
    ordinal: int
    fingerprint: str
    canonical: bytes

    def payload(self) -> dict[str, object]:
        """Return an independent JSON object for projection or continuation binding."""
        value = json.loads(self.canonical)
        if not isinstance(value, dict):
            raise AssertionError("canonical human request is not an object")
        return value


class HumanRequestSuspensionError(RuntimeError):
    """Control signal raised only after a request passes Team admission."""

    def __init__(self, request: HumanRequest) -> None:
        super().__init__("Assistant Action requested human input")
        self.request = request


@dataclass(frozen=True, slots=True)
class HumanResponse:
    """One Team-admitted response bound to the exact request that produced it."""

    kind: str
    ordinal: int
    fingerprint: str
    value: object

    @property
    def secret(self) -> bool:
        """Return whether this response must remain in process memory only."""
        return self.kind == "input:password"

    def payload(self) -> dict[str, object]:
        """Project the closed replay frame consumed by the Assistant SDK."""
        return {
            "kind": self.kind,
            "ordinal": self.ordinal,
            "fingerprint": self.fingerprint,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class ActionTranscript:
    """Bounded replay responses for one immutable Brain Action interrupt."""

    interrupt_id: str
    responses: tuple[HumanResponse, ...] = ()

    def append(self, request: HumanRequest, value: object) -> ActionTranscript:
        """Admit the next exact response and return an immutable transcript."""
        if len(self.responses) >= MAX_REQUESTS_PER_ACTION or request.ordinal != len(self.responses):
            raise HumanRequestError("Assistant Action human request sequence is invalid")
        if any(response.secret for response in self.responses):
            raise HumanRequestError("Assistant Action requested input after a secret response")
        if request.kind in AUTHORIZATION_KINDS and any(
            response.kind in AUTHORIZATION_KINDS for response in self.responses
        ):
            raise HumanRequestError("Assistant Action requested authorization more than once")
        return ActionTranscript(
            interrupt_id=self.interrupt_id,
            responses=(*self.responses, admit_response(request, value)),
        )

    def payloads(self) -> tuple[Mapping[str, object], ...]:
        """Return independent replay frames in their admitted order."""
        return tuple(response.payload() for response in self.responses)

    def protected_values(self) -> dict[str, str]:
        """Return ephemeral arbitrary secrets that a final result must not expose."""
        return {
            f"human-response-{response.ordinal}": response.value
            for response in self.responses
            if response.secret and isinstance(response.value, str)
        }


def transcript_for(
    transcripts: tuple[ActionTranscript, ...],
    interrupt_id: str,
) -> ActionTranscript:
    """Resolve one interrupt transcript while rejecting ambiguous duplicate state."""
    matching = tuple(item for item in transcripts if item.interrupt_id == interrupt_id)
    if len(matching) > 1:
        raise HumanRequestError("Action human transcript is ambiguous")
    return matching[0] if matching else ActionTranscript(interrupt_id)


def append_response(
    transcripts: tuple[ActionTranscript, ...],
    interrupt_id: str,
    request: HumanRequest,
    value: object,
) -> tuple[ActionTranscript, ...]:
    """Append one response while enforcing the Team-wide turn budget."""
    if sum(len(item.responses) for item in transcripts) >= MAX_REQUESTS_PER_TURN:
        raise HumanRequestError("Team turn exceeded its human request limit")
    current = transcript_for(transcripts, interrupt_id)
    updated = current.append(request, value)
    if current.responses:
        return tuple(updated if item is current else item for item in transcripts)
    return (*transcripts, updated)


def validate_request(value: object, capabilities: tuple[str, ...]) -> HumanRequest:
    """Validate one request and bind its advertised fingerprint to canonical bytes."""
    if not isinstance(value, dict) or "fingerprint" not in value:
        raise HumanRequestError("Assistant Action human request is invalid")
    request = dict(value)
    fingerprint = request.pop("fingerprint")
    error = _request_error(request)
    kind = request.get("kind")
    if (
        error is not None
        or not isinstance(kind, str)
        or kind not in capabilities
        or not isinstance(fingerprint, str)
        or len(fingerprint) != 64
    ):
        raise HumanRequestError("Assistant Action human request is invalid")
    expected = _fingerprint(request)
    if not hmac.compare_digest(fingerprint, expected):
        raise HumanRequestError("Assistant Action human request fingerprint is invalid")
    framed = {**request, "fingerprint": fingerprint}
    return HumanRequest(
        kind=kind,
        ordinal=int(request["ordinal"]),
        fingerprint=fingerprint,
        canonical=_canonical(framed),
    )


def admit_response(request: HumanRequest, value: object) -> HumanResponse:
    """Validate one user decision against its canonical reviewed request."""
    descriptor = request.payload()
    kind = request.kind
    if kind == "approval" or kind in AUTH_KINDS:
        valid = value is True
    elif kind in CHOICE_KINDS:
        valid = _single_choice_response(descriptor, value)
    elif kind == "input:choices":
        valid = _multiple_choice_response(descriptor, value)
    else:
        valid = _text_response(descriptor, value)
    if not valid:
        raise HumanRequestError("human response does not match its reviewed request")
    return HumanResponse(kind, request.ordinal, request.fingerprint, value)


def _single_choice_response(request: Mapping[str, object], value: object) -> bool:
    options = request.get("options")
    if not isinstance(options, list):
        return False
    allowed = {option["value"] for option in options if isinstance(option, dict)}
    return isinstance(value, str) and (value in allowed or (value == "" and request.get("required") is False))


def _multiple_choice_response(request: Mapping[str, object], value: object) -> bool:
    options = request.get("options")
    if (
        not isinstance(options, list)
        or not isinstance(value, list)
        or not all(isinstance(item, str) for item in value)
        or len(value) != len(set(value))
    ):
        return False
    allowed = {option["value"] for option in options if isinstance(option, dict)}
    minimum = request.get("min_selections")
    maximum = request.get("max_selections")
    return type(minimum) is int and type(maximum) is int and minimum <= len(value) <= maximum and set(value) <= allowed


def _text_response(request: Mapping[str, object], value: object) -> bool:
    minimum = request.get("min_length")
    maximum = request.get("max_length")
    return (
        isinstance(value, str)
        and type(minimum) is int
        and type(maximum) is int
        and (request.get("required") is False or bool(value))
        and minimum <= len(value) <= maximum
    )


def _fingerprint(request: object) -> str:
    return hashlib.sha256(_canonical(request)).hexdigest()


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise HumanRequestError("Assistant Action human request is invalid") from exc


def _request_error(request: object) -> str | None:
    if not isinstance(request, dict):
        return "shape"
    ordinal = request.get("ordinal")
    kind = request.get("kind")
    if (
        not isinstance(kind, str)
        or type(ordinal) is not int
        or not 0 <= ordinal < MAX_REQUESTS_PER_ACTION
        or not _text(request.get("title"), 80)
        or not _text(request.get("description"), 500)
    ):
        return "base"
    return _kind_error(request, kind)


def _kind_error(request: dict[str, object], kind: str) -> str | None:
    if kind == "approval" or kind in AUTH_KINDS:
        return None if set(request) == _BASE_FIELDS else "shape"
    if kind in LENGTH_KINDS:
        return _length_error(request, LENGTH_KINDS[kind])
    if kind in CHOICE_KINDS:
        return _choice_error(request, multiple=False)
    if kind == "input:choices":
        return _choice_error(request, multiple=True)
    return "kind"


def _length_error(request: dict[str, object], limit: int) -> str | None:
    expected = _BASE_FIELDS | {"label", "required", "placeholder", "min_length", "max_length"}
    if set(request) != expected or not _input_base(request):
        return "shape"
    placeholder = request["placeholder"]
    minimum = request["min_length"]
    maximum = request["max_length"]
    if (
        (placeholder is not None and not _text(placeholder, 120))
        or type(minimum) is not int
        or type(maximum) is not int
        or not 0 <= minimum <= maximum <= limit
    ):
        return "bounds"
    return None


def _choice_error(request: dict[str, object], *, multiple: bool) -> str | None:
    bounds = {"min_selections", "max_selections"} if multiple else set()
    expected = _BASE_FIELDS | {"label", "required", "options"} | bounds
    options = request.get("options")
    if (
        set(request) != expected
        or not _input_base(request)
        or not isinstance(options, list)
        or not 2 <= len(options) <= 32
        or not all(_option(option) for option in options)
    ):
        return "options"
    values = [option["value"] for option in options]
    if len(values) != len(set(values)):
        return "options"
    if multiple:
        minimum = request["min_selections"]
        maximum = request["max_selections"]
        if type(minimum) is not int or type(maximum) is not int or not 0 <= minimum <= maximum <= len(options):
            return "bounds"
    return None


def _input_base(request: Mapping[str, object]) -> bool:
    return type(request.get("required")) is bool and _text(request.get("label"), 80)


def _option(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"value", "label", "description"}
        and _text(value["value"], 128)
        and _text(value["label"], 80)
        and (value["description"] is None or _text(value["description"], 160))
    )


def _text(value: object, maximum: int) -> bool:
    return isinstance(value, str) and value == value.strip() and 0 < len(value) <= maximum and value.isprintable()
