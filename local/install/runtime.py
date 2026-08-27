"""Local runtime shape derived only from a verified Assistant publication."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from assistant.spec import ActionSpec, IntegrationSpec, StoredInputSpec, digest_is_bound

_DIGEST_REF = re.compile(
    r"(?:[a-z0-9.-]+(?::[0-9]{1,5})?/)?"
    r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}"
)


@dataclass(frozen=True, slots=True)
class AssistantSpec:
    assistant_id: str
    version: str
    name: str
    summary: str
    image: str
    actions: dict[str, ActionSpec]
    allowed_hosts: tuple[str, ...]
    required_image_labels: tuple[tuple[str, str], ...]
    integrations: dict[str, IntegrationSpec] = field(default_factory=dict)
    stored_inputs: dict[str, StoredInputSpec] = field(default_factory=dict)
    machine_contract: dict[str, object] = field(default_factory=dict)


def is_digest_ref(value: object) -> bool:
    return isinstance(value, str) and _DIGEST_REF.fullmatch(value) is not None and digest_is_bound(value)
