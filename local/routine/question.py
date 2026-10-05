"""A Routine question and its bound answer (ADR-0092 section 2).

When the planner is unsure of exactly one field, its turn ends with an ordinary multiple-choice clarification and a
candidate change that leaves that field open, with one value per visible option. Team admits each option's complete
Routine against the user's own words and the exact current contracts before the question is shown, and keeps them in
the Team-bound lineage. A message that answers exactly that question with one of its own labels then commits the
Routine of that option, as it was admitted, with no model call: the selection changes only the open field, and the
model's question text or an unselected option never becomes the user's grant.
"""

from __future__ import annotations

import dataclasses
import time
from http import HTTPStatus

from local.errors import ApiProblemError as ApiProblem
from local.routine import lineage as routine_lineage
from local.routine import source as routine_source
from local.routine import turn as routine_turn
from routine import change as routine_change
from routine import pin as routine_pin
from routine import plan as routine_plan
from routine import record
from routine.request import Request as RoutineRequest


def admit(self, response: object, proposed: object, clarification: dict[str, object]) -> routine_lineage.Question:
    """Admit a completed turn's Routine question: every option's Routine, before the question reaches the user."""
    labels = tuple(option["label"] for option in clarification["options"])
    try:
        question = routine_change.parse_question(proposed, len(labels))
    except routine_change.ChangeError as exc:
        raise ApiProblem(
            HTTPStatus.BAD_GATEWAY, "Brain could not complete the Team turn", code="brain-runtime-failed"
        ) from exc
    request, _network_id, assistants = routine_turn.checked(self, response)
    head = question.changes[0]
    existing = routine_turn.current(self, response.team_id, head)
    scope = dict(response.segment.contracts)
    routine_id = head.routine_id or record.new_id()
    now = int(time.time())
    routines = []
    for change in question.changes:
        value = routine_turn.definition(change, request, assistants, scope, existing, question.selected)
        # Each option must also schedule; only the answer's commit schedules it for real.
        routine_turn.scheduled(value, now)
        routines.append(dataclasses.replace(value, routine_id=routine_id))
    return routine_lineage.Question(
        request.principal,
        request.message,
        clarification["question"],
        labels,
        question.field,
        head.op,
        head.expected_revision,
        tuple(routines),
        question.reply,
        earlier=request.earlier,
    )


def _current(self, team_id: str, value: record.Routine) -> tuple[str, str]:
    """The Team's name and network id, when every Assistant and Action contract the Routine pins is still current."""
    team_name, network_id, active = self._team_assistants(team_id)
    pinned = dict(value.assistants)
    try:
        scope = routine_turn.current_contracts(self, team_id, tuple(pinned))
        routine_plan.admit(value.plan, routine_turn.contracts(tuple(active.values()), routine_pin.SCOPE_LOCALE))
    except (routine_turn.ContractsUnavailableError, routine_plan.PlanError) as exc:
        raise ApiProblem(HTTPStatus.CONFLICT, "Team capabilities changed; retry", code="team-context-changed") from exc
    if scope != pinned:
        raise ApiProblem(HTTPStatus.CONFLICT, "Team capabilities changed; retry", code="team-context-changed")
    return team_name, network_id


def answer(self, team_id: str, token: str, request: RoutineRequest, bound: routine_lineage.Answer) -> dict[str, object]:
    """Commit the Routine the selected option completes, with its reply, exactly when Stop did not win the turn.

    The question stays pending until that commit, so a failed or stopped answer may be sent again.
    """
    if not request.fresh(int(time.time())):
        raise routine_turn.expired()
    question = bound.question
    with self._lock(team_id):
        team_name, network_id = _current(self, team_id, bound.routine)
        value = routine_turn.scheduled(bound.routine, int(time.time()))
        source = None
        if question.op == "create":
            selected = (question.field, routine_source.field_value(value, question.field))
            source = routine_source.Source(
                value.routine_id, network_id, question.message, selected, earlier=question.earlier
            )
        write = routine_turn.writer(
            self, team_id, (question.op, question.expected_revision), value, request, network_id, source=source
        )
        committed = self._commit_chat_terminal(team_id, token, write)
    if not committed:
        raise ApiProblem(HTTPStatus.CONFLICT, "chat turn stopped", code="chat-stopped")
    self.routine_lineage.settle(team_id, question)
    return {"team_id": team_id, "team_name": team_name, "reply": question.reply, "clarification": None}
