"""Closed public and private contracts for Assistant OAuth integrations.

Public projections contain only reviewed intent and bounded integration metadata.
OAuth tokens remain Controller-owned and are resolved into the exact Power RPC
envelope only at the last private boundary before an Assistant invocation.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol

from inference import client as brain_runtime_client
from integrations import challenges as integration_challenges
from integrations import providers as integration_providers

MAX_BATCH_POWERS = 64
MAX_INTEGRATION_REQUIREMENTS = 64
MAX_INVENTORY_ASSISTANTS = 64
MAX_INVENTORY_ACCOUNTS = 256
MAX_INTEGRATIONS_PER_POWER = 16
MAX_ACCESS_TOKEN_BYTES = 16 * 1024
MAX_PUBLIC_TEXT_BYTES = 512
_TEAM_ID = re.compile(r"[a-z0-9_]{1,40}\Z")
_COMPONENT_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_FORBIDDEN_PUBLIC_FIELDS = frozenset(
    {
        "access_token",
        "refresh_token",
        "client_id",
        "client_secret",
        "authorization_code",
        "code",
        "code_verifier",
        "token",
    }
)
_PROVIDER_PUBLIC_METADATA = {
    "cloudflare": (
        "Cloudflare",
        "Connect your Cloudflare integration so this Assistant can use only its reviewed read permissions.",
    ),
    "x": (
        "X",
        "Connect your X integration so this Assistant can use only its reviewed X permissions.",
    ),
}


class IntegrationFlowError(RuntimeError):
    """An Assistant integration request violated its closed contract."""


class _IntegrationSpec(Protocol):
    provider: str
    scopes: tuple[str, ...]


class _PowerSpec(Protocol):
    summary: str
    integrations: tuple[str, ...]


class _AssistantSpec(Protocol):
    assistant_id: str
    name: str
    powers: Mapping[str, _PowerSpec]
    integrations: Mapping[str, _IntegrationSpec]


class _ActiveBinding(Protocol):
    spec: _AssistantSpec


class _IntegrationMetadata(Protocol):
    id: str
    provider: str
    scopes: tuple[str, ...]
    status: str
    integration: object
    expires_at: int | None
    generation: int


class _IntegrationStore(Protocol):
    def metadata(
        self,
        team_id: object,
        assistant_id: object,
        declarations: object,
    ) -> tuple[_IntegrationMetadata, ...]: ...

    def resolve(
        self,
        team_id: object,
        assistant_id: object,
        integration_id: object,
        provider: object,
        scopes: object,
        refresh_callback: Callable[[str, str | None], object],
    ) -> str: ...


RefreshCallback = Callable[[str, tuple[str, ...], str, str | None], object]


def _team_id(value: object) -> str:
    if not isinstance(value, str) or _TEAM_ID.fullmatch(value) is None:
        raise IntegrationFlowError("Team id is invalid")
    return value


def _component_id(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) > 64 or _COMPONENT_ID.fullmatch(value) is None:
        raise IntegrationFlowError(f"{label} is invalid")
    return value


def _public_text(value: object, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip() or not value.isprintable():
        raise IntegrationFlowError(f"{label} is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise IntegrationFlowError(f"{label} is invalid") from exc
    if len(encoded) > MAX_PUBLIC_TEXT_BYTES:
        raise IntegrationFlowError(f"{label} is invalid")
    return value


def _assistant(spec: object) -> _AssistantSpec:
    try:
        assistant_id = spec.assistant_id  # type: ignore[attr-defined]
        name = spec.name  # type: ignore[attr-defined]
        powers = spec.powers  # type: ignore[attr-defined]
        integrations = spec.integrations  # type: ignore[attr-defined]
    except (AttributeError, TypeError) as exc:
        raise IntegrationFlowError("Assistant integration contract is unavailable") from exc
    _component_id(assistant_id, "Assistant id")
    _public_text(name, "Assistant name")
    if not isinstance(powers, Mapping) or not isinstance(integrations, Mapping):
        raise IntegrationFlowError("Assistant integration contract is unavailable")
    if len(integrations) > MAX_INTEGRATIONS_PER_POWER:
        raise IntegrationFlowError("Assistant declares too many integrations")
    return spec  # type: ignore[return-value]


def _intent(integration_id: object, declaration: object) -> tuple[str, str, tuple[str, ...]]:
    identifier = _component_id(integration_id, "integration id")
    try:
        provider = declaration.provider  # type: ignore[attr-defined]
        scopes = declaration.scopes  # type: ignore[attr-defined]
        resolved = integration_providers.integration_intent(provider, scopes)
    except (AttributeError, TypeError, integration_providers.OAuthProviderError) as exc:
        raise IntegrationFlowError("Assistant integration declaration is invalid") from exc
    return identifier, resolved.provider.id, resolved.scopes


def _provider_metadata(provider_id: str) -> tuple[str, str]:
    try:
        provider = integration_providers.resolve(provider_id)
        name, summary = _PROVIDER_PUBLIC_METADATA[provider.id]
    except (KeyError, integration_providers.OAuthProviderError) as exc:
        raise IntegrationFlowError("OAuth provider has no reviewed public metadata") from exc
    return name, summary


def _power(spec: _AssistantSpec, power_id: object) -> tuple[str, _PowerSpec]:
    identifier = _component_id(power_id, "Power id")
    power = spec.powers.get(identifier)
    if power is None:
        raise IntegrationFlowError("Power integration contract is unavailable")
    try:
        summary = power.summary
        integrations = power.integrations
    except (AttributeError, TypeError) as exc:
        raise IntegrationFlowError("Power integration contract is unavailable") from exc
    _public_text(summary, "Power summary")
    if (
        not isinstance(integrations, tuple)
        or len(integrations) > MAX_INTEGRATIONS_PER_POWER
        or len(integrations) != len(set(integrations))
    ):
        raise IntegrationFlowError("Power integration contract is invalid")
    return identifier, power


def _power_name(power_id: str) -> str:
    return " ".join(part.capitalize() for part in power_id.split("-"))


def _metadata_for(
    team_id: str,
    spec: _AssistantSpec,
    declarations: Mapping[str, _IntegrationSpec],
    store: _IntegrationStore,
) -> dict[str, _IntegrationMetadata]:
    try:
        metadata = store.metadata(team_id, spec.assistant_id, declarations)
    except Exception as exc:
        if isinstance(exc, IntegrationFlowError):
            raise
        raise IntegrationFlowError("Assistant integration inventory is unavailable") from exc
    if not isinstance(metadata, tuple) or len(metadata) != len(declarations):
        raise IntegrationFlowError("Assistant integration inventory is invalid")
    indexed: dict[str, _IntegrationMetadata] = {}
    for item in metadata:
        try:
            integration_id = _component_id(item.id, "integration id")
            status = item.status
            generation = item.generation
            declared = declarations[integration_id]
            expected_id, expected_provider, expected_scopes = _intent(integration_id, declared)
        except (AttributeError, KeyError, TypeError) as exc:
            raise IntegrationFlowError("Assistant integration inventory is invalid") from exc
        if (
            integration_id in indexed
            or item.provider != expected_provider
            or item.scopes != expected_scopes
            or status not in {"missing", "connected", "refresh-required", "reauthorization-required"}
            or type(generation) is not int
            or generation < 0
            or generation > 2**53 - 1
            or expected_id != integration_id
        ):
            raise IntegrationFlowError("Assistant integration inventory is invalid")
        if (
            status == "missing" and (item.integration is not None or item.expires_at is not None or generation != 0)
        ) or (
            status != "missing"
            and (type(item.expires_at) is not int or not 1 <= item.expires_at <= 2**53 - 1 or generation < 1)
        ):
            raise IntegrationFlowError("Assistant integration inventory is invalid")
        indexed[integration_id] = item
    if set(indexed) != set(declarations):
        raise IntegrationFlowError("Assistant integration inventory is invalid")
    return indexed


def requirements_for_batch(
    team_id: str,
    bindings: Mapping[str, _ActiveBinding],
    requests: Sequence[brain_runtime_client.PowerRequest],
    store: _IntegrationStore,
) -> tuple[integration_challenges.IntegrationRequirement, ...]:
    """Return every unusable integration before the first Power may execute."""
    team = _team_id(team_id)
    if isinstance(requests, str | bytes) or len(requests) > MAX_BATCH_POWERS:
        raise IntegrationFlowError("Power batch has too many integration requests")
    grouped: dict[str, dict[str, set[str]]] = {}
    specs: dict[str, _AssistantSpec] = {}
    for request in requests:
        if not isinstance(request, brain_runtime_client.PowerRequest):
            raise IntegrationFlowError("Power integration request is invalid")
        active = bindings.get(request.assistant_id)
        if active is None:
            raise IntegrationFlowError("Power Assistant is unavailable")
        spec = _assistant(active.spec)
        if request.assistant_id != spec.assistant_id:
            raise IntegrationFlowError("Power Assistant binding is invalid")
        power_id, power = _power(spec, request.power)
        specs[spec.assistant_id] = spec
        for integration_id in power.integrations:
            identifier = _component_id(integration_id, "integration id")
            if identifier not in spec.integrations:
                raise IntegrationFlowError("Power references an undeclared integration")
            grouped.setdefault(spec.assistant_id, {}).setdefault(identifier, set()).add(power_id)

    requirements: list[integration_challenges.IntegrationRequirement] = []
    for assistant_id in sorted(grouped):
        spec = specs[assistant_id]
        declarations = {identifier: spec.integrations[identifier] for identifier in sorted(grouped[assistant_id])}
        metadata = _metadata_for(team, spec, declarations, store)
        for integration_id in sorted(declarations):
            item = metadata[integration_id]
            if item.status == "connected":
                continue
            identifier, provider, scopes = _intent(integration_id, declarations[integration_id])
            requirements.append(
                integration_challenges.IntegrationRequirement(
                    assistant_id=assistant_id,
                    assistant_name=spec.name,
                    power_ids=tuple(sorted(grouped[assistant_id][integration_id])),
                    integrations=((identifier, provider, scopes),),
                )
            )
            if len(requirements) > MAX_INTEGRATION_REQUIREMENTS:
                raise IntegrationFlowError("Power batch requires too many Assistant integrations")
    return tuple(requirements)


def _expires_in(challenge: integration_challenges.PendingIntegrationChallenge) -> int:
    try:
        remaining = math.ceil(challenge.expires_at - time.monotonic())
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise IntegrationFlowError("integration challenge expiry is invalid") from exc
    if not 1 <= remaining <= 900:
        raise IntegrationFlowError("integration challenge is expired")
    return remaining


def _assert_public_payload(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str) or key.lower() in _FORBIDDEN_PUBLIC_FIELDS:
                raise IntegrationFlowError("public integration payload contains a sensitive field")
            _assert_public_payload(nested)
    elif isinstance(value, list | tuple):
        for nested in value:
            _assert_public_payload(nested)


def challenge_payload(
    challenge: integration_challenges.PendingIntegrationChallenge,
    bindings: Mapping[str, _ActiveBinding],
) -> dict[str, object]:
    """Project one pending turn without Power input, OAuth state, or token material."""
    if (
        not isinstance(challenge, integration_challenges.PendingIntegrationChallenge)
        or not 1 <= len(challenge.requirements) <= MAX_INTEGRATION_REQUIREMENTS
    ):
        raise IntegrationFlowError("integration challenge is invalid")
    requirements: list[dict[str, object]] = []
    for requirement in challenge.requirements:
        if len(requirement.integrations) != 1:
            raise IntegrationFlowError("integration challenge is invalid")
        active = bindings.get(requirement.assistant_id)
        if active is None:
            raise IntegrationFlowError("integration challenge Assistant is unavailable")
        spec = _assistant(active.spec)
        if spec.assistant_id != requirement.assistant_id or spec.name != requirement.assistant_name:
            raise IntegrationFlowError("integration challenge Assistant changed")
        integration_id, provider, scopes = requirement.integrations[0]
        declaration = spec.integrations.get(integration_id)
        if declaration is None or _intent(integration_id, declaration) != (integration_id, provider, scopes):
            raise IntegrationFlowError("integration challenge declaration changed")
        powers: list[dict[str, str]] = []
        for power_id in requirement.power_ids:
            identifier, power = _power(spec, power_id)
            if integration_id not in power.integrations:
                raise IntegrationFlowError("integration challenge Power changed")
            powers.append(
                {
                    "id": identifier,
                    "name": _power_name(identifier),
                    "summary": str(_public_text(power.summary, "Power summary")),
                }
            )
        if not powers or len(powers) != len({item["id"] for item in powers}):
            raise IntegrationFlowError("integration challenge Power list is invalid")
        name, summary = _provider_metadata(provider)
        requirements.append(
            {
                "assistant_id": spec.assistant_id,
                "assistant_name": spec.name,
                "integration_id": integration_id,
                "provider": provider,
                "name": name,
                "summary": summary,
                "scopes": list(scopes),
                "powers": powers,
            }
        )
    payload: dict[str, object] = {
        "team_id": challenge.team_id,
        "status": "integrations-required",
        "turn_id": challenge.id,
        "challenge_id": challenge.id,
        "expires_in": _expires_in(challenge),
        "requirements": requirements,
    }
    _assert_public_payload(payload)
    return payload


def _integration_payload(value: object) -> dict[str, str | None] | None:
    if value is None:
        return None
    try:
        identifier = _public_text(value.id, "OAuth integration id")  # type: ignore[attr-defined]
        name = _public_text(value.name, "OAuth integration name", optional=True)  # type: ignore[attr-defined]
        username = _public_text(value.username, "OAuth integration username", optional=True)  # type: ignore[attr-defined]
    except (AttributeError, TypeError) as exc:
        raise IntegrationFlowError("OAuth integration metadata is invalid") from exc
    return {"id": identifier, "name": name, "username": username}


def _expiry_payload(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 2**53 - 1:
        raise IntegrationFlowError("OAuth integration expiry is invalid")
    try:
        return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (OSError, OverflowError, ValueError) as exc:
        raise IntegrationFlowError("OAuth integration expiry is invalid") from exc


def inventory_payload(
    team_id: str,
    assistants: Sequence[_AssistantSpec],
    store: _IntegrationStore,
) -> dict[str, object]:
    """Return flat, status-only Admin rows for every declared integration."""
    team = _team_id(team_id)
    if isinstance(assistants, str | bytes) or len(assistants) > MAX_INVENTORY_ASSISTANTS:
        raise IntegrationFlowError("Assistant integration inventory is too large")
    listing: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_spec in sorted(assistants, key=lambda item: item.assistant_id):
        spec = _assistant(raw_spec)
        if spec.assistant_id in seen:
            raise IntegrationFlowError("Assistant integration inventory is invalid")
        seen.add(spec.assistant_id)
        declarations = {identifier: spec.integrations[identifier] for identifier in sorted(spec.integrations)}
        metadata = _metadata_for(team, spec, declarations, store)
        for integration_id in sorted(declarations):
            item = metadata[integration_id]
            identifier, provider, scopes = _intent(integration_id, declarations[integration_id])
            name, summary = _provider_metadata(provider)
            status = "expired" if item.status == "refresh-required" else item.status
            listing.append(
                {
                    "assistant_id": spec.assistant_id,
                    "assistant_name": spec.name,
                    "id": identifier,
                    "provider": provider,
                    "name": name,
                    "summary": summary,
                    "scopes": list(scopes),
                    "status": status,
                    "integration": _integration_payload(item.integration),
                    "expires_at": _expiry_payload(item.expires_at),
                }
            )
            if len(listing) > MAX_INVENTORY_ACCOUNTS:
                raise IntegrationFlowError("Assistant integration inventory is too large")
    payload = {"integrations": listing}
    _assert_public_payload(payload)
    return payload


def resolve_power_integrations(
    team_id: str,
    spec: _AssistantSpec,
    power_id: str,
    store: _IntegrationStore,
    refresh_callback: RefreshCallback,
) -> dict[str, dict[str, str]]:
    """Resolve only one Power's declared access tokens into its private envelope."""
    team = _team_id(team_id)
    safe_spec = _assistant(spec)
    _, power = _power(safe_spec, power_id)
    if not callable(refresh_callback):
        raise IntegrationFlowError("OAuth refresh callback is invalid")
    resolved: dict[str, dict[str, str]] = {}
    for raw_integration_id in power.integrations:
        integration_id = _component_id(raw_integration_id, "integration id")
        declaration = safe_spec.integrations.get(integration_id)
        if declaration is None:
            raise IntegrationFlowError("Power references an undeclared integration")
        _, provider, scopes = _intent(integration_id, declaration)
        try:
            access_token = store.resolve(
                team,
                safe_spec.assistant_id,
                integration_id,
                provider,
                scopes,
                lambda token, lease, p=provider, s=scopes: refresh_callback(
                    p,
                    s,
                    token,
                    lease,
                ),
            )
        except Exception as exc:
            raise IntegrationFlowError("Assistant integration could not be resolved") from exc
        if not isinstance(access_token, str):
            raise IntegrationFlowError("OAuth access token is invalid")
        try:
            encoded = access_token.encode("ascii")
        except UnicodeError as exc:
            raise IntegrationFlowError("OAuth access token is invalid") from exc
        if not 16 <= len(encoded) <= MAX_ACCESS_TOKEN_BYTES or any(byte <= 32 or byte >= 127 for byte in encoded):
            raise IntegrationFlowError("OAuth access token is invalid")
        resolved[integration_id] = {"type": "oauth2-bearer", "access_token": access_token}
    return resolved
