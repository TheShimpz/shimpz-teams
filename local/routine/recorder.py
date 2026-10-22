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

Each send also keeps what its turn spent, which a Routine created from the span shows (ADR-0101 amendment, 2026-10-16,
creation receipt): the finished Team steps it measured while it ran, at most ``MAX_CREATION_STEPS`` across the span, and
its usage once its reply committed. The span is an accounting policy, not a causal proof: it can hold a send unrelated
to the Routine and may have dropped an earlier one that contributed.
"""

import dataclasses
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from assistant import effect as action_effect
from inference import client as brain_runtime_client
from inference import usage as brain_usage
from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import phrase
from protocol.http.v1 import routine_context as http_routine_context
from protocol.http.v1 import routine_notice as http_routine_notice
from protocol.http.v1 import routine_proposal as http_routine_proposal
from routine import pin as routine_pin
from routine import recording as routine_recording
from routine import reference as routine_reference
from routine import trace

SPAN_SECONDS = 15 * 60
MAX_SENDS = 16
# The span's retained text: each send's message, its person-authored lines, its window lines, and its context.
MAX_TEXT_BYTES = 64 * 1024
_SEND_CONTEXT_BYTES = 128


@dataclass(frozen=True, slots=True)
class Intent:
    """What the span's latest ``record`` call asked for: the Routine's name, what it replaces, and its description.

    The replaced Routine is bound at exactly the revision that call was shown, which admission then requires unchanged.
    The description is kept with it, so a card Team builds from the person's answer shows what the agent wrote, and so
    is the id of the send whose calls are its work (``selected_work``), or None when a replacement keeps its steps.
    """

    name: str
    replaces: str | None
    revision: int | None
    description: str
    work: str | None = None


@dataclass(frozen=True, slots=True)
class Receipt:
    """What one send's turn spent: the finished Team steps it measured, and its usage once its reply committed.

    A send whose reply never committed spends nothing a Routine's creation shows.
    """

    steps: tuple[dict[str, object], ...] = ()
    spent: brain_usage.Spent | None = None


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
    # What the latest record call asked for, which an answer to the span's question records again.
    intent: Intent | None = None
    # What each send's turn spent, by send id, for the sends the span still holds.
    receipts: tuple[tuple[str, Receipt], ...] = ()
    # The one Routine, by id, the person's latest send selected, or an earlier send when the latest named none
    # (ADR-0122); None when no send selected one, or the latest named several or a Routine the Team does not list.
    selected: str | None = None

    @property
    def recording_id(self) -> str:
        return self.send_ids[-1]

    @property
    def started_at(self) -> int:
        return self.sends[-1].started_at


@dataclass(frozen=True, slots=True)
class Started:
    """A new send: its message, the conversation window it carried, its browser timezone, and its interface language."""

    message: str
    conversation: tuple[object, ...]
    timezone: str | None
    # The chat's interface language, or None.
    locale: str | None = None


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
    receipts = tuple(item for item in span.receipts if item[0] in send_ids)
    bounded = dataclasses.replace(span, send_ids=send_ids, sends=sends, receipts=receipts)
    if not _fits(sends):
        return dataclasses.replace(bounded, refused="routine-recording-too-large")
    gone = len(span.sends) - len(sends)
    # The question counted the sends before it; those that went leave the count too.
    if gone and bounded.asked is not None:
        asked = bounded.asked
        repair_from = None if asked.repair_from is None else max(asked.repair_from - gone, 0)
        asked = dataclasses.replace(asked, after=max(asked.after - gone, 0), repair_from=repair_from)
        bounded = dataclasses.replace(bounded, asked=asked)
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
    return routine_recording.Send(started.message, person, window, started.timezone, now, locale=started.locale)


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
            # A send whose reply never committed spends nothing a creation shows, so its steps go as the next opens.
            spent = tuple((item, receipt if receipt.spent else Receipt()) for item, receipt in found.receipts)
            opened = dataclasses.replace(
                found,
                send_ids=(*found.send_ids, send_id),
                sends=(*found.sends, send),
                revisions=(),
                receipts=(*spent, (send_id, Receipt())),
            )
            self._spans[team_id] = _bounded(opened)
        return send_id

    def get(self, team_id: str, recording_id: str | None) -> Span | None:
        """The Team's span whose latest send has exactly this id, or None."""
        with self._guard:
            found = self._spans.get(team_id)
        return found if found is not None and found.recording_id == recording_id else None

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

    def listed(
        self,
        team_id: str,
        recording_id: str,
        revisions: Iterable[tuple[str, int]],
        reference: tuple[str, str | None] = (routine_reference.NONE, None),
    ) -> None:
        """Keep the Routines, by id and revision, the send's Brain was shown as it started, and the one it selects.

        ``reference`` is what the send says about them (``routine.reference``): a send that names none keeps what an
        earlier send of the span selected, while that Routine is still listed.
        """
        shown = tuple(revisions)
        kind, routine_id = reference

        def keep(found: Span) -> Span:
            selected = routine_id if kind != routine_reference.NONE else found.selected
            listed = selected is not None and selected in dict(shown)
            return dataclasses.replace(found, revisions=shown, selected=selected if listed else None)

        self._change(team_id, recording_id, keep)

    def occurred(self, team_id: str, recording_id: str, occurrence: trace.Occurrence) -> None:
        """Keep one successful Action call in the send in progress; a send past its bound refuses the recording."""

        def add(found: Span) -> Span:
            if found.refused:
                return found
            latest = found.sends[-1]
            grown = dataclasses.replace(latest, occurrences=(*latest.occurrences, occurrence))
            return _bounded(dataclasses.replace(found, sends=(*found.sends[:-1], grown)))

        self._change(team_id, recording_id, add)

    def stepped(self, team_id: str, recording_id: str, event: dict[str, object]) -> None:
        """Keep one finished Team step of the send in progress, while the span holds fewer than it may show."""
        if event.get("state") != "finished":
            return
        step = {name: value for name, value in event.items() if name != "seq"}

        def add(found: Span) -> Span:
            if len(creation_steps(found)) >= MAX_CREATION_STEPS:
                return found
            *earlier, (send_id, latest) = found.receipts
            grown = dataclasses.replace(latest, steps=(*latest.steps, step))
            return dataclasses.replace(found, receipts=(*earlier, (send_id, grown)))

        self._change(team_id, recording_id, add)

    def committed(self, team_id: str, recording_id: str, spent: brain_usage.Spent) -> None:
        """Keep the usage of the send whose reply just committed, so a Routine the span later creates counts it."""

        def keep(found: Span) -> Span:
            *earlier, (send_id, latest) = found.receipts
            return dataclasses.replace(found, receipts=(*earlier, (send_id, dataclasses.replace(latest, spent=spent))))

        self._change(team_id, recording_id, keep)

    def asked(self, team_id: str, recording_id: str, question: routine_recording.Question, intent: Intent) -> None:
        """Keep what the span asked, what it binds and settled, and the intent the person's answer records again."""

        def keep(found: Span) -> Span:
            asked = routine_recording.Asked(
                question.code,
                len(found.sends),
                question.pending,
                question.manifest,
                question.wire(),
                question.chosen,
                question.repair_from,
            )
            return dataclasses.replace(found, asked=asked, intent=intent)

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


