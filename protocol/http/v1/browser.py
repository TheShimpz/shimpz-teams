"""The Browser agent's closed step set and the `browser-step` exchange Team and the Brain share (ADR-0117 §5).

Team owns this module; the Brain consumes it as a pinned mirror. The Browser agent answers one step at a time from
the closed set below, and both sides admit a step only in its canonical form. The model answers through the flat
schema `STEP_SCHEMA`, every field present and the unused ones null, which `canonical_step` turns into the compact
step for its kind or refuses.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

if __package__:
    from . import payload
else:  # The protocol verifier runs every module of this directory flat.
    import payload

MAX_OBJECTIVE_CHARS = 2_000
MAX_SUMMARY_BYTES = 4 * 1024
MAX_SENTENCE_CHARS = 300
MAX_TEXT_CHARS = 2_000
MAX_URL_CHARS = 2_048
# A step's note: what the agent read on the page that the objective needs, kept in its history.
MAX_NOTE_CHARS = 300
MAX_TITLE_CHARS = 300
MAX_OBSERVATION_BYTES = 24 * 1024
# A task's step ceiling; a thread never holds more steps (ADR-0117, 2026-10-22 amendment).
MAX_STEPS = 300
# A read step opens at most this many links of its page in turn.
MAX_READ_REFS = 10
# The thread a step request carries: its history (the earlier-steps message plus the unfolded steps) within
# MAX_HISTORY_BYTES; one fold note, all notes, and the folded step lines each within their own bound.
MAX_HISTORY_BYTES = 48 * 1024
MAX_FOLD_NOTE_BYTES = 8 * 1024
MAX_NOTES_BYTES = 16 * 1024
MAX_FOLDED_LINE_BYTES = 300
MAX_FOLDED_BYTES = 8 * 1024
# Each thread entry's rendering adds at most this much to its fields' bytes (headings, markers, its step record).
ENTRY_OVERHEAD_BYTES = 512
# The whole text a request renders to: its history, its current page, its objective, and their framing.
MAX_REQUEST_TEXT_BYTES = 88 * 1024
# What one Brain call of a Browser task may cost, so Team reserves its worst case before sending it: the provider
# attempts the Brain makes (its SDK retries included), the input it admits, and the output it lets a model write.
MAX_CALL_ATTEMPTS = 3
MAX_CALL_INPUT_TOKENS = 100_000
MAX_CALL_OUTPUT_TOKENS = 4_096
LANGUAGES = frozenset({"en", "pt-BR"})
REASONS = ("sign-in", "captcha", "payment", "confirmation", "blocked")
DIRECTIONS = ("up", "down")
# A step's outcome: done, refused with no effect, a navigation whose page did not load for a network error, an action
# under way when the Driver session failed, whose effect is unknown, or a done answer Team returned to the agent with a
# note of what it still needs.
OUTCOMES = ("ok", "refused", "navigation-failed", "interrupted", "returned")
REF_RE = re.compile(r"p[0-9]{1,6}:[0-9]{1,6}")
# Every field each step kind carries besides `kind`.
FIELDS = {
    "navigate": ("url",),
    "click": ("ref",),
    "type": ("ref", "text"),
    "scroll": ("direction",),
    "wait": (),
    "screenshot": (),
    "read": ("refs",),
    "done": ("summary",),
    "needs_person": ("reason", "sentence"),
}
ALL_FIELDS = ("url", "ref", "refs", "text", "direction", "summary", "reason", "sentence")


def _nullable(schema: dict[str, object]) -> dict[str, object]:
    return {"anyOf": [schema, {"type": "null"}]}


STEP_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", *ALL_FIELDS, "note"],
    "properties": {
        "kind": {"type": "string", "enum": list(FIELDS)},
        "url": _nullable({"type": "string", "maxLength": MAX_URL_CHARS}),
        "ref": _nullable({"type": "string", "maxLength": 16}),
        "refs": _nullable({"type": "array", "maxItems": MAX_READ_REFS, "items": {"type": "string", "maxLength": 16}}),
        "text": _nullable({"type": "string", "maxLength": MAX_TEXT_CHARS}),
        "direction": _nullable({"type": "string", "enum": list(DIRECTIONS)}),
        "summary": _nullable({"type": "string", "maxLength": MAX_SUMMARY_BYTES}),
        "reason": _nullable({"type": "string", "enum": list(REASONS)}),
        "sentence": _nullable({"type": "string", "maxLength": MAX_SENTENCE_CHARS}),
        "note": _nullable({"type": "string", "maxLength": MAX_NOTE_CHARS}),
    },
}


def _plain(value: object, limit: int, *, multiline: bool = False) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return False
    allowed = {"\n", "\t"} if multiline else set()
    return all(character.isprintable() or character in allowed for character in value)


def canonical_url(value: object) -> str | None:
    """An absolute `https` or `http` URL with a host and no credentials; the Browser's policy upgrades `http`."""
    if not isinstance(value, str) or not value or len(value) > MAX_URL_CHARS or not value.isprintable():
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
        return None
    if " " in value:
        return None
    return value


