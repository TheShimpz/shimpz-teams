"""A Team's Sign-ins over the Team HTTP protocol (ADR-0117, Sign-ins amendment).

A person signs in to one site in a Browser only they drive (a sign-in session), then Saves it as a Sign-in: the
Team's sealed copy of that Chrome profile. Admin starts, saves, and cancels a session, lists the Team's Sign-ins, and
forgets one. Team owns this module; Admin consumes it whole as a pinned mirror. Every body and answer has one exact
form; a Sign-in is shown by its site, the person's label for the account, and its times, never its capsule.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

if __package__:
    from . import payload
else:  # The protocol verifier runs every module of this directory flat.
    import payload

ID_RE = re.compile(r"[0-9a-f]{32}")
# A site is one lowercase DNS hostname of at least two labels; the label is the person's own name for the account.
SITE_RE = re.compile(r"(?=.{4,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
MAX_URL_CHARS = 2_048
MAX_LABEL_CHARS = 64
MAX_SIGN_INS = 16
# The closed reasons a sign-in session refuses what was asked.
NOT_RUNNING = "sign-in-not-running"
URL_INVALID = "sign-in-url-invalid"
LABEL_INVALID = "sign-in-label-invalid"
WINDOWS = "sign-in-windows"
IDENTITY_PROVIDER = "sign-in-identity-provider"
UNSAVED = "sign-in-unsaved"
_SIGN_IN_KEYS = frozenset({"sign_in_id", "site", "label", "created_at", "last_used_at", "expires_at"})


def canonical_site(value: object) -> str | None:
    return value if isinstance(value, str) and SITE_RE.fullmatch(value) else None


def canonical_label(value: object) -> str | None:
    """A label as kept: trimmed, printable, 1 to 64 characters."""
    if not isinstance(value, str):
        return None
    label = value.strip()
    return label if 0 < len(label) <= MAX_LABEL_CHARS and label.isprintable() else None


def start_site(url: object) -> str | None:
    """The site an https sign-in URL names, or None: no credentials or other port, nothing unprintable or spaced."""
    if (
        not isinstance(url, str)
        or len(url) > MAX_URL_CHARS
        or any(character.isspace() or not character.isprintable() for character in url)
    ):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password or port not in {None, 443}:
        return None
    return canonical_site((parts.hostname or "").lower())


def _id(value: object) -> bool:
    return isinstance(value, str) and ID_RE.fullmatch(value) is not None


def canonical_start(value: object) -> dict[str, object] | None:
    """`{url, locale}`: where the sign-in starts, and the interface language its Browser's pages follow."""
    if not isinstance(value, dict) or set(value) != {"url", "locale"}:
        return None
    if start_site(value["url"]) is None or payload.canonical_locale(value["locale"]) is None:
        return None
    return value


def canonical_save(value: object) -> dict[str, object] | None:
    """`{sign_in_session_id, label}` exactly as kept."""
    if not isinstance(value, dict) or set(value) != {"sign_in_session_id", "label"}:
        return None
    if not _id(value["sign_in_session_id"]) or canonical_label(value["label"]) != value["label"]:
        return None
    return value


def canonical_session(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or set(value) != {"sign_in_session_id"} or not _id(value["sign_in_session_id"]):
        return None
    return value


def canonical_forget(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or set(value) != {"sign_in_id"} or not _id(value["sign_in_id"]):
        return None
    return value


def canonical_started(value: object) -> dict[str, object] | None:
    """A started session: its id, the view the person controls it through, and the site it signs in to."""
    if not isinstance(value, dict) or set(value) != {"sign_in_session_id", "view_id", "site"}:
        return None
    if not _id(value["sign_in_session_id"]) or not _id(value["view_id"]) or canonical_site(value["site"]) is None:
        return None
    return value


def canonical_sign_in(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or set(value) != _SIGN_IN_KEYS:
        return None
    times = (value["created_at"], value["last_used_at"], value["expires_at"])
    if (
        not _id(value["sign_in_id"])
        or canonical_site(value["site"]) is None
        or canonical_label(value["label"]) != value["label"]
        or not all(isinstance(moment, str) and TIME_RE.fullmatch(moment) for moment in times)
        or not value["created_at"] <= value["last_used_at"] < value["expires_at"]
    ):
        return None
    return value


def canonical_sign_ins(value: object) -> dict[str, object] | None:
    """`{sign_ins}`: at most 16, each once."""
    if not isinstance(value, dict) or set(value) != {"sign_ins"} or not isinstance(value["sign_ins"], list):
        return None
    listed = value["sign_ins"]
    if len(listed) > MAX_SIGN_INS or any(canonical_sign_in(item) is None for item in listed):
        return None
    if len({item["sign_in_id"] for item in listed}) != len(listed):
        return None
    return value


def canonical_forgotten(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or set(value) != {"forgotten"} or not isinstance(value["forgotten"], bool):
        return None
    return value


def canonical_cancelled(value: object) -> dict[str, object] | None:
    return value if value == {"cancelled": True} else None
