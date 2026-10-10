"""Pure Team wire contract shared with its consumers."""

from __future__ import annotations

import json
import re
import unicodedata

if __package__:
    from . import identifiers, purpose, turn
else:  # The protocol verifier runs every module of this directory flat.
    import identifiers
    import purpose
    import turn

# The identifier grammars, the purpose sentence rule, and the chat-turn bounds live in their own modules, which the
# Brain mirrors too.
TEAM_ID_PATTERN = identifiers.TEAM_ID_PATTERN
ASSISTANT_ID_PATTERN = identifiers.ASSISTANT_ID_PATTERN
ACTION_ID_PATTERN = identifiers.ACTION_ID_PATTERN
MAX_ASSISTANT_ID_CHARS = identifiers.MAX_ASSISTANT_ID_CHARS
MAX_IDENTIFIER_CHARS = identifiers.MAX_IDENTIFIER_CHARS
MAX_ACTION_ID_CHARS = identifiers.MAX_ACTION_ID_CHARS
TEAM_ID_RE = identifiers.TEAM_ID_RE
ASSISTANT_ID_RE = identifiers.ASSISTANT_ID_RE
IDENTIFIER_RE = identifiers.IDENTIFIER_RE
ACTION_ID_RE = identifiers.ACTION_ID_RE
canonical_team_id = identifiers.canonical_team_id
canonical_assistant_id = identifiers.canonical_assistant_id
canonical_identifier = identifiers.canonical_identifier
canonical_action_id = identifiers.canonical_action_id
MAX_PURPOSE_CHARS = purpose.MAX_PURPOSE_CHARS
canonical_purpose = purpose.canonical_purpose
MAX_CHAT_MESSAGE_CHARS = turn.MAX_CHAT_MESSAGE_CHARS
MAX_CLARIFICATION_QUESTION_CHARS = turn.MAX_CLARIFICATION_QUESTION_CHARS
MAX_CLARIFICATION_LABEL_CHARS = turn.MAX_CLARIFICATION_LABEL_CHARS
MAX_CLARIFICATION_DESCRIPTION_CHARS = turn.MAX_CLARIFICATION_DESCRIPTION_CHARS
MIN_CLARIFICATION_OPTIONS = turn.MIN_CLARIFICATION_OPTIONS
MAX_CLARIFICATION_OPTIONS = turn.MAX_CLARIFICATION_OPTIONS
MAX_MEMORIES = turn.MAX_MEMORIES
MAX_MEMORY_PREFERENCE_CHARS = turn.MAX_MEMORY_PREFERENCE_CHARS
MEMORY_TOPIC_RE = turn.MEMORY_TOPIC_RE
MAX_SKILLS = turn.MAX_SKILLS
MAX_MEMORY_CHANGES = turn.MAX_MEMORY_CHANGES
MIN_SKILL_STEPS = turn.MIN_SKILL_STEPS
MAX_SKILL_STEPS = turn.MAX_SKILL_STEPS
MAX_SKILL_INPUTS = turn.MAX_SKILL_INPUTS
SKILL_KEY_PREFIX = turn.SKILL_KEY_PREFIX
SKILL_KEY_RE = turn.SKILL_KEY_RE
SKILL_INPUT_RE = turn.SKILL_INPUT_RE
skill_key = turn.skill_key

FILE_ID_PATTERN = r"^[0-9a-f]{32}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
SOURCE_DIGEST_PATTERN = rf"^sha256:{SHA256_PATTERN[1:-1]}$"
MEDIA_TYPE_PATTERN = r"^[a-z0-9][a-z0-9!#$&^_.+\-]*/[a-z0-9][a-z0-9!#$&^_.+\-]*$"
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
# A Stored Input's help text (Developers manifest `description`) rendered in one interface language: what the value is
# and how to get it, within the catalog bound every translation fits (Assistant Spec v1).
MAX_STORED_INPUT_HELP_CHARS = 500
# The Creator's public links an Assistant page shows (Developers manifest `[shimpz.links]`): unverified presentation,
# in this canonical display order, each one help-URL-grammar URL on its own kind's host.
CREATOR_LINK_PREFIXES = {
    "site": ("https://",),
    "github": ("https://github.com/",),
    "x": ("https://x.com/",),
    "youtube": ("https://youtube.com/", "https://www.youtube.com/"),
    "linkedin": ("https://linkedin.com/", "https://www.linkedin.com/"),
    "instagram": ("https://instagram.com/", "https://www.instagram.com/"),
}
MAX_CREATOR_LINK_CHARS = 256

