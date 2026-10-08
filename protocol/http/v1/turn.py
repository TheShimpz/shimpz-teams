"""Bounds of a chat turn that Team, the Brain, and Admin enforce.

A turn carries one user message; the Brain may end it with one clarification question or propose memory and skill
changes, and Team admits each only within these bounds. A skill's content key is derived the same way on both sides.
The reply, the Action labels, capability planning, intent routing, and the attachment content a turn carries are
bounded here too, so every side reads one definition of each.
"""

from __future__ import annotations

import hashlib
import json
import re

MAX_CHAT_MESSAGE_CHARS = 16_000
MAX_CLARIFICATION_QUESTION_CHARS = 240
MAX_CLARIFICATION_LABEL_CHARS = 80
MAX_CLARIFICATION_DESCRIPTION_CHARS = 160
MIN_CLARIFICATION_OPTIONS = 2
MAX_CLARIFICATION_OPTIONS = 5
MAX_MEMORIES = 32
MAX_MEMORY_PREFERENCE_CHARS = 280
MEMORY_TOPIC_PATTERN = r"^[a-z][a-z0-9-]{0,39}$"
MAX_SKILLS = 8
# One completed turn may forget every memory and every skill at once, and never change more.
MAX_MEMORY_CHANGES = MAX_MEMORIES + MAX_SKILLS
MIN_SKILL_STEPS = 2
MAX_SKILL_STEPS = 16
MAX_SKILL_INPUTS = 32
SKILL_KEY_PREFIX = "procedure-"
SKILL_KEY_PATTERN = rf"^{SKILL_KEY_PREFIX}[0-9a-f]{{12}}$"
SKILL_INPUT_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$"
# A completed turn's reply.
MAX_REPLY_CHARS = 60_000
# The Action requests one Brain suspension carries, and so the results the resume that answers it may carry.
MAX_ACTION_REQUESTS = 64
# The Action identifiers one label request names; each label's own bound is payload.MAX_ACTION_LABEL_CHARS.
MAX_ACTION_LABELS = 64
# The objective a capability plan or an intent route reasons over.
MAX_OBJECTIVE_CHARS = 16_000
# Capability planning over a closed shortlist of public Assistants.
MAX_CAPABILITY_CANDIDATES = 8
MAX_CAPABILITY_SELECTED = 4
MAX_CAPABILITY_NAME_CHARS = 80
MAX_CAPABILITY_SUMMARY_CHARS = 80
MAX_CAPABILITY_ACTIONS = 64
MAX_CAPABILITY_INTEGRATIONS = 16
# Intent routing of one fresh objective.
MAX_INTENT_ROUTE_CANDIDATES = 8
MAX_INTENT_ROUTE_SELECTED = 4
MAX_INTENT_ROUTE_QUERY_CHARS = 160
MAX_INTENT_ROUTE_NAME_CHARS = 80
MAX_INTENT_ROUTE_SUMMARY_CHARS = 80
MAX_INTENT_ROUTE_REPLY_CHARS = 240
# The attachment content a turn carries (ADR-0093); the file count is payload.MAX_CHAT_FILES. Every ceiling applies at
# once: one file's text, a message's text in all, and an image as its prepared derivative.
MAX_ATTACHMENT_IMAGES = 4
MAX_ATTACHMENT_TEXT_CHARS = 32_768
MAX_ATTACHMENT_TEXT_BYTES = 128 * 1024
MAX_ATTACHED_TEXT_CHARS = 131_072
MAX_ATTACHED_TEXT_BYTES = 512 * 1024
ATTACHMENT_IMAGE_EDGE = 1_568
ATTACHMENT_IMAGE_PIXELS = 1_200_000
MAX_ATTACHMENT_IMAGE_BYTES = 512 * 1024
# The complete encoded attachments field of one Brain request.
MAX_ATTACHMENTS_FIELD_BYTES = 1536 * 1024
# Why an attachment is not readable by the model.
ATTACHMENT_OPAQUE_REASONS = frozenset({"unsupported", "too_large", "encrypted", "no_text", "animated", "unreadable"})

# Every consumer matches these with fullmatch, so a trailing newline is refused.
MEMORY_TOPIC_RE = re.compile(MEMORY_TOPIC_PATTERN)
SKILL_KEY_RE = re.compile(SKILL_KEY_PATTERN)
SKILL_INPUT_RE = re.compile(SKILL_INPUT_PATTERN)


def skill_key(contracts: dict[str, str], steps: list[dict[str, object]]) -> str:
    """The content key of one skill: the same Actions, inputs, and contracts always name the same procedure."""
    body = json.dumps({"contracts": contracts, "steps": steps}, separators=(",", ":"), sort_keys=True)
    return SKILL_KEY_PREFIX + hashlib.sha256(body.encode()).hexdigest()[:12]
