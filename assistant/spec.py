"""Shared reviewed Assistant contract primitives for both Controllers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from assistant import manifest as assistant_manifest

ALL_ZERO_SHA256 = "0" * 64
ASSISTANT_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
DIGEST_IMAGE_RE = re.compile(r"^[a-z0-9.-]+(?::[0-9]{1,5})?/[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$")


class AssistantSpecError(RuntimeError):
    """A published Assistant contract is malformed or unavailable."""


@dataclass(frozen=True, slots=True)
class ActionSpec:
    summary: str
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    integrations: tuple[str, ...] = ()
    human_requests: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IntegrationSpec:
    provider: str
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AssistantContract:
    name: str
    actions: dict[str, ActionSpec]
    integrations: dict[str, IntegrationSpec] = field(default_factory=dict)
    machine_contract: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AssistantSpec:
    version: str
    summary: str
    image: str
    allowed_hosts: tuple[str, ...]
    archs: tuple[str, ...]
    required_image_labels: tuple[tuple[str, str], ...]
    contract: AssistantContract


def validate_assistant_id(value: object) -> str:
    if not isinstance(value, str) or len(value) > 40 or ASSISTANT_ID_RE.fullmatch(value) is None:
        raise AssistantSpecError("the Assistant id is invalid")
    return value


def action_summary(action_id: str) -> str:
    return action_id.replace("-", " ").capitalize()


def validate_action_payload(
    action: ActionSpec,
    direction: str,
    payload: object,
) -> dict[str, object]:
    if direction == "input":
        schema = action.input_schema
    elif direction == "output":
        schema = action.output_schema
    else:
        raise ValueError("unknown Action payload direction")
    return assistant_manifest.validate_schema_payload(
        Draft202012Validator(dict(schema)),
        payload,
    )


def digest_is_bound(ref: object) -> bool:
    return isinstance(ref, str) and not ref.endswith(f"sha256:{ALL_ZERO_SHA256}")


def is_digest_image(image: object) -> bool:
    """Accept only a complete, non-placeholder registry digest reference."""
    return isinstance(image, str) and DIGEST_IMAGE_RE.fullmatch(image) is not None and digest_is_bound(image)
