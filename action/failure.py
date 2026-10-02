"""The sanitized failure envelope of one handled Action failure (ADR-0092 section 8).

A handled failure is one ``{"type": "failure", ...}`` stdout frame after exit 0 with empty stderr. Team admits it
with the mirrored Developers validator and then re-redacts every member itself, never trusting the Assistant's own
sanitization: each value Team injected for the invocation (Integration tokens, Stored Input values, secret human
responses, and the egress capability in the workload environment) is replaced in every common encoding, matched
ASCII-case-insensitively, and secret-shaped text is replaced through the end of its value. A secret the producer cut
at the end of a text cannot be matched whole, so any trailing prefix of one is withheld rather than shown clipped.
Redaction runs before the bound is enforced again, and both flags record what changed. A diagnostic is never
evidence that an effect did or did not occur, and never authority.

Redaction is best effort, not a guarantee. It removes whole injected values and their listed encodings, secret-shaped
text, and trailing prefixes of at least ``MIN_CLIPPED`` characters. It does not remove an arbitrary short fragment of
a secret: a piece of fewer than ``MIN_CLIPPED`` characters, a derived encoding shorter than ``MIN_DERIVED``, a piece
taken from the middle of a value, or a value the Assistant transformed in a way not listed here may remain. Unknown
secrets in Creator prose cannot be detected universally (ADR-0092 section 8), so diagnostics stay private, encrypted,
bounded, and shown only to the Team's Supervisor.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import quote, quote_plus

from protocol.assistant.v1 import failure_validator

REDACTED = "[REDACTED]"
MAX_TEXT_BYTES = failure_validator.MAX_TEXT_BYTES
MAX_ERROR_TYPE = 128
# A derived encoding shorter than this matches too much ordinary text to identify a secret.
MIN_DERIVED = 4
# The shortest trailing prefix of an injected value that is withheld as a possibly clipped secret.
MIN_CLIPPED = 4
_PROXY_KEYS = ("HTTPS_PROXY", "https_proxy")
_USERINFO = re.compile(r"[a-z][a-z0-9+.-]*://([^\s/@]+)@", re.IGNORECASE)
# Each pattern consumes the whole value it recognizes, through the end of the text when nothing closes it.
_SHAPED = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.DOTALL),
    re.compile(r"\b(?:bearer|basic)\s+\S+", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]*)?"),
    re.compile(
        r"\b(?:sk|pk|rk|ghp|gho|ghu|ghs|ghr|github_pat|glpat|xox[abprs]|AKIA|ASIA|AIza|ya29)[-_.]?[A-Za-z0-9_.-]{8,}"
    ),
)
_ASSIGNED = re.compile(
    r"(?P<key>\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|secret|password"
    r"|passwd|pwd|token|authorization|cookie|session|signature|private[_-]?key)\b[\"']?\s*[:=]\s*)"
    r"(?:\[REDACTED\]|\"(?:[^\"\\]|\\.)*(?:\"|\Z)|'[^']*(?:'|\Z)|[^\s,;&}\]]+)",
    re.IGNORECASE | re.DOTALL,
)
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_URL_USERINFO = re.compile(r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)[^\s/@]+@", re.IGNORECASE)


class FailureEnvelopeError(ValueError):
    """A failure frame broke the closed protocol; it is a policy hold, never a handled failure."""


@dataclass(frozen=True, slots=True)
class ActionFailure:
    """One admitted, Team-sanitized handled failure diagnostic."""

    error_type: str
    message: str
    provider: str | None
    http_status: int | None
    response_excerpt: str | None
    redacted: bool
    truncated: bool

    def document(self) -> dict[str, object]:
        """The closed failure object, as the protocol and the diagnostic store carry it."""
        return {
            "error_type": self.error_type,
            "message": self.message,
            "provider": self.provider,
            "http_status": self.http_status,
            "response_excerpt": self.response_excerpt,
            "redacted": self.redacted,
            "truncated": self.truncated,
        }


class ActionFailedError(RuntimeError):
    """An Assistant reported a handled failure; Controllers raise their public problem from it."""

    def __init__(self, failure: ActionFailure) -> None:
        super().__init__("Assistant Action failed")
        self.failure = failure


def is_failure(raw: object) -> bool:
    """Whether a decoded stdout frame claims the failure branch, which only failure admission may then judge."""
    return isinstance(raw, dict) and raw.get("type") == "failure"


def failure_of(exc: BaseException | None) -> ActionFailure | None:
    """The handled failure a Controller problem was raised from, if any."""
    for _depth in range(8):
        if exc is None:
            return None
        if isinstance(exc, ActionFailedError):
            return exc.failure
        exc = exc.__cause__
    return None


def capability_values(container: object) -> tuple[str, ...]:
    """The egress capability Team injected into a workload's environment, read from its proxy URLs."""
    attrs = getattr(container, "attrs", None)
    config = attrs.get("Config") if isinstance(attrs, dict) else None
    environment = config.get("Env") if isinstance(config, dict) else None
    values: set[str] = set()
    for entry in environment if isinstance(environment, list) else ():
        key, separator, value = entry.partition("=") if isinstance(entry, str) else ("", "", "")
        match = _USERINFO.match(value) if separator and key in _PROXY_KEYS else None
        if match is not None:
            values.add(match[1])
    return tuple(sorted(values))


