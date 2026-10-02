"""Bounded Team-owned challenges for admitted Action human requests."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from action import human
from assistant import language as assistant_language
from integrations import challenge_store
from protocol.assistant.v1.validators import message_catalog as catalog_validator
from protocol.http.v1 import payload as http_payload

DEFAULT_TTL_SECONDS = 300


class HumanChallengeError(RuntimeError):
    """An Action human challenge is invalid, unavailable, or conflicts."""


class HumanChallengeNotFoundError(HumanChallengeError):
    """The opaque challenge expired, was consumed, or belongs to another Team."""


@dataclass(frozen=True, slots=True)
class RequestCopy:
    """A request's copy rendered for one concrete interface language from one exact binding (ADR-0091).

    The challenge binds this locale and both digests beside the canonical fingerprint; another locale or another
    pack needs a fresh rendering and a fresh challenge.
    """

    locale: str
    catalog_digest: str
    pack_digest: str
    rendered: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class HumanRequirement:
    """Public context for one exact request without Action input or private values."""

    assistant_id: str
    assistant_name: str
    action_id: str
    action_summary: str
    interrupt_id: str
    request: human.HumanRequest
    assistant_version: str
    copy: RequestCopy
    # The reviewed key page of the Stored Input the request names, copied only from that binding's declaration.
    help_url: str | None = None
    # The Brain's task-bound sentence for why this Action pauses, in the turn's interface language (ADR-0090).
    purpose: str | None = None
    # The concrete interface language the purpose was written in; it is shown only in a challenge of that locale.
    purpose_locale: str | None = None


def render_copy(
    request: human.HumanRequest,
    pack: assistant_language.LanguagePack,
    locale: str,
) -> RequestCopy:
    """Render every copy field of an admitted request in one interface language from its binding's pack.

    English is the catalog itself. Each referenced message must be exactly the binding's catalog entry, each parameter
    is inserted once, and every rendering must still be bounded public text.
    """
    if http_payload.canonical_locale(locale) is None:
        raise HumanChallengeError("Action human request locale is invalid")
    payload = request.payload()
    messages = request.messages()
    if {message["id"] for message in messages} != set(human.referenced_messages(payload)) or any(
        pack.messages.get(message["id"]) != message for message in messages
    ):
        raise HumanChallengeError("Action human request copy does not match its binding")

    def text(reference: object) -> str | None:
        if not isinstance(reference, Mapping):
            return None
        return catalog_validator.render(reference, pack.template(str(reference["message"]), locale))

    rendered: dict[str, object] = {field: text(payload[field]) for field in human.COPY_FIELDS if field in payload}
    if "options" in payload:
        rendered["options"] = [
            {field: text(option[field]) for field in human.OPTION_COPY_FIELDS} for option in payload["options"]
        ]
    if http_payload.canonical_rendered(rendered, payload) is None:
        raise HumanChallengeError("Action human request copy cannot be rendered")
    return RequestCopy(locale, pack.catalog_digest, pack.pack_digest, rendered)


def copy_binding_current(requirement: HumanRequirement, machine_contract: Mapping[str, Any], pack_digest: str) -> bool:
    """Whether the requirement was rendered from the binding's current catalog and pack."""
    current = (assistant_language.catalog_digest(machine_contract), pack_digest)
    return (requirement.copy.catalog_digest, requirement.copy.pack_digest) == current


def relocalize(
    requirement: HumanRequirement,
    pack: assistant_language.LanguagePack,
    locale: str,
) -> HumanRequirement:
    """Render the same request in another interface language, never against a different catalog or pack."""
    if (pack.catalog_digest, pack.pack_digest) != (requirement.copy.catalog_digest, requirement.copy.pack_digest):
        raise HumanChallengeError("Action human request binding changed")
    return replace(requirement, copy=render_copy(requirement.request, pack, locale))


def declared_help_url(request: human.HumanRequest, stored_inputs: object) -> str | None:
    """The key page the reviewed binding declares for the one Stored Input a password request names, if any."""
    if request.kind != "input:password" or request.stored_input is None or not isinstance(stored_inputs, Mapping):
        return None
    declaration = stored_inputs.get(request.stored_input)
    return getattr(declaration, "help_url", None)


@dataclass(frozen=True, slots=True)
class PendingHumanChallenge:
    id: str
    team_id: str
    expires_at: float
    requirement: HumanRequirement
    payload: Any


def _copy(value: object, request: human.HumanRequest) -> bool:
    return (
        isinstance(value, RequestCopy)
        and http_payload.canonical_locale(value.locale) is not None
        and http_payload.canonical_pack_digest(value.catalog_digest) is not None
        and http_payload.canonical_pack_digest(value.pack_digest) is not None
        and http_payload.canonical_rendered(value.rendered, request.payload()) is not None
    )


def _purpose(purpose: object, locale: object) -> bool:
    if purpose is None:
        return locale is None
    return http_payload.canonical_purpose(purpose) == purpose and http_payload.canonical_locale(locale) is not None


def _requirement(value: object) -> bool:
    return (
        isinstance(value, HumanRequirement)
        and isinstance(value.request, human.HumanRequest)
        and _copy(value.copy, value.request)
        and isinstance(value.assistant_version, str)
        and 1 <= len(value.assistant_version) <= 40
        and (
            value.help_url is None
            or (
                value.request.kind == "input:password"
                and value.request.stored_input is not None
                and http_payload.canonical_help_url(value.help_url) == value.help_url
            )
        )
        and _purpose(value.purpose, value.purpose_locale)
    )


_CONTRACT = challenge_store.ChallengeContract(
    PendingHumanChallenge,
    _requirement,
    HumanChallengeError,
    HumanChallengeNotFoundError,
    "Action human request",
)


class HumanChallengeStore(challenge_store.ChallengeStore[PendingHumanChallenge]):
    """Keep one short-lived, one-use human decision per Team."""

    def __init__(
        self,
        *,
        capacity: int = challenge_store.MAX_PENDING_CHALLENGES,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            _CONTRACT,
            capacity=capacity,
            ttl_seconds=ttl_seconds,
            clock=clock,
        )


def challenge_payload(challenge: PendingHumanChallenge) -> dict[str, object]:
    """Project one public modal descriptor without Action input or response material."""
    if not isinstance(challenge, PendingHumanChallenge) or not _requirement(challenge.requirement):
        raise HumanChallengeError("Action human challenge is invalid")
    remaining = math.ceil(challenge.expires_at - time.monotonic())
    if not 1 <= remaining <= DEFAULT_TTL_SECONDS:
        raise HumanChallengeError("Action human challenge is expired")
    requirement = challenge.requirement
    return {
        "team_id": challenge.team_id,
        "status": "human-required",
        "turn_id": challenge.id,
        "challenge_id": challenge.id,
        "expires_in": remaining,
        "assistant": {
            "id": requirement.assistant_id,
            "name": requirement.assistant_name,
            "version": requirement.assistant_version,
        },
        "action": {
            "id": requirement.action_id,
            "summary": requirement.action_summary,
        },
        "request": requirement.request.payload(),
        "rendered": requirement.copy.rendered,
        "locale": requirement.copy.locale,
        "pack_digest": requirement.copy.pack_digest,
        # A purpose is written in its turn's language, so only a challenge in that same locale shows it (ADR-0091).
        **({} if requirement.purpose_locale != requirement.copy.locale else {"purpose": requirement.purpose}),
        **({} if requirement.help_url is None else {"help_url": requirement.help_url}),
    }