def canonical_objective(value: object) -> str | None:
    """A self-contained task objective of at most 2,000 characters."""
    return value.strip() if _plain(value, MAX_OBJECTIVE_CHARS, multiline=True) else None


def _ref(value: object) -> str | None:
    return value if isinstance(value, str) and REF_RE.fullmatch(value) else None


def _refs(value: object) -> list[str] | None:
    """One to ten distinct refs, in the order the step names them."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_READ_REFS or len(set(map(str, value))) != len(value):
        return None
    return list(value) if all(_ref(ref) is not None for ref in value) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) <= MAX_TEXT_CHARS and value.isprintable() else None


def _summary(value: object) -> str | None:
    fits = _plain(value, MAX_SUMMARY_BYTES, multiline=True) and len(str(value).encode()) <= MAX_SUMMARY_BYTES
    return value if fits else None


def _sentence(value: object) -> str | None:
    return value if _plain(value, MAX_SENTENCE_CHARS) else None


def _member(choices: tuple[str, ...]):
    return lambda value: value if value in choices else None


# How each field of a step is admitted; a field's check returns its canonical value or None.
FIELD_CHECKS = {
    "url": canonical_url,
    "ref": _ref,
    "refs": _refs,
    "text": _text,
    "direction": _member(DIRECTIONS),
    "summary": _summary,
    "reason": _member(REASONS),
    "sentence": _sentence,
}


def canonical_step(value: object) -> dict[str, object] | None:
    """The compact step a flat or compact answer names, or None for anything outside the closed set.

    Any step may carry a `note`; a null or blank one is no note.
    """
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        return None
    kind = value["kind"]
    fields = FIELDS.get(kind)
    if fields is None:
        return None
    extra = set(value) - {"kind", *ALL_FIELDS, "note"}
    unused = [name for name in ALL_FIELDS if name not in fields and value.get(name) is not None]
    if extra or unused:
        return None
    step: dict[str, object] = {"kind": kind}
    for name in fields:
        admitted = FIELD_CHECKS[name](value.get(name))
        if admitted is None:
            return None
        step[name] = admitted
    note = value.get("note")
    if isinstance(note, str) and not note.strip():
        note = None
    if note is not None:
        if not _plain(note, MAX_NOTE_CHARS):
            return None
        step["note"] = note
    return step


def _page(value: object) -> dict[str, str] | None:
    """The URL and title of the page a step was chosen on, as Team observed them: one bounded line each."""
    if not isinstance(value, dict) or set(value) != {"url", "title"}:
        return None
    url, title = value["url"], value["title"]
    if not isinstance(url, str) or len(url) > MAX_URL_CHARS or not url.isprintable():
        return None
    if not isinstance(title, str) or len(title) > MAX_TITLE_CHARS or not title.isprintable():
        return None
    return {"url": url, "title": title}


def canonical_record(value: object) -> dict[str, object] | None:
    """One earlier step: the admitted step, its closed outcome, and the page it was chosen on."""
    if not isinstance(value, dict) or set(value) != {"step", "outcome", "page"} or value["outcome"] not in OUTCOMES:
        return None
    step = canonical_step(value["step"])
    page = _page(value["page"])
    if step is None or step != value["step"] or step["kind"] in {"done", "needs_person"} or page is None:
        return None
    return {"step": step, "outcome": value["outcome"], "page": page}


def _entry(value: object) -> dict[str, object] | None:
    """One unfolded step: its record, the observation it was chosen on, the downloads, and whether a screenshot came.

    Only the current page carries the image itself.
    """
    if not isinstance(value, dict) or set(value) != {"step", "outcome", "page", "text", "downloads", "screenshot"}:
        return None
    record = canonical_record({key: value[key] for key in ("step", "outcome", "page")})
    downloads = value["downloads"]
    if (
        record is None
        or canonical_observation(value["text"]) is None
        or (downloads is not None and canonical_downloads(downloads) != downloads)
        or type(value["screenshot"]) is not bool
    ):
        return None
    return {**record, "text": value["text"], "downloads": downloads, "screenshot": value["screenshot"]}


def _count(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_STEPS * MAX_STEPS


def _bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def entry_bytes(entry: dict[str, object]) -> int:
    """At most what one unfolded step renders to."""
    page = entry["page"]
    return (
        len(str(entry["text"]).encode())
        + _bytes(entry["step"])
        + len(str(page["url"]).encode())
        + len(str(page["title"]).encode())
        + _bytes(entry["downloads"])
        + ENTRY_OVERHEAD_BYTES
    )


def history_bytes(thread: dict[str, object]) -> int:
    """At most what a thread's history renders to: its folded lines, its notes, and its unfolded steps."""
    folded = len(thread["folded"]) * (MAX_FOLDED_LINE_BYTES + 1)
    notes = sum(len(str(note).encode()) + 2 for note in thread["notes"])
    return ENTRY_OVERHEAD_BYTES + folded + notes + sum(entry_bytes(entry) for entry in thread["steps"])


