"""A Team's private memory entries: each memory with its provenance (ADR-0084, ADR-0121).

Team keeps, beside each memory's topic and preference, an opaque id, the UTC time it committed the memory, and its
source: the signed request nonce of the turn that taught it, the verified person who sent that message, and the Team
incarnation. Brain never sees any of it; the turn context carries only topic and preference.
"""

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import sign_in as sign_in_protocol

# The activity operations of a memory change (ADR-0121), each recorded under the ADR-0120 contract.
REMEMBER, FORGET, EVICTED = "memory-remember", "memory-forget", "memory-evicted"
ENTRY_FIELDS = frozenset({"memory_id", "topic", "preference", "since", "source"})
SOURCE_FIELDS = frozenset({"nonce", "actor_id", "incarnation"})
_OPAQUE_ID = re.compile(r"[0-9a-f]{32}\Z")
_SINCE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")


@dataclass(frozen=True, slots=True)
class Source:
    """Who taught a memory: the turn's signed request nonce, its verified author, and the Team incarnation.

    Team captures it when a turn starts and carries it unchanged through every continuation, so a person who only
    resumes a turn never becomes its author.
    """

    nonce: str
    actor_id: str
    incarnation: str


@dataclass(frozen=True, slots=True)
class Change:
    """One memory change's activity operation and the memory it concerns, never its topic or preference."""

    operation: str
    memory_id: str


@dataclass(frozen=True, slots=True)
class Plan:
    """The private entries one turn's changes leave, oldest first, and the change each one made."""

    memory: tuple[dict[str, object], ...]
    changes: tuple[Change, ...]


def canonical_source(value: object) -> Source | None:
    """One exact source, from its stored form or as captured, or None."""
    if isinstance(value, Source):
        value = source_record(value)
    if not isinstance(value, dict) or set(value) != SOURCE_FIELDS:
        return None
    nonce, actor_id, incarnation = value["nonce"], value["actor_id"], value["incarnation"]
    if (
        not isinstance(nonce, str)
        or http_payload.REQUEST_NONCE_RE.fullmatch(nonce) is None
        or not isinstance(actor_id, str)
        or _OPAQUE_ID.fullmatch(actor_id) is None
        or not isinstance(incarnation, str)
        or sign_in_protocol.INCARNATION_RE.fullmatch(incarnation) is None
    ):
        return None
    return Source(nonce, actor_id, incarnation)


def source_record(source: Source) -> dict[str, str]:
    return {"nonce": source.nonce, "actor_id": source.actor_id, "incarnation": source.incarnation}


def canonical_entries(value: object) -> list[dict[str, object]] | None:
    """The exact private entries of a knowledge record, or None: ADR-0084's memory, each with distinct provenance."""
    if not isinstance(value, list) or not all(
        isinstance(entry, dict)
        and set(entry) == ENTRY_FIELDS
        and isinstance(entry["memory_id"], str)
        and _OPAQUE_ID.fullmatch(entry["memory_id"]) is not None
        and isinstance(entry["since"], str)
        and _SINCE.fullmatch(entry["since"]) is not None
        and canonical_source(entry["source"]) is not None
        for entry in value
    ):
        return None
    if http_payload.canonical_memory(projection(value)) is None:
        return None
    return value if len({entry["memory_id"] for entry in value}) == len(value) else None


def projection(entries: list[dict[str, object]]) -> list[dict[str, str]]:
    """The memory as a turn carries it to the Brain: topic and preference only, never provenance."""
    return [{"topic": entry["topic"], "preference": entry["preference"]} for entry in entries]


def plan(
    entries: list[dict[str, object]],
    changes: list[dict[str, str]],
    source: Source,
    since: str,
    new_id: Callable[[], str] = lambda: secrets.token_hex(16),
) -> Plan:
    """Apply canonical changes as ``apply_memory_changes`` does, stamping each remembered entry and naming each change.

    A `remember` gives its entry a new id, time, and source, replacing any entry of its topic; a `forget` of a present
    topic removes it, one of an absent topic changes nothing; the oldest entries beyond the bound are evicted.
    """
    kept = {entry["topic"]: entry for entry in entries}
    made: list[Change] = []
    for change in changes:
        removed = kept.pop(change["topic"], None)
        if change["op"] == "remember":
            entry = {
                "memory_id": new_id(),
                "topic": change["topic"],
                "preference": change["preference"],
                "since": since,
                "source": source_record(source),
            }
            kept[change["topic"]] = entry
            made.append(Change(REMEMBER, entry["memory_id"]))
        elif removed is not None:
            made.append(Change(FORGET, removed["memory_id"]))
    ordered = list(kept.values())
    made.extend(Change(EVICTED, entry["memory_id"]) for entry in ordered[: -http_payload.MAX_MEMORIES])
    return Plan(tuple(ordered[-http_payload.MAX_MEMORIES :]), tuple(made))
