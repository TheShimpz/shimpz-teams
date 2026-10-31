//! Bounds of a chat turn that Team, the Brain, and Admin enforce.
//!
//! A turn carries one user message; the Brain may end it with one clarification question or propose memory changes,
//! and Team admits each only within these bounds. The reply, capability planning, intent routing, and the attachment
//! content a turn carries are bounded here too.

use std::sync::LazyLock;

use crate::python::regex::{Pattern, declared};

/// The longest chat message.
pub const MAX_CHAT_MESSAGE_CHARS: usize = 16_000;
/// The longest clarification question.
pub const MAX_CLARIFICATION_QUESTION_CHARS: usize = 240;
/// The longest clarification option label.
pub const MAX_CLARIFICATION_LABEL_CHARS: usize = 80;
/// The longest clarification option description.
pub const MAX_CLARIFICATION_DESCRIPTION_CHARS: usize = 160;
/// The fewest clarification options.
pub const MIN_CLARIFICATION_OPTIONS: usize = 2;
/// The most clarification options.
pub const MAX_CLARIFICATION_OPTIONS: usize = 5;
/// The most memories a Team keeps.
pub const MAX_MEMORIES: usize = 32;
/// The longest memory preference.
pub const MAX_MEMORY_PREFERENCE_CHARS: usize = 280;
/// A memory topic.
pub const MEMORY_TOPIC_PATTERN: &str = r"^[a-z][a-z0-9-]{0,39}$";
/// One completed turn may forget every memory at once, and never change more.
pub const MAX_MEMORY_CHANGES: usize = MAX_MEMORIES;
/// The longest completed turn's reply.
pub const MAX_REPLY_CHARS: usize = 60_000;
/// A reply never carries a control character other than a tab, line feed, or carriage return.
pub static REPLY_CONTROL_RE: LazyLock<Pattern> =
    LazyLock::new(|| declared(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"));
/// The Action requests one Brain suspension carries.
pub const MAX_ACTION_REQUESTS: usize = 64;
/// The objective a capability plan or an intent route reasons over.
pub const MAX_OBJECTIVE_CHARS: usize = 16_000;
/// Capability planning: the most candidates.
pub const MAX_CAPABILITY_CANDIDATES: usize = 8;
/// Capability planning: the most selected.
pub const MAX_CAPABILITY_SELECTED: usize = 4;
/// Capability planning: the longest name.
pub const MAX_CAPABILITY_NAME_CHARS: usize = 80;
/// Capability planning: the longest summary.
pub const MAX_CAPABILITY_SUMMARY_CHARS: usize = 80;
/// Capability planning: the most Actions.
pub const MAX_CAPABILITY_ACTIONS: usize = 64;
/// Capability planning: the most Integrations.
pub const MAX_CAPABILITY_INTEGRATIONS: usize = 16;
/// Intent routing: the most candidates.
pub const MAX_INTENT_ROUTE_CANDIDATES: usize = 8;
/// Intent routing: the most selected.
pub const MAX_INTENT_ROUTE_SELECTED: usize = 4;
/// Intent routing: the longest query.
pub const MAX_INTENT_ROUTE_QUERY_CHARS: usize = 160;
/// Intent routing: the longest name.
pub const MAX_INTENT_ROUTE_NAME_CHARS: usize = 80;
/// Intent routing: the longest summary.
pub const MAX_INTENT_ROUTE_SUMMARY_CHARS: usize = 80;
/// Intent routing: the longest reply.
pub const MAX_INTENT_ROUTE_REPLY_CHARS: usize = 240;
/// The largest capability plan request body.
pub const MAX_CAPABILITY_PLAN_BODY_BYTES: usize = 256 * 1024;
/// The largest intent route request body.
pub const MAX_INTENT_ROUTE_BODY_BYTES: usize = 192 * 1024;
/// The most images a turn's attachments carry.
pub const MAX_ATTACHMENT_IMAGES: usize = 4;
/// The longest text of one attached file, in characters.
pub const MAX_ATTACHMENT_TEXT_CHARS: usize = 32_768;
/// The longest text of one attached file, in bytes.
pub const MAX_ATTACHMENT_TEXT_BYTES: usize = 128 * 1024;
/// The longest attached text of one message, in characters.
pub const MAX_ATTACHED_TEXT_CHARS: usize = 131_072;
/// The longest attached text of one message, in bytes.
pub const MAX_ATTACHED_TEXT_BYTES: usize = 512 * 1024;
/// The longest edge of a prepared attachment image.
pub const ATTACHMENT_IMAGE_EDGE: usize = 1_568;
/// The most pixels of a prepared attachment image.
pub const ATTACHMENT_IMAGE_PIXELS: usize = 1_200_000;
/// The largest prepared attachment image.
pub const MAX_ATTACHMENT_IMAGE_BYTES: usize = 512 * 1024;
/// The complete encoded attachments field of one Brain request.
pub const MAX_ATTACHMENTS_FIELD_BYTES: usize = 1536 * 1024;
/// Why an attachment is not readable by the model, sorted.
pub const ATTACHMENT_OPAQUE_REASONS: [&str; 6] = [
    "animated",
    "encrypted",
    "no_text",
    "too_large",
    "unreadable",
    "unsupported",
];
/// A thread, interrupt, or model id Team and the Brain exchange, and every opaque id Team's Action journal keeps.
pub static SAFE_ID_RE: LazyLock<Pattern> =
    LazyLock::new(|| declared(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z"));
/// [`MEMORY_TOPIC_PATTERN`] compiled.
pub static MEMORY_TOPIC_RE: LazyLock<Pattern> = LazyLock::new(|| declared(MEMORY_TOPIC_PATTERN));