def request_text_bytes(objective: str, thread: dict[str, object], observation: str, downloads: object) -> int:
    """At most the text a step request renders to; a fold, check, or final request adds its own instruction."""
    return (
        len(objective.encode())
        + history_bytes(thread)
        + len(observation.encode())
        + _bytes(downloads)
        + ENTRY_OVERHEAD_BYTES
    )


def canonical_thread(value: object) -> dict[str, object] | None:
    """A task's thread, oldest first: folded step records, fold notes, and unfolded steps, each within its bound.

    Each list's dropped items are counted; the history stays within MAX_HISTORY_BYTES.
    """
    keys = {"folded", "folded_omitted", "notes", "notes_omitted", "steps"}
    if not isinstance(value, dict) or set(value) != keys:
        return None
    folded, notes, steps = value["folded"], value["notes"], value["steps"]
    if (
        not isinstance(folded, list)
        or not isinstance(notes, list)
        or not isinstance(steps, list)
        or len(folded) + len(steps) > MAX_STEPS
        or not _count(value["folded_omitted"])
        or not _count(value["notes_omitted"])
    ):
        return None
    records = [canonical_record(item) for item in folded]
    entries = [_entry(item) for item in steps]
    if any(item is None for item in (*records, *entries)) or not all(canonical_note(note) == note for note in notes):
        return None
    thread = {
        "folded": records,
        "folded_omitted": value["folded_omitted"],
        "notes": list(notes),
        "notes_omitted": value["notes_omitted"],
        "steps": entries,
    }
    if (
        len(records) * (MAX_FOLDED_LINE_BYTES + 1) > MAX_FOLDED_BYTES
        or sum(len(note.encode()) for note in notes) > MAX_NOTES_BYTES
        or history_bytes(thread) > MAX_HISTORY_BYTES
    ):
        return None
    return thread


def canonical_note(value: object) -> str | None:
    """One fold note: the agent's own plain-text record of pages leaving its view, untrusted like the pages."""
    if not _plain(value, MAX_FOLD_NOTE_BYTES, multiline=True) or len(str(value).encode()) > MAX_FOLD_NOTE_BYTES:
        return None
    return str(value)


def canonical_observation(value: object) -> str | None:
    """The bounded page text Team observed; it is untrusted content, never instructions."""
    if not isinstance(value, str) or len(value.encode()) > MAX_OBSERVATION_BYTES:
        return None
    return value


# A finished task's completeness as the Brain's advisory check judged it, or not checked at all.
COMPLETENESS = ("complete", "incomplete", "not-checked")
# A task result lists at most this many cited URLs that provenance screening could not match, and counts the rest.
MAX_UNSCREENED_URLS = 20


