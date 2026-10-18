"""The Browser agent's closed step set and the `browser-step` exchange Team and the Brain share (ADR-0117 §5).

Team owns this module; the Brain consumes it as a pinned mirror. The Browser agent answers one step at a time from
the closed set below, and both sides admit a step only in its canonical form. The model answers through the flat
schema `STEP_SCHEMA`, every field present and the unused ones null, which `canonical_step` turns into the compact
step for its kind or refuses.
"""

from __future__ import annotations

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
MAX_HISTORY = 40
MAX_OBSERVATION_BYTES = 24 * 1024
LANGUAGES = frozenset({"en", "pt-BR"})
REASONS = ("sign-in", "captcha", "payment", "confirmation", "blocked")
DIRECTIONS = ("up", "down")
OUTCOMES = ("ok", "refused")
REF_RE = re.compile(r"p[0-9]{1,6}:[0-9]{1,6}")
# Every field each step kind carries besides `kind`.
FIELDS = {
    "navigate": ("url",),
    "click": ("ref",),
    "type": ("ref", "text"),
    "scroll": ("direction",),
    "wait": (),
    "screenshot": (),
    "done": ("summary",),
    "needs_person": ("reason", "sentence"),
}
ALL_FIELDS = ("url", "ref", "text", "direction", "summary", "reason", "sentence")


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


def canonical_history(value: object) -> list[dict[str, object]] | None:
    """At most 40 earlier steps, each with its closed outcome and the page it was chosen on."""
    if not isinstance(value, list) or len(value) > MAX_HISTORY:
        return None
    history = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"step", "outcome", "page"} or entry["outcome"] not in OUTCOMES:
            return None
        step = canonical_step(entry["step"])
        page = _page(entry["page"])
        if step is None or step != entry["step"] or step["kind"] in {"done", "needs_person"} or page is None:
            return None
        history.append({"step": step, "outcome": entry["outcome"], "page": page})
    return history


def canonical_observation(value: object) -> str | None:
    """The bounded page text Team observed; it is untrusted content, never instructions."""
    if not isinstance(value, str) or len(value.encode()) > MAX_OBSERVATION_BYTES:
        return None
    return value


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
