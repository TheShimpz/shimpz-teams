"""Closed validation for one reviewed Action human-request suspension."""

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace

from core import canonical_json
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
# Team's own confirmation of a mutating Action that declares no authorization (Supervisor policy). It is never an
# Assistant request: the workload never sees it, and its answer is kept beside the transcript, not in it.
CONFIRMATION_KIND = "confirmation"
CONFIRMATION_POLICY = "mutating-actions"
_CONFIRMATION_FIELDS = frozenset({"kind", "ordinal", "policy", "binding", "fingerprint"})
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
    """Bounded replay responses for one immutable Brain Action interrupt.

    ``confirmation`` is Team's own policy confirmation of the Action before it first runs; it is never replayed to the
    workload, which sees only ``responses``.
    """

    interrupt_id: str
    responses: tuple[HumanResponse, ...] = ()
    confirmation: HumanResponse | None = None

    def confirm(self, request: HumanRequest, value: object) -> ActionTranscript:
        """Admit Team's policy confirmation, which precedes every run of the Action and is given once."""
        if request.kind != CONFIRMATION_KIND or self.confirmation is not None or self.responses:
            raise HumanRequestError("Action confirmation sequence is invalid")
        return replace(self, confirmation=admit_response(request, value))

    def confirmed(self, request: HumanRequest) -> bool:
        """Whether the transcript holds the confirmation of exactly this policy request."""
        return self.confirmation is not None and hmac.compare_digest(self.confirmation.fingerprint, request.fingerprint)

    def append(self, request: HumanRequest, value: object) -> ActionTranscript:
        """Admit the next exact response and return an immutable transcript.

        A Stored Input request is answered by injection, never by a replay response (ADR-0059).
        """
        if request.stored_input is not None:
            raise HumanRequestError("Assistant Action Stored Input is answered by injection")
        if request.kind == CONFIRMATION_KIND:
            return self.confirm(request, value)
        self.require_next(request)
        if request.kind in AUTHORIZATION_KINDS and any(
            response.kind in AUTHORIZATION_KINDS for response in self.responses
        ):
            raise HumanRequestError("Assistant Action requested authorization more than once")
        return replace(self, responses=(*self.responses, admit_response(request, value)))

    def require_next(self, request: HumanRequest) -> None:
        """Refuse a request that is not the next ordinal or exceeds the Action budget."""
        if len(self.responses) >= MAX_REQUESTS_PER_ACTION or request.ordinal != len(self.responses):
            raise HumanRequestError("Assistant Action human request sequence is invalid")

    def payloads(self) -> tuple[Mapping[str, object], ...]:
        """Return independent replay frames in their admitted order."""
        return tuple(response.payload() for response in self.responses)


@dataclass(frozen=True, slots=True, repr=False)
class StoredInputSubmission:
    """One admitted Stored Input value Team seals before the Action replays; its representation omits the value."""

    stored_input: str
    value: str


@dataclass(frozen=True, slots=True)
class HumanResponseAdmission:
    """Updated transcript state and its monotonic Team-turn request budget.

    ``stored_input`` is the value of an answered Stored Input request, which the caller seals instead of appending it
    to the transcript; the replay then receives it injected (ADR-0059).
    """

    transcripts: tuple[ActionTranscript, ...]
    requests_used: int
    stored_input: StoredInputSubmission | None = None


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
    """Admit one response while enforcing the Team-wide turn budget.

    A Stored Input answer counts against that budget but stays out of the transcript: the admission carries it for the
    caller to seal, and the transcripts are returned unchanged.
    """
    if type(requests_used) is not int or not 0 <= requests_used < MAX_REQUESTS_PER_TURN:
        raise HumanRequestError("Team turn exceeded its human request limit")
    current = transcript_for(transcripts, interrupt_id)
    if request.stored_input is not None:
        current.require_next(request)
        # A Stored Input request is a reviewed input:password request, so its admitted value is always a string.
        submission = StoredInputSubmission(request.stored_input, admit_response(request, value).value)
        return HumanResponseAdmission(transcripts, requests_used + 1, submission)
    updated = current.append(request, value)
    if any(item is current for item in transcripts):
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


def confirmation_request(binding: Mapping[str, object]) -> HumanRequest:
    """Team's confirmation request for one exact policy binding.

    ``binding`` names everything the confirmation authorizes: the policy, the principal, the Team, the Assistant's
    immutable binding, the Action and its interrupt, and the canonical validated arguments. The request carries only
    its SHA-256, so a confirmation never authorizes any other argument or binding.
    """
    request = {
        "kind": CONFIRMATION_KIND,
        "ordinal": 0,
        "policy": CONFIRMATION_POLICY,
        "binding": hashlib.sha256(_canonical(binding)).hexdigest(),
    }
    fingerprint = _fingerprint(request)
    return HumanRequest(CONFIRMATION_KIND, 0, fingerprint, _canonical({**request, "fingerprint": fingerprint}))


def restore_confirmation_request(value: object) -> HumanRequest:
    """Re-admit a recorded confirmation request of exactly the current shape, with its fingerprint recomputed."""
    if (
        not isinstance(value, dict)
        or set(value) != _CONFIRMATION_FIELDS
        or value["kind"] != CONFIRMATION_KIND
        or type(value["ordinal"]) is not int
        or value["ordinal"] != 0
        or value["policy"] != CONFIRMATION_POLICY
        or not isinstance(value["binding"], str)
        or http_payload.SHA256_RE.fullmatch(value["binding"]) is None
        or not isinstance(value["fingerprint"], str)
    ):
        raise HumanRequestError("Action confirmation request is invalid")
    request = {key: value[key] for key in ("kind", "ordinal", "policy", "binding")}
    if not hmac.compare_digest(value["fingerprint"], _fingerprint(request)):
        raise HumanRequestError("Action confirmation request fingerprint is invalid")
    return HumanRequest(CONFIRMATION_KIND, 0, value["fingerprint"], _canonical(value))


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
    if kind in AUTHORIZATION_KINDS or kind == CONFIRMATION_KIND:
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
        return canonical_json.encode(value)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise HumanRequestError("Assistant Action human request is invalid") from exc
