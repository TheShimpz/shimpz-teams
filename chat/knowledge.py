"""A Team's learned skills (ADR-0085): recipes derived from the Actions a completed turn actually ran.

A skill is structure only, the ordered Assistant Actions and their input names bound to each Assistant's contract
fingerprint, so no argument value, Action result, page, or other text ever becomes part of it.
"""

from chat import orchestrator as chat_orchestrator
from inference import client as brain_runtime_client
from protocol.http.v1 import payload as http_payload


def learned_skill(actions: tuple[chat_orchestrator.InvokedAction, ...]) -> dict[str, object] | None:
    """The skill a completed turn's succeeded Actions form, or None for fewer than two steps or too many."""
    if not http_payload.MIN_SKILL_STEPS <= len(actions) <= http_payload.MAX_SKILL_STEPS or not all(
        action.learnable for action in actions
    ):
        return None
    contracts: dict[str, str] = {}
    for action in actions:
        if contracts.setdefault(action.assistant_id, action.contract) != action.contract:
            return None
    ordered = dict(sorted(contracts.items()))
    steps = [
        {"assistant_id": action.assistant_id, "action": action.action, "inputs": list(action.inputs)}
        for action in actions
    ]
    return http_payload.canonical_skill(
        {"key": http_payload.skill_key(ordered, steps), "contracts": ordered, "steps": steps}
    )


def turn_skills(
    skills: list[dict[str, object]], assistants: tuple[brain_runtime_client.RuntimeAssistant, ...]
) -> tuple[dict[str, object], ...]:
    """Every stored skill for Brain, so each can be named and forgotten.

    A skill is marked usable only when its every Assistant is in this turn with the exact contract it learned.
    """
    current = {assistant.id: brain_runtime_client.contract_digest(assistant) for assistant in assistants}
    return tuple(
        {
            **skill,
            "usable": all(current.get(assistant_id) == digest for assistant_id, digest in skill["contracts"].items()),
        }
        for skill in skills
    )
