"""Shared reviewed Assistant registry primitives for both Controllers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from jsonschema import Draft202012Validator

from assistant_human import assistant_manifest

ALL_ZERO_SHA256 = "0" * 64
@dataclass(frozen=True, slots=True)
class PowerSpec:
    summary: str
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    accounts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AccountSpec:
    provider: str
    scopes: tuple[str, ...]


def power_summary(power_id: str) -> str:
    return power_id.replace("-", " ").capitalize()


def validate_power_payload(
    power: PowerSpec,
    direction: str,
    payload: object,
) -> dict[str, object]:
    if direction == "input":
        schema = power.input_schema
    elif direction == "output":
        schema = power.output_schema
    else:
        raise ValueError("unknown Power payload direction")
    return assistant_manifest.validate_schema_payload(
        Draft202012Validator(dict(schema)),
        payload,
    )


def digest_is_bound(ref: object) -> bool:
    return isinstance(ref, str) and not ref.endswith(f"sha256:{ALL_ZERO_SHA256}")
