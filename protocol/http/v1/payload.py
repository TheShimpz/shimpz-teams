"""Pure Team wire contract shared by the Admin and Store backends."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

TEAM_ID_PATTERN = r"^[a-z0-9_]{1,40}$"
ASSISTANT_ID_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
ACTION_ID_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
FILE_ID_PATTERN = r"^[0-9a-f]{32}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
SOURCE_DIGEST_PATTERN = rf"^sha256:{SHA256_PATTERN[1:-1]}$"
ASSURANCE_HANDLE_PATTERN = r"^[A-Za-z0-9_-]{43}$"
MEDIA_TYPE_PATTERN = r"^[a-z0-9][a-z0-9!#$&^_.+\-]*/[a-z0-9][a-z0-9!#$&^_.+\-]*$"
ACCOUNT_SESSION_HEADER = "X-Shimpz-Account"
# A Stored Input's key page (Developers manifest `help_url`): one canonical public https URL that WHATWG URL
# serialization prints unchanged, with a path, an optional query, and no port, credentials, fragment, or dot segment.
HELP_URL_PATTERN = (
    r"^https://(?=[^/]{1,253}/)"
    r"(?![^/]*\.(?:arpa|example|home|internal|invalid|lan|local|localdomain|localhost|onion|test)/)"
    r"(?:(?!xn--)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?!xn--)[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:/(?!\.\.?(?:/|\?|(?![\s\S])))(?:[A-Za-z0-9._~!$&()*+,;=:@-]|%(?!2E)[0-9A-F]{2})*)+"
    r"(?:\?(?:[A-Za-z0-9._~!$&()*+,;=:@/?-]|%[0-9A-F]{2})+)?(?![\s\S])"
)
MAX_HELP_URL_CHARS = 2_048
# The Brain's task-bound sentence for why an Action pauses for a person (ADR-0090).
MAX_PURPOSE_CHARS = 280
# The rendered copy bounds of a human request's catalog references (Assistant Spec v1, ADR-0091).
RENDERED_FIELD_CHARS = {"title": 80, "description": 500, "label": 80, "placeholder": 120}
RENDERED_OPTION_CHARS = {"label": 80, "description": 160}

TEAM_ID_RE = re.compile(TEAM_ID_PATTERN)
ASSISTANT_ID_RE = re.compile(ASSISTANT_ID_PATTERN)
ACTION_ID_RE = re.compile(ACTION_ID_PATTERN)
FILE_ID_RE = re.compile(FILE_ID_PATTERN)
SHA256_RE = re.compile(SHA256_PATTERN)
SOURCE_DIGEST_RE = re.compile(SOURCE_DIGEST_PATTERN)
ASSURANCE_HANDLE_RE = re.compile(ASSURANCE_HANDLE_PATTERN)
MEDIA_TYPE_RE = re.compile(MEDIA_TYPE_PATTERN)
HELP_URL_RE = re.compile(HELP_URL_PATTERN)

MAX_CHAT_MESSAGE_CHARS = 16_000
MAX_CHAT_FILES = 8
MAX_CHAT_ASSISTANTS = 16
MAX_TEAM_FILES = 256
MAX_TEAM_NAME_CHARS = 80
MAX_ACTION_LABEL_CHARS = 80
MAX_FILE_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_FILENAME_BYTES = 255
MAX_MEDIA_TYPE_CHARS = 127
# Committed presentation history carried by a Local Team chat turn (ADR-0065).
MAX_CONVERSATION_ENTRIES = 8
MAX_CONVERSATION_TEXT_CHARS = 512
MAX_CONVERSATION_CHARS = 4_096
MAX_CLARIFICATION_QUESTION_CHARS = 240
MAX_CLARIFICATION_LABEL_CHARS = 80
MAX_CLARIFICATION_DESCRIPTION_CHARS = 160
MIN_CLARIFICATION_OPTIONS = 2
MAX_CLARIFICATION_OPTIONS = 5
MAX_MEMORIES = 32
MAX_MEMORY_PREFERENCE_CHARS = 280
MEMORY_TOPIC_RE = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")
MAX_SKILLS = 8
MIN_SKILL_STEPS = 2
MAX_SKILL_STEPS = 16
MAX_SKILL_INPUTS = 32
SKILL_KEY_PREFIX = "procedure-"
SKILL_KEY_RE = re.compile(r"procedure-[0-9a-f]{12}\Z")
SKILL_INPUT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
CHAT_BODY_FIELDS = frozenset({"message", "files", "assistant_ids", "conversation", "locale"})
# The closed Admin interface languages a chat turn may name; a turn without one carries null (ADR-0090).
CHAT_LOCALES = frozenset({"ar", "de", "en", "es", "fr", "ja", "pt", "zh"})
SNAPSHOT_SUMMARY_FIELDS = frozenset({"locale", "summary"})
MAX_SNAPSHOT_SUMMARY_CHARS = 160
_LANGUAGE_LAYOUT_CONTROLS = frozenset({"\n", "\r", "\t"})


def _conversation_text(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_CONVERSATION_TEXT_CHARS
        or unicodedata.normalize("NFC", value) != value
        or value.strip() != value
        or any(
            unicodedata.category(character).startswith("C")
            and unicodedata.category(character) != "Cf"
            and character not in _LANGUAGE_LAYOUT_CONTROLS
            for character in value
        )
    ):
        return None
    return value


def canonical_conversation(value: object) -> list[dict[str, object]] | None:
    """Return one exact window of committed presentation history, or None when any bound or shape fails."""
    if not isinstance(value, list) or len(value) > MAX_CONVERSATION_ENTRIES:
        return None
    entries: list[dict[str, object]] = []
    for entry in value:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"role", "text", "truncated"}
            or not isinstance(entry["role"], str)
            or entry["role"] not in {"user", "assistant"}
            or not isinstance(entry["truncated"], bool)
            or _conversation_text(entry["text"]) is None
        ):
            return None
        entries.append({"role": entry["role"], "text": entry["text"], "truncated": entry["truncated"]})
    # Eight entries of at most 512 characters cannot exceed MAX_CONVERSATION_CHARS.
    return entries


def canonical_team_id(value: object) -> str | None:
    return value if isinstance(value, str) and TEAM_ID_RE.fullmatch(value) is not None else None


def canonical_assistant_id(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 80 or ASSISTANT_ID_RE.fullmatch(value) is None:
        return None
    return value


def canonical_action_id(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 128 or ACTION_ID_RE.fullmatch(value) is None:
        return None
    return value


def canonical_locale(value: object) -> str | None:
    """Return one closed interface language code, or None."""
    return value if isinstance(value, str) and value in CHAT_LOCALES else None


def canonical_help_url(value: object) -> str | None:
    """Return one exact Stored Input key page, or None."""
    if not isinstance(value, str) or len(value) > MAX_HELP_URL_CHARS or HELP_URL_RE.fullmatch(value) is None:
        return None
    return value


def canonical_purpose(value: object) -> str | None:
    """Return one plain single-line purpose sentence, or None.

    It carries no control, format, or line-separator character, no dash punctuation other than a hyphen inside a word,
    and nothing that reads as a link, so it can only explain, never point somewhere.
    """
    if (
        not isinstance(value, str)
        or unicodedata.normalize("NFC", value) != value
        or value.strip() != value
        or not 1 <= len(value) <= MAX_PURPOSE_CHARS
        or any(
            unicodedata.category(character)[0] == "C"
            or unicodedata.category(character) in {"Zl", "Zp"}
            or (unicodedata.category(character) == "Pd" and character != "-")
            for character in value
        )
        or " -" in value
        or "- " in value
        or "://" in value
        or "www." in value.casefold()
    ):
        return None
    return value


def canonical_pack_digest(value: object) -> str | None:
    """Return one `sha256:` language-pack digest (ADR-0091), or None."""
    return value if isinstance(value, str) and SOURCE_DIGEST_RE.fullmatch(value) else None


def canonical_rendered(value: object, request: object) -> dict[str, object] | None:
    """Return the rendered copy of exactly the canonical request's copy fields, or None (ADR-0091).

    The request keeps its catalog references, option values, and kind; this block carries only display text in the
    challenge's interface language, in the request's field and option order, within each field's bound.
    """
    if not isinstance(value, dict) or not isinstance(request, dict):
        return None
    fields = [field for field in RENDERED_FIELD_CHARS if field in request]
    expected = {*fields, *(("options",) if "options" in request else ())}
    if set(value) != expected or not all(
        _rendered(value[field], request[field], RENDERED_FIELD_CHARS[field], nullable=field == "placeholder")
        for field in fields
    ):
        return None
    if "options" in request and not _rendered_options(value["options"], request["options"]):
        return None
    return value


def _rendered_options(values: object, options: object) -> bool:
    return (
        isinstance(values, list)
        and isinstance(options, list)
        and len(values) == len(options)
        and all(
            isinstance(item, dict)
            and isinstance(option, dict)
            and set(item) == {"label", "description"}
            and _rendered(item["label"], option.get("label"), RENDERED_OPTION_CHARS["label"], nullable=False)
            and _rendered(
                item["description"], option.get("description"), RENDERED_OPTION_CHARS["description"], nullable=True
            )
            for item, option in zip(values, options, strict=True)
        )
    )


def _rendered(text: object, reference: object, maximum: int, *, nullable: bool) -> bool:
    """A nullable field renders to null exactly when its reference is null; anything else is bounded public text."""
    if nullable and reference is None:
        return text is None
    return (
        isinstance(text, str)
        and text == text.strip()
        and 0 < len(text) <= maximum
        and text.isprintable()
        and unicodedata.is_normalized("NFC", text)
    )


def canonical_snapshot_summary(value: object) -> dict[str, object] | None:
    """Return one Local snapshot's summary in one interface language (ADR-0091), or None.

    The summary is the snapshot catalog's English summary for `en` and its translation from the snapshot's own pack
    otherwise: bounded public text, never request copy. The caller compares `locale` with the one it asked for.
    """
    if (
        not isinstance(value, dict)
        or set(value) != SNAPSHOT_SUMMARY_FIELDS
        or canonical_locale(value["locale"]) is None
        or not _rendered(value["summary"], value["summary"], MAX_SNAPSHOT_SUMMARY_CHARS, nullable=False)
    ):
        return None
    return value


def canonical_action_label(value: object) -> str | None:
    if not isinstance(value, str) or unicodedata.normalize("NFC", value) != value:
        return None
    if value.strip() != value or not 1 <= len(value) <= MAX_ACTION_LABEL_CHARS:
        return None
    if any(unicodedata.category(character).startswith("C") for character in value):
        return None
    return value


class _ClarificationShapeError(ValueError):
    pass


def _clarification_text(value: object, maximum: int, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or unicodedata.normalize("NFC", value) != value
        or value.strip() != value
        or len(value) > maximum
        or (not value and not empty)
        or any(
            unicodedata.category(character)[0] == "C" or unicodedata.category(character) in {"Zl", "Zp"}
            for character in value
        )
    ):
        raise _ClarificationShapeError
    return value


def _clarification(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"question", "options", "default_index"}:
        raise _ClarificationShapeError
    question = _clarification_text(value["question"], MAX_CLARIFICATION_QUESTION_CHARS)
    raw_options = value["options"]
    if (
        not isinstance(raw_options, list)
        or not MIN_CLARIFICATION_OPTIONS <= len(raw_options) <= MAX_CLARIFICATION_OPTIONS
    ):
        raise _ClarificationShapeError
    options = []
    for option in raw_options:
        if not isinstance(option, dict) or set(option) != {"label", "description"}:
            raise _ClarificationShapeError
        options.append(
            {
                "label": _clarification_text(option["label"], MAX_CLARIFICATION_LABEL_CHARS),
                "description": _clarification_text(
                    option["description"], MAX_CLARIFICATION_DESCRIPTION_CHARS, empty=True
                ),
            }
        )
    default_index = value["default_index"]
    if (
        len({option["label"].casefold() for option in options}) != len(options)
        or isinstance(default_index, bool)
        or not isinstance(default_index, int)
        or not 0 <= default_index < len(options)
    ):
        raise _ClarificationShapeError
    return {"question": question, "options": options, "default_index": default_index}


def canonical_clarification(value: object) -> dict[str, object] | None:
    """Return one exact Brain multiple-choice clarification, or None when it breaks the closed shape (ADR-0081).

    Every text is already NFC, trimmed, and free of control and line-separator characters; labels are distinct
    ignoring case; the default points to one option. The shape is presentation only and carries no authority.
    """
    try:
        return _clarification(value)
    except _ClarificationShapeError:
        return None


def render_clarification(clarification: dict[str, object]) -> str:
    """The exact plain reply that accompanies one canonical clarification: the question, then numbered options.

    The recommended default is marked with " ✓" and a non-empty description follows " — ". Every boundary requires a
    clarified reply to equal this rendering, so a reply can never say something the question does not.
    """
    lines = [str(clarification["question"]), ""]
    for index, option in enumerate(clarification["options"]):
        mark = " ✓" if index == clarification["default_index"] else ""
        detail = f" — {option['description']}" if option["description"] else ""
        lines.append(f"{index + 1}. {option['label']}{mark}{detail}")
    return "\n".join(lines)


def _memory_entry(topic: object, preference: object, *, empty: bool = False) -> dict[str, str]:
    # Skill keys are reserved: only `forget` may name one, and no preference is ever stored under one.
    if not isinstance(topic, str) or MEMORY_TOPIC_RE.fullmatch(topic) is None or topic.startswith(SKILL_KEY_PREFIX):
        raise _ClarificationShapeError
    return {"topic": topic, "preference": _clarification_text(preference, MAX_MEMORY_PREFERENCE_CHARS, empty=empty)}


def canonical_memory(value: object) -> list[dict[str, str]] | None:
    """Return the Team's exact learned memory, or None when it breaks the closed shape (ADR-0084).

    At most 32 entries, each a distinct lowercase `topic` key and one NFC `preference` line of 1 to 280 characters
    without control or line-separator characters. Memories are data for the Brain and carry no Action authority.
    """
    if not isinstance(value, list) or len(value) > MAX_MEMORIES:
        return None
    try:
        entries = []
        for entry in value:
            if not isinstance(entry, dict) or set(entry) != {"topic", "preference"}:
                raise _ClarificationShapeError
            entries.append(_memory_entry(entry["topic"], entry["preference"]))
    except _ClarificationShapeError:
        return None
    return entries if len({entry["topic"] for entry in entries}) == len(entries) else None


def canonical_memory_changes(value: object) -> list[dict[str, str]] | None:
    """Return one completed turn's exact memory changes, or None: `remember` carries a preference, `forget` none."""
    if not isinstance(value, list) or len(value) > MAX_MEMORIES:
        return None
    try:
        changes = []
        for change in value:
            if not isinstance(change, dict) or set(change) != {"op", "topic", "preference"}:
                raise _ClarificationShapeError
            op = change["op"]
            forget = op == "forget"
            if not isinstance(op, str) or op not in {"remember", "forget"} or forget != (change["preference"] == ""):
                raise _ClarificationShapeError
            if forget and isinstance(change["topic"], str) and SKILL_KEY_RE.fullmatch(change["topic"]):
                changes.append({"op": "forget", "topic": change["topic"], "preference": ""})
                continue
            changes.append({"op": change["op"], **_memory_entry(change["topic"], change["preference"], empty=forget)})
    except _ClarificationShapeError:
        return None
    return changes


