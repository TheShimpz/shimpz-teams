"""Immutable Assistant manifest admission for reviewed security intent."""

import copy
import io
import json
import re
import tarfile
import tomllib
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing.exceptions import Unresolvable

from assistant import action_schema
from assistant import cache as assistant_cache
from assistant import effect as action_effect
from integrations import providers as integration_providers
from protocol.assistant.v1.validators import input_file as input_file_validator
from protocol.assistant.v1.validators import message_catalog as catalog_validator
from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import strict_json

MANIFEST_PATH = "/opt/shimpz/shimpz.toml"
CONTRACT_PATH = "/opt/shimpz/shimpz.contract.json"
MAX_MANIFEST_BYTES = 256 * 1024
MAX_CONTRACT_BYTES = 512 * 1024
# Each Action schema has its own JSON value bound; this one keeps up to 128 Actions from adding up to dense data.
MAX_CONTRACT_NODES = 32_768
MAX_ARCHIVE_BYTES = MAX_MANIFEST_BYTES + (32 * 1024)
MAX_ALLOWED_HOSTS = 32
MAX_INTEGRATIONS = 16
MAX_STORED_INPUTS = 8
MAX_GENESIS_LENGTH = 65_536
DEFAULT_CACHE_ENTRIES = 256
HUMAN_REQUEST_KINDS = frozenset(
    {
        "approval",
        "auth:passkey",
        "auth:password",
        "auth:totp",
        "input:choice",
        "input:choices",
        "input:password",
        "input:phone",
        "input:select",
        "input:text",
        "input:textarea",
    }
)
AUTHORIZATION_REQUEST_KINDS = frozenset({"approval", "auth:passkey", "auth:password", "auth:totp"})
VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_CREATOR_RE = re.compile(r"@[a-z0-9][a-z0-9-]{1,30}[a-z0-9]\Z")
_GITHUB_RE = re.compile(
    r"https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?\Z"
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~-]{12,}|(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)"
    r"\s*[:=]\s*\S+|(?:sk|ghp|github_pat|glpat|xox[baprs])[-_][a-z0-9_-]{12,})"
)
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\Z")
_PUBLIC_HOST_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_NON_PUBLIC_HOST_SUFFIXES = (
    ".arpa",
    ".example",
    ".home",
    ".internal",
    ".invalid",
    ".lan",
    ".local",
    ".localdomain",
    ".localhost",
    ".onion",
    ".test",
)


# Fields Team owns in every provider call, so no Stored Input is ever placed in one (ADR-0106).
RESERVED_HEADERS = frozenset(
    {
        "accept-encoding",
        "connection",
        "content-length",
        "expect",
        "host",
        "keep-alive",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_HEADER_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}\Z")
_QUERY_RE = re.compile(r"[A-Za-z0-9._~-]{1,64}\Z")
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,31}\Z")
_PLACEMENT_FIELDS = ("host", "header", "query", "scheme", "hmac")


class ManifestError(RuntimeError):
    """An immutable Assistant package did not expose its reviewed security intent."""


class ManifestUnavailableError(ManifestError):
    """The immutable Assistant package could not be read from its container."""


@dataclass(frozen=True, slots=True, order=True)
class IntegrationDeclaration:
    """Public provider intent for one controller-owned integration."""

    id: str
    provider: str
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True, order=True)
class StoredInputDeclaration:
    """Public intent for one controller-custodied persistent Action input."""

    id: str
    kind: str
    label: str
    # The English help text a person reads before entering the value: what it is and how to get it.
    description: str
    # The official page where a person creates or finds the value, or the documentation that explains how.
    help_url: str
    # Its placement (ADR-0106): the one host that receives the value and the header or query field Team puts it in,
    # with an optional header scheme and an optional HMAC proof over another Stored Input's value.
    host: str = ""
    header: str | None = None
    query: str | None = None
    scheme: str | None = None
    hmac: str | None = None

    def metadata(self) -> dict[str, str]:
        """The closed declaration fields after its id, as manifests, resolutions, and records carry them."""
        fields = {"kind": self.kind, "label": self.label, "description": self.description, "help_url": self.help_url}
        optional = {name: getattr(self, name) for name in _PLACEMENT_FIELDS}
        return fields | {name: value for name, value in optional.items() if value is not None}

    def document(self) -> dict[str, str]:
        """One declaration as a resolution or Local record lists it."""
        return {"id": self.id, **self.metadata()}


@dataclass(frozen=True, slots=True)
class ManifestContract:
    """Canonical security intent admitted from one immutable Assistant package."""

    allowed_hosts: tuple[str, ...]
    integrations: tuple[IntegrationDeclaration, ...]
    stored_inputs: tuple[StoredInputDeclaration, ...]


