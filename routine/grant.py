"""The evidence of the request that granted a Routine revision, and its plan's safe projection (ADR-0092).

Each committed revision keeps minimal Team-owned evidence of what authorized it, bound to the receipt of the request
that made it, the revision it defines, and its plan digest: a commitment to the user's message and the earlier sends of
theirs it cites, where in that message the user's own words state the request, the option a bound question's answer
selected, each step input's validated provenance with the message, receipt, revision, and selected answer that first
granted it, and the Stored Inputs each step's Action uses, by name only. It holds no secret: a literal never holds one,
and a Stored Input appears only as its declared id. Provenance is kept only as spans of the committed message, never as
the cited prose, so words around a value never persist. A Supervisor inspects a Routine through ``steps``, a projection
of its plan that shows each literal as a bounded preview and every reference by its step and pointer.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping

from protocol.http.v1 import routine as http_routine
from routine import plan as routine_plan
from routine import request as routine_request

FIELDS = frozenset({"receipt", "revision", "plan", "message", "quote", "selected", "sources", "stored_inputs"})
_PARTIAL = FIELDS - {"receipt", "revision", "plan"}
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_ORIGINS = 64
# Spans index the Routine's words: the message and the earlier sends it cites, joined (ADR-0092, 2026-10-04).
MAX_MESSAGE_CHARS = routine_request.MAX_SOURCE_CHARS


def plan_digest(plan: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(routine_plan.canonical(plan)).hexdigest()


def evidence(
    commitment: str,
    quote: tuple[int, int],
    sources: Mapping[str, Mapping[str, Mapping[str, object]]],
    stored_inputs: Mapping[str, list[str]],
) -> dict[str, object]:
    """A revision's evidence before it commits; the commit binds its receipt, revision, and plan digest.

    ``commitment`` commits to the Routine's words: the message and the earlier sends it cites (``Request.commitment``).
    """
    return {
        "message": commitment,
        "quote": list(quote),
        "selected": None,
        "sources": copy.deepcopy(dict(sources)),
        "stored_inputs": {step: sorted(names) for step, names in stored_inputs.items()},
    }


def complete(partial: object, receipt: str, revision: int, plan: Mapping[str, object]) -> dict[str, object]:
    """Bind a revision's evidence to the request receipt, the revision, and the exact plan it authorizes.

    Each input this revision proved is bound to its message, receipt, revision, and the answer a bound question
    selected for it; an input kept from an earlier revision keeps what first granted it.
    """
    if not isinstance(partial, dict) or not set(partial) >= _PARTIAL or not isinstance(partial["sources"], dict):
        return {}
    value = copy.deepcopy({key: partial[key] for key in _PARTIAL})
    selected = value["selected"] or {}
    for step, members in value["sources"].items():
        for member, entry in members.items() if isinstance(members, dict) else ():
            if isinstance(entry, dict) and entry.get("by", False) is None:
                label = selected.get("label") if selected.get("field") == ["input", step, member] else None
                entry["by"] = {"message": value["message"], "receipt": receipt, "revision": revision, "selected": label}
    return {**value, "receipt": receipt, "revision": revision, "plan": plan_digest(plan)}


def _span(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(type(item) is int for item in value)
        and 0 <= value[0] < value[1] <= MAX_MESSAGE_CHARS
    )


def _origin(value: object) -> bool:
    """One origin as spans of its message: cited words, an adopted quote and its adopting words, or no text."""
    if not isinstance(value, dict) or not isinstance(value.get("at"), str):
        return False
    kind = value.get("from")
    if kind == "message":
        return set(value) == {"at", "from", "span"} and _span(value["span"])
    if kind == "quote":
        return (
            set(value) == {"at", "from", "region", "span", "instruction"}
            and type(value["region"]) is int
            and value["region"] >= 0
            and _span(value["span"])
            and _span(value["instruction"])
        )
    return set(value) == {"at", "from"} and kind in ("default", "answer")


def _granted(value: object, revision: int, message: str, receipt: str) -> bool:
    """One input's provenance and what granted it: its message, receipt, revision, and any selected answer."""
    if not isinstance(value, dict) or set(value) != {"proof", "by"} or not _provenance(value["proof"]):
        return False
    by = value["by"]
    if not isinstance(by, dict) or set(by) != {"message", "receipt", "revision", "selected"}:
        return False
    current = by["revision"] == revision
    return (
        isinstance(by["message"], str)
        and _HEX64_RE.fullmatch(by["message"]) is not None
        and isinstance(by["receipt"], str)
        and _HEX64_RE.fullmatch(by["receipt"]) is not None
        and type(by["revision"]) is int
        and 1 <= by["revision"] <= revision
        and (not current or (by["message"], by["receipt"]) == (message, receipt))
        and (by["selected"] is None or http_routine.canonical_name(by["selected"]) == by["selected"])
    )