def apply_memory_changes(memory: list[dict[str, str]], changes: list[dict[str, str]]) -> list[dict[str, str]]:
    """Apply canonical changes in order, keeping at most the 32 newest entries.

    `remember` replaces its topic and makes it the newest entry; `forget` removes it.
    """
    entries = {entry["topic"]: entry["preference"] for entry in memory}
    for change in changes:
        entries.pop(change["topic"], None)
        if change["op"] == "remember":
            entries[change["topic"]] = change["preference"]
    return [{"topic": topic, "preference": preference} for topic, preference in entries.items()][-MAX_MEMORIES:]


def skill_key(contracts: dict[str, str], steps: list[dict[str, object]]) -> str:
    """The content key of one skill: the same Actions, inputs, and contracts always name the same procedure."""
    body = json.dumps({"contracts": contracts, "steps": steps}, separators=(",", ":"), sort_keys=True)
    return SKILL_KEY_PREFIX + hashlib.sha256(body.encode()).hexdigest()[:12]


def _skill(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"key", "contracts", "steps"}:
        raise _ClarificationShapeError
    contracts, raw_steps = value["contracts"], value["steps"]
    if (
        not isinstance(contracts, dict)
        or not isinstance(raw_steps, list)
        or not MIN_SKILL_STEPS <= len(raw_steps) <= MAX_SKILL_STEPS
    ):
        raise _ClarificationShapeError
    steps = []
    for step in raw_steps:
        if not isinstance(step, dict) or set(step) != {"assistant_id", "action", "inputs"}:
            raise _ClarificationShapeError
        inputs = step["inputs"]
        if (
            canonical_assistant_id(step["assistant_id"]) is None
            or canonical_action_id(step["action"]) is None
            or not isinstance(inputs, list)
            or len(inputs) > MAX_SKILL_INPUTS
            or any(not isinstance(name, str) or SKILL_INPUT_RE.fullmatch(name) is None for name in inputs)
            or inputs != sorted(set(inputs))
        ):
            raise _ClarificationShapeError
        steps.append({"assistant_id": step["assistant_id"], "action": step["action"], "inputs": list(inputs)})
    if set(contracts) != {step["assistant_id"] for step in steps} or any(
        canonical_source_digest(digest) is None for digest in contracts.values()
    ):
        raise _ClarificationShapeError
    ordered = dict(sorted(contracts.items()))
    if value["key"] != skill_key(ordered, steps):
        raise _ClarificationShapeError
    return {"key": value["key"], "contracts": ordered, "steps": steps}


