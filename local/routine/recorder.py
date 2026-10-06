"""The recording span of a Team's Local chat, in Team memory only (ADR-0101).

A person's fresh request without files opens a send in the Team's recording span, or a new span when the last one is
another person's, another Team incarnation's, or older than 15 minutes. A span keeps each of the person's consecutive
sends: its message, the person's authored segments of it, the lines of the untruncated earlier sends its window carried,
its browser timezone, its start, and every successful Action call it made, in dispatch order; and one protection set
across them, every value Team injected into an attempt before its RPC and every string at a secret position of every
result. A send's id is the only thing a paused turn keeps, so the same send goes on recording across a person's answer
and an Integration resume in the same process.

When the chat agent calls ``record`` within 15 minutes of the span's latest send, Team builds the plan from the span
(``routine/recording.py``). A card or a refusal ends the span; a question keeps it, with what it asked, so the person's
answer is the span's next send. A send with files or not a person's own ends it too. The span is bounded: at most 16
sends, and its texts and kept calls within their byte bounds; to fit a new send or call, its oldest sends go whole, and
the send in progress never does, so a send that alone outgrows a bound refuses the recording. Protection never shrinks
and a loss is never undone. Nothing here persists: a Team restart drops every span, and a send in progress then records
nothing.
"""

from __future__ import annotations

import dataclasses
import json
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from assistant import effect as action_effect
from inference import client as brain_runtime_client
from protocol.http.v1 import payload as http_payload
from routine import phrase, trace
from routine import pin as routine_pin
from routine import recording as routine_recording

SPAN_SECONDS = 15 * 60
MAX_SENDS = 16
# The span's retained text: each send's message, its person-authored lines, its window lines, and its context.
MAX_TEXT_BYTES = 64 * 1024
_SEND_CONTEXT_BYTES = 128


@dataclass(frozen=True, slots=True)
class Intent:
    """What the span's latest ``record`` call asked for: the Routine's name, output, and the Routine it replaces.

    The replaced Routine is bound at exactly the revision that call was shown, which admission then requires unchanged.
    """

    name: str
    output: dict[str, object]
    decide_actions: tuple[tuple[str, str], ...]
    replaces: str | None
    revision: int | None


@dataclass(frozen=True, slots=True)
class Span:
    """One Team's recording span: whose it is, its sends, its protection, and what it last asked."""

    team_id: str
    principal: str
    incarnation: str
    # The ids of its sends, oldest first; the last one is the send in progress or the latest that ended.
    send_ids: tuple[str, ...]
    sends: tuple[routine_recording.Send, ...]
    protection: trace.Protection = dataclasses.field(default_factory=trace.Protection)
    # Why nothing recorded may define a Routine any more, such as a send past its bound; empty while it may.
    refused: str = ""
    # The revision of every Routine the latest send's Brain was shown, by id: only these may be replaced.
    revisions: tuple[tuple[str, int], ...] = ()
    asked: routine_recording.Asked | None = None
    # The first send whose calls count: the send that settled a rerun question, kept whatever is asked after it.
    frontier: int = 0
    # What the latest record call asked for, which an answer to the span's question records again.
    intent: Intent | None = None

    @property
    def recording_id(self) -> str:
        return self.send_ids[-1]

    @property
    def started_at(self) -> int:
        return self.sends[-1].started_at


@dataclass(frozen=True, slots=True)
class Started:
    """A new send: its message, the conversation window it carried, and its browser timezone."""

    message: str
    conversation: tuple[object, ...]
    timezone: str | None


def _text_bytes(send: routine_recording.Send) -> int:
    texts = (send.message, *send.person, *send.window)
    return sum(len(text.encode()) for text in texts) + _SEND_CONTEXT_BYTES


def _trace_bytes(sends: Iterable[routine_recording.Send]) -> tuple[int, int]:
    occurrences = [item for send in sends for item in send.occurrences]
    return len(occurrences), sum(item.size for item in occurrences)


def _fits(sends: tuple[routine_recording.Send, ...]) -> bool:
    count, size = _trace_bytes(sends)
    return (
        len(sends) <= MAX_SENDS
        and sum(_text_bytes(send) for send in sends) <= MAX_TEXT_BYTES
        and count <= trace.MAX_OCCURRENCES
        and size <= trace.MAX_TRACE_BYTES
    )