FILE_ID_RE = re.compile(FILE_ID_PATTERN)
SHA256_RE = re.compile(SHA256_PATTERN)
SOURCE_DIGEST_RE = re.compile(SOURCE_DIGEST_PATTERN)
MEDIA_TYPE_RE = re.compile(MEDIA_TYPE_PATTERN)
HELP_URL_RE = re.compile(HELP_URL_PATTERN)

MAX_CHAT_FILES = 8
# The most Assistants one Team may have installed; a chat turn may name every one of them.
MAX_TEAM_ASSISTANTS = 16
MAX_CHAT_ASSISTANTS = MAX_TEAM_ASSISTANTS
MAX_TEAM_FILES = 256
MAX_TEAM_NAME_CHARS = 80
MAX_FILE_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_FILENAME_BYTES = 255
MAX_MEDIA_TYPE_CHARS = 127
# Committed presentation history carried by a Local Team chat turn (ADR-0065).
MAX_CONVERSATION_ENTRIES = 8
MAX_CONVERSATION_TEXT_CHARS = 512
MAX_CONVERSATION_CHARS = 4_096
# A chat body carries what direct Routine creation binds (ADR-0092): the request identity Admin issues once per sent
# message and keeps across a transport retry or a resend, and the user's IANA timezone, or null.
CHAT_BODY_FIELDS = frozenset({"message", "files", "assistant_ids", "conversation", "locale", "request", "timezone"})
REQUEST_IDENTITY_FIELDS = frozenset({"issued_at", "nonce"})
# How long a request identity may mutate a Routine after Admin issued it, and how far ahead of Team's clock it may be.
REQUEST_IDENTITY_SECONDS = 900
REQUEST_IDENTITY_SKEW_SECONDS = 60
REQUEST_NONCE_RE = re.compile(r"[0-9a-f]{32}\Z")
# The closed Admin interface languages a chat turn may name; a turn without one carries null (ADR-0090).
CHAT_LOCALES = frozenset({"ar", "de", "en", "es", "fr", "ja", "pt", "zh"})
SNAPSHOT_SUMMARY_FIELDS = frozenset({"locale", "summary"})
MAX_SNAPSHOT_SUMMARY_CHARS = 80
# An Assistant page in one interface language: a staged Local snapshot's or an installed binding's identity, display
# copy, and declared capabilities. Localized text is rendered text within its catalog bound (Assistant Spec v1).
ASSISTANT_DETAILS_FIELDS = frozenset(
    {
        "locale",
        "assistant_id",
        "assistant_version",
        "name",
        "creators",
        "summary",
        "description",
        "links",
        "actions",
        "integrations",
        "stored_inputs",
    }
)
ASSISTANT_VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
DETAILS_CREATOR_RE = re.compile(r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
ACTION_EFFECTS = frozenset({"read_only", "mutating"})
MAX_DETAILS_NAME_CHARS = 80
MAX_DETAILS_DESCRIPTION_CHARS = 500
MAX_DETAILS_LINE_CHARS = 120
MAX_DETAILS_CREATORS = 16
MAX_DETAILS_ACTIONS = 128
MAX_DETAILS_INTEGRATIONS = 16
MAX_DETAILS_STORED_INPUTS = 8
# What one completed chat turn consumed: its wall-clock duration and the model tokens it was told it used.
MAX_TURN_DURATION_MS = 86_400_000
MAX_TURN_USAGE_MODELS = 16
MAX_TURN_USAGE_TOKENS = 1_000_000_000
TURN_USAGE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
TURN_USAGE_MODEL_FIELDS = frozenset({"provider", "model", "input_tokens", "output_tokens"})
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


def canonical_locale(value: object) -> str | None:
    """Return one closed interface language code, or None."""
    return value if isinstance(value, str) and value in CHAT_LOCALES else None


def request_identity_fresh(issued_at: int, now: int) -> bool:
    """Whether an identity issued at ``issued_at`` may still change a Routine at ``now``.

    The window is exclusive at its end: an identity issued at ``t`` is fresh through ``t + 899`` and expired from
    ``t + 900``, the same second its receipt stops being live, and it may be at most 60 s ahead of the clock judging it.
    Admin and Team judge a resend with this one predicate.
    """
    return now - REQUEST_IDENTITY_SECONDS < issued_at <= now + REQUEST_IDENTITY_SKEW_SECONDS


def canonical_request_identity(value: object) -> dict[str, object] | None:
    """Return one exact chat request identity, or None: a whole-second issue instant and a 32-hex nonce.

    The identity names one sent message; Team binds it to the principal, the Team incarnation, and the message, and a
    Routine change it carries commits at most once while it is fresh (ADR-0092).
    """
    if not isinstance(value, dict) or set(value) != REQUEST_IDENTITY_FIELDS:
        return None
    issued_at, nonce = value["issued_at"], value["nonce"]
    if type(issued_at) is not int or not 0 < issued_at < 2**40:
        return None
    if not isinstance(nonce, str) or REQUEST_NONCE_RE.fullmatch(nonce) is None:
        return None
    return {"issued_at": issued_at, "nonce": nonce}


def canonical_help_url(value: object) -> str | None:
    """Return one exact Stored Input key page, or None."""
    if not isinstance(value, str) or len(value) > MAX_HELP_URL_CHARS or HELP_URL_RE.fullmatch(value) is None:
        return None
    return value


def canonical_stored_input_help(value: object) -> str | None:
    """Return one Stored Input help text rendered in one interface language: printable NFC public text, or None."""
    return value if _rendered(value, value, MAX_STORED_INPUT_HELP_CHARS, nullable=False) else None


def canonical_creator_links(value: object) -> dict[str, str] | None:
    """Return zero to six Creator links in canonical display order, or None when any kind or URL is invalid."""
    if not isinstance(value, dict) or not set(value) <= CREATOR_LINK_PREFIXES.keys():
        return None
    links = {kind: value[kind] for kind in CREATOR_LINK_PREFIXES if kind in value}
    if any(
        not isinstance(url, str)
        or len(url) > MAX_CREATOR_LINK_CHARS
        or canonical_help_url(url) is None
        or not url.startswith(CREATOR_LINK_PREFIXES[kind])
        for kind, url in links.items()
    ):
        return None
    return links


def canonical_pack_digest(value: object) -> str | None:
    """Return one `sha256:` language-pack digest (ADR-0091), or None."""
    return value if isinstance(value, str) and SOURCE_DIGEST_RE.fullmatch(value) else None


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


def canonical_assistant_details(value: object) -> dict[str, object] | None:
    """Return one exact Assistant page in one interface language, or None when any member or bound fails.

    Creators are self-declared handles, links are unverified Creator presentation, and every list of declared
    capabilities is sorted by its unique id. The caller compares `locale` with the one it asked for.
    """
    if not isinstance(value, dict) or set(value) != ASSISTANT_DETAILS_FIELDS:
        return None
    version = value["assistant_version"]
    if (
        canonical_locale(value["locale"]) is None
        or canonical_assistant_id(value["assistant_id"]) is None
        or not isinstance(version, str)
        or ASSISTANT_VERSION_RE.fullmatch(version) is None
        or not _details_creators(value["creators"])
        or canonical_creator_links(value["links"]) is None
    ):
        return None
    texts = (
        (value["name"], MAX_DETAILS_NAME_CHARS),
        (value["summary"], MAX_SNAPSHOT_SUMMARY_CHARS),
        (value["description"], MAX_DETAILS_DESCRIPTION_CHARS),
    )
    if not all(_rendered(text, text, maximum, nullable=False) for text, maximum in texts):
        return None
    return value if _details_capabilities(value) else None


def _details_creators(value: object) -> bool:
    return (
        isinstance(value, list)
        and 1 <= len(value) <= MAX_DETAILS_CREATORS
        and all(isinstance(creator, str) and DETAILS_CREATOR_RE.fullmatch(creator) for creator in value)
        and len(set(value)) == len(value)
    )


def _details_capabilities(value: dict[str, object]) -> bool:
    """Whether the Actions, Integrations, and Stored Inputs are each a bounded list of closed items sorted by id."""
    return (
        _details_items(value["actions"], 1, MAX_DETAILS_ACTIONS, {"id", "effect", "description"})
        and all(
            isinstance(action["effect"], str)
            and action["effect"] in ACTION_EFFECTS
            and _rendered(action["description"], action["description"], MAX_DETAILS_LINE_CHARS, nullable=False)
            for action in value["actions"]
        )
        and _details_items(value["integrations"], 0, MAX_DETAILS_INTEGRATIONS, {"id", "provider"})
        and all(canonical_identifier(item["provider"]) is not None for item in value["integrations"])
        and _details_items(
            value["stored_inputs"], 0, MAX_DETAILS_STORED_INPUTS, {"id", "label", "description", "help_url"}
        )
        and all(
            _rendered(item["label"], item["label"], MAX_DETAILS_LINE_CHARS, nullable=False)
            and canonical_stored_input_help(item["description"]) is not None
            and canonical_help_url(item["help_url"]) is not None
            for item in value["stored_inputs"]
        )
    )


def _details_items(items: object, minimum: int, maximum: int, keys: set[str]) -> bool:
    if not isinstance(items, list) or not minimum <= len(items) <= maximum:
        return False
    if not all(isinstance(item, dict) and set(item) == keys for item in items):
        return False
    ids = [item["id"] for item in items]
    return all(canonical_identifier(identifier) is not None for identifier in ids) and ids == sorted(set(ids))


def _turn_usage_count(value: object, maximum: int) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _turn_usage_model(value: object) -> tuple[str, str] | None:
    if not isinstance(value, dict) or set(value) != TURN_USAGE_MODEL_FIELDS:
        return None
    provider = value["provider"]
    model = value["model"]
    if (
        not isinstance(provider, str)
        or not isinstance(model, str)
        or TURN_USAGE_ID_RE.fullmatch(provider) is None
        or TURN_USAGE_ID_RE.fullmatch(model) is None
        or not _turn_usage_count(value["input_tokens"], MAX_TURN_USAGE_TOKENS)
        or not _turn_usage_count(value["output_tokens"], MAX_TURN_USAGE_TOKENS)
    ):
        return None
    return provider, model


# The Actions a turn withheld because readable attachment content was in it (ADR-0093): listed first by identity,
# at most this many, within this many canonical JSON bytes, with the turn's total beside them.
MAX_RESTRICTED_ACTIONS = 16
MAX_RESTRICTED_ACTION_TOTAL = 2_048
MAX_RESTRICTED_ACTIONS_BYTES = 2_048


def canonical_restricted_actions(value: object) -> dict[str, object] | None:
    """Return the exact Actions a completed turn withheld for its attachment content, or None.

    ``actions`` holds 1 to 16 distinct ``{assistant, action}`` identities in identity order, within the byte bound;
    ``total`` counts every withheld Action, at least as many as are listed. It names capabilities only and grants
    nothing.
    """
    if not isinstance(value, dict) or set(value) != {"actions", "total"}:
        return None
    actions, total = value["actions"], value["total"]
    if (
        not isinstance(actions, list)
        or not 1 <= len(actions) <= MAX_RESTRICTED_ACTIONS
        or type(total) is not int
        or not len(actions) <= total <= MAX_RESTRICTED_ACTION_TOTAL
    ):
        return None
    identities = []
    for item in actions:
        if (
            not isinstance(item, dict)
            or set(item) != {"assistant", "action"}
            or canonical_assistant_id(item["assistant"]) is None
            or canonical_action_id(item["action"]) is None
        ):
            return None
        identities.append((item["assistant"], item["action"]))
    if identities != sorted(set(identities)):
        return None
    projected = {"actions": [{"assistant": a, "action": b} for a, b in identities], "total": total}
    encoded = json.dumps(projected, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return projected if len(encoded) <= MAX_RESTRICTED_ACTIONS_BYTES else None


def canonical_turn_usage(value: object) -> dict[str, object] | None:
    """Return one exact completed-turn usage, or None when it breaks the closed shape.

    It is presentation metadata only: a duration and, per provider and model, the input and output tokens the
    provider responses reported. Models are distinct, sorted by provider then model, and at least one is present.
    """
    return _usage(value, 1)


def canonical_run_usage(value: object) -> dict[str, object] | None:
    """Return one exact Routine run usage, or None: a chat turn's shape, whose models may be none (ADR-0101).

    A run that called no model, a replay-only one, reports its active duration alone.
    """
    return _usage(value, 0)


def _usage(value: object, minimum: int) -> dict[str, object] | None:
    if (
        not isinstance(value, dict)
        or set(value) != {"duration_ms", "models"}
        or not _turn_usage_count(value["duration_ms"], MAX_TURN_DURATION_MS)
        or not isinstance(value["models"], list)
        or not minimum <= len(value["models"]) <= MAX_TURN_USAGE_MODELS
    ):
        return None
    keys = [_turn_usage_model(model) for model in value["models"]]
    if None in keys or keys != sorted(set(keys)):
        return None
    return {"duration_ms": value["duration_ms"], "models": [dict(model) for model in value["models"]]}


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
    # A null default recommends no option, as a Routine question does, so options never steer the person's choice.
    default_index = value["default_index"]
    if len({option["label"].casefold() for option in options}) != len(options) or (
        default_index is not None
        and (
            isinstance(default_index, bool)
            or not isinstance(default_index, int)
            or not 0 <= default_index < len(options)
        )
    ):
        raise _ClarificationShapeError
    return {"question": question, "options": options, "default_index": default_index}


def canonical_clarification(value: object) -> dict[str, object] | None:
    """Return one exact Brain multiple-choice clarification, or None when it breaks the closed shape (ADR-0081).

    Every text is already NFC, trimmed, and free of control and line-separator characters; labels are distinct
    ignoring case; the default points to the one recommended option. The shape is presentation only and carries no
    authority.
    """
    try:
        return _clarification(value)
    except _ClarificationShapeError:
        return None


# The labels Admin composes a clarified request with, in each interface language (ADR-0081): the original request, a
# blank line, then "<question>: <the question>" and "<answer>: <the person's answer>" on their own lines. A request may
# be clarified more than once, so these pairs may repeat. Team reads the person's own words out of such a message as
# its authored segments, in order (ADR-0101): the original request, then each answer, never a question.
CLARIFICATION_LABELS = {
    "ar": {"question": "السؤال", "answer": "الإجابة"},
    "de": {"question": "Frage", "answer": "Antwort"},
    "en": {"question": "Question", "answer": "Answer"},
    "es": {"question": "Pregunta", "answer": "Respuesta"},
    "fr": {"question": "Question", "answer": "Réponse"},
    "ja": {"question": "質問", "answer": "回答"},
    "pt": {"question": "Pergunta", "answer": "Resposta"},
    "zh": {"question": "问题", "answer": "回答"},
}


def compose_clarified(original: str, question: str, answer: str, locale: str) -> str:
    """The exact message a clarification's answer sends, as Admin composes it in ``locale``."""
    labels = CLARIFICATION_LABELS[locale]
    return f"{original.strip()}\n\n{labels['question']}: {question}\n{labels['answer']}: {answer.strip()}"


def authored_segments(message: str) -> tuple[str, ...]:
    """What a person wrote in a sent message, in order: its original text, then each answer, without any question.

    A later segment is the person's later word, so it wins over the request it repeats.
    """
    questions = tuple(f"{labels['question']}: " for labels in CLARIFICATION_LABELS.values())
    answers = tuple(f"{labels['answer']}: " for labels in CLARIFICATION_LABELS.values())
    segments: list[list[str]] = [[]]
    for line in message.split("\n"):
        answer = next((prefix for prefix in answers if line.startswith(prefix)), None)
        if line.startswith(questions):
            segments.append([])
        elif answer is not None:
            segments.append([line[len(answer) :]])
        else:
            segments[-1].append(line)
    return tuple(text for text in ("\n".join(lines).strip() for lines in segments) if text)


def render_clarification(clarification: dict[str, object]) -> str:
    """The exact plain reply that accompanies one canonical clarification: the question, then numbered options.

    A recommended default is marked with " ✓" (a null default marks none) and a non-empty description follows " — ".
    Every boundary requires a clarified reply to equal this rendering, so a reply can never say something the question
    does not.
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
    """Return one completed turn's exact memory changes, or None: `remember` carries a preference, `forget` none.

    At most 40 changes, enough to forget all 32 memories and all 8 skills in one turn.
    """
    if not isinstance(value, list) or len(value) > MAX_MEMORY_CHANGES:
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
