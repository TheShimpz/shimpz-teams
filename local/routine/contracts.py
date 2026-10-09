"""The Team's current Assistant contracts as Routines pin and admit them (ADR-0092 section 4, ADR-0101).

Recording, confirmation, every run, and every recovery read the same thing: each running Assistant's Actions at their
complete pin, with the reviewed input and output schemas, file inputs, whether the reviewed effect proves the Action
read-only, and the Stored Inputs it uses by name. Pins are computed in one fixed locale; each still covers the whole
language pack.
"""

from http import HTTPStatus

from assistant import effect as action_effect
from install import bindings
from local.chat import segment as local_chat_segment
from local.errors import ApiProblemError as ApiProblem
from routine import pin as routine_pin
from routine import plan as routine_plan


def contracts(assistants: tuple[object, ...]) -> dict[tuple[str, str], routine_plan.ActionContract]:
    """Each Action of these Assistants with its complete current pin, its reviewed schemas, and its effect."""
    admitted: dict[tuple[str, str], routine_plan.ActionContract] = {}
    for active in assistants:
        pins = routine_pin.action_pins(active.spec, active.spec.actions, routine_pin.SCOPE_LOCALE)
        admitted.update(
            {
                (active.spec.assistant_id, action_id): routine_plan.ActionContract(
                    pins[action_id],
                    action.input_schema,
                    action.input_files,
                    action.output_schema,
                    read_only=action.effect == action_effect.READ_ONLY,
                    stored_inputs=tuple(sorted(action.stored_inputs)),
                )
                for action_id, action in active.spec.actions.items()
            }
        )
    return admitted


class ContractsUnavailableError(Exception):
    """The Team's current Assistants could not be read, so nothing about a Routine's scope is proven either way."""


def context_unavailable() -> ApiProblem:
    """The retryable problem for a Routine step whose Team could not be read; nothing it held was changed."""
    return ApiProblem(
        HTTPStatus.SERVICE_UNAVAILABLE, "Team capabilities could not be checked; retry", code="team-context-unavailable"
    )


def current_contracts(self, team_id: str, assistant_ids: tuple[str, ...]) -> dict[str, str]:
    """Each named Assistant's current Routine scope pin, exactly as a recording turn computes it.

    An Assistant the Team no longer runs has no entry, which proves the scope changed. When the Team or an Assistant's
    contract cannot be read, ContractsUnavailableError is raised instead, so a transient failure never reads as a
    changed scope.
    """
    try:
        _name, _network, active_by_id = self._team_assistants(team_id)
        return {
            assistant_id: local_chat_segment.routine_scope(active, self._active_assistant_genesis(active))
            for assistant_id in assistant_ids
            if (active := active_by_id.get(assistant_id)) is not None
        }
    except (ApiProblem, bindings.DynamicAssistantError) as exc:
        raise ContractsUnavailableError from exc
