"""What a Local chat turn knows about the Team's Routines, and how its compiled change commits (ADR-0086, ADR-0092).

A Routine is created or changed only from the authenticated user's own chat message, with no confirmation card. The
Brain's isolated compiler proposes the change; Team admits it against the Routine's words, the exact contracts of the
Assistants the turn saw, and the request's fresh identity, then commits the Routine, its notice, and the request's
receipt in one write exactly when the reply commits, under the Team lifecycle lock and the Stop guard. When the person's
words leave a piece missing, the compiler asks instead, and the turn keeps the person's Routine draft for the answer to
continue; a person may also discard it (ADR-0092 amendment, 2026-10-05).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from http import HTTPStatus

from install import bindings
from local import audit as local_audit
from local.chat import segment as local_chat_segment
from local.errors import ApiProblemError as ApiProblem
from local.routine import draft as routine_draft
from local.routine import source as routine_source
from local.routine import state as routine_state
from local.routine import store as routine_store
from routine import change as routine_change
from routine import grant as routine_grant
from routine import pin as routine_pin
from routine import plan as routine_plan
from routine import record
from routine import request as routine_request
from routine.request import Request as RoutineRequest

# The zone of a Routine whose message named none and whose browser reported none.
DEFAULT_TIMEZONE = "UTC"


def chat_routines(self, team_id: str) -> tuple[dict[str, object], ...]:
    """The Team's Routines as data for the Brain: enough to name one and to keep its steps; never an input value."""
    try:
        state = self.routine_store.load(team_id)
    except routine_store.RoutineStoreError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE, "Team Routine state is unavailable", code="routine-state-unavailable"
        ) from exc
    return tuple(
        {
            "routine_id": item.routine_id,
            "name": item.name,
            "quote": item.quote,
            "schedule": dict(item.schedule),
            "timezone": item.timezone,
            "revision": item.revision,
            "steps": [
                {
                    "id": step["id"],
                    "assistant": step["assistant"],
                    "action": step["action"],
                    "inputs": sorted(step["input"]),
                }
                for step in item.plan["steps"]
            ],
        }
        for item in state.routines
        if not item.deleting
    )


def expired() -> ApiProblem:
    return ApiProblem(
        HTTPStatus.CONFLICT, "this request can no longer change a Routine", code="routine-request-expired"
    )


def contracts(assistants: tuple[object, ...], locale: str) -> dict[tuple[str, str], routine_plan.ActionContract]:
    """Each Action of the turn's Assistants with its complete current pin and reviewed input schema."""
    admitted: dict[tuple[str, str], routine_plan.ActionContract] = {}
    for active in assistants:
        pins = routine_pin.action_pins(active.spec, active.spec.actions, locale)
        admitted.update(
            {
                (active.spec.assistant_id, action_id): routine_plan.ActionContract(
                    pins[action_id], action.input_schema, action.input_files
                )
                for action_id, action in active.spec.actions.items()
            }
        )
    return admitted


def current(self, team_id: str, change: routine_change.Change) -> record.Routine | None:
    """The Routine an update names, which must still be listed; None for a create."""
    if change.op == "create":
        return None
    state = routine_state.load(self, team_id)
    found = next((item for item in state.routines if item.routine_id == change.routine_id), None)
    if found is None or found.deleting:
        raise ApiProblem(HTTPStatus.NOT_FOUND, "Routine is unavailable", code="routine-not-found")
    return found


def checked(self, response: object) -> tuple[RoutineRequest, str, tuple[object, ...]]:
    """The turn's fresh request, the Team's network id, and the Assistants the turn saw, still exactly current."""
    request = response.routine_request
    if request is None or response.file_ids or not request.fresh(int(time.time())):
        raise expired()
    setup = self._chat_setup(response.team_id, list(response.file_ids), response.provider, response.assistant_ids)
    if self._chat_identity(*setup) != response.segment.identity:
        raise ApiProblem(HTTPStatus.CONFLICT, "Team capabilities changed; retry", code="team-context-changed")
    return request, setup[1], setup[2]


