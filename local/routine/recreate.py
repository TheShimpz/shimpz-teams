"""Recriar: a held Routine recompiled from scratch from its exact creation message, in place (ADR-0092, 2026-10-02).

The authenticated person's Recriar on a card bound to the Routine's sealed creation source adopts that exact message,
and the field value they once selected, as their request again; nothing the compiler chose ever becomes one. Brain
compiles it once, as a chat create would, under the Team's current contracts and with no Routine to keep members from.
Team admits the change against the message exactly as a chat create, retargets it to the current Routine, and commits
it as the Routine's next revision in the same write that sets the held run aside and lifts the pause, under the Team
lifecycle lock and the Stop guard. The compile is registered under the incident, so Stop, deletion, and its deadline
reach it; anything refused, asked, or stopped changes nothing.
"""

from __future__ import annotations

import dataclasses
import time
from http import HTTPStatus

from inference import client as brain_runtime_client
from inference import config as inference_config
from inference import recreate as inference_recreate
from local.chat import segment as local_chat_segment
from local.errors import ApiProblemError as ApiProblem
from local.routine import incident as routine_incident
from local.routine import run as routine_run
from local.routine import source as routine_source
from local.routine import state as routine_state
from local.routine import turn as routine_turn
from routine import change as routine_change
from routine import hold as routine_hold
from routine import record
from routine.request import Request as RoutineRequest

# The compile's registered deadline, after which the watchdog stops it like an overdue run.
RECREATE_SECONDS = 120


def _problem(status: HTTPStatus, message: str, code: str) -> ApiProblem:
    return ApiProblem(status, message, code=code)


def _refused() -> ApiProblem:
    return _problem(HTTPStatus.UNPROCESSABLE_ENTITY, "the Routine could not be recreated", "routine-recreate-refused")


def _unavailable() -> ApiProblem:
    return _problem(
        HTTPStatus.SERVICE_UNAVAILABLE, "the Routine compile is unavailable", "routine-recreate-unavailable"
    )


def _contracts(self, team_id: str) -> tuple[str, tuple[object, ...], tuple[object, ...], dict[str, str]]:
    """The Team's network id, its active Assistants, how Brain sees them, and each one's Routine scope pin."""
    _name, network_id, active_by_id = self._team_assistants(team_id)
    active = tuple(active_by_id[key] for key in sorted(active_by_id))
    genesis = {item.spec.assistant_id: self._active_assistant_genesis(item) for item in active}
    runtime = tuple(local_chat_segment.runtime_assistant(item, genesis[item.spec.assistant_id]) for item in active)
    scope = {
        item.spec.assistant_id: local_chat_segment.routine_scope(item, genesis[item.spec.assistant_id])
        for item in active
    }
    return network_id, active, runtime, scope


def _admitted(change: routine_change.Change, request: RoutineRequest, active, scope, selected=None) -> record.Routine:
    if change.op != "create":
        raise _refused()
    try:
        return routine_turn.definition(change, request, active, scope, None, selected)
    except ApiProblem as exc:
        raise _refused() from exc


def _definition(
    compiled: inference_recreate.Compiled,
    request: RoutineRequest,
    active: tuple[object, ...],
    scope: dict[str, str],
    source: routine_source.Source,
) -> record.Routine:
    """The compiled create admitted against the message; a question is answered only by the person's selected value."""
    if compiled.clarification is None:
        try:
            value = _admitted(routine_change.parse(compiled.routine), request, active, scope)
        except routine_change.ChangeError as exc:
            raise _refused() from exc
        # The value the person once selected still stands: a compile that settles that field otherwise is refused.
        if source.selected is not None and routine_source.field_value(value, source.selected[0]) != source.selected[1]:
            raise _refused()
        return value
    if source.selected is None:
        raise _refused()
    labels = [option["label"] for option in compiled.clarification["options"]]
    try:
        question = routine_change.parse_question(compiled.routine, len(labels))
    except routine_change.ChangeError as exc:
        raise _refused() from exc
    field, wanted = source.selected
    if question.field != field:
        raise _refused()
    for label, change in zip(labels, question.changes, strict=True):
        value = _admitted(change, request, active, scope, question.selected)
        if routine_source.field_value(value, field) == wanted:
            return dataclasses.replace(value, grant={**value.grant, "selected": {"field": list(field), "label": label}})
    raise _refused()


def _compile(self, team_id: str, source: routine_source.Source, credential: tuple[str, str], runtime) -> object:
    provider, api_key = credential
    try:
        config = self.inference_store.load(team_id)
    except inference_config.InferenceConfigError as exc:
        raise _unavailable() from exc
    if config.provider != provider:
        raise _unavailable()
    try:
        compiled = inference_recreate.compile_routine(
            self.brain_runtime, (config.provider, config.model, api_key), source.message, runtime
        )
    except brain_runtime_client.BrainRuntimeError as exc:
        raise _unavailable() from exc
    if compiled == "unavailable":
        raise _unavailable()
    if isinstance(compiled, str):
        raise _refused()
    return compiled


def recreate(self, team_id: str, card, expected: routine_hold.Expected, context, credential: tuple[str, str]) -> str:
    """Recriar in the Team's execution slot: ``context`` is the current Routine, the person, and the slot's token."""
    current, principal, token = context
    source = routine_source.load(self, team_id, card.routine_id)
    if source is None or source.commitment != card.source or source.incarnation != card.incarnation:
        raise _problem(HTTPStatus.CONFLICT, "the Routine's creation message is gone", "routine-source-unavailable")
    # The card's nonce is this request's own: a replayed answer finds its card consumed and never compiles again.
    request = RoutineRequest(principal, source.message, int(time.time()), card.nonce, current.timezone)
    # A change the Team could not hold anyway never pays for a compile; its write checks again.
    full = record.change_room(routine_state.load(self, team_id), int(time.time()))
    if full is not None:
        raise _problem(HTTPStatus.CONFLICT, "the Team cannot hold this Routine change", full)
    committed: list[record.Incident] = []
    with routine_run.registered(self, team_id, card.incident_id, token, RECREATE_SECONDS):
        network_id, active, runtime, scope = _contracts(self, team_id)
        if network_id != card.incarnation:
            raise _problem(HTTPStatus.CONFLICT, "the recovery card is stale; open it again", "routine-card-stale")
        compiled = _compile(self, team_id, source, credential, runtime)
        with self._lock(team_id):
            # Nothing the compile saw may have changed by the time its change commits.
            fresh = _contracts(self, team_id)
            if (fresh[0], fresh[2], fresh[3]) != (network_id, runtime, scope):
                raise _problem(HTTPStatus.CONFLICT, "Team capabilities changed; retry", "team-context-changed")
            value = _definition(compiled, request, active, scope, source)
            now = int(time.time())
            value = routine_turn.scheduled(dataclasses.replace(value, routine_id=card.routine_id), now)
            replacement = record.Replacement(value, (request.receipt(network_id), request.expires_at))

            def commit() -> None:
                committed.append(
                    routine_incident.set_aside(
                        self,
                        team_id,
                        card.incident_id,
                        lambda state: routine_hold.recreate_incident(
                            state, card.incident_id, now, expected, replacement
                        ),
                        release_now=False,
                    )
                )

            if not routine_run.unstopped(self, token, lambda: False, commit):
                raise _problem(HTTPStatus.CONFLICT, "Recriar was stopped", "routine-recovery-stopped")
    # Committed: from here a cleanup failure only waits for the watchdog, never reads as nothing changed.
    routine_incident.settled(self, team_id, committed[0])
    return "recreated"
