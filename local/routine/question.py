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
from collections.abc import Callable
from http import HTTPStatus

from local.errors import ApiProblemError as ApiProblem
from local.routine import draft as routine_draft
from local.routine import lineage as routine_lineage
from local.routine import source as routine_source
from local.routine import turn as routine_turn
from routine import change as routine_change
from routine import pin as routine_pin
from routine import plan as routine_plan
from routine import record
from routine.request import Request as RoutineRequest


def admit(self, response: object, proposed: object, clarification: dict[str, object] | None) -> Callable[..., None]:
    """Admit a completed turn's Routine question: every option's Routine, before the question reaches the user.

    The returned write runs exactly when the reply commits, calling ``before`` once nothing can refuse it any more: a
    create question keeps the Routine's words as the person's draft, so a typed or free-text answer continues it
    (ADR-0092 amendment, 2026-10-05), and records the question bound to that very draft; an update question only
    records itself.
    """
    if clarification is None or clarification["default_index"] is not None or len(clarification["options"]) < 2:
        # A Routine question recommends nothing, and one field with a single value leaves nothing to ask.
        raise ApiProblem(HTTPStatus.BAD_GATEWAY, "Brain could not complete the Team turn", code="brain-runtime-failed")
    labels = tuple(option["label"] for option in clarification["options"])
    try:
        question = routine_change.parse_question(proposed, len(labels))
    except routine_change.ChangeError as exc:
        raise ApiProblem(
            HTTPStatus.BAD_GATEWAY, "Brain could not complete the Team turn", code="brain-runtime-failed"
        ) from exc
    request, network_id, assistants = routine_turn.checked(self, response)
    head = question.changes[0]
    routine_turn.continued(request, head.continues, network_id)
    existing = routine_turn.current(self, response.team_id, head)
    scope = dict(response.segment.contracts)
    routine_id = head.routine_id or record.new_id()
    now = int(time.time())
    routines = []
    for label, change in zip(labels, question.changes, strict=True):
        cap_label = label if routine_change.caps_vary(question) else None
        value = routine_turn.definition(change, request, assistants, scope, existing, question.selected, cap_label)
        # Each option must also schedule; only the answer's commit schedules it for real.
        routine_turn.scheduled(value, now)
        routines.append(dataclasses.replace(value, routine_id=routine_id))
    words = request.parts(head.continues)
    asked = routine_lineage.Question(
        request.principal,
        request.message,
        clarification["question"],
        labels,
        question.field,
        head.op,
        head.expected_revision,
        tuple(routines),
        question.replies,
        words=words,
    )

    def write(before: Callable[[], None]) -> None:
        if head.op != "create":
            before()
            self.routine_lineage.record(response.team_id, asked)
            return
        generation = routine_draft.save(
            self, response.team_id, request, network_id, (words, clarification["question"]), before
        )
        # A create question binds only inside the draft it wrote: words that did not fit one leave nothing to bind.
        if generation is not None:
            self.routine_lineage.record(response.team_id, dataclasses.replace(asked, generation=generation))

    return write


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
    """Commit the Routine the selected option completes, with the reply written for it, unless Stop won the turn.

    The question stays pending until that commit, so a failed or stopped answer may be sent again. A create question
    commits only while the person's draft is still the one it was asked in, which the commit then removes; its source
    and evidence keep the asking words and the selected label, the person's own answer.
    """
    if not request.fresh(int(time.time())):
        raise routine_turn.expired()
    question = bound.question
    with self._lock(team_id):
        team_name, network_id = _current(self, team_id, bound.routine)
        value = routine_turn.scheduled(bound.routine, int(time.time()))
        source = None
        if question.op == "create":
            # The asking words may hold the person's draft, which must still be of this Team incarnation.
            routine_turn.continued(request, True, network_id)
            selected = (question.field, routine_source.field_value(value, question.field))
            source = routine_source.Source(value.routine_id, network_id, bound.words, selected)
        write = routine_turn.writer(
            self,
            team_id,
            (question.op, question.expected_revision),
            value,
            request,
            network_id,
            source=source,
        )
        committed = self._commit_chat_terminal(team_id, token, write)
    if not committed:
        raise ApiProblem(HTTPStatus.CONFLICT, "chat turn stopped", code="chat-stopped")
    self.routine_lineage.settle(team_id, question)
    return {"team_id": team_id, "team_name": team_name, "reply": bound.reply, "clarification": None}
