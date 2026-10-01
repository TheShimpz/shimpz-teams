"""What a Local chat turn knows about the Team's Routines, and what it does with a proposed change (ADR-0086)."""

from __future__ import annotations

import time
from http import HTTPStatus

from inference import client as brain_runtime_client
from install import bindings
from local.chat import segment as local_chat_segment
from local.errors import ApiProblemError as ApiProblem
from local.routine import proposal as proposal_book
from local.routine import store as routine_store


def chat_routines(self, team_id: str) -> tuple[dict[str, object], ...]:
    """The Team's Routines as data for the Brain, so the user can name one to cancel; a deleting one is gone."""
    try:
        state = self.routine_store.load(team_id)
    except routine_store.RoutineStoreError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE, "Team Routine state is unavailable", code="routine-state-unavailable"
        ) from exc
    return tuple(
        {"routine_id": item.routine_id, "quote": item.quote, "schedule": dict(item.schedule), "timezone": item.timezone}
        for item in state.routines
        if not item.deleting
    )


def routine_proposal(self, response: object, change: dict[str, object] | None) -> dict[str, object] | None:
    """Bind a chat turn's confirmed Routine change to the exact contracts its Brain saw, as a one-use proposal.

    It is inserted under the Team's lifecycle lock and only while the Team is still exactly as the turn saw it, so
    neither a changed Assistant nor a concurrent destroy or reset can cross that step.
    """
    if change is None:
        return None
    team_id, segment = response.team_id, response.segment
    with self._lock(team_id):
        current = self._chat_setup(team_id, list(response.file_ids), response.provider, response.assistant_ids)
        if self._chat_identity(*current) != segment.identity:
            raise ApiProblem(HTTPStatus.CONFLICT, "Team capabilities changed; retry", code="team-context-changed")
        try:
            proposal = self.routine_proposals.create(team_id, change, dict(segment.contracts))
        except proposal_book.ProposalError as exc:
            raise ApiProblem(
                HTTPStatus.CONFLICT, "Routine proposals are unavailable", code="routine-proposal-unavailable"
            ) from exc
    return proposal.view(time.time())


def withdraw_routine_proposal(self, team_id: str, proposal: dict[str, object] | None) -> None:
    """Stop won the turn's commit, so its offer disappears with its reply."""
    if proposal is not None:
        self.routine_proposals.drop(team_id, proposal["proposal_id"])


class ContractsUnavailableError(Exception):
    """The Team's current Assistants could not be read, so nothing about a Routine's scope is proven either way."""


def context_unavailable() -> ApiProblem:
    """The retryable problem for a Routine step whose Team could not be read; nothing it held was changed."""
    return ApiProblem(
        HTTPStatus.SERVICE_UNAVAILABLE, "Team capabilities could not be checked; retry", code="team-context-unavailable"
    )


def current_contracts(self, team_id: str, assistant_ids: tuple[str, ...]) -> dict[str, str]:
    """Each named Assistant's current contract digest, exactly as a Brain turn sees it.

    An Assistant the Team no longer runs has no entry, which proves the scope changed. When the Team or an Assistant's
    contract cannot be read, ContractsUnavailableError is raised instead, so a transient failure never reads as a
    changed scope.
    """
    try:
        _name, _network, active_by_id = self._team_assistants(team_id)
        return {
            assistant_id: brain_runtime_client.contract_digest(
                local_chat_segment.runtime_assistant(active, self._active_assistant_genesis(active))
            )
            for assistant_id in assistant_ids
            if (active := active_by_id.get(assistant_id)) is not None
        }
    except (ApiProblem, bindings.DynamicAssistantError) as exc:
        raise ContractsUnavailableError from exc
