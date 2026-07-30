"""Encrypted, controller-owned OAuth tokens for Assistant integrations.

Tokens never belong to an Assistant manifest, Brain prompt, process environment,
command argument, public API response, or log.  Each encrypted record is bound to
the exact Team, Assistant, integration, provider, scopes, expiry, status, and
generation through AES-GCM authenticated additional data (AAD).
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core import strict_json
from integrations import private_state
from integrations import providers as integration_providers

STATE_PATH = Path("/var/lib/shimpz-local/assistant-integrations/state/integrations.json")
KEY_PATH = Path("/var/lib/shimpz-local/assistant-integrations/key/aes256.key")
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_TOKEN_BYTES = 16 * 1024
MAX_PLAINTEXT_BYTES = (MAX_TOKEN_BYTES * 3) + 2048
MAX_INTEGRATIONS_PER_ASSISTANT = 16
MAX_TOTAL_RECORDS = 4096
MAX_INTEGRATION_ID_BYTES = 256
MAX_INTEGRATION_TEXT_BYTES = 512
REFRESH_WINDOW_SECONDS = 60
_TEAM_ID = re.compile(r"[a-z0-9_]{1,40}\Z")
_COMPONENT_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_STORED_STATUSES = frozenset({"connected", "reauthorization-required"})
StoredStatus = Literal["connected", "reauthorization-required"]
IntegrationStatus = Literal[
    "missing",
    "connected",
    "refresh-required",
    "reauthorization-required",
]


class OAuthIntegrationStoreError(RuntimeError):
    """OAuth integration state is invalid, unavailable, or unauthentic."""


class OAuthIntegrationValidationError(OAuthIntegrationStoreError):
    """A caller supplied invalid OAuth integration data."""


class OAuthIntegrationMissingError(OAuthIntegrationStoreError):
    """The requested OAuth integration has not been configured."""


class OAuthIntegrationReauthorizationError(OAuthIntegrationStoreError):
    """A new provider authorization is required before this integration can run."""


_PRIVATE_STATE = private_state.PrivateState(
    OAuthIntegrationStoreError,
    "OAuth integration state is malformed",
    "OAuth integration envelope is malformed",
    (MAX_PLAINTEXT_BYTES * 2) + 128,
)


@dataclass(frozen=True, slots=True)
class OAuthIntegrationIdentity:
    id: str
    username: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class OAuthIntegrationMetadata:
    """Bounded public inventory that never contains either OAuth token."""

    id: str
    provider: str
    scopes: tuple[str, ...]
    status: IntegrationStatus
    integration: OAuthIntegrationIdentity | None
    expires_at: int | None
    generation: int


class _IntegrationFlight:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.users = 0


@dataclass(frozen=True, slots=True, repr=False)
class _TokenGrant:
    access_token: str
    refresh_token: str | None
    broker_lease: str | None
    scopes: tuple[str, ...]
    expires_at: int
    integration: OAuthIntegrationIdentity | None
    status: StoredStatus
    generation: int = 0


def _component_id(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) > 64 or _COMPONENT_ID.fullmatch(value) is None:
        raise OAuthIntegrationValidationError(f"{label} is invalid")
    return value


def _team_id(value: object) -> str:
    if not isinstance(value, str) or _TEAM_ID.fullmatch(value) is None:
        raise OAuthIntegrationValidationError("Team id is invalid")
    return value


def _bounded_text(
    value: object,
    label: str,
    maximum: int,
    *,
    optional: bool = False,
) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip() or not value.isprintable():
        raise OAuthIntegrationValidationError(f"{label} is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise OAuthIntegrationValidationError(f"{label} is invalid") from exc
    if len(encoded) > maximum:
        raise OAuthIntegrationValidationError(f"{label} is invalid")
    return value


def _token(value: object, label: str, *, optional: bool = False) -> str | None:
    return _bounded_text(value, label, MAX_TOKEN_BYTES, optional=optional)


def _integration(value: object) -> OAuthIntegrationIdentity | None:
    if value is None:
        return None
    if isinstance(value, OAuthIntegrationIdentity):
        raw_id, raw_username, raw_name = value.id, value.username, value.name
    elif isinstance(value, Mapping) and set(value) <= {"id", "username", "name"} and "id" in value:
        raw_id = value.get("id")
        raw_username = value.get("username")
        raw_name = value.get("name")
    else:
        raise OAuthIntegrationValidationError("OAuth integration is invalid")
    return OAuthIntegrationIdentity(
        id=str(_bounded_text(raw_id, "OAuth integration id", MAX_INTEGRATION_ID_BYTES)),
        username=_bounded_text(
            raw_username,
            "OAuth integration username",
            MAX_INTEGRATION_TEXT_BYTES,
            optional=True,
        ),
        name=_bounded_text(raw_name, "OAuth integration name", MAX_INTEGRATION_TEXT_BYTES, optional=True),
    )


def _stored_status(value: object) -> StoredStatus:
    if not isinstance(value, str) or value not in _STORED_STATUSES:
        raise OAuthIntegrationValidationError("OAuth integration status is invalid")
    return value  # type: ignore[return-value]


def _intent(provider_id: object, scopes: object) -> tuple[str, tuple[str, ...]]:
    try:
        intent = integration_providers.integration_intent(provider_id, scopes)
    except integration_providers.OAuthProviderError as exc:
        raise OAuthIntegrationValidationError("OAuth integration declaration is invalid") from exc
    return intent.provider.id, intent.scopes


def _token_set(
    value: object,
    expected_scopes: tuple[str, ...],
    now: int,
    integration: object,
) -> _TokenGrant:
    try:
        access_token = value.access_token  # type: ignore[attr-defined]
        refresh_token = value.refresh_token  # type: ignore[attr-defined]
        broker_lease = getattr(value, "broker_lease", None)
        raw_scopes = value.scopes  # type: ignore[attr-defined]
        expires_in = value.expires_in  # type: ignore[attr-defined]
    except (AttributeError, TypeError) as exc:
        raise OAuthIntegrationValidationError("OAuth token set is invalid") from exc
    if (
        not isinstance(raw_scopes, tuple)
        or raw_scopes != expected_scopes
        or type(expires_in) is not int
        or not 30 <= expires_in <= 31_536_000
    ):
        raise OAuthIntegrationValidationError("OAuth token set is invalid")
    expiry = now + expires_in
    if not 1 <= expiry <= (2**53 - 1):
        raise OAuthIntegrationValidationError("OAuth token expiry is invalid")
    return _TokenGrant(
        access_token=str(_token(access_token, "OAuth access token")),
        refresh_token=_token(refresh_token, "OAuth refresh token", optional=True),
        broker_lease=_token(broker_lease, "OAuth broker lease", optional=True),
        scopes=expected_scopes,
        expires_at=expiry,
        integration=_integration(integration),
        status="connected",
    )


def _strict_json(payload: bytes) -> object:
    try:
        return strict_json.loads(payload)
    except UnicodeDecodeError as exc:
        raise OAuthIntegrationStoreError("OAuth integration state is not valid JSON") from exc
    except ValueError as exc:
        message = (
            "OAuth integration state has duplicate fields"
            if str(exc) == "duplicate JSON field"
            else "OAuth integration state is not valid JSON"
        )
        raise OAuthIntegrationStoreError(message) from exc


def _record_metadata(
    record: Mapping[str, object],
) -> tuple[str, tuple[str, ...], int, StoredStatus, int]:
    provider = record.get("provider")
    raw_scopes = record.get("scopes")
    expires_at = record.get("expires_at")
    status = record.get("status")
    generation = record.get("generation")
    try:
        canonical_provider, scopes = _intent(provider, raw_scopes)
        canonical_status = _stored_status(status)
    except OAuthIntegrationValidationError as exc:
        raise OAuthIntegrationStoreError("OAuth integration state record is malformed") from exc
    if (
        not isinstance(raw_scopes, list)
        or tuple(raw_scopes) != scopes
        or type(expires_at) is not int
        or not 1 <= expires_at <= (2**53 - 1)
        or type(generation) is not int
        or generation < 1
    ):
        raise OAuthIntegrationStoreError("OAuth integration state record is malformed")
    return canonical_provider, scopes, expires_at, canonical_status, generation


def _validate_record(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "provider",
        "scopes",
        "expires_at",
        "status",
        "generation",
        "updated_at",
        "envelope",
    }:
        raise OAuthIntegrationStoreError("OAuth integration state record is malformed")
    _record_metadata(value)
    updated_at = value.get("updated_at")
    envelope = value.get("envelope")
    if (
        not isinstance(updated_at, str)
        or _TIMESTAMP.fullmatch(updated_at) is None
        or not isinstance(envelope, dict)
        or set(envelope) != {"algorithm", "nonce", "ciphertext"}
        or envelope.get("algorithm") != "AES-256-GCM"
    ):
        raise OAuthIntegrationStoreError("OAuth integration state record is malformed")
    _PRIVATE_STATE.decode_part(envelope.get("nonce"), expected=12)
    _PRIVATE_STATE.decode_part(envelope.get("ciphertext"), minimum=17, maximum=MAX_PLAINTEXT_BYTES + 16)
    return value


def _validate_state(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"schema", "teams"} or value.get("schema") != 1:
        raise OAuthIntegrationStoreError("OAuth integration state has an unsupported shape")
    teams = value.get("teams")
    if not isinstance(teams, dict):
        raise OAuthIntegrationStoreError("OAuth integration state is malformed")
    total = 0
    for raw_team, raw_assistants in teams.items():
        try:
            _team_id(raw_team)
        except OAuthIntegrationValidationError as exc:
            raise OAuthIntegrationStoreError("OAuth integration state is malformed") from exc
        if not isinstance(raw_assistants, dict):
            raise OAuthIntegrationStoreError("OAuth integration state is malformed")
        for raw_assistant, raw_integrations in raw_assistants.items():
            try:
                _component_id(raw_assistant, "Assistant id")
            except OAuthIntegrationValidationError as exc:
                raise OAuthIntegrationStoreError("OAuth integration state is malformed") from exc
            if not isinstance(raw_integrations, dict) or len(raw_integrations) > MAX_INTEGRATIONS_PER_ASSISTANT:
                raise OAuthIntegrationStoreError("OAuth integration state is malformed")
            for raw_integration, raw_record in raw_integrations.items():
                try:
                    _component_id(raw_integration, "integration id")
                except OAuthIntegrationValidationError as exc:
                    raise OAuthIntegrationStoreError("OAuth integration state is malformed") from exc
                _validate_record(raw_record)
                total += 1
                if total > MAX_TOTAL_RECORDS:
                    raise OAuthIntegrationStoreError("OAuth integration state exceeds its record limit")
    return value


def _aad(
    team_id: str,
    assistant_id: str,
    integration_id: str,
    record: Mapping[str, object],
) -> bytes:
    provider, scopes, expires_at, status, generation = _record_metadata(record)
    return json.dumps(
        [
            "shimpz-oauth-integration-v1",
            team_id,
            assistant_id,
            integration_id,
            provider,
            list(scopes),
            expires_at,
            status,
            generation,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def _declarations(value: object) -> dict[str, tuple[str, tuple[str, ...]]]:
    if not isinstance(value, Mapping) or len(value) > MAX_INTEGRATIONS_PER_ASSISTANT:
        raise OAuthIntegrationValidationError("OAuth integration declarations are invalid")
    declared: dict[str, tuple[str, tuple[str, ...]]] = {}
    for raw_id, raw_spec in value.items():
        integration_id = _component_id(raw_id, "integration id")
        if isinstance(raw_spec, Mapping) and set(raw_spec) == {"provider", "scopes"}:
            provider = raw_spec.get("provider")
            scopes = raw_spec.get("scopes")
        else:
            try:
                provider = raw_spec.provider  # type: ignore[attr-defined]
                scopes = raw_spec.scopes  # type: ignore[attr-defined]
            except (AttributeError, TypeError) as exc:
                raise OAuthIntegrationValidationError("OAuth integration declarations are invalid") from exc
        declared[integration_id] = _intent(provider, scopes)
    return declared


def _declared_ids(value: object) -> tuple[str, ...]:
    values: Iterable[object]
    if isinstance(value, Mapping):
        values = value.keys()
    elif isinstance(value, Iterable) and not isinstance(value, str | bytes):
        values = value
    else:
        raise OAuthIntegrationValidationError("OAuth integration ids are invalid")
    declared: list[str] = []
    seen: set[str] = set()
    for raw_id in values:
        if len(declared) == MAX_INTEGRATIONS_PER_ASSISTANT:
            raise OAuthIntegrationValidationError("OAuth integration ids are invalid")
        integration_id = _component_id(raw_id, "integration id")
        if integration_id in seen:
            raise OAuthIntegrationValidationError("OAuth integration ids are invalid")
        declared.append(integration_id)
        seen.add(integration_id)
    return tuple(declared)


class OAuthIntegrationStore:
    def __init__(
        self,
        state_path: Path = STATE_PATH,
        key_path: Path = KEY_PATH,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state_path = Path(state_path)
        self.key_path = Path(key_path)
        if not self.state_path.is_absolute() or not self.key_path.is_absolute():
            raise OAuthIntegrationStoreError("OAuth integration state and key paths must be absolute")
        try:
            state_parent = self.state_path.parent.resolve()
            key_parent = self.key_path.parent.resolve()
        except OSError as exc:
            raise OAuthIntegrationStoreError("OAuth integration storage paths are unavailable") from exc
        if state_parent == key_parent:
            raise OAuthIntegrationStoreError("OAuth integration keyring must be separate from encrypted state")
        if not callable(clock):
            raise OAuthIntegrationStoreError("OAuth integration clock is invalid")
        self._clock = clock
        self._lock = threading.RLock()
        self._integration_flights: dict[tuple[str, str, str], _IntegrationFlight] = {}
        self._state_cache_identity: private_state.PrivateFileIdentity | None = None
        self._state_cache: dict[str, object] | None = None

    @contextmanager
    def _integration_flight(self, team: str, assistant: str, integration: str):
        key = (team, assistant, integration)
        with self._lock:
            flight = self._integration_flights.setdefault(key, _IntegrationFlight())
            flight.users += 1
        flight.lock.acquire()
        try:
            yield
        finally:
            flight.lock.release()
            with self._lock:
                flight.users -= 1
                if flight.users == 0 and self._integration_flights.get(key) is flight:
                    self._integration_flights.pop(key)

    def _now(self) -> int:
        now = self._clock()
        if not isinstance(now, int | float) or isinstance(now, bool) or not 0 <= now <= (2**53 - 1):
            raise OAuthIntegrationStoreError("OAuth integration clock is invalid")
        return int(now)

    def _read_state(self) -> dict[str, object]:
        snapshot = _PRIVATE_STATE.read_private_file_if_changed(
            self.state_path,
            MAX_STATE_BYTES,
            "OAuth integration state",
            self._state_cache_identity,
            cache_initialized=self._state_cache is not None,
        )
        if snapshot.unchanged:
            if self._state_cache is None:
                raise OAuthIntegrationStoreError("OAuth integration state cache is unavailable")
            return self._state_cache
        state = (
            private_state.empty_state() if snapshot.payload is None else _validate_state(_strict_json(snapshot.payload))
        )
        self._state_cache_identity = snapshot.identity
        self._state_cache = state
        return state

    def _read_state_for_update(self) -> dict[str, object]:
        return copy.deepcopy(self._read_state())

    def _drop_state_cache(self) -> None:
        self._state_cache_identity = None
        self._state_cache = None

    def _write_state(self, state: Mapping[str, object]) -> None:
        try:
            validated = _validate_state(dict(state))
            payload = json.dumps(validated, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if len(payload) > MAX_STATE_BYTES:
                raise OAuthIntegrationStoreError("OAuth integration state exceeds its fixed byte limit")
            _PRIVATE_STATE.atomic_write(self.state_path, payload, "OAuth integration state")
        finally:
            self._drop_state_cache()

    def _key(self, *, allow_create: bool = False) -> bytes:
        return _PRIVATE_STATE.key(self.key_path, "OAuth integration keyring", allow_create=allow_create)

    @staticmethod
    def _plaintext(grant: _TokenGrant) -> bytes:
        payload = json.dumps(
            {
                "access_token": grant.access_token,
                "refresh_token": grant.refresh_token,
                "broker_lease": grant.broker_lease,
                "integration": (
                    None
                    if grant.integration is None
                    else {
                        "id": grant.integration.id,
                        "username": grant.integration.username,
                        "name": grant.integration.name,
                    }
                ),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_PLAINTEXT_BYTES:
            raise OAuthIntegrationValidationError("OAuth token set is too large")
        return payload

    @staticmethod
    def _decrypted(
        plaintext: bytes,
        provider: str,
        scopes: tuple[str, ...],
        expires_at: int,
        status: StoredStatus,
        generation: int,
    ) -> _TokenGrant:
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise OAuthIntegrationStoreError("decrypted OAuth integration is malformed")
        value = _strict_json(plaintext)
        if not isinstance(value, dict) or set(value) != {
            "access_token",
            "refresh_token",
            "broker_lease",
            "integration",
        }:
            raise OAuthIntegrationStoreError("decrypted OAuth integration is malformed")
        try:
            integration = _integration(value.get("integration"))
            return _TokenGrant(
                access_token=str(_token(value.get("access_token"), "OAuth access token")),
                refresh_token=_token(value.get("refresh_token"), "OAuth refresh token", optional=True),
                broker_lease=_token(value.get("broker_lease"), "OAuth broker lease", optional=True),
                scopes=scopes,
                expires_at=expires_at,
                integration=integration,
                status=status,
                generation=generation,
            )
        except OAuthIntegrationValidationError as exc:
            raise OAuthIntegrationStoreError("decrypted OAuth integration is malformed") from exc

    def _resolve_record(
        self,
        team: str,
        assistant: str,
        integration: str,
        record: object,
    ) -> _TokenGrant:
        validated = _validate_record(record)
        provider, scopes, expires_at, status, generation = _record_metadata(validated)
        envelope = validated["envelope"]
        if not isinstance(envelope, dict):
            raise OAuthIntegrationStoreError("OAuth integration envelope is malformed")
        try:
            plaintext = AESGCM(self._key()).decrypt(
                _PRIVATE_STATE.decode_part(envelope.get("nonce"), expected=12),
                _PRIVATE_STATE.decode_part(envelope.get("ciphertext")),
                _aad(team, assistant, integration, validated),
            )
        except InvalidTag as exc:
            raise OAuthIntegrationStoreError("OAuth integration envelope authentication failed") from exc
        return self._decrypted(plaintext, provider, scopes, expires_at, status, generation)

    def _declared_grant(
        self,
        team: str,
        assistant: str,
        integration: str,
        provider: str,
        scopes: tuple[str, ...],
    ) -> _TokenGrant:
        state = self._read_state()
        records = _PRIVATE_STATE.records(state, team, assistant, create=False)
        if integration not in records:
            raise OAuthIntegrationMissingError("OAuth integration is not configured")
        record = _validate_record(records[integration])
        stored_provider, stored_scopes, _, _, _ = _record_metadata(record)
        if stored_provider != provider or stored_scopes != scopes:
            raise OAuthIntegrationReauthorizationError(
                "OAuth integration declaration changed; reauthorization is required"
            )
        grant = self._resolve_record(team, assistant, integration, record)
        if grant.status == "reauthorization-required":
            raise OAuthIntegrationReauthorizationError("OAuth integration requires reauthorization")
        return grant

    def put(
        self,
        team_id: object,
        assistant_id: object,
        integration_id: object,
        provider: object,
        scopes: object,
        token_set: object,
        identity: object = None,
    ) -> OAuthIntegrationMetadata:
        """Encrypt one exchanged token set and atomically advance its generation."""
        team = _team_id(team_id)
        assistant = _component_id(assistant_id, "Assistant id")
        integration = _component_id(integration_id, "integration id")
        canonical_provider, canonical_scopes = _intent(provider, scopes)
        canonical = _token_set(token_set, canonical_scopes, self._now(), identity)
        plaintext = self._plaintext(canonical)
        with self._lock:
            state = self._read_state_for_update()
            key = self._key(allow_create=not _PRIVATE_STATE.has_records(state))
            records = _PRIVATE_STATE.records(state, team, assistant, create=True)
            if integration not in records and len(records) >= MAX_INTEGRATIONS_PER_ASSISTANT:
                raise OAuthIntegrationStoreError("OAuth integration capacity reached")
            previous = records.get(integration)
            generation = int(previous.get("generation", 0)) + 1 if isinstance(previous, dict) else 1
            record: dict[str, object] = {
                "provider": canonical_provider,
                "scopes": list(canonical.scopes),
                "expires_at": canonical.expires_at,
                "status": canonical.status,
                "generation": generation,
                "updated_at": private_state.timestamp(),
                "envelope": {},
            }
            nonce = os.urandom(12)
            ciphertext = AESGCM(key).encrypt(
                nonce,
                plaintext,
                _aad(team, assistant, integration, record),
            )
            record["envelope"] = {
                "algorithm": "AES-256-GCM",
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            }
            records[integration] = record
            self._write_state(state)
            return OAuthIntegrationMetadata(
                integration,
                canonical_provider,
                canonical.scopes,
                "connected",
                canonical.integration,
                canonical.expires_at,
                generation,
            )

    def resolve(
        self,
        team_id: object,
        assistant_id: object,
        integration_id: object,
        provider: object,
        scopes: object,
        refresh_callback: Callable[[str, str | None], object],
    ) -> str:
        """Return one bounded access token, refreshing once under a single-flight lock."""
        team = _team_id(team_id)
        assistant = _component_id(assistant_id, "Assistant id")
        integration = _component_id(integration_id, "integration id")
        canonical_provider, expected_scopes = _intent(provider, scopes)
        if not callable(refresh_callback):
            raise OAuthIntegrationValidationError("OAuth refresh callback is invalid")
        with self._integration_flight(team, assistant, integration):
            with self._lock:
                grant = self._declared_grant(
                    team,
                    assistant,
                    integration,
                    canonical_provider,
                    expected_scopes,
                )
            if grant.expires_at > self._now() + REFRESH_WINDOW_SECONDS:
                return grant.access_token
            if grant.refresh_token is None:
                raise OAuthIntegrationReauthorizationError("OAuth integration requires reauthorization")
            refreshed = refresh_callback(grant.refresh_token, grant.broker_lease)
            canonical = _token_set(refreshed, expected_scopes, self._now(), grant.integration)
            with self._lock:
                current = self._declared_grant(
                    team,
                    assistant,
                    integration,
                    canonical_provider,
                    expected_scopes,
                )
                if current.generation != grant.generation:
                    raise OAuthIntegrationReauthorizationError("OAuth integration changed during refresh")
                self.put(
                    team,
                    assistant,
                    integration,
                    canonical_provider,
                    expected_scopes,
                    refreshed,
                    grant.integration,
                )
            return canonical.access_token

    def metadata(
        self,
        team_id: object,
        assistant_id: object,
        declarations: object,
    ) -> tuple[OAuthIntegrationMetadata, ...]:
        """Return complete declared inventory, including missing integration rows."""
        team = _team_id(team_id)
        assistant = _component_id(assistant_id, "Assistant id")
        declared = _declarations(declarations)
        with self._lock:
            state = self._read_state()
            records = _PRIVATE_STATE.records(state, team, assistant, create=False)
            now = self._now()
            result: list[OAuthIntegrationMetadata] = []
            for integration, (provider, scopes) in declared.items():
                if integration not in records:
                    result.append(
                        OAuthIntegrationMetadata(
                            integration,
                            provider,
                            scopes,
                            "missing",
                            None,
                            None,
                            0,
                        )
                    )
                    continue
                record = _validate_record(records[integration])
                stored_provider, stored_scopes, expires_at, status, generation = _record_metadata(record)
                grant = self._resolve_record(team, assistant, integration, record)
                if stored_provider != provider or stored_scopes != scopes:
                    result.append(
                        OAuthIntegrationMetadata(
                            integration,
                            provider,
                            scopes,
                            "reauthorization-required",
                            None,
                            expires_at,
                            generation,
                        )
                    )
                    continue
                projected: IntegrationStatus = status
                if status == "connected" and expires_at <= now:
                    projected = "refresh-required" if grant.refresh_token is not None else "reauthorization-required"
                result.append(
                    OAuthIntegrationMetadata(
                        integration,
                        provider,
                        scopes,
                        projected,
                        grant.integration,
                        expires_at,
                        generation,
                    )
                )
            return tuple(result)

    def retain_declared(
        self,
        team_id: object,
        assistant_id: object,
        declared_ids: object,
    ) -> bool:
        """Atomically discard integrations removed from a new Assistant release."""
        team = _team_id(team_id)
        assistant = _component_id(assistant_id, "Assistant id")
        declared = set(_declared_ids(declared_ids))
        with self._lock:
            state = self._read_state_for_update()
            records = _PRIVATE_STATE.records(state, team, assistant, create=False)
            undeclared = set(records) - declared
            if not undeclared:
                return False
            for integration in undeclared:
                records.pop(integration)
            _PRIVATE_STATE.prune_empty_records(state, team, assistant)
            self._write_state(state)
            return True

    def delete_integration(
        self,
        team_id: object,
        assistant_id: object,
        integration_id: object,
    ) -> bool:
        team = _team_id(team_id)
        assistant = _component_id(assistant_id, "Assistant id")
        integration = _component_id(integration_id, "integration id")
        with self._lock:
            state = self._read_state_for_update()
            records = _PRIVATE_STATE.records(state, team, assistant, create=False)
            removed = records.pop(integration, None) is not None
            if removed:
                _PRIVATE_STATE.prune_empty_records(state, team, assistant)
                self._write_state(state)
            return removed

    def revoke_then_delete(
        self,
        team_id: object,
        assistant_id: object,
        integration_id: object,
        revoke_callback: Callable[[str, str, str | None, str | None], None],
    ) -> bool:
        """Delete one grant only after its authenticated tokens are revoked upstream."""
        team = _team_id(team_id)
        assistant = _component_id(assistant_id, "Assistant id")
        integration = _component_id(integration_id, "integration id")
        if not callable(revoke_callback):
            raise OAuthIntegrationValidationError("OAuth revocation callback is invalid")
        with self._integration_flight(team, assistant, integration):
            with self._lock:
                state = self._read_state()
                records = _PRIVATE_STATE.records(state, team, assistant, create=False)
                raw_record = records.get(integration)
                if raw_record is None:
                    return False
                record = _validate_record(raw_record)
                provider, _, _, _, generation = _record_metadata(record)
                grant = self._resolve_record(team, assistant, integration, record)
            revoke_callback(provider, grant.access_token, grant.refresh_token, grant.broker_lease)
            with self._lock:
                state = self._read_state_for_update()
                records = _PRIVATE_STATE.records(state, team, assistant, create=False)
                current = records.get(integration)
                if current is None or _record_metadata(_validate_record(current))[4] != generation:
                    raise OAuthIntegrationStoreError("OAuth integration changed during revocation")
                records.pop(integration)
                _PRIVATE_STATE.prune_empty_records(state, team, assistant)
                self._write_state(state)
                return True

    def delete_assistant(self, team_id: object, assistant_id: object) -> bool:
        team = _team_id(team_id)
        assistant = _component_id(assistant_id, "Assistant id")
        with self._lock:
            state = self._read_state_for_update()
            removed = _PRIVATE_STATE.delete_assistant(state, team, assistant)
            if removed:
                self._write_state(state)
            return removed

    def delete_team(self, team_id: object) -> bool:
        team = _team_id(team_id)
        with self._lock:
            state = self._read_state_for_update()
            removed = _PRIVATE_STATE.delete_team(state, team)
            if removed:
                self._write_state(state)
            return removed

    def delete_all(self) -> bool:
        """Atomically purge all integration material during an owned Space reset."""
        with self._lock:
            state = self._read_state_for_update()
            if not _PRIVATE_STATE.has_records(state):
                return False
            self._write_state(private_state.empty_state())
            return True