@dataclass(frozen=True, slots=True)
class ManifestIdentity:
    """Bounded Assistant identity and display metadata from one complete manifest."""

    assistant_id: str
    version: str
    name: str
    summary: str


@dataclass(frozen=True, slots=True)
class ManifestPresentation:
    """Creator-declared Assistant page copy: one description paragraph and unverified public links."""

    description: str
    # Kind to URL in canonical display order; empty when the manifest declares no links.
    links: Mapping[str, str]


def canonical_allowed_hosts(value: object) -> tuple[str, ...]:
    """Return one deterministic list of exact public DNS host names."""
    if not isinstance(value, list | tuple) or len(value) > MAX_ALLOWED_HOSTS:
        raise ManifestError("Assistant allowed_hosts is invalid")
    hosts: list[str] = []
    for host in value:
        if not isinstance(host, str) or not 1 <= len(host) <= 253 or not host.isascii() or host != host.lower():
            raise ManifestError("Assistant allowed_hosts is invalid")
        if _PUBLIC_HOST_RE.fullmatch(host) is None or host.endswith(_NON_PUBLIC_HOST_SUFFIXES):
            raise ManifestError("Assistant allowed_hosts is invalid")
        hosts.append(host)
    if len(set(hosts)) != len(hosts):
        raise ManifestError("Assistant allowed_hosts is invalid")
    return tuple(sorted(hosts))


def resembles_credential(value: str) -> bool:
    """Whether text looks like credential material: a bearer token, an assigned secret, a provider key, or a JWT."""
    return _SECRET_VALUE_RE.search(value) is not None or _JWT_RE.fullmatch(value.strip()) is not None


def _identifier(
    value: object, *, kind: str, canonical: Callable[[object], str | None] = http_payload.canonical_identifier
) -> str:
    identifier = canonical(value)
    if identifier is None:
        raise ManifestError(f"Assistant {kind} identifier is invalid")
    return identifier


