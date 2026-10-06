"""A person's Routine draft: the short-term memory of a Routine still being set up (ADR-0092 amendment, 2026-10-05).

When the planner needs something the person has not said yet, it asks instead of refusing, and Team keeps what the
person said so far: their own messages and answers while setting the Routine up, and the earlier sends those cited, in
order, with the question last asked. The next message or answer then continues it, so nothing already said is typed
again. Only the person's own admitted words ever enter a draft: never a reply, an Action result, a file, memory, the
conversation projection, a question's text, or an unselected option.

One draft per Team and person is sealed apart from plaintext Routine state, bound to the Team and person, and lives 30
minutes from its last change: long enough to look something up or to outlast a Team restart or release, short enough
that an abandoned one never merges into an unrelated request much later. Creating a Routine, discarding the draft, and
deleting or resetting the Team remove it; the watchdog removes an expired one.

Every write mints a new generation. A request freezes the generation it saw when it was first admitted, and every change
to the draft, under the Team's Routine lock, first proves the person's draft still has exactly that generation, absence
included; otherwise the request is stale and changes nothing.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Callable
from http import HTTPStatus

from assistant import manifest as assistant_manifest
from local.errors import ApiProblemError as ApiProblem
from local.routine import lineage as routine_lineage
from local.routine import state as routine_state
from local.routine import store as routine_store
from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import strict_json
from routine import plan as routine_plan
from routine import request as routine_request
from routine.request import Draft
from routine.request import Request as RoutineRequest

VERSION = 1
DRAFT_SECONDS = 1_800
_FIELDS = frozenset({"version", "incarnation", "generation", "parts", "question", "updated_at"})
_GENERATION_CHARS = 32
_HEX = frozenset("0123456789abcdef")


def _hex(value: object, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and set(value) <= _HEX


def storable(parts: tuple[routine_request.Part, ...]) -> bool:
    """Whether parts fit a draft whole: at most 8 canonical texts, 32,000 characters, none resembling a credential."""
    return (
        0 < len(parts) <= routine_request.MAX_DRAFT_PARTS
        and all(
            isinstance(kind, str)
            and kind in routine_request.KINDS
            and routine_request.canonical_text(text, routine_request.MAX_MESSAGE_CHARS) is not None
            and not assistant_manifest.resembles_credential(text)
            for kind, text in parts
        )
        and sum(len(text) for _kind, text in parts) <= routine_request.MAX_DRAFT_CHARS
    )


def encode(draft: Draft, updated_at: int) -> bytes:
    question = None if draft.question is None else {"text": draft.question[0], "asked": draft.question[1]}
    return routine_plan.canonical(
        {
            "version": VERSION,
            "incarnation": draft.incarnation,
            "generation": draft.generation,
            "parts": [{"kind": kind, "text": text} for kind, text in draft.parts],
            "question": question,
            "updated_at": updated_at,
        }
    )


def _parts(value: object) -> tuple[routine_request.Part, ...] | None:
    if not isinstance(value, list):
        return None
    parts = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"kind", "text"}:
            return None
        parts.append((item["kind"], item["text"]))
    parts = tuple(parts)
    return parts if storable(parts) else None


# Two placeholder options, only to hold a question text to the clarification protocol's exact rule.
_PROBE = ({"label": "a", "description": ""}, {"label": "b", "description": ""})


def _question(value: object) -> tuple[str, str] | None | bool:
    """The question as (text, asked), None for none, or False when malformed."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"text", "asked"} or not _hex(value["asked"], 64):
        return False
    probe = {"question": value["text"], "options": list(_PROBE), "default_index": None}
    if http_payload.canonical_clarification(probe) is None:
        return False
    return value["text"], value["asked"]


def decode(payload: bytes) -> tuple[Draft, int]:
    """A sealed draft and the instant it was last written; anything else is proven unusable."""
    try:
        value = strict_json.loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise routine_store.RoutineRecordInvalidError("Routine draft is malformed") from exc
    if not isinstance(value, dict) or set(value) != _FIELDS or value["version"] != VERSION:
        raise routine_store.RoutineRecordInvalidError("Routine draft is malformed")
    parts, question = _parts(value["parts"]), _question(value["question"])
    updated_at, incarnation = value["updated_at"], value["incarnation"]
    if (
        parts is None
        or question is False
        or type(updated_at) is not int
        or updated_at < 0
        or not isinstance(incarnation, str)
        or not incarnation
        or not _hex(value["generation"], _GENERATION_CHARS)
    ):
        raise routine_store.RoutineRecordInvalidError("Routine draft is malformed")
    draft = Draft(value["generation"], incarnation, parts, question)
    if encode(draft, updated_at) != payload:
        raise routine_store.RoutineRecordInvalidError("Routine draft is malformed")
    return draft, updated_at