def _bounded(span: Span) -> Span:
    """The span with its oldest sends gone until it fits; one whose latest send alone does not fit is refused."""
    send_ids, sends = span.send_ids, span.sends
    while len(sends) > 1 and not _fits(sends):
        send_ids, sends = send_ids[1:], sends[1:]
    bounded = dataclasses.replace(span, send_ids=send_ids, sends=sends)
    if not _fits(sends):
        return dataclasses.replace(bounded, refused="routine-recording-too-large")
    gone = len(span.sends) - len(sends)
    if gone:
        # The question and the frontier counted the sends before them; those that went leave the count too.
        bounded = dataclasses.replace(bounded, frontier=max(bounded.frontier - gone, 0))
        if bounded.asked is not None:
            after = max(bounded.asked.after - gone, 0)
            bounded = dataclasses.replace(bounded, asked=dataclasses.replace(bounded.asked, after=after))
    return bounded


def earlier_sends(conversation: Iterable[object]) -> tuple[str, ...]:
    """The person's own earlier sends in a conversation window: never the assistant's, and never a truncated one."""
    return tuple(entry.text for entry in conversation if entry.role == "user" and not entry.truncated)


def _send(started: Started, now: int) -> routine_recording.Send:
    earlier = (
        segment for text in earlier_sends(started.conversation) for segment in http_payload.authored_segments(text)
    )
    window = tuple(line for segment in earlier for line in segment.split("\n"))
    person = http_payload.authored_segments(started.message)
    return routine_recording.Send(started.message, person, window, started.timezone, now)


class RecordingBook:
    """Every Team's recording span, at most one each, as a Team runs one chat turn at a time."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._guard = threading.Lock()
        self._spans: dict[str, Span] = {}

    def start(self, team_id: str, binding: tuple[str, str], started: Started, now: int) -> str:
        """Open a send in the Team's span, or in a new span when the last one is not this person's or is stale."""
        principal, incarnation = binding
        send_id = secrets.token_hex(16)
        send = _send(started, now)
        with self._guard:
            found = self._spans.get(team_id)
            if (
                found is None
                or found.refused
                or (found.principal, found.incarnation) != (principal, incarnation)
                or now - found.started_at > SPAN_SECONDS
            ):
                found = Span(team_id, principal, incarnation, (), ())
            opened = dataclasses.replace(
                found, send_ids=(*found.send_ids, send_id), sends=(*found.sends, send), revisions=()
            )
            self._spans[team_id] = _bounded(opened)
        return send_id

    def get(self, team_id: str, recording_id: str | None) -> Span | None:
        """The Team's span whose latest send has exactly this id, or None."""
        with self._guard:
            found = self._spans.get(team_id)
        return found if found is not None and recording_id is not None and found.recording_id == recording_id else None

    def live(self, team_id: str, recording_id: str | None) -> Span | None:
        """The span to record from: its latest send has exactly this id and started at most 15 minutes ago."""
        found = self.get(team_id, recording_id)
        return None if found is None or self._clock() - found.started_at > SPAN_SECONDS else found

    def _change(self, team_id: str, recording_id: str, change: Callable[[Span], Span]) -> None:
        with self._guard:
            found = self._spans.get(team_id)
            if found is not None and found.recording_id == recording_id:
                self._spans[team_id] = change(found)

    def protect(self, team_id: str, recording_id: str, values: Iterable[str]) -> None:
        """Protect more of the span's values; a protection past its bound is lost, and so is the recording."""
        grown = tuple(values)
        self._change(
            team_id, recording_id, lambda found: dataclasses.replace(found, protection=found.protection.grow(grown))
        )

    def listed(self, team_id: str, recording_id: str, revisions: Iterable[tuple[str, int]]) -> None:
        """Keep the Routines, by id and revision, the send's Brain was shown as it started."""
        shown = tuple(revisions)
        self._change(team_id, recording_id, lambda found: dataclasses.replace(found, revisions=shown))

    def occurred(self, team_id: str, recording_id: str, occurrence: trace.Occurrence) -> None:
        """Keep one successful Action call in the send in progress; a send past its bound refuses the recording."""

        def add(found: Span) -> Span:
            if found.refused:
                return found
            latest = found.sends[-1]
            grown = dataclasses.replace(latest, occurrences=(*latest.occurrences, occurrence))
            return _bounded(dataclasses.replace(found, sends=(*found.sends[:-1], grown)))

        self._change(team_id, recording_id, add)

    def asked(self, team_id: str, recording_id: str, question: routine_recording.Question, intent: Intent) -> None:
        """Keep what the span asked, what it binds and settled, and the intent the person's answer records again."""

        def keep(found: Span) -> Span:
            asked = routine_recording.Asked(
                question.code, len(found.sends), question.pending, question.manifest, question.wire(), question.chosen
            )
            frontier = max(found.frontier, question.frontier)
            return dataclasses.replace(found, asked=asked, frontier=frontier, intent=intent)

        self._change(team_id, recording_id, keep)

    def finish(self, team_id: str, recording_id: str) -> None:
        """End the Team's span after the card or refusal its latest send made."""
        with self._guard:
            found = self._spans.get(team_id)
            if found is not None and found.recording_id == recording_id:
                del self._spans[team_id]

    def drop(self, team_id: str) -> None:
        with self._guard:
            self._spans.pop(team_id, None)

    def clear(self) -> None:
        with self._guard:
            self._spans.clear()