def refused(exc: routine_change.ChangeError | record.RoutineStateError) -> ApiProblem:
    return ApiProblem(
        HTTPStatus.UNPROCESSABLE_ENTITY, "the Routine change was refused", code=getattr(exc, "code", str(exc))
    )


def definition(
    change: routine_change.Change,
    request: RoutineRequest,
    assistants: tuple[object, ...],
    scope: dict[str, str],
    existing: record.Routine | None,
    selected: tuple[str, str] | None = None,
) -> record.Routine:
    """The Routine a change defines, admitted against the user's own words and the exact current contracts.

    ``selected`` is the one step input a bound Routine question fills from the option the user picks. A change that
    continues the person's draft, which ``continued`` proved the request froze, holds the draft's parts in its words.
    """
    parts = request.parts(change.continues)
    try:
        compiled = routine_change.compile_change(
            change,
            routine_change.Words(parts),
            # Every Routine pins its Actions in one fixed locale; each pin still covers the whole language pack.
            contracts(assistants, routine_pin.SCOPE_LOCALE),
            None if existing is None else (existing.plan, existing.grant["sources"]),
            request.timezone or DEFAULT_TIMEZONE,
            selected,
        )
    except routine_change.ChangeError as exc:
        raise refused(exc) from exc
    specs = {active.spec.assistant_id: active.spec for active in assistants}
    # Each step's Action names the Stored Inputs it uses; the evidence keeps their ids, never a value.
    stored = {
        step["id"]: list(specs[step["assistant"]].actions[step["action"]].stored_inputs)
        for step in compiled.document["steps"]
    }
    return record.Routine(
        change.routine_id or record.new_id(),
        compiled.name,
        compiled.quote,
        compiled.schedule,
        compiled.timezone,
        tuple((assistant, scope[assistant]) for assistant in compiled.assistants),
        compiled.document,
        anchor=0,
        next_run_at=0,
        grant=routine_grant.evidence(routine_request.commitment(parts), compiled.quote_span, compiled.sources, stored),
    )


def scheduled(value: record.Routine, now: int) -> record.Routine:
    try:
        return record.scheduled(value, now)
    except record.RoutineStateError as exc:
        raise refused(exc) from exc


def writer(
    self,
    team_id: str,
    change: tuple[str, int | None],
    value: record.Routine,
    request: RoutineRequest,
    network_id: str,
    *,
    source: routine_source.Source | None,
) -> Callable[[], None]:
    """The write that commits a scheduled Routine, its notice, and the request's receipt in one transition.

    ``change`` is the operation and, for an update, the revision the request saw. A create first proves the request was
    not already committed, then removes the person's Routine draft, exactly the one the request froze (a bound create
    question binds only inside that same draft), then seals its ``source``, all under the same Team lock as the write,
    so neither a retry nor the watchdog ever crosses it. A committed request's retry touches nothing again.
    """
    op, expected_revision = change
    receipt = request.receipt(network_id)

    def apply(state: record.TeamRoutines) -> tuple[record.TeamRoutines, str]:
        moment = int(time.time())
        try:
            if op == "create":
                state, changed = record.create(state, value, moment, receipt, request.expires_at)
            else:
                state, changed = record.update(state, value, expected_revision, moment, receipt, request.expires_at)
        except record.RoutineStateError as exc:
            return state, str(exc)
        return state, "ok" if changed else "repeated"

    def write() -> None:
        if not request.fresh(int(time.time())):
            raise expired()
        with self.routine_store.lock(team_id):
            if op == "create":
                if record.has_receipt(routine_state.load(self, team_id), receipt, int(time.time())):
                    outcome = "repeated"
                else:
                    routine_draft.discard(self, team_id, request)
                    routine_source.seal(self, team_id, source)
                    outcome = routine_state.update(self, team_id, apply)
            else:
                outcome = routine_state.update(self, team_id, apply)
        if outcome not in {"ok", "repeated"}:
            raise ApiProblem(HTTPStatus.CONFLICT, "the Team cannot hold this Routine change", code=outcome)
        local_audit.record_request(
            "routine-change", result="ok", team_id=team_id, detail=f"{op}:{value.routine_id}:{outcome}"
        )

    return write