def _live(self, team_id: str, principal: str, now: int) -> Draft | None:
    """The person's unexpired draft, removing a proven unusable or expired one; the caller holds the Routine lock."""
    store = self.routine_store
    try:
        payload = store.draft(team_id, principal)
        if payload is None:
            return None
        draft, updated_at = decode(payload)
    except routine_store.RoutineRecordInvalidError:
        routine_state.call(lambda: store.delete_draft(team_id, principal))
        return None
    except routine_store.RoutineStoreError as exc:
        raise routine_state.unavailable() from exc
    if not updated_at <= now + http_payload.REQUEST_IDENTITY_SKEW_SECONDS or now >= updated_at + DRAFT_SECONDS:
        routine_state.call(lambda: store.delete_draft(team_id, principal))
        return None
    return draft


def current(self, team_id: str, principal: str) -> Draft | None:
    """The person's live draft, as a request first admitted now would freeze it."""
    with self.routine_store.lock(team_id):
        return _live(self, team_id, principal, int(time.time()))


def stale() -> ApiProblem:
    return ApiProblem(
        HTTPStatus.CONFLICT, "this request can no longer change a Routine", code="routine-request-expired"
    )


def expect(self, team_id: str, request: RoutineRequest) -> None:
    """Prove the person's draft is exactly the one the request froze, absence included; the caller holds the lock."""
    expected = None if request.draft is None else request.draft.generation
    live = _live(self, team_id, request.principal, int(time.time()))
    if (None if live is None else live.generation) != expected:
        raise stale()


def save(
    self,
    team_id: str,
    request: RoutineRequest,
    network_id: str,
    words: tuple[tuple[routine_request.Part, ...], str],
    before: Callable[[], None] = lambda: None,
) -> str | None:
    """Keep the words and the question just asked as the person's draft; returns its new generation.

    ``words`` are the parts and the question's text. ``before`` runs after the draft is proven current and before it
    changes, so a failure there leaves the draft exactly as the request froze it and the request retryable. Parts that
    do not fit a draft whole are never cut down: the person's draft is removed instead, so a later answer cannot
    continue it and the person states the Routine again. Returns None then.
    """
    parts, asked = words
    store = self.routine_store
    with store.lock(team_id):
        expect(self, team_id, request)
        before()
        if not storable(parts):
            routine_state.call(lambda: store.delete_draft(team_id, request.principal))
            return None
        generation = secrets.token_hex(_GENERATION_CHARS // 2)
        draft = Draft(generation, network_id, parts, (asked, hashlib.sha256(request.message.encode()).hexdigest()))
        payload = encode(draft, int(time.time()))
        routine_state.call(lambda: store.put_draft(team_id, request.principal, payload))
        return generation


def discard(self, team_id: str, request: RoutineRequest, before: Callable[[], None] = lambda: None) -> None:
    """Remove the person's draft, exactly the one the request froze; ``before`` runs once that is proven."""
    with self.routine_store.lock(team_id):
        expect(self, team_id, request)
        before()
        routine_state.call(lambda: self.routine_store.delete_draft(team_id, request.principal))


def answer(draft: Draft | None, message: str) -> str | None:
    """The answer a composed reply gives to exactly the draft's question, or None.

    The reply is the message the question was asked about, a blank line, the question line, and the answer line; only
    the answer itself, canonical and at most 4,000 characters, is ever taken. The question's text and the original
    message never become words.
    """
    if draft is None or draft.question is None:
        return None
    text, asked = draft.question
    head, separator, tail = message.rpartition("\n\n")
    lines = tail.split("\n")
    if not separator or len(lines) != 2 or hashlib.sha256(head.encode()).hexdigest() != asked:
        return None
    question_line, answer_line = lines
    label = question_line[: -len(text) - 2]
    if not question_line.endswith(": " + text) or not routine_lineage.label(label):
        return None
    answer_label, colon, given = answer_line.partition(": ")
    if not colon or not routine_lineage.label(answer_label):
        return None
    return routine_request.canonical_text(given, routine_request.MAX_ANSWER_CHARS)