def _provenance(value: object) -> bool:
    if value == {}:
        return True
    if not isinstance(value, dict) or len(value) != 1:
        return False
    if "instruction" in value:
        return _span(value["instruction"])
    origins = value.get("origins")
    return isinstance(origins, list) and 0 < len(origins) <= MAX_ORIGINS and all(map(_origin, origins))


def _selected(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {"field", "label"}:
        return False
    field = value["field"]
    return (
        isinstance(field, list)
        and 0 < len(field) <= 3
        and all(isinstance(item, str) and item for item in field)
        and http_routine.canonical_name(value["label"]) == value["label"]
    )


def _stored(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= http_routine.MAX_STEP_STORED_INPUTS
        and all(isinstance(item, str) and http_routine.ASSISTANT_ID_RE.fullmatch(item) for item in value)
        and value == sorted(set(value))
    )


def valid(value: object, plan: Mapping[str, object], revision: int) -> bool:
    """Whether the evidence is complete and bound to exactly this revision and plan."""
    if not isinstance(value, dict) or set(value) != FIELDS:
        return False
    steps = {step["id"]: step for step in plan["steps"]}
    quote, sources, stored = value["quote"], value["sources"], value["stored_inputs"]
    return (
        isinstance(value["receipt"], str)
        and _HEX64_RE.fullmatch(value["receipt"]) is not None
        and value["revision"] == revision
        and value["plan"] == plan_digest(plan)
        and isinstance(value["message"], str)
        and _HEX64_RE.fullmatch(value["message"]) is not None
        and _span(quote)
        and _selected(value["selected"])
        and isinstance(sources, dict)
        and set(sources) == set(steps)
        and all(
            isinstance(sources[step], dict)
            and set(sources[step]) == set(steps[step]["input"])
            and all(_granted(item, revision, value["message"], value["receipt"]) for item in sources[step].values())
            for step in steps
        )
        and isinstance(stored, dict)
        and set(stored) == set(steps)
        and all(_stored(item) for item in stored.values())
    )


def _input(member: str, source: Mapping[str, object]) -> dict[str, object]:
    if source["kind"] == "literal":
        return {"member": member, "source": "literal", "value": http_routine.literal_preview(source["value"])}
    if source["kind"] == "run_clock":
        return {"member": member, "source": "run_clock", "value": source["format"]}
    return {"member": member, "source": "step_output", "step": source["step"], "pointer": source["pointer"]}


def steps(plan: Mapping[str, object], value: Mapping[str, object]) -> list[dict[str, object]]:
    """The plan's safe projection: each step's Action, every input's source, and its Stored Inputs by name only."""
    return [
        {
            "id": step["id"],
            "assistant": step["assistant"],
            "action": step["action"],
            "inputs": [_input(member, step["input"][member]) for member in sorted(step["input"])],
            "stored_inputs": list(value["stored_inputs"][step["id"]]),
        }
        for step in plan["steps"]
    ]