def canonical_skill(value: object) -> dict[str, object] | None:
    """Return one exact learned skill, or None (ADR-0085).

    A skill is structure only: 2 to 16 ordered steps naming an Assistant, an Action, and the sorted input names it
    used, the `sha256:` contract fingerprint of every Assistant it names, and the content key derived from both. It
    holds no argument value or text and grants no authority.
    """
    try:
        return _skill(value)
    except _ClarificationShapeError:
        return None


def canonical_skills(value: object) -> list[dict[str, object]] | None:
    """Return a Team's exact skills, at most 8 with distinct keys, or None."""
    if not isinstance(value, list) or len(value) > MAX_SKILLS:
        return None
    skills = [canonical_skill(item) for item in value]
    if any(skill is None for skill in skills) or len({skill["key"] for skill in skills}) != len(skills):
        return None
    return skills


def apply_knowledge(
    memory: list[dict[str, str]],
    skills: list[dict[str, object]],
    changes: list[dict[str, str]],
    skill: dict[str, object] | None,
) -> tuple[list[dict[str, str]], list[dict[str, object]]]:
    """Apply one committed turn: memory changes in order (a skill key forgets that skill), then its new skill.

    A skill that is learned again becomes the newest, and the oldest skills give way beyond the 8-skill bound; a skill
    the same turn forgets is not learned again.
    """
    forgotten = {change["topic"] for change in changes if SKILL_KEY_RE.fullmatch(change["topic"])}
    entries = apply_memory_changes(memory, [change for change in changes if change["topic"] not in forgotten])
    kept = [item for item in skills if item["key"] not in forgotten]
    # The user's forget wins over relearning the same procedure in the same turn.
    if skill is not None and skill["key"] not in forgotten:
        kept = [item for item in kept if item["key"] != skill["key"]] + [skill]
    return entries, kept[-MAX_SKILLS:]