def continued(request: RoutineRequest, continues: bool, network_id: str) -> None:
    """A change that continues the person's draft needs the draft the request froze, from this Team incarnation."""
    if continues and (request.draft is None or request.draft.incarnation != network_id):
        raise ApiProblem(HTTPStatus.CONFLICT, "Team capabilities changed; retry", code="team-context-changed")


def admit_change(self, response: object, proposed: dict[str, object]) -> Callable[[], None]:
    """Admit a completed turn's compiled change and return the write that commits it with the reply.

    The caller holds the Team lifecycle lock from here to the commit, so a changed Assistant, a destroy, or a reset
    cannot cross it.
    """
    try:
        change = routine_change.parse(proposed)
    except routine_change.ChangeError as exc:
        raise ApiProblem(
            HTTPStatus.BAD_GATEWAY, "Brain could not complete the Team turn", code="brain-runtime-failed"
        ) from exc
    request, network_id, assistants = checked(self, response)
    continued(request, change.continues, network_id)
    existing = current(self, response.team_id, change)
    value = definition(change, request, assistants, dict(response.segment.contracts), existing)
    value = scheduled(value, int(time.time()))
    source = (
        routine_source.Source(value.routine_id, network_id, request.parts(change.continues))
        if change.op == "create"
        else None
    )
    return writer(
        self, response.team_id, (change.op, change.expected_revision), value, request, network_id, source=source
    )


def _unasked(clarification: dict[str, object] | None) -> bool:
    """Whether a Routine question is missing, or recommends an option, which no Routine question ever does."""
    return clarification is None or clarification["default_index"] is not None


def admit_need(self, response: object, proposed: object, clarification: dict | None) -> Callable[[], None]:
    """Admit a ``need`` question: the person's words leave a piece missing, so the turn asks and keeps their draft.

    The write keeps the Routine's words as the person's draft, with the question it asks, as the reply commits.
    """
    try:
        continues = routine_change.parse_need(proposed)
    except routine_change.ChangeError as exc:
        raise ApiProblem(
            HTTPStatus.BAD_GATEWAY, "Brain could not complete the Team turn", code="brain-runtime-failed"
        ) from exc
    if _unasked(clarification):
        raise ApiProblem(HTTPStatus.BAD_GATEWAY, "Brain could not complete the Team turn", code="brain-runtime-failed")
    request, network_id, _assistants = checked(self, response)
    continued(request, continues, network_id)
    parts = request.parts(continues)
    return lambda: routine_draft.save(self, response.team_id, request, network_id, parts, clarification["question"])


def admit_discard(self, response: object, proposed: object) -> Callable[[], None]:
    """Admit a ``discard``: the person asked to drop the Routine being set up; the write removes their draft."""
    try:
        routine_change.parse_discard(proposed)
    except routine_change.ChangeError as exc:
        raise ApiProblem(
            HTTPStatus.BAD_GATEWAY, "Brain could not complete the Team turn", code="brain-runtime-failed"
        ) from exc
    request, _network_id, _assistants = checked(self, response)

    def write() -> None:
        with self.routine_store.lock(response.team_id):
            routine_draft.discard(self, response.team_id, request)

    return write


class ContractsUnavailableError(Exception):
    """The Team's current Assistants could not be read, so nothing about a Routine's scope is proven either way."""


def context_unavailable() -> ApiProblem:
    """The retryable problem for a Routine step whose Team could not be read; nothing it held was changed."""
    return ApiProblem(
        HTTPStatus.SERVICE_UNAVAILABLE, "Team capabilities could not be checked; retry", code="team-context-unavailable"
    )


def current_contracts(self, team_id: str, assistant_ids: tuple[str, ...]) -> dict[str, str]:
    """Each named Assistant's current Routine scope pin, exactly as a chat turn compiling a Routine computes it.

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
