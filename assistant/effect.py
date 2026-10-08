"""Team admission of Action effect classes, verifiers, and provider idempotency (ADR-0092).

The mirrored Developers reference validator fixes the closed shapes. Team then enforces, independently, what only the
admitted manifest and the verifier's own declaration can prove: an idempotency provider is one of the Assistant's
exact outbound hosts, and a verifier runs without a person, asking at most for its own declared Stored Input.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from protocol.assistant.v1.validators import action_effect as action_effect_validator

EFFECTS = action_effect_validator.EFFECTS
READ_ONLY = "read_only"
MUTATING = "mutating"


def refusal(actions: Sequence[Mapping[str, object]], allowed_hosts: tuple[str, ...]) -> str | None:
    """A stable reason when the Actions' effect, verifier, or idempotency declarations are refused, else None."""
    error = action_effect_validator.effect_error(list(actions))
    if error is not None:
        return error
    by_id = {action["id"]: action for action in actions}
    for action in actions:
        idempotency = action.get("idempotency")
        if isinstance(idempotency, Mapping) and idempotency["provider"] not in allowed_hosts:
            return "idempotency_provider_undeclared"
        verifier = action.get("verifier")
        if isinstance(verifier, Mapping) and not unattended(by_id[verifier["action"]]):
            return "verifier_interactive"
    return None


def unattended(verifier: Mapping[str, object]) -> bool:
    """A verifier declares no human request, or only a password request with at least one declared Stored Input."""
    requests = list(verifier["human_requests"])
    stored_inputs = list(verifier["stored_inputs"])
    return requests == [] or (requests == ["input:password"] and bool(stored_inputs))


def verifier_request_admitted(verifier: object, kind: object, stored_input: object) -> bool:
    """Whether a verifier's runtime request is the one Team may satisfy without a person.

    Only a password request naming one of the verifier's own declared Stored Inputs qualifies; any other request,
    including an ordinary password request that names none, would need a person and is refused as a policy hold.
    """
    stored_inputs = tuple(getattr(verifier, "stored_inputs", ()))
    return (
        tuple(getattr(verifier, "human_requests", ())) == ("input:password",)
        and kind == "input:password"
        and isinstance(stored_input, str)
        and stored_input in stored_inputs
    )