def canonical_source_digest(value: object) -> str | None:
    return value if isinstance(value, str) and SOURCE_DIGEST_RE.fullmatch(value) is not None else None


def canonical_assurance_handle(value: object) -> str | None:
    return value if isinstance(value, str) and ASSURANCE_HANDLE_RE.fullmatch(value) is not None else None


def canonical_team_name(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_TEAM_NAME_CHARS
        or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    return value


def canonical_local_team_name(value: object) -> str | None:
    """A Local Team display name (ADR-0088): the shared bounds, already NFC so names compare exactly, and public text.

    Public text is what every consumer stores and shows: no control, format, surrogate, private, unassigned, or line
    and paragraph separator character.
    """
    name = canonical_team_name(value)
    if (
        name is None
        or unicodedata.normalize("NFC", name) != name
        or any(
            unicodedata.category(character).startswith("C") or unicodedata.category(character) in {"Zl", "Zp"}
            for character in name
        )
    ):
        return None
    return name


def canonical_file_id(value: object) -> str | None:
    return value if isinstance(value, str) and FILE_ID_RE.fullmatch(value) is not None else None


def canonical_filename(value: object) -> str | None:
    if not isinstance(value, str) or not value or value.strip() != value:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return None
    if (
        len(encoded) > MAX_FILENAME_BYTES
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    return value


def canonical_media_type(value: object) -> str | None:
    if value is None or value == "":
        return "application/octet-stream"
    if not isinstance(value, str) or len(value) > MAX_MEDIA_TYPE_CHARS:
        return None
    media_type = value.lower()
    return media_type if MEDIA_TYPE_RE.fullmatch(media_type) is not None else None


def _integer(value: object, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def project_storage_usage(value: object) -> dict[str, int] | None:
    """Accept exact usage arithmetic, including cleanup after a quota reduction."""
    if not isinstance(value, dict):
        return None
    used = _integer(value.get("used_bytes"))
    limit = _integer(value.get("limit_bytes"), minimum=1)
    remaining = _integer(value.get("remaining_bytes"))
    if used is None or limit is None or remaining is None:
        return None
    within_quota = used <= limit and remaining == limit - used
    over_quota = used >= limit and remaining == 0
    if not (within_quota or over_quota):
        return None
    return {"used_bytes": used, "limit_bytes": limit, "remaining_bytes": remaining}


def project_file_metadata(value: object, *, include_usage: bool) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    file_id = canonical_file_id(value.get("id"))
    name = canonical_filename(value.get("name"))
    media_type = canonical_media_type(value.get("media_type"))
    size = _integer(value.get("size"), minimum=1)
    sha256 = value.get("sha256")
    created_at = _integer(value.get("created_at"), minimum=1)
    if (
        file_id is None
        or name is None
        or media_type is None
        or size is None
        or size > MAX_FILE_UPLOAD_BYTES
        or not isinstance(sha256, str)
        or SHA256_RE.fullmatch(sha256) is None
        or created_at is None
    ):
        return None
    metadata: dict[str, object] = {
        "id": file_id,
        "name": name,
        "media_type": media_type,
        "size": size,
        "sha256": sha256,
        "created_at": created_at,
    }
    if include_usage:
        usage = project_storage_usage(value)
        if usage is None:
            return None
        metadata.update(usage)
    return metadata


def project_storage_response(
    value: object,
    *,
    kind: str,
    expected_team_id: str,
    expected_file_id: str | None = None,
    include_team_id: bool,
) -> dict[str, object] | None:
    if not isinstance(value, dict) or value.get("team_id") != expected_team_id:
        return None
    if kind == "upload":
        metadata = project_file_metadata(value.get("file"), include_usage=True)
        if metadata is None:
            projected = None
        else:
            usage = {key: metadata.pop(key) for key in ("used_bytes", "limit_bytes", "remaining_bytes")}
            projected = {"file": metadata, **usage}
    elif kind == "list":
        raw_files = value.get("files")
        if not isinstance(raw_files, list) or len(raw_files) > MAX_TEAM_FILES:
            return None
        files = [project_file_metadata(item, include_usage=False) for item in raw_files]
        ids = [item["id"] for item in files if item is not None]
        usage = project_storage_usage(value)
        projected = (
            {"files": files, **usage}
            if usage is not None and len(files) == len(ids) and len(ids) == len(set(ids))
            else None
        )
    elif kind == "delete":
        file_id = canonical_file_id(value.get("id"))
        deleted = value.get("deleted")
        usage = project_storage_usage(value)
        if (
            file_id is None
            or (expected_file_id is not None and file_id != expected_file_id)
            or not isinstance(deleted, bool)
            or usage is None
        ):
            return None
        projected = {"id": file_id, "deleted": deleted, **usage}
    else:
        return None
    if projected is None:
        return None
    return {"team_id": expected_team_id, **projected} if include_team_id else projected