# The Team steps a Routine's creation lists of its recording, the protocol's bound.
MAX_CREATION_STEPS = http_routine_notice.MAX_CREATION_STEPS


def _shown(span: Span) -> list[Receipt]:
    """The receipts a creation from the span shows: every committed earlier send's, then the send in progress."""
    *earlier, (_send_id, latest) = span.receipts
    return [receipt for _id, receipt in earlier if receipt.spent is not None] + [latest]


def creation_steps(span: Span) -> list[dict[str, object]]:
    """The Team steps a Routine created now from the span lists, in order, numbered from 1, within their bound."""
    steps = [step for receipt in _shown(span) for step in receipt.steps][:MAX_CREATION_STEPS]
    return [{"seq": position, **step} for position, step in enumerate(steps, 1)]


Creation = tuple[dict[str, object] | None, list[dict[str, object]]]


def creation(span: Span, current: brain_usage.Spent | None) -> Creation:
    """What a Routine created from the span shows it spent: its sends' summed usage, and their Team steps.

    ``current`` is the usage of the send in progress, whose reply carries the card; its reply has not committed yet.
    """
    spent = [receipt.spent for receipt in _shown(span)[:-1]] + ([] if current is None else [current])
    return brain_usage.Spent.total(spent).wire(), creation_steps(span)


