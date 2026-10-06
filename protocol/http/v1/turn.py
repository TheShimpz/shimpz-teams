"""Bounds of a chat turn that both Team and the Brain enforce.

A turn carries one user message; the Brain may end it with one clarification question or propose memory and skill
changes, and Team admits each only within these bounds. A skill's content key is derived the same way on both sides.
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

# Every consumer matches these with fullmatch, so a trailing newline is refused.
MEMORY_TOPIC_RE = re.compile(MEMORY_TOPIC_PATTERN)
SKILL_KEY_RE = re.compile(SKILL_KEY_PATTERN)
SKILL_INPUT_RE = re.compile(SKILL_INPUT_PATTERN)


def skill_key(contracts: dict[str, str], steps: list[dict[str, object]]) -> str:
    """The content key of one skill: the same Actions, inputs, and contracts always name the same procedure."""
    body = json.dumps({"contracts": contracts, "steps": steps}, separators=(",", ":"), sort_keys=True)
    return SKILL_KEY_PREFIX + hashlib.sha256(body.encode()).hexdigest()[:12]
