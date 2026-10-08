"""The ADR-0093 attachment ceilings, shared by the controller and the networkless preparation helper.

Every ceiling applies at the same time. A file beyond a per-file ceiling becomes opaque with a closed reason; a message
beyond a per-message ceiling is refused before dispatch. Nothing is truncated or silently dropped. The ceilings the
Brain enforces too are the Team HTTP protocol's own (``protocol/http/v1/turn.py``), never a copy.
"""

from __future__ import annotations

from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import turn as http_turn

MIB = 1024 * 1024

# Selection.
MAX_SELECTED_FILES = http_payload.MAX_CHAT_FILES
MAX_SELECTED_ORIGINAL_BYTES = 32 * MIB

# Text, code, CSV, JSON, and Markdown.
MAX_TEXT_SOURCE_BYTES = 1 * MIB
MAX_TEXT_CHARACTERS = http_turn.MAX_ATTACHMENT_TEXT_CHARS
MAX_TEXT_BYTES = http_turn.MAX_ATTACHMENT_TEXT_BYTES
MAX_MESSAGE_TEXT_CHARACTERS = http_turn.MAX_ATTACHED_TEXT_CHARS
MAX_MESSAGE_TEXT_BYTES = http_turn.MAX_ATTACHED_TEXT_BYTES

# PDF, text only.
MAX_PDF_BYTES = 8 * MIB
MAX_PDF_PAGES = 50
MAX_PDF_PAGE_STREAM_BYTES = 4 * MIB
MAX_PDF_FILE_STREAM_BYTES = 16 * MIB

# Images: JPEG, PNG, and static WebP.
MAX_IMAGE_BYTES = 8 * MIB
MAX_IMAGE_PIXELS = 10_000_000
MAX_IMAGE_EDGE = 8_192
MAX_MESSAGE_IMAGES = http_turn.MAX_ATTACHMENT_IMAGES
DERIVATIVE_LONG_EDGE = http_turn.ATTACHMENT_IMAGE_EDGE
DERIVATIVE_PIXELS = http_turn.ATTACHMENT_IMAGE_PIXELS
MAX_DERIVATIVE_BYTES = http_turn.MAX_ATTACHMENT_IMAGE_BYTES

# The complete encoded attachments field of one Brain request.
MAX_ATTACHMENTS_FIELD_BYTES = http_turn.MAX_ATTACHMENTS_FIELD_BYTES

# The networkless preparation helper: one container per preparation segment, one fresh process per file.
HELPER_MEMORY_BYTES = 256 * MIB
HELPER_NANO_CPUS = 500_000_000
HELPER_PIDS = 128
HELPER_CPU_SECONDS = 5
HELPER_WALL_SECONDS = 10.0
# One request frame: a bounded JSON header line and at most one 8 MiB original.
MAX_HELPER_HEADER_BYTES = 256
MAX_HELPER_INPUT_BYTES = MAX_HELPER_HEADER_BYTES + max(MAX_PDF_BYTES, MAX_IMAGE_BYTES)
# The largest helper answer: a 512 KiB derivative in base64, or 128 KiB of text escaped as ASCII JSON.
MAX_HELPER_OUTPUT_BYTES = 1 * MIB

# Closed reasons an attachment is not readable by the model.
OPAQUE_REASONS = http_turn.ATTACHMENT_OPAQUE_REASONS
