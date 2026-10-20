"""A Team's Sign-in over the Team HTTP protocol (ADR-0117, Sign-ins amendment and its 2026-10-20 amendment).

Each Team has at most one Sign-in: the sealed browser session a person saved their logins into. A person adds logins
in a sign-in session, a Browser only they drive, restored from the Sign-in when there is one, and Saves it as the next
generation; Admin reads the Sign-in, starts, saves, and cancels a session, and deletes the Sign-in after the
Supervisor's second factor, binding the exact Sign-in, generation, and Team incarnation it confirmed. Team owns this
module; Admin consumes it whole as a pinned mirror. Every body and answer has one exact form, and a Sign-in is shown by
its state, the sites found in its saved cookies, and its times, never its capsule.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

if __package__:
    from . import payload
else:  # The protocol verifier runs every module of this directory flat.
    import payload

ID_RE = re.compile(r"[0-9a-f]{32}")
# A Team incarnation: the Team network's Docker id.
INCARNATION_RE = re.compile(r"[0-9a-f]{64}")
# A site is one lowercase DNS hostname of at least two labels.
SITE_RE = re.compile(r"(?=.{4,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
MAX_URL_CHARS = 2_048
# The sites a Sign-in shows: those found in its saved cookies, at most this many, the rest marked as truncated.
MAX_SITES = 64
MAX_GENERATION = 2**31 - 1
STATES = frozenset({"ready", "needs-person"})
FACTORS = frozenset({"totp", "passkey"})
DELETIONS = frozenset({"deleted", "pending"})
# The closed reasons a sign-in session or the Sign-in refuses what was asked.
NOT_RUNNING = "sign-in-not-running"
URL_INVALID = "sign-in-url-invalid"
WINDOWS = "sign-in-windows"
IDENTITY_PROVIDER = "sign-in-identity-provider"
UNSAVED = "sign-in-unsaved"
BUSY = "sign-in-busy"
CHANGED = "sign-in-changed"
ABSENT = "sign-in-absent"
_SIGN_IN_KEYS = frozenset(
    {
        "sign_in_id",
        "state",
        "generation",
        "team_incarnation",
        "sites",
        "sites_truncated",
        "created_at",
        "saved_at",
        "last_used_at",
        "expires_at",
    }
)


def canonical_site(value: object) -> str | None:
    return value if isinstance(value, str) and SITE_RE.fullmatch(value) else None


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


def _generation(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_GENERATION


def _time(value: object) -> bool:
    return isinstance(value, str) and TIME_RE.fullmatch(value) is not None


def canonical_start(value: object) -> dict[str, object] | None:
    """`{url, locale}`: where the sign-in starts, and the interface language its Browser's pages follow."""
    if not isinstance(value, dict) or set(value) != {"url", "locale"}:
        return None
    if start_site(value["url"]) is None or payload.canonical_locale(value["locale"]) is None:
        return None
    return value


def canonical_session(value: object) -> dict[str, object] | None:
    """`{sign_in_session_id}`: the session a Save or a Cancel ends."""
    if not isinstance(value, dict) or set(value) != {"sign_in_session_id"} or not _id(value["sign_in_session_id"]):
        return None
    return value


def canonical_delete(value: object) -> dict[str, object] | None:
    """The exact Sign-in, generation, and Team incarnation the Supervisor confirmed, and the second factor they used."""
    if not isinstance(value, dict) or set(value) != {"sign_in_id", "generation", "team_incarnation", "factor"}:
        return None
    if (
        not _id(value["sign_in_id"])
        or not _generation(value["generation"])
        or not isinstance(value["team_incarnation"], str)
        or INCARNATION_RE.fullmatch(value["team_incarnation"]) is None
        or value["factor"] not in FACTORS
    ):
        return None
    return value


def canonical_started(value: object) -> dict[str, object] | None:
    """A started session: its id, the view the person controls it through, and the site it starts at."""
    if not isinstance(value, dict) or set(value) != {"sign_in_session_id", "view_id", "site"}:
        return None
    if not _id(value["sign_in_session_id"]) or not _id(value["view_id"]) or canonical_site(value["site"]) is None:
        return None
    return value


def canonical_sign_in(value: object) -> dict[str, object] | None:
    """The Team's Sign-in as Admin shows it: never its capsule."""
    if not isinstance(value, dict) or set(value) != _SIGN_IN_KEYS:
        return None
    sites = value["sites"]
    times = (value["created_at"], value["saved_at"], value["last_used_at"], value["expires_at"])
    if (
        not _id(value["sign_in_id"])
        or value["state"] not in STATES
        or not _generation(value["generation"])
        or not isinstance(value["team_incarnation"], str)
        or INCARNATION_RE.fullmatch(value["team_incarnation"]) is None
        or not isinstance(sites, list)
        or len(sites) > MAX_SITES
        or any(canonical_site(site) is None for site in sites)
        or sites != sorted(set(sites))
        or not isinstance(value["sites_truncated"], bool)
        or not all(_time(moment) for moment in times)
        or not value["created_at"] <= value["saved_at"] < value["expires_at"]
        or value["last_used_at"] < value["created_at"]
    ):
        return None
    return value


def canonical_current(value: object) -> dict[str, object] | None:
    """`{sign_in}`: the Team's Sign-in, or null when it has none."""
    if not isinstance(value, dict) or set(value) != {"sign_in"}:
        return None
    if value["sign_in"] is not None and canonical_sign_in(value["sign_in"]) is None:
        return None
    return value


def canonical_saved(value: object) -> dict[str, object] | None:
    """`{sign_in, recorded}`: the committed Sign-in, and whether its activity completion was recorded (ADR-0120)."""
    if not isinstance(value, dict) or set(value) != {"sign_in", "recorded"}:
        return None
    if canonical_sign_in(value["sign_in"]) is None or not isinstance(value["recorded"], bool):
        return None
    return value


def canonical_deleted(value: object) -> dict[str, object] | None:
    """`{deletion}`: `deleted` once its absence is proven, else `pending` while Team retries."""
    return value if isinstance(value, dict) and set(value) == {"deletion"} and value["deletion"] in DELETIONS else None


def canonical_cancelled(value: object) -> dict[str, object] | None:
    return value if value == {"cancelled": True} else None