def answered(span: Span | None) -> Intent | None:
    """The intent to record again when the span's latest send is Admin's composed answer that binds its question.

    It binds a schedule question when it states a schedule, an interval question when it states an interval, and a
    target question when it is exactly one target's JSON text. Anything else, typed freely or not binding, is the
    Brain's to read.
    """
    if span is None or span.intent is None or span.asked is None or span.refused:
        return None
    answer = _latest_answer(span.sends[-1].message)
    if answer is None:
        return None
    asked = span.asked
    stated = phrase.stated(answer)
    pending = asked.pending
    bound = {
        "routine-schedule-unstated": bool(stated),
        "routine-interval-over-budget": any(item["kind"] in _INTERVAL_KINDS for item in stated),
        "routine-binding-ambiguous": pending is not None
        and any(answer == json.dumps(target, ensure_ascii=False) for target, _label in pending.targets),
    }
    return span.intent if bound.get(asked.code, False) else None


_INTERVAL_KINDS = frozenset({"continuous", "hourly"})


def _latest_answer(message: str) -> str | None:
    """The answer a message ends with when it is exactly Admin's composed clarification answer, else None.

    The message must be the composition ``payload.compose_clarified`` makes of an original request, one question, and
    one answer in one interface language, so the answer is its last authored segment and nothing follows it.
    """
    lines = message.split("\n")
    if len(lines) < 4:
        return None
    for locale, labels in http_payload.CLARIFICATION_LABELS.items():
        question, answer = f"{labels['question']}: ", f"{labels['answer']}: "
        if not (lines[-2].startswith(question) and lines[-1].startswith(answer)):
            continue
        original, asked, given = "\n".join(lines[:-3]), lines[-2][len(question) :], lines[-1][len(answer) :]
        composed = http_payload.compose_clarified(original, asked, given, locale)
        if original.strip() and given.strip() and composed == message:
            return given
    return None


def settled(span: Span | None) -> Intent | None:
    """The intent to record again when the span's latest send repeated the work its pending question asked for."""
    if span is None or span.intent is None or span.asked is None or span.asked.manifest is None or span.refused:
        return None
    return span.intent if routine_recording.settlement(span.sends, span.asked) == len(span.sends) - 1 else None


class AnsweredRuntime:
    """The runtime of a send Team records itself: it never asks the Brain and completes with its fixed reply."""

    def __init__(self, reply: str, intent: Intent) -> None:
        self._turn = brain_runtime_client.RuntimeTurn("completed", reply, (), routine=intent)

    def start(self, _context: object, _message: str, *, conversation: object = ()) -> object:
        return self._turn


def recorded(book: RecordingBook, recording: tuple[str, str], call: tuple, invoke: Callable[[], object]) -> object:
    """Run one Action call of a recording send, keeping it as an occurrence when it succeeds.

    ``call`` is the active Assistant, the Action request, the attempt's evidence, and the values Team injected into it.
    Those values are protected before the RPC; the result's secret strings are protected before the result goes on.
    The Action's own errors pass through, and a failed call is never an occurrence.
    """
    team_id, recording_id = recording
    active, action_request, evidence, injected = call
    action = active.spec.actions[action_request.action]
    book.protect(team_id, recording_id, injected)
    dispatched_at = int(time.time())
    result = invoke()
    book.protect(team_id, recording_id, trace.secret_values(result, action.output_schema))
    found = book.get(team_id, recording_id)
    protected = () if found is None else tuple(found.protection.values)
    pin = routine_pin.action_pins(active.spec, (action_request.action,), routine_pin.SCOPE_LOCALE)[
        action_request.action
    ]
    occurrence = trace.Occurrence(
        evidence.operation_id,
        active.spec.assistant_id,
        action_request.action,
        pin,
        action.effect == action_effect.READ_ONLY,
        dispatched_at,
        trace.keep(dict(action_request.input), action.input_schema, protected),
        trace.keep(result, action.output_schema, protected),
    )
    book.occurred(team_id, recording_id, occurrence)
    return result
