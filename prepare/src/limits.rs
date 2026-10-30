//! The ADR-0093 attachment ceilings, shared by the controller and the networkless preparation helper.
//!
//! Every ceiling applies at the same time. A file beyond a per-file ceiling becomes opaque with a closed reason; a
//! message beyond a per-message ceiling is refused before dispatch. Nothing is truncated or silently dropped. The
//! ceilings the Brain enforces too are the Team HTTP protocol's own, never a copy.

use team_protocol::{payload, preview, turn};

const MIB: usize = 1024 * 1024;

/// The most files one message selects.
pub const MAX_SELECTED_FILES: usize = payload::MAX_CHAT_FILES;
/// The most original bytes one message selects.
pub const MAX_SELECTED_ORIGINAL_BYTES: u64 = 32 * 1024 * 1024;

/// The largest text, code, CSV, JSON, or Markdown original.
pub const MAX_TEXT_SOURCE_BYTES: usize = MIB;
/// The longest text of one file, in characters.
pub const MAX_TEXT_CHARACTERS: usize = turn::MAX_ATTACHMENT_TEXT_CHARS;
/// The longest text of one file, in UTF-8 bytes.
pub const MAX_TEXT_BYTES: usize = turn::MAX_ATTACHMENT_TEXT_BYTES;
/// The longest text of one message, in characters.
pub const MAX_MESSAGE_TEXT_CHARACTERS: usize = turn::MAX_ATTACHED_TEXT_CHARS;
/// The longest text of one message, in UTF-8 bytes.
pub const MAX_MESSAGE_TEXT_BYTES: usize = turn::MAX_ATTACHED_TEXT_BYTES;

/// The largest PDF original.
pub const MAX_PDF_BYTES: usize = 8 * MIB;
/// The most pages a PDF may have.
pub const MAX_PDF_PAGES: i64 = preview::MAX_PDF_PAGES;
/// The most bytes one page's decoded content streams may hold.
pub const MAX_PDF_PAGE_STREAM_BYTES: usize = 4 * MIB;
/// The most bytes every page's decoded content streams may hold together.
pub const MAX_PDF_FILE_STREAM_BYTES: usize = 16 * MIB;

/// The largest JPEG, PNG, or static WebP original.
pub const MAX_IMAGE_BYTES: usize = 8 * MIB;
/// The most pixels an original image may declare.
pub const MAX_IMAGE_PIXELS: u64 = 10_000_000;
/// The longest edge an original image may declare.
pub const MAX_IMAGE_EDGE: u32 = 8_192;
/// The most images one message carries.
pub const MAX_MESSAGE_IMAGES: usize = turn::MAX_ATTACHMENT_IMAGES;
/// The longest edge of a derivative.
pub const DERIVATIVE_LONG_EDGE: usize = turn::ATTACHMENT_IMAGE_EDGE;
/// The most pixels of a derivative.
pub const DERIVATIVE_PIXELS: usize = turn::ATTACHMENT_IMAGE_PIXELS;
/// The largest encoded derivative.
pub const MAX_DERIVATIVE_BYTES: usize = turn::MAX_ATTACHMENT_IMAGE_BYTES;

/// The complete encoded attachments field of one Brain request.
pub const MAX_ATTACHMENTS_FIELD_BYTES: usize = turn::MAX_ATTACHMENTS_FIELD_BYTES;

/// The helper's memory (and memory plus swap).
pub const HELPER_MEMORY_BYTES: i64 = 256 * 1024 * 1024;
/// The helper's CPU quota: half a processor.
pub const HELPER_NANO_CPUS: i64 = 500_000_000;
/// The helper's process and thread ceiling.
pub const HELPER_PIDS: i64 = 128;
/// The CPU seconds one worker process may use.
pub const HELPER_CPU_SECONDS: u64 = 5;
/// The descriptors one worker process may hold.
pub const HELPER_OPEN_FILES: u64 = 64;
/// The wall time of one file's exec, from its start to its exit state.
pub const HELPER_WALL: std::time::Duration = std::time::Duration::from_secs(10);
/// One request frame's bounded JSON header line.
pub const MAX_HELPER_HEADER_BYTES: usize = 256;
/// One request frame: the header line and at most one original.
pub const MAX_HELPER_INPUT_BYTES: usize = MAX_HELPER_HEADER_BYTES + 1 + MAX_PDF_BYTES;
/// The largest helper answer: a 512 KiB derivative in base64, or 128 KiB of text escaped as ASCII JSON.
pub const MAX_HELPER_OUTPUT_BYTES: usize = MIB;

/// Why an attachment is not readable by the model, sorted.
pub const OPAQUE_REASONS: [&str; 6] = turn::ATTACHMENT_OPAQUE_REASONS;
