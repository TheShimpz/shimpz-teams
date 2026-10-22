"""Bounds of a chat turn that Team, the Brain, and Admin enforce.

A turn carries one user message; the Brain may end it with one clarification question or propose memory changes, and
Team admits each only within these bounds. The reply, capability planning, intent routing, and the attachment content a
turn carries are bounded here too, so every side reads one definition of each.
"""

from __future__ import annotations

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
# One completed turn may forget every memory at once, and never change more.
MAX_MEMORY_CHANGES = MAX_MEMORIES
# A completed turn's reply, which never carries a control character other than a tab, line feed, or carriage return.
MAX_REPLY_CHARS = 60_000
REPLY_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# The Action requests one Brain suspension carries, and so the results the resume that answers it may carry.
MAX_ACTION_REQUESTS = 64
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
# The largest request body Team admits, and Admin sends, for one capability plan or one intent route: every text bound
# at its JSON worst case (6 bytes for one code point), so a message as long as a chat message admits is always routed
# and planned. Team's tests serialize the largest admissible request of each to prove it fits.
MAX_CAPABILITY_PLAN_BODY_BYTES = 256 * 1024
MAX_INTENT_ROUTE_BODY_BYTES = 192 * 1024
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
# A thread, interrupt, or model id Team and the Brain exchange, and every opaque id Team's Action journal keeps.
SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
MEMORY_TOPIC_RE = re.compile(MEMORY_TOPIC_PATTERN)