def answered(span: Span | None) -> Intent | None:
    """The intent to record again when the span's latest send is Admin's composed answer that binds its question.

    Only a target question binds, when the answer is exactly one target's JSON text and states no schedule. Any other
    answer is the Brain's to read: it records again, and a settings answer runs no Action again.
    """
    if span is None or span.intent is None or span.asked is None or span.refused:
        return None
    parsed = routine_recording.clarified_answer(span.sends[-1].message)
    if parsed is None:
        return None
    _original, _question, answer = parsed
    if phrase.stated(answer):
        return None
    pending = span.asked.pending
    bound = (
        span.asked.code == "routine-binding-ambiguous"
        and pending is not None
        and any(answer == routine_recording.json_text(target) for target, _label in pending.targets)
    )
    return span.intent if bound else None


def continued(span: Span, replaces: str | None) -> Intent | None:
    """The stored intent a record call continues, or None when it starts a new one.

    A record call whose send ran no Action and that replaces exactly what the stored intent replaces only says more
    about the same work, whatever it now names the Routine: the person's answers and any unrelated send between them
    never replace that work, even once it left the bounded span, which admission then refuses, and a replacement stays
    bound to the revision its Routine was first shown at. A send that ran Actions, or a call naming another Routine to
    replace, is a new intent.
    """
    stored = span.intent
    if stored is None or span.sends[-1].occurrences or stored.replaces != replaces:
        return None
    return stored


def selected_work(span: Span, replaces: str | None) -> str | None:
    """The send whose calls a new intent's work is, by id, which the intent then keeps.

    A send that ran Actions selects itself. A record call that ran nothing and continues no stored intent selects the
    latest send that ran any Action, except that a replacement then keeps its steps (None).
    """
    if span.sends[-1].occurrences:
        return span.recording_id
    index = None if replaces is not None else routine_recording.latest_work(span.sends)
    return None if index is None else span.send_ids[index]


def settings_answer(span: Span | None) -> bool:
    """Whether the span's latest send is exactly the person's pick of one fixed answer to a settings question.

    A settings question is Team's question about how often the Routine runs, its interval over the Team's budget, or
    what is done with each run's result. Only Admin's composed answer whose answer is one of that question's fixed
    labels, in any interface language, qualifies: the Brain then only records again. A typed answer, one that also asks
    for anything else, and an answer to a target or rerun question are the Brain's to read with every Action, so new
    work they name still runs.
    """
    if span is None or span.asked is None or span.asked.code not in http_routine_proposal.SETTINGS_ANSWERS:
        return False
    parsed = routine_recording.clarified_answer(span.sends[-1].message)
    if parsed is None:
        return False
    # Only the over-budget answers hold the question's interval, which its value fills.
    seconds = str((span.asked.wire or {}).get("value"))
    offered = http_routine_proposal.SETTINGS_ANSWERS[span.asked.code]
    return parsed[2] in {answer.replace("{seconds}", seconds) for answer in offered}


def stated_schedule(span: Span | None) -> dict[str, object] | None:
    """The one schedule the span's person states, as Team records it; the Brain admits a settings answer beside it."""
    found = () if span is None else routine_recording.stated_schedules(span.sends)
    return dict(found[0]) if len(found) == 1 else None


def stated_output(span: Span | None) -> str | None:
    """The one output mode the span's person states, as Team records it; a settings answer is admitted beside it."""
    return None if span is None else routine_recording.stated_output(span.sends)


def routine_mode(span: Span | None) -> bool:
    """Whether the Brain should treat this chat as about a Routine: advisory only, and read with no model.

    It is when any segment the person wrote in the span states a schedule or asks for a Routine by name, or the span
    holds a record intent, a pending Team question, or a selected Routine (ADR-0122). The Brain still reads what the
    current message asks before it acts or records.
    """
    if span is None:
        return False
    if span.intent is not None or span.asked is not None or span.selected is not None:
        return True
    return any(
        phrase.stated(segment) or phrase.requests_routine(segment) for send in span.sends for segment in send.person
    )


