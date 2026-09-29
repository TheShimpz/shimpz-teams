"""Pure Team wire contract shared by the Admin and Store backends."""

from __future__ import annotations

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

TEAM_ID_RE = re.compile(TEAM_ID_PATTERN)
ASSISTANT_ID_RE = re.compile(ASSISTANT_ID_PATTERN)
ACTION_ID_RE = re.compile(ACTION_ID_PATTERN)
FILE_ID_RE = re.compile(FILE_ID_PATTERN)
SHA256_RE = re.compile(SHA256_PATTERN)
SOURCE_DIGEST_RE = re.compile(SOURCE_DIGEST_PATTERN)
ASSURANCE_HANDLE_RE = re.compile(ASSURANCE_HANDLE_PATTERN)
MEDIA_TYPE_RE = re.compile(MEDIA_TYPE_PATTERN)

MAX_CHAT_MESSAGE_CHARS = 16_000
MAX_CHAT_FILES = 8
MAX_CHAT_ASSISTANTS = 16
MAX_TEAM_FILES = 256
MAX_TEAM_NAME_CHARS = 80
MAX_ACTION_LABEL_CHARS = 80
MAX_LANGUAGE_EXEMPLAR_CHARS = 2_000
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
CHAT_BODY_FIELDS = frozenset({"message", "files", "assistant_ids", "conversation"})
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


def canonical_language_exemplar(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFC", value.strip())
    if not 1 <= len(normalized) <= MAX_LANGUAGE_EXEMPLAR_CHARS:
        return None
    if any(
        unicodedata.category(character).startswith("C")
        and unicodedata.category(character) != "Cf"
        and character not in _LANGUAGE_LAYOUT_CONTROLS
        for character in normalized
    ):
        return None
    return normalized


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
    if not isinstance(topic, str) or MEMORY_TOPIC_RE.fullmatch(topic) is None:
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
