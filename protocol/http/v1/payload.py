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
        unicodedata.category(character).startswith("C") and character not in {"\n", "\t"} for character in normalized
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