def _public_text(value: object, *, kind: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or "\n" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ManifestError(f"Assistant {kind} is invalid")
    if resembles_credential(value):
        raise ManifestError(f"Assistant {kind} resembles credential material")
    return value


def _display_text(value: object, *, kind: str, maximum: int) -> str:
    """Public text an Assistant page displays: one printable NFC line, as every translation of it must be."""
    text = _public_text(value, kind=kind, maximum=maximum)
    if not text.isprintable() or not unicodedata.is_normalized("NFC", text):
        raise ManifestError(f"Assistant {kind} is invalid")
    return text


def _genesis(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_GENESIS_LENGTH
        or any(not character.isprintable() and character not in {"\n", "\t"} for character in value)
    ):
        raise ManifestError("Assistant genesis is invalid")
    return value


def canonical_integration_declarations(value: object) -> tuple[IntegrationDeclaration, ...]:
    """Canonicalize integrations whose id is a controller-reviewed provider id."""
    if not isinstance(value, Mapping) or len(value) > MAX_INTEGRATIONS:
        raise ManifestError("Assistant integration declarations are invalid")
    declarations: list[IntegrationDeclaration] = []
    for integration_id, scopes in value.items():
        identifier = _identifier(integration_id, kind="integration")
        try:
            intent = integration_providers.integration_intent(identifier, scopes)
        except integration_providers.OAuthProviderError as exc:
            raise ManifestError("Assistant integration declaration is invalid") from exc
        declarations.append(
            IntegrationDeclaration(
                identifier,
                intent.provider.id,
                intent.scopes,
            )
        )
    return tuple(sorted(declarations))


def canonical_stored_input_declarations(
    value: object, allowed_hosts: tuple[str, ...]
) -> tuple[StoredInputDeclaration, ...]:
    """Canonicalize bounded declarations and their placements without accepting credential values."""
    if not isinstance(value, Mapping) or len(value) > MAX_STORED_INPUTS:
        raise ManifestError("Assistant Stored Input declarations are invalid")
    declarations: list[StoredInputDeclaration] = []
    for stored_input_id, metadata in value.items():
        identifier = _identifier(stored_input_id, kind="Stored Input")
        if not isinstance(metadata, Mapping) or not {"kind", "label", "description", "help_url", "host"} <= set(
            metadata
        ) <= {
            "kind",
            "label",
            "description",
            "help_url",
            *_PLACEMENT_FIELDS,
        }:
            raise ManifestError("Assistant Stored Input declaration is invalid")
        if metadata["kind"] != "password":
            raise ManifestError("Assistant Stored Input kind is invalid")
        if (help_url := http_payload.canonical_help_url(metadata["help_url"])) is None:
            raise ManifestError("Assistant Stored Input help_url is invalid")
        declarations.append(
            StoredInputDeclaration(
                id=identifier,
                kind="password",
                label=_public_text(metadata["label"], kind="Stored Input label", maximum=80),
                description=_display_text(
                    metadata["description"],
                    kind="Stored Input description",
                    maximum=catalog_validator.DESCRIPTION_CHARS,
                ),
                help_url=help_url,
                **_placement(metadata, allowed_hosts),
            )
        )
    _require_distinct_placements(declarations)
    return tuple(sorted(declarations))


def _placement(metadata: Mapping[str, object], allowed_hosts: tuple[str, ...]) -> dict[str, str | None]:
    """One Stored Input's placement: an allowed host and exactly one field Team does not own there (ADR-0106)."""
    placement = {name: metadata.get(name) for name in _PLACEMENT_FIELDS}
    header, query, scheme, signed = (placement[name] for name in ("header", "query", "scheme", "hmac"))
    if (
        placement["host"] not in allowed_hosts
        or (header is None) == (query is None)
        or (header is not None and (not isinstance(header, str) or _HEADER_RE.match(header) is None))
        or (header is not None and header.lower() in RESERVED_HEADERS)
        or (query is not None and (not isinstance(query, str) or _QUERY_RE.match(query) is None))
        or (scheme is not None and (header is None or not isinstance(scheme, str) or _SCHEME_RE.match(scheme) is None))
        or (signed is not None and http_payload.canonical_identifier(signed) is None)
    ):
        raise ManifestError("Assistant Stored Input placement is invalid")
    return placement


def _require_distinct_placements(declarations: list[StoredInputDeclaration]) -> None:
    """No two values share a field on one host, and a proof signs exactly one plain Stored Input of its host."""
    by_id = {declaration.id: declaration for declaration in declarations}
    fields = [placement_field(declaration) for declaration in declarations]
    for declaration in declarations:
        target = by_id.get(declaration.hmac) if declaration.hmac is not None else None
        if declaration.hmac is not None and (
            target is None or target is declaration or target.hmac is not None or target.host != declaration.host
        ):
            raise ManifestError("Assistant Stored Input proof is invalid")
    if len(set(fields)) != len(fields):
        raise ManifestError("Assistant Stored Input placements overlap")


def placement_field(declaration: StoredInputDeclaration) -> tuple[str, str, str]:
    """The host and case-folded header, or exact query parameter, one Stored Input occupies."""
    if declaration.header is not None:
        return declaration.host, "header", declaration.header.lower()
    return declaration.host, "query", declaration.query or ""


def stored_input_declarations_from_documents(
    value: Iterable[Mapping[str, object]], allowed_hosts: tuple[str, ...]
) -> tuple[StoredInputDeclaration, ...]:
    """Canonicalize the declaration list a resolution carries, each entry its id plus its closed fields."""
    return canonical_stored_input_declarations(
        {document["id"]: {key: item for key, item in document.items() if key != "id"} for document in value},
        allowed_hosts,
    )


def canonical_manifest_contract(
    *,
    allowed_hosts: object,
    integration_declarations: object | None = None,
    stored_input_declarations: object | None = None,
) -> ManifestContract:
    """Build one deterministic contract for package and reviewed registry comparison."""
    integrations = canonical_integration_declarations(
        {} if integration_declarations is None else integration_declarations
    )
    hosts = canonical_allowed_hosts(allowed_hosts)
    stored_inputs = canonical_stored_input_declarations(
        {} if stored_input_declarations is None else stored_input_declarations, hosts
    )
    # An Integration bearer owns Authorization on its provider's API hosts, so no Stored Input may take it there.
    bearer_hosts = {
        host for integration in integrations for host in integration_providers.resolve(integration.provider).api_hosts
    }
    if any(
        placement_field(item) in {(host, "header", "authorization") for host in bearer_hosts} for item in stored_inputs
    ):
        raise ManifestError("Assistant Stored Input placements overlap")
    return ManifestContract(allowed_hosts=hosts, integrations=integrations, stored_inputs=stored_inputs)


def automatic_update_preserves_egress(previous: ManifestContract, successor: ManifestContract) -> bool:
    """Return whether a successor stays within the installed outbound-host and credential-placement envelope."""
    if not isinstance(previous, ManifestContract) or not isinstance(successor, ManifestContract):
        raise ManifestError("Assistant update manifest contract is invalid")
    # A retained Stored Input keeps its value only while its value keeps its exact placement (ADR-0106).
    retained = {item.id: _placement_of(item) for item in previous.stored_inputs}
    return set(successor.allowed_hosts).issubset(previous.allowed_hosts) and all(
        retained[item.id] == _placement_of(item) for item in successor.stored_inputs if item.id in retained
    )


def _placement_of(declaration: StoredInputDeclaration) -> tuple[str | None, ...]:
    return tuple(getattr(declaration, name) for name in _PLACEMENT_FIELDS)


def reviewed_manifest_contract(
    *,
    allowed_hosts: object,
    integrations: object | None = None,
    stored_inputs: object | None = None,
) -> ManifestContract:
    """Normalize the controller-owned registry dataclasses without trusting package input."""
    if stored_inputs is None:
        stored_inputs = {}
    if not isinstance(integrations, Mapping) or not isinstance(stored_inputs, Mapping):
        raise ManifestError("Assistant reviewed manifest contract is invalid")
    try:
        integration_declarations = {
            integration_id: metadata.scopes for integration_id, metadata in integrations.items()
        }
        stored_input_declarations = {
            stored_input_id: {
                name: value
                for name in ("kind", "label", "description", "help_url", *_PLACEMENT_FIELDS)
                if (value := getattr(metadata, name)) is not None
            }
            for stored_input_id, metadata in stored_inputs.items()
        }
        if any(integration_id != metadata.provider for integration_id, metadata in integrations.items()):
            raise ManifestError("Assistant reviewed integration provider does not match its id")
    except AttributeError as exc:
        raise ManifestError("Assistant reviewed manifest contract is invalid") from exc
    return canonical_manifest_contract(
        allowed_hosts=allowed_hosts,
        integration_declarations=integration_declarations,
        stored_input_declarations=stored_input_declarations,
    )


def _strict_json(raw: bytes, *, maximum: int, kind: str) -> object:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= maximum:
        raise ManifestError(f"Assistant {kind} has an invalid size")
    try:
        return strict_json.loads(raw)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ManifestError(f"Assistant {kind} is invalid JSON") from exc


def action_schema_validator(schema: dict[str, Any]) -> Draft202012Validator:
    """Build a validator for a reviewed Action schema that resolves references only inside that schema."""
    return action_schema.payload_validator(schema)


def _machine_schema(value: object, *, kind: str) -> dict[str, Any]:
    try:
        return action_schema.admitted(value)
    except action_schema.ActionSchemaError as exc:
        raise ManifestError(f"Assistant Action {kind} schema {exc}") from exc.__cause__


_ACTION_FIELDS = frozenset(
    {
        "id",
        "description",
        "input_schema",
        "output_schema",
        "integrations",
        "stored_inputs",
        "input_files",
        "human_requests",
    }
)
_ACTION_REQUIRED = _ACTION_FIELDS | {"effect"}
_ACTION_OPTIONAL = frozenset({"verifier", "idempotency"})


def canonical_machine_contract(
    value: object,
    declared_integrations: tuple[IntegrationDeclaration, ...],
    declared_stored_inputs: tuple[StoredInputDeclaration, ...] = (),
    *,
    summary: str,
    description: str,
    allowed_hosts: tuple[str, ...],
) -> dict[str, Any]:
    """Validate and canonicalize an untrusted SDK-generated Action contract and its English message catalog.

    The published summary must be one catalog message (ADR-0091), and so must every other displayed static text: the
    Assistant description, each Action description, and each Stored Input label and help text, within its catalog
    bound. The caller
    supplies the summary and description it admitted. An idempotency provider must be one of the manifest's exact
    outbound hosts (ADR-0092), so the caller supplies those too.
    """
    if not isinstance(value, dict) or set(value) != {"version", "actions", "messages"} or value["version"] != 1:
        raise ManifestError("Assistant machine contract has an unsupported shape")
    if not action_schema.json_nodes_within(value, MAX_CONTRACT_NODES):
        raise ManifestError("Assistant machine contract is too large")
    if catalog_validator.catalog_error(value["messages"], summary) is not None:
        raise ManifestError("Assistant machine contract message catalog is invalid")
    raw_actions = value["actions"]
    if not isinstance(raw_actions, list) or not 1 <= len(raw_actions) <= 128:
        raise ManifestError("Assistant machine contract Actions are invalid")
    declared_ids = {integration.id for integration in declared_integrations}
    declared_stored_input_ids = {stored_input.id for stored_input in declared_stored_inputs}
    actions = [_canonical_action(raw, declared_ids, declared_stored_input_ids) for raw in raw_actions]
    if len({action["id"] for action in actions}) != len(actions):
        raise ManifestError("Assistant machine contract Action id is duplicated")
    used_integrations = {integration for action in actions for integration in action["integrations"]}
    if used_integrations != declared_ids:
        raise ManifestError("Assistant machine contract must use every declared integration")
    if not proofs_signed(actions, declared_stored_inputs):
        raise ManifestError("Assistant machine contract Action declares a proof without the Stored Input it signs")
    refused = action_effect.refusal(actions, allowed_hosts)
    if refused is not None:
        raise ManifestError(f"Assistant machine contract Action effect is invalid: {refused}")
    displayed = catalog_validator.display_uses(
        description,
        (action["description"] for action in actions),
        (stored_input.label for stored_input in declared_stored_inputs),
        (stored_input.description for stored_input in declared_stored_inputs),
    )
    if catalog_validator.display_error(value["messages"], displayed) is not None:
        raise ManifestError("Assistant machine contract message catalog lacks its displayed copy")
    return {
        "version": 1,
        "actions": sorted(actions, key=lambda action: action["id"]),
        "messages": json.loads(catalog_validator.canonical_json(value["messages"])),
    }


def proofs_signed(actions: Iterable[Mapping[str, Any]], declarations: Iterable[StoredInputDeclaration]) -> bool:
    """Whether every Action declaring a proof also declares the Stored Input it signs, which Team places beside it."""
    signed = {declaration.id: declaration.hmac for declaration in declarations if declaration.hmac}
    return all(
        signed[slot] in action["stored_inputs"]
        for action in actions
        for slot in action["stored_inputs"]
        if slot in signed
    )


def _canonical_action(
    raw_action: object, declared_ids: set[str], declared_stored_input_ids: set[str]
) -> dict[str, Any]:
    """One Action's closed members, its declared capabilities, and its admitted schemas."""
    if not isinstance(raw_action, dict) or not _ACTION_REQUIRED <= set(raw_action) <= (
        _ACTION_REQUIRED | _ACTION_OPTIONAL
    ):
        raise ManifestError("Assistant machine contract Action is invalid")
    action_id = _identifier(raw_action["id"], kind="Action")
    integrations = raw_action["integrations"]
    if (
        not isinstance(integrations, list)
        or len(integrations) > 4
        or len(integrations) != len(set(integrations))
        or any(
            not isinstance(integration_id, str) or integration_id not in declared_ids for integration_id in integrations
        )
    ):
        raise ManifestError("Assistant machine contract Action integrations are invalid")
    stored_inputs = raw_action["stored_inputs"]
    if (
        not isinstance(stored_inputs, list)
        or len(stored_inputs) > MAX_STORED_INPUTS
        or any(
            not isinstance(stored_input_id, str) or stored_input_id not in declared_stored_input_ids
            for stored_input_id in stored_inputs
        )
        # An Action may use any of its manifest's Stored Inputs, as one sorted, unique list (ADR-0059).
        or stored_inputs != sorted(set(stored_inputs))
    ):
        raise ManifestError("Assistant machine contract Action Stored Inputs are invalid")
    human_requests = raw_action["human_requests"]
    if (
        not isinstance(human_requests, list)
        or len(human_requests) > 8
        or len(human_requests) != len(set(human_requests))
        or any(kind not in HUMAN_REQUEST_KINDS for kind in human_requests)
        or sum(kind in AUTHORIZATION_REQUEST_KINDS for kind in human_requests) > 1
    ):
        raise ManifestError("Assistant machine contract Action human requests are invalid")
    if stored_inputs and "input:password" not in human_requests:
        raise ManifestError("Assistant machine contract Action Stored Input request is undeclared")
    # A file input is one required direct file-id property behind exactly one authorization request (ADR-0093).
    if input_file_validator.declaration_error(raw_action) is not None:
        raise ManifestError("Assistant machine contract Action file input is invalid")
    return {
        "id": action_id,
        "description": _display_text(
            raw_action["description"], kind="Action description", maximum=catalog_validator.LINE_CHARS
        ),
        "input_schema": _machine_schema(raw_action["input_schema"], kind="input"),
        "output_schema": _machine_schema(raw_action["output_schema"], kind="output"),
        "integrations": sorted(integrations),
        "stored_inputs": sorted(stored_inputs),
        "input_files": list(raw_action["input_files"]),
        "human_requests": sorted(human_requests),
        # The effect class and its optional declarations, closed by the effect validator; copied, never aliased.
        **{key: copy.deepcopy(raw_action[key]) for key in sorted(raw_action.keys() - _ACTION_FIELDS)},
    }


def parse_machine_contract(
    raw: bytes,
    declared_integrations: tuple[IntegrationDeclaration, ...],
    declared_stored_inputs: tuple[StoredInputDeclaration, ...] = (),
    *,
    summary: str,
    description: str,
    allowed_hosts: tuple[str, ...],
) -> dict[str, Any]:
    """Parse a bounded SDK artifact without executing Assistant code."""
    return canonical_machine_contract(
        _strict_json(raw, maximum=MAX_CONTRACT_BYTES, kind="machine contract"),
        declared_integrations,
        declared_stored_inputs,
        summary=summary,
        description=description,
        allowed_hosts=allowed_hosts,
    )


def validate_schema_payload(validator: Draft202012Validator, payload: object) -> dict[str, object]:
    """Validate one untrusted Action input or output against its reviewed schema."""
    if not isinstance(payload, dict):
        raise ValueError("Action payload must be an object")
    if not action_schema.json_depth_within(payload, action_schema.MAX_PAYLOAD_DEPTH):
        raise ValueError("Action payload nests too deeply")
    try:
        with action_schema.pattern_work_budget():
            validator.validate(payload)
    except (ValidationError, Unresolvable, RecursionError, action_schema.PatternError) as exc:
        raise ValueError("Action payload does not match its reviewed schema") from exc
    return payload


def _reject_credential_material(value: object) -> None:
    pending: list[tuple[object, tuple[str, ...], int]] = [(value, (), 0)]
    while pending:
        current, path, depth = pending.pop()
        if depth > 64:
            raise ManifestError("Assistant manifest exceeds the safe nesting limit")
        if isinstance(current, dict):
            for key, child in current.items():
                if not isinstance(key, str):
                    raise ManifestError("Assistant manifest contains an invalid key")
                lowered = key.lower()
                if path != ("stored_inputs",) and any(
                    marker in lowered
                    for marker in ("secret", "password", "token", "api_key", "private_key", "access_key", "env")
                ):
                    raise ManifestError("Assistant manifest contains a forbidden credential field")
                pending.append((child, (*path, key), depth + 1))
        elif isinstance(current, list):
            pending.extend((child, path, depth + 1) for child in current)
        elif isinstance(current, str) and resembles_credential(current):
            raise ManifestError("Assistant manifest contains credential material")


def _manifest_table(raw: bytes) -> dict[str, object]:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_MANIFEST_BYTES:
        raise ManifestError("Assistant manifest has an invalid size")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestError("Assistant manifest is not UTF-8") from exc
    if any(not character.isprintable() and character not in {"\n", "\r", "\t"} for character in text):
        raise ManifestError("Assistant manifest contains invalid text")
    try:
        manifest = tomllib.loads(text)
    except (RecursionError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError("Assistant manifest is invalid TOML") from exc
    required_root = {"shimpz", "network"}
    if not required_root <= set(manifest) or set(manifest) - (required_root | {"integrations", "stored_inputs"}):
        raise ManifestError("Assistant manifest contains an unsupported top-level field")
    metadata = manifest["shimpz"]
    network = manifest["network"]
    if not isinstance(metadata, dict) or not isinstance(network, dict):
        raise ManifestError("Assistant manifest contains an invalid section")
    required_metadata = {
        "spec",
        "id",
        "version",
        "name",
        "summary",
        "description",
        "creators",
        "github",
        "genesis",
    }
    if set(metadata) - {"links"} != required_metadata or set(network) != {"allowed_hosts"}:
        raise ManifestError("Assistant manifest contains an unsupported section field")
    _reject_credential_material(manifest)
    return manifest


def parse_manifest_contract(raw: bytes) -> ManifestContract:
    """Parse the bounded public security contract from one UTF-8 TOML manifest."""
    manifest = _manifest_table(raw)
    metadata = manifest["shimpz"]
    network = manifest["network"]
    if metadata["spec"] != 1:
        raise ManifestError("Assistant spec is unsupported")
    assistant_id = _identifier(metadata["id"], kind="id", canonical=http_payload.canonical_assistant_id)
    if assistant_id in {"postgres", "assistant-egress", "shimpz-assistant-egress"}:
        raise ManifestError("Assistant id is reserved")
    version = metadata["version"]
    if not isinstance(version, str) or VERSION_RE.fullmatch(version) is None:
        raise ManifestError("Assistant version is invalid")
    _public_text(metadata["name"], kind="name", maximum=80)
    _public_text(metadata["summary"], kind="summary", maximum=80)
    _manifest_presentation(metadata)
    _genesis(metadata["genesis"])
    canonical_manifest_creators(metadata["creators"])
    github = metadata["github"]
    if not isinstance(github, str) or _GITHUB_RE.fullmatch(github) is None:
        raise ManifestError("Assistant github repository is invalid")

    raw_integrations = manifest.get("integrations", {})
    if not isinstance(raw_integrations, dict):
        raise ManifestError("Assistant integration declarations are invalid")
    integration_declarations: dict[str, object] = {}
    for integration_id, metadata in raw_integrations.items():
        if not isinstance(metadata, dict) or set(metadata) != {"scopes"}:
            raise ManifestError("Assistant integration declaration is invalid")
        integration_declarations[integration_id] = metadata["scopes"]

    raw_stored_inputs = manifest.get("stored_inputs", {})
    if not isinstance(raw_stored_inputs, dict):
        raise ManifestError("Assistant Stored Input declarations are invalid")

    return canonical_manifest_contract(
        allowed_hosts=network["allowed_hosts"],
        integration_declarations=integration_declarations,
        stored_input_declarations=raw_stored_inputs,
    )


def parse_manifest_identity(raw: bytes) -> ManifestIdentity:
    """Parse identity only after the complete Spec v1 manifest passes admission."""
    parse_manifest_contract(raw)
    metadata = _manifest_table(raw)["shimpz"]
    return canonical_manifest_identity(
        assistant_id=metadata["id"],
        version=metadata["version"],
        name=metadata["name"],
        summary=metadata["summary"],
    )


def parse_manifest_presentation(raw: bytes) -> ManifestPresentation:
    """Parse the Creator-declared page copy only after complete manifest admission."""
    parse_manifest_contract(raw)
    return _manifest_presentation(_manifest_table(raw)["shimpz"])


def _manifest_presentation(metadata: dict[str, object]) -> ManifestPresentation:
    # A declared links table names at least one link; a record or resolution carries an absent table as empty.
    if "links" in metadata and metadata["links"] == {}:
        raise ManifestError("Assistant links are invalid")
    return canonical_manifest_presentation(description=metadata["description"], links=metadata.get("links", {}))


def canonical_manifest_presentation(*, description: object, links: object) -> ManifestPresentation:
    """Canonicalize the description and the zero to six Creator links a manifest, record, or resolution carries."""
    canonical_links = http_payload.canonical_creator_links(links)
    if canonical_links is None:
        raise ManifestError("Assistant links are invalid")
    return ManifestPresentation(description=_description(description), links=MappingProxyType(canonical_links))


def _description(value: object) -> str:
    return _display_text(value, kind="description", maximum=catalog_validator.DESCRIPTION_CHARS)


def parse_manifest_creators(raw: bytes) -> tuple[str, ...]:
    """Parse self-declared Creator handles after complete manifest admission."""
    parse_manifest_contract(raw)
    return canonical_manifest_creators(_manifest_table(raw)["shimpz"]["creators"])


def canonical_manifest_creators(value: object, *, maximum: int = 16) -> tuple[str, ...]:
    """Validate bounded manifest Creator handles without granting identity authority."""
    if (
        not isinstance(value, list | tuple)
        or not 1 <= len(value) <= maximum
        or any(not isinstance(creator, str) or _CREATOR_RE.fullmatch(creator) is None for creator in value)
        or len(value) != len(set(value))
    ):
        raise ManifestError("Assistant creators are invalid")
    return tuple(value)


def canonical_manifest_identity(
    *,
    assistant_id: object,
    version: object,
    name: object,
    summary: object,
) -> ManifestIdentity:
    """Canonicalize persisted identity fields without accepting publication attribution."""
    if not isinstance(version, str) or VERSION_RE.fullmatch(version) is None:
        raise ManifestError("Assistant version is invalid")
    return ManifestIdentity(
        assistant_id=_identifier(assistant_id, kind="id", canonical=http_payload.canonical_assistant_id),
        version=version,
        name=_public_text(name, kind="name", maximum=80),
        summary=_public_text(summary, kind="summary", maximum=80),
    )


def parse_manifest_genesis(raw: bytes) -> str:
    """Read the canonical model guidance from one complete Spec v1 manifest."""
    manifest = _manifest_table(raw)
    metadata = manifest["shimpz"]
    return _genesis(metadata["genesis"]).strip()


def _bounded_archive(chunks: Iterable[bytes], maximum: int = MAX_ARCHIVE_BYTES) -> bytes:
    archive = bytearray()
    try:
        with ExitStack() as cleanup:
            close = getattr(chunks, "close", None)
            if callable(close):
                cleanup.callback(close)
            for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise ManifestError("Assistant manifest archive is invalid")
                archive.extend(chunk)
                if len(archive) > maximum:
                    raise ManifestError("Assistant package archive is too large")
    except ManifestError:
        raise
    except Exception as exc:
        raise ManifestUnavailableError("Assistant manifest archive is unavailable") from exc
    return bytes(archive)


def read_container_file(container, *, path: str, name: str, maximum: int) -> bytes:
    """Read one fixed immutable regular file from a digest-bound root."""
    try:
        chunks, metadata = container.get_archive(path)
    except Exception as exc:
        raise ManifestUnavailableError("Assistant package file is unavailable") from exc
    if not isinstance(metadata, dict):
        raise ManifestError("Assistant package metadata is invalid")
    size = metadata.get("size")
    mode = metadata.get("mode")
    metadata_name = metadata.get("name")
    if (
        metadata_name != name
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not 1 <= size <= maximum
        or not isinstance(mode, int)
        or isinstance(mode, bool)
        or mode != 0o444
    ):
        raise ManifestError("Assistant package metadata is invalid")

    archive = _bounded_archive(chunks, maximum + (32 * 1024))
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            members = bundle.getmembers()
            if (
                len(members) != 1
                or members[0].name not in {name, f"./{name}"}
                or not members[0].isreg()
                or members[0].size != size
                or members[0].mode & 0o777 != 0o444
            ):
                raise ManifestError("Assistant package archive is invalid")
            extracted = bundle.extractfile(members[0])
            if extracted is None:
                raise ManifestError("Assistant package archive is invalid")
            raw = extracted.read(maximum + 1)
    except ManifestError:
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ManifestError("Assistant package archive is invalid") from exc
    if len(raw) != size:
        raise ManifestError("Assistant package archive is invalid")
    return raw


def _read_container_manifest_bytes(container) -> bytes:
    return read_container_file(
        container,
        path=MANIFEST_PATH,
        name="shimpz.toml",
        maximum=MAX_MANIFEST_BYTES,
    )


def read_container_manifest_contract(container) -> ManifestContract:
    """Read the fixed regular manifest contract from a digest-bound root."""
    return parse_manifest_contract(_read_container_manifest_bytes(container))


def read_container_manifest_genesis(container) -> str:
    """Read model guidance from the same fixed manifest admitted for security intent."""
    return parse_manifest_genesis(_read_container_manifest_bytes(container))


def read_container_machine_contract(
    container,
    declared_integrations: tuple[IntegrationDeclaration, ...],
    declared_stored_inputs: tuple[StoredInputDeclaration, ...] = (),
    *,
    summary: str,
    description: str,
    allowed_hosts: tuple[str, ...],
) -> dict[str, Any]:
    """Read and validate the fixed SDK contract artifact from an immutable image."""
    raw = read_container_file(
        container,
        path=CONTRACT_PATH,
        name="shimpz.contract.json",
        maximum=MAX_CONTRACT_BYTES,
    )
    return parse_machine_contract(
        raw,
        declared_integrations,
        declared_stored_inputs,
        summary=summary,
        description=description,
        allowed_hosts=allowed_hosts,
    )


class ManifestContractCache:
    """Admit reviewed security intent once per immutable container generation."""

    def __init__(self, max_entries: int = DEFAULT_CACHE_ENTRIES) -> None:
        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries < 1:
            raise ValueError("Assistant manifest cache size must be positive")
        self._cache: assistant_cache.ContainerReadCache[ManifestContract] = assistant_cache.ContainerReadCache(
            max_entries
        )

    def get(self, container, reviewed: object) -> ManifestContract:
        """Return declared intent only when it exactly matches controller review."""
        container_id = assistant_cache.container_id(container)
        if container_id is None:
            raise ManifestError("Assistant container identity is invalid")
        if not isinstance(reviewed, ManifestContract):
            raise ManifestError("Assistant reviewed manifest contract is invalid")
        declared = self._cache.get(container_id, lambda: read_container_manifest_contract(container))
        if declared != reviewed:
            raise ManifestError("Assistant manifest does not match its reviewed contract")
        return declared

    def discard(self, container_id: object) -> None:
        if isinstance(container_id, str):
            self._cache.discard(container_id)


class MachineContractCache:
    """Admit the image-baked SDK artifact only when it equals controller review."""

    def __init__(self, max_entries: int = DEFAULT_CACHE_ENTRIES) -> None:
        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries < 1:
            raise ValueError("Assistant machine contract cache size must be positive")
        self._cache: assistant_cache.ContainerReadCache[dict[str, Any]] = assistant_cache.ContainerReadCache(
            max_entries
        )

    def get(
        self,
        container,
        declared_integrations: tuple[IntegrationDeclaration, ...],
        declared_stored_inputs: tuple[StoredInputDeclaration, ...],
        reviewed: object,
        *,
        summary: str,
        description: str,
        allowed_hosts: tuple[str, ...],
    ) -> dict[str, Any]:
        """Return the machine contract only after exact semantic equality."""
        container_id = assistant_cache.container_id(container)
        if container_id is None:
            raise ManifestError("Assistant container identity is invalid")
        declared = self._cache.get(
            container_id,
            lambda: read_container_machine_contract(
                container,
                declared_integrations,
                declared_stored_inputs,
                summary=summary,
                description=description,
                allowed_hosts=allowed_hosts,
            ),
        )
        if declared != reviewed:
            raise ManifestError("Assistant machine contract does not match controller review")
        return declared

    def discard(self, container_id: object) -> None:
        if isinstance(container_id, str):
            self._cache.discard(container_id)