def admit(envelope: object, secrets: Iterable[str]) -> ActionFailure:
    """Admit one failure frame and re-redact every member with each value Team injected for the invocation."""
    error = failure_validator.failure_error(envelope)
    if error is not None:
        raise FailureEnvelopeError(error)
    failure = envelope["failure"]
    redactor = _Redactor(secrets)
    error_type, type_changed, type_cut = redactor.text(failure["error_type"], MAX_ERROR_TYPE)
    message, message_changed, message_cut = redactor.text(failure["message"], MAX_TEXT_BYTES)
    excerpt, excerpt_changed, excerpt_cut = (None, False, False)
    if failure["response_excerpt"] is not None:
        excerpt, excerpt_changed, excerpt_cut = redactor.text(failure["response_excerpt"], MAX_TEXT_BYTES)
    provider = failure["provider"]
    provider_changed = provider is not None and redactor.text(provider, MAX_TEXT_BYTES)[1]
    return ActionFailure(
        error_type=error_type,
        message=message,
        provider=None if provider_changed else provider,
        http_status=failure["http_status"],
        response_excerpt=excerpt,
        redacted=failure["redacted"] or type_changed or message_changed or excerpt_changed or provider_changed,
        truncated=failure["truncated"] or type_cut or message_cut or excerpt_cut,
    )


class _Redactor:
    """Replace every encoding of each injected value, then secret-shaped text, then any clipped trailing prefix."""

    def __init__(self, secrets: Iterable[str]) -> None:
        needles: set[str] = set()
        for secret in secrets:
            if isinstance(secret, str) and secret:
                needles.add(secret)
                needles.update(item for item in _encodings(secret) if len(item) >= MIN_DERIVED)
        # Longest first, folded once, so a longer encoding is never left partly matched by a shorter one.
        folded = {needle.translate(_ASCII_LOWER) for needle in needles}
        self._lowered = tuple(sorted(folded, key=lambda item: (-len(item), item)))

    def text(self, value: str, limit: int) -> tuple[str, bool, bool]:
        """The redacted text, whether anything was replaced or withheld, and whether it was cut to ``limit``."""
        redacted = self._replace_injected(value)
        for pattern in _SHAPED:
            redacted = pattern.sub(REDACTED, redacted)
        redacted = _ASSIGNED.sub(lambda match: match["key"] + REDACTED, redacted)
        redacted = _URL_USERINFO.sub(lambda match: match["scheme"] + REDACTED + "@", redacted)
        redacted = self._withhold_clipped(redacted)
        changed = redacted != value
        bounded = _bound(redacted, limit)
        return bounded, changed, bounded != redacted

    def _replace_injected(self, value: str) -> str:
        """Replace every ASCII-case-insensitive occurrence of each injected value and encoding."""
        for needle in self._lowered:
            folded = value.translate(_ASCII_LOWER)
            position = folded.find(needle)
            if position < 0:
                continue
            pieces, start = [], 0
            while position >= 0:
                pieces.extend((value[start:position], REDACTED))
                start = position + len(needle)
                position = folded.find(needle, start)
            value = "".join((*pieces, value[start:]))
        return value

    def _withhold_clipped(self, value: str) -> str:
        """Withhold the longest trailing prefix of any injected value, which a producer's bound may have cut.

        Only an ASCII case fold is used here, so every position in the folded text is the same position in ``value``.
        """
        folded = value.translate(_ASCII_LOWER)
        start = len(folded)
        for needle in self._lowered:
            position = folded.find(needle[0], max(0, len(folded) - len(needle) + 1))
            while -1 < position <= len(folded) - MIN_CLIPPED and position < start:
                if needle.startswith(folded[position:]):
                    start = position
                    break
                position = folded.find(needle[0], position + 1)
        return value if start == len(value) else value[:start] + REDACTED


def _encodings(secret: str) -> tuple[str, ...]:
    """The common encodings an Action may print a value in: JSON, percent, hexadecimal, and both base64 alphabets.

    Base64 is taken at each of the three byte alignments, keeping only the characters that depend on the value alone,
    so the value is found inside a longer encoded string such as a Basic credential.
    """
    raw = secret.encode("utf-8")
    encodings = [
        json.dumps(secret)[1:-1],
        json.dumps(secret, ensure_ascii=False)[1:-1],
        json.dumps(secret)[1:-1].replace("/", "\\/"),
        quote(secret, safe=""),
        quote_plus(secret, safe=""),
        binascii.hexlify(raw).decode("ascii"),
    ]
    for offset in range(3):
        encoded = base64.b64encode(bytes(offset) + raw).decode("ascii")
        first = -(-offset * 4 // 3)
        last = (offset + len(raw)) * 8 // 6
        core = encoded[first:last]
        encodings.extend((core, core.translate(str.maketrans("+/", "-_"))))
    return tuple(encodings)


def _bound(value: str, limit: int) -> str:
    """Cut ``value`` to at most ``limit`` UTF-8 bytes on a character boundary."""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")