def rerun_work(span: Span | None) -> tuple[dict[str, object], ...] | None:
    """The work a pending rerun question asks the Brain to run again, in order, bounded and redacted.

    Each entry is one call, or ``count`` consecutive identical ones: its Action and each input member's kind. A value
    input shows its exact JSON text unless the span protects it or it is too long, and whether it is a target the
    person chose; a fresh input shows only the Action Team found its earlier value in, which must run again first.
    Work past the protocol's bounds (``routine_context.canonical_rerun``), or of a span whose protection was lost, is
    not shown at all.
    """
    if span is None or span.asked is None or span.asked.manifest is None or span.asked.code != routine_recording.RERUN:
        return None
    if span.protection.lost:
        # With its protection lost, nothing the span holds may be shown.
        return None
    entries: list[dict[str, object]] = []
    for slot in span.asked.manifest.slots:
        entry = {
            "assistant": slot.action[0],
            "action": slot.action[1],
            "count": 1,
            "inputs": [_rerun_input(span, slot, item) for item in slot.inputs],
        }
        if entries and {**entries[-1], "count": 1} == entry:
            entries[-1]["count"] += 1
        else:
            entries.append(entry)
    return tuple(entries) if http_routine_context.canonical_rerun(entries) is not None else None


def _rerun_input(span: Span, slot: routine_recording.Slot, item: tuple[str, str, object]) -> dict[str, object]:
    member, kind, value = item
    shown = {"member": member, "kind": kind, "value": None, "chosen": False, "source": None}
    if kind == "value":
        text = routine_recording.json_text(value)
        # The value itself is checked, every string and key in it: escaping its JSON text could hide a protected one.
        visible = len(text) <= http_routine_context.MAX_RERUN_LITERAL_CHARS and not trace.exposes(
            value, span.protection.values
        )
        chosen = any(
            (binding.action, binding.member) == (slot.action, member)
            and routine_recording.json_text(binding.chosen) == text
            for binding in span.asked.chosen
        )
        return {**shown, "value": text if visible else None, "chosen": chosen}
    if kind == "fresh":
        found = dict(slot.sources).get(member)
        source = None if found is None else {"assistant": found[0], "action": found[1]}
        return {**shown, "source": source}
    return shown


def labeled(span: Span | None, results: dict[str, object]) -> dict[str, object]:
    """The Action results a recording send returns to its Brain, each traced call's named by its Team handle.

    Only while the span selects a Routine, which the Brain may refine with this send's traced calls (ADR-0122): the
    send's k-th traced call is ``c{k}``, and its result reaches the Brain as ``{"call": "c{k}", "result": ...}``. A
    failed call is never traced, so its result stays as it is; so does every result of a send that selects nothing.
    """
    if span is None or span.selected is None:
        return results
    handles = {item.interrupt: f"c{place}" for place, item in enumerate(span.sends[-1].occurrences, start=1)}
    return {
        interrupt: {"call": handles[interrupt], "result": value} if interrupt in handles else value
        for interrupt, value in results.items()
    }


class AnsweredRuntime:
    """The runtime of a send Team records itself: it never asks the Brain, and its reply is empty.

    The card, question, or refusal the recording ends with says everything.
    """

    def __init__(self, intent: Intent) -> None:
        self._turn = brain_runtime_client.RuntimeTurn("completed", "", (), routine=intent)

    def start(self, _context: object, _message: str, *, conversation: object) -> object:
        return self._turn


def teed(sink: Callable[[dict[str, object]], None], keep: Callable[[dict[str, object]], None]):
    """A progress sink that also hands each event to ``keep`` first, which never raises."""

    def both(event: dict[str, object]) -> None:
        keep(event)
        sink(event)

    return both


def recorded(book: RecordingBook, recording: tuple[str, str], call: tuple, invoke: Callable[[], object]) -> object:
    """Run one Action call of a recording send, keeping it as an occurrence when it succeeds.

    ``call`` is the active Assistant, the Action request, and the values Team injected into it.
    Those values are protected before the RPC; the result's secret strings are protected before the result goes on.
    The Action's own errors pass through, and a failed call is never an occurrence.
    """
    team_id, recording_id = recording
    active, action_request, injected = call
    action = active.spec.actions[action_request.action]
    book.protect(team_id, recording_id, injected)
    result = invoke()
    book.protect(team_id, recording_id, trace.secret_values(result, action.output_schema))
    found = book.get(team_id, recording_id)
    protected = () if found is None else tuple(found.protection.values)
    pin = routine_pin.action_pins(active.spec, (action_request.action,))[action_request.action]
    occurrence = trace.Occurrence(
        active.spec.assistant_id,
        action_request.action,
        pin,
        action.effect == action_effect.READ_ONLY,
        trace.keep(dict(action_request.input), action.input_schema, protected),
        trace.keep(result, action.output_schema, protected),
        interrupt=action_request.interrupt_id,
    )
    book.occurred(team_id, recording_id, occurrence)
    return result
