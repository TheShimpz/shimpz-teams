"""The evidence of the request that granted a Routine revision, and its plan's safe projection (ADR-0092).

Each committed revision keeps minimal Team-owned evidence of what authorized it, bound to the receipt of the request
that made it, the revision it defines, and its plan digest: a commitment to the user's message, where in that message
the user's own words state the request, the option a bound question's answer selected, each step input's validated
provenance, and the Stored Inputs each step's Action uses, by name only. It holds no secret: a literal never holds one,
and a Stored Input appears only as its declared id. A Supervisor inspects a Routine through ``steps``, a projection of
its plan that shows each literal as a bounded preview and every reference by its step and pointer.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping

from protocol.http.v1 import routine as http_routine
from routine import plan as routine_plan

FIELDS = frozenset({"receipt", "revision", "plan", "message", "quote", "selected", "sources", "stored_inputs"})
_PARTIAL = FIELDS - {"receipt", "revision", "plan"}
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_ORIGIN_FIELDS = frozenset({"at", "from", "text", "region", "instruction"})
MAX_ORIGINS = 64
MAX_MESSAGE_CHARS = 16_000


def plan_digest(plan: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(routine_plan.canonical(plan)).hexdigest()


def evidence(
    message: str,
    quote: tuple[int, int],
    sources: Mapping[str, Mapping[str, Mapping[str, object]]],
    stored_inputs: Mapping[str, list[str]],
) -> dict[str, object]:
    """A revision's evidence before it commits; the commit binds its receipt, revision, and plan digest."""
    return {
        "message": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "quote": list(quote),
        "selected": None,
        "sources": copy.deepcopy(dict(sources)),
        "stored_inputs": {step: sorted(names) for step, names in stored_inputs.items()},
    }


def complete(partial: object, receipt: str, revision: int, plan: Mapping[str, object]) -> dict[str, object]:
    """Bind a revision's evidence to the request receipt, the revision, and the exact plan it authorizes."""
    if not isinstance(partial, dict) or not set(partial) >= _PARTIAL:
        return {}
    return {
        **copy.deepcopy({key: partial[key] for key in _PARTIAL}),
        "receipt": receipt,
        "revision": revision,
        "plan": plan_digest(plan),
    }


def _provenance(value: object) -> bool:
    if value == {}:
        return True
    if not isinstance(value, dict) or len(value) != 1:
        return False
    if "instruction" in value:
        return isinstance(value["instruction"], str) and bool(value["instruction"])
    origins = value.get("origins")
    return (
        isinstance(origins, list)
        and 0 < len(origins) <= MAX_ORIGINS
        and all(isinstance(item, dict) and set(item) == _ORIGIN_FIELDS for item in origins)
    )


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
        and isinstance(quote, list)
        and len(quote) == 2
        and all(type(item) is int for item in quote)
        and 0 <= quote[0] < quote[1] <= MAX_MESSAGE_CHARS
        and _selected(value["selected"])
        and isinstance(sources, dict)
        and set(sources) == set(steps)
        and all(
            isinstance(sources[step], dict)
            and set(sources[step]) == set(steps[step]["input"])
            and all(_provenance(item) for item in sources[step].values())
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
