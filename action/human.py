"""Closed validation for one reviewed Action human-request suspension."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass

from protocol.assistant.v1.validators import human_request as human_request_validator
from protocol.http.v1 import payload as http_payload

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
# The copy fields of a request, each a reviewed catalog reference (ADR-0091); option copy lives in each option.
COPY_FIELDS = ("title", "description", "label", "placeholder")
OPTION_COPY_FIELDS = ("label", "description")
type Catalog = Mapping[str, Mapping[str, object]]


class HumanRequestError(ValueError):
    """An Assistant human request violated its reviewed closed contract."""


@dataclass(frozen=True, slots=True)
class HumanRequest:
    """One canonical request safe to bind into a Team-owned challenge.

    ``catalog`` holds the canonical English catalog entries the request's copy references, so the request can be
    rendered and revalidated without the rest of its binding (ADR-0091).
    """

    kind: str
    ordinal: int
    fingerprint: str
    canonical: bytes
    stored_input: str | None = None
    catalog: bytes = b"[]"

    def payload(self) -> dict[str, object]:
        """Return an independent JSON object for projection or continuation binding."""
        value = json.loads(self.canonical)
        if not isinstance(value, dict):
            raise AssertionError("canonical human request is not an object")
        return value

    def messages(self) -> list[dict[str, object]]:
        """Return the referenced catalog entries sorted by id."""
        return json.loads(self.catalog)


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
    stored_input: str | None = None

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
            (
                f"stored-input:{response.stored_input}"
                if response.stored_input is not None
                else f"human-response-{response.ordinal}"
            ): response.value
            for response in self.responses
            if response.secret and isinstance(response.value, str)
        }

    def submitted_stored_inputs(self) -> dict[str, str]:
        """Return newly supplied persistent values without adding them to replay frames."""
        submitted = {
            response.stored_input: response.value
            for response in self.responses
            if response.stored_input is not None and isinstance(response.value, str)
        }
        if len(submitted) > 1:
            raise HumanRequestError("Assistant Action submitted multiple Stored Inputs")
        return submitted


@dataclass(frozen=True, slots=True)
class HumanResponseAdmission:
    """Updated transcript state and its monotonic Team-turn request budget."""

    transcripts: tuple[ActionTranscript, ...]
    requests_used: int


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
    requests_used: int,
) -> HumanResponseAdmission:
    """Append one response while enforcing the Team-wide turn budget."""
    if type(requests_used) is not int or not 0 <= requests_used < MAX_REQUESTS_PER_TURN:
        raise HumanRequestError("Team turn exceeded its human request limit")
    current = transcript_for(transcripts, interrupt_id)
    updated = current.append(request, value)
    if current.responses:
        admitted = tuple(updated if item is current else item for item in transcripts)
    else:
        admitted = (*transcripts, updated)
    return HumanResponseAdmission(admitted, requests_used + 1)


def retain_unfinished_transcripts(
    transcripts: tuple[ActionTranscript, ...],
    completed_interrupts: tuple[str, ...],
) -> tuple[ActionTranscript, ...]:
    """Drop memory-only responses after their exact Action interrupt completed."""
    completed = set(completed_interrupts)
    return tuple(transcript for transcript in transcripts if transcript.interrupt_id not in completed)


def validate_request(
    value: object,
    capabilities: tuple[str, ...],
    stored_inputs: tuple[str, ...] = (),
    *,
    catalog: Catalog,
) -> HumanRequest:
    """Validate one request against its reviewed catalog and bind its advertised fingerprint to canonical bytes.

    Every copy field must reference a declared message with exactly its declared parameters (ADR-0091); the
    fingerprint covers the references, never a display language.
    """
    if not isinstance(value, dict) or "fingerprint" not in value:
        raise HumanRequestError("Assistant Action human request is invalid")
    request = dict(value)
    fingerprint = request.pop("fingerprint")
    error = human_request_validator.request_error(request, catalog)
    kind = request.get("kind")
    if (
        error is not None
        or not isinstance(kind, str)
        or kind not in capabilities
        or not isinstance(fingerprint, str)
        # Exactly the lowercase ASCII hex SHA-256 the canonical request produces; nothing else reaches compare_digest.
        or http_payload.SHA256_RE.fullmatch(fingerprint) is None
    ):
        raise HumanRequestError("Assistant Action human request is invalid")
    expected = _fingerprint(request)
    if not hmac.compare_digest(fingerprint, expected):
        raise HumanRequestError("Assistant Action human request fingerprint is invalid")
    framed = {**request, "fingerprint": fingerprint}
    stored_input = request.get("stored_input")
    if stored_input is not None and stored_input not in stored_inputs:
        raise HumanRequestError("Assistant Action Stored Input request is undeclared")
    referenced = sorted(set(referenced_messages(request)))
    return HumanRequest(
        kind=kind,
        ordinal=int(request["ordinal"]),
        fingerprint=fingerprint,
        canonical=_canonical(framed),
        stored_input=stored_input if isinstance(stored_input, str) else None,
        catalog=_canonical([catalog[identifier] for identifier in referenced]),
    )


def catalog_by_id(machine_contract: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    """Index a reviewed machine contract's English message catalog by message id."""
    return {message["id"]: message for message in machine_contract["messages"]}


def referenced_messages(request: Mapping[str, object]) -> list[str]:
    """The message ids of every non-null copy reference of an already-admitted request."""
    references = [request[field] for field in COPY_FIELDS if field in request]
    for option in request.get("options", ()):
        references.extend(option[field] for field in OPTION_COPY_FIELDS)
    return [reference["message"] for reference in references if reference is not None]


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
    return HumanResponse(kind, request.ordinal, request.fingerprint, value, request.stored_input)


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