def canonical_check(value: object) -> dict[str, object] | None:
    """The completeness check's verdict: complete, or incomplete with one sentence, or null when not checked."""
    if not isinstance(value, dict) or set(value) != {"complete", "missing"}:
        return None
    complete, missing = value["complete"], value["missing"]
    if complete is True or complete is None:
        return {"complete": complete, "missing": None} if missing is None else None
    if complete is False and _sentence(missing) is not None:
        return {"complete": False, "missing": missing}
    return None


# The downloads a step sees: at most this many completed file names, and at most this many still downloading counted.
MAX_DOWNLOAD_NAMES = 20
MAX_PENDING_DOWNLOADS = 1_000


def canonical_downloads(value: object) -> dict[str, object] | None:
    """The completed files present in the task's Browser, how many still download, and whether names were left out.

    Names come from websites: untrusted, each one plain line of at most 255 UTF-8 bytes, sorted and distinct. A
    listing Team could not read is null in the request, never an empty one.
    """
    if not isinstance(value, dict) or set(value) != {"files", "pending", "truncated"}:
        return None
    files, pending, truncated = value["files"], value["pending"], value["truncated"]
    if (
        not isinstance(files, list)
        or len(files) > MAX_DOWNLOAD_NAMES
        or not all(isinstance(name, str) and name and _plain(name, MAX_NAME_BYTES) for name in files)
        or any(len(name.encode()) > MAX_NAME_BYTES for name in files)
        or files != sorted(set(files))
        or type(pending) is not int
        or not 0 <= pending <= MAX_PENDING_DOWNLOADS
        or type(truncated) is not bool
    ):
        return None
    return {"files": list(files), "pending": pending, "truncated": truncated}


TERMINAL_LOSSES = frozenset(
    {"incomplete", "over-limit", "empty", "quota-exceeded", "name-refused", "store-unavailable", "browser-lost"}
)
MAX_TERMINAL_FILES = 8
MAX_TERMINAL_LOSSES = 128
MAX_TERMINAL_SKIPPED = 1_000
MAX_NAME_BYTES = 255


def canonical_file_reference(value: object) -> dict[str, object] | None:
    """One kept download as a reply shows it: its id, literal name, Team-determined media type, and size."""
    if not isinstance(value, dict) or set(value) != {"id", "name", "media_type", "size"}:
        return None
    size = value["size"]
    if (
        payload.canonical_file_id(value["id"]) is None
        or payload.canonical_filename(value["name"]) != value["name"]
        or payload.canonical_media_type(value["media_type"]) != value["media_type"]
        or type(size) is not int
        or not 1 <= size <= payload.MAX_FILE_UPLOAD_BYTES
    ):
        return None
    return {key: value[key] for key in ("id", "name", "media_type", "size")}


def _canonical_loss(value: object) -> dict[str, str] | None:
    """One named download a reply says it did not keep, with its closed reason."""
    if not isinstance(value, dict) or set(value) != {"name", "reason"} or value["reason"] not in TERMINAL_LOSSES:
        return None
    name = value["name"]
    if not isinstance(name, str) or not name or len(name.encode()) > MAX_NAME_BYTES or not name.isprintable():
        return None
    return {"name": name, "reason": value["reason"]}


def canonical_terminal(value: object) -> dict | None:
    """The `browser` field of a chat terminal: the kept files' references, the named losses, the unnamed count."""
    if not isinstance(value, dict) or set(value) != {"files", "lost", "skipped"}:
        return None
    files, lost, skipped = value["files"], value["lost"], value["skipped"]
    bounded = (
        isinstance(files, list)
        and len(files) <= MAX_TERMINAL_FILES
        and isinstance(lost, list)
        and len(lost) <= MAX_TERMINAL_LOSSES
        and type(skipped) is int
        and 0 <= skipped <= MAX_TERMINAL_SKIPPED
    )
    if not bounded:
        return None
    references = [canonical_file_reference(item) for item in files]
    losses = [_canonical_loss(item) for item in lost]
    if any(item is None for item in (*references, *losses)):
        return None
    return {"files": references, "lost": losses, "skipped": skipped}
