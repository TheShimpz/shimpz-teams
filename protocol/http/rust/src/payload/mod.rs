//! Pure Team wire contract shared with its consumers (`payload.py`).
//!
//! The identifier grammars, the purpose sentence rule, and the chat-turn bounds live in their own modules and are
//! re-exported here, as Python re-exports them.

mod chat;
mod clarification;
mod details;
mod storage;

use std::sync::LazyLock;

use strict_json::Value;

pub use self::chat::{
    MAX_TURN_DURATION_MS, MAX_TURN_USAGE_MODELS, MAX_TURN_USAGE_TOKENS, TURN_USAGE_ID_RE,
    TURN_USAGE_MODEL_FIELDS, canonical_conversation, canonical_creator_links, canonical_help_url,
    canonical_local_team_name, canonical_pack_digest, canonical_run_usage,
    canonical_snapshot_summary, canonical_stored_input_help, canonical_team_name,
    canonical_turn_usage, rendered_text,
};
pub use self::clarification::{
    CLARIFICATION_LABELS, apply_memory_changes, authored_segments, canonical_clarification,
    canonical_memory, canonical_memory_changes, compose_clarified, render_clarification,
};
pub use self::details::{
    ACTION_EFFECTS, ASSISTANT_DETAILS_FIELDS, ASSISTANT_VERSION_RE, DETAILS_CREATOR_RE,
    GITHUB_REPOSITORY_PATTERN, GITHUB_REPOSITORY_RE, LOCAL_ASSISTANT_ADMISSIONS,
    LOCAL_ASSISTANT_CREATED_RE, LOCAL_ASSISTANT_CREATOR_RE, LOCAL_ASSISTANT_FIELDS,
    LOCAL_ASSISTANT_LABEL_FIELDS, LOCAL_ASSISTANT_PLATFORMS, LOCAL_ASSISTANT_REASONS,
    MAX_DETAILS_ACTIONS, MAX_DETAILS_CREATORS, MAX_DETAILS_DESCRIPTION_CHARS,
    MAX_DETAILS_INTEGRATIONS, MAX_DETAILS_LINE_CHARS, MAX_DETAILS_NAME_CHARS,
    MAX_DETAILS_STORED_INPUTS, MAX_LOCAL_ASSISTANT_CREATORS, MAX_LOCAL_ASSISTANT_TEXT_CHARS,
    MAX_LOCAL_ASSISTANTS, canonical_assistant_details, canonical_created_at,
    canonical_local_assistant,
};
pub use self::storage::{
    canonical_file_id, canonical_filename, canonical_media_type, integer, project_file_metadata,
    project_storage_response, project_storage_usage,
};
pub use crate::identifiers::{
    ACTION_ID_PATTERN, ACTION_ID_RE, ASSISTANT_ID_PATTERN, ASSISTANT_ID_RE, IDENTIFIER_RE,
    MAX_ACTION_ID_CHARS, MAX_ASSISTANT_ID_CHARS, MAX_IDENTIFIER_CHARS, TEAM_ID_PATTERN, TEAM_ID_RE,
    canonical_action_id, canonical_assistant_id, canonical_identifier, canonical_team_id,
};
pub use crate::purpose::{MAX_PURPOSE_CHARS, canonical_purpose};
use crate::python::regex::{Pattern, declared};
use crate::python::value::{exact_object, field, object_of, string, whole};
pub use crate::turn::{
    MAX_CHAT_MESSAGE_CHARS, MAX_CLARIFICATION_DESCRIPTION_CHARS, MAX_CLARIFICATION_LABEL_CHARS,
    MAX_CLARIFICATION_OPTIONS, MAX_CLARIFICATION_QUESTION_CHARS, MAX_MEMORIES, MAX_MEMORY_CHANGES,
    MAX_MEMORY_PREFERENCE_CHARS, MEMORY_TOPIC_RE, MIN_CLARIFICATION_OPTIONS,
};

/// A Team file id.
pub const FILE_ID_PATTERN: &str = r"^[0-9a-f]{32}$";
/// A lowercase SHA-256 hex digest.
pub const SHA256_PATTERN: &str = r"^[0-9a-f]{64}$";
/// A `sha256:` digest.
pub const SOURCE_DIGEST_PATTERN: &str = r"^sha256:[0-9a-f]{64}$";
/// A media type.
pub const MEDIA_TYPE_PATTERN: &str = r"^[a-z0-9][a-z0-9!#$&^_.+\-]*/[a-z0-9][a-z0-9!#$&^_.+\-]*$";
/// A Stored Input's key page: one canonical public https URL that WHATWG URL serialization prints unchanged, with a
/// path, an optional query, and no port, credentials, fragment, or dot segment.
pub const HELP_URL_PATTERN: &str = concat!(
    r"^https://(?=[^/]{1,253}/)",
    r"(?![^/]*\.(?:arpa|example|home|internal|invalid|lan|local|localdomain|localhost|onion|test)/)",
    r"(?:(?!xn--)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?!xn--)[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?",
    r"(?:/(?!\.\.?(?:/|\?|(?![\s\S])))(?:[A-Za-z0-9._~!$&()*+,;=:@-]|%(?!2E)[0-9A-F]{2})*)+",
    r"(?:\?(?:[A-Za-z0-9._~!$&()*+,;=:@/?-]|%[0-9A-F]{2})+)?(?![\s\S])",
);
/// The longest help URL.
pub const MAX_HELP_URL_CHARS: usize = 2_048;
/// The longest Stored Input help text.
pub const MAX_STORED_INPUT_HELP_CHARS: usize = 500;
/// The Creator link kinds in canonical display order, each with the prefixes its URL may start with.
pub const CREATOR_LINK_PREFIXES: [(&str, &[&str]); 6] = [
    ("site", &["https://"]),
    ("github", &["https://github.com/"]),
    ("x", &["https://x.com/"]),
    (
        "youtube",
        &["https://youtube.com/", "https://www.youtube.com/"],
    ),
    (
        "linkedin",
        &["https://linkedin.com/", "https://www.linkedin.com/"],
    ),
    (
        "instagram",
        &["https://instagram.com/", "https://www.instagram.com/"],
    ),
];
/// The longest Creator link.
pub const MAX_CREATOR_LINK_CHARS: usize = 256;

/// [`FILE_ID_PATTERN`] compiled.
pub static FILE_ID_RE: LazyLock<Pattern> = LazyLock::new(|| declared(FILE_ID_PATTERN));
/// [`SHA256_PATTERN`] compiled.
pub static SHA256_RE: LazyLock<Pattern> = LazyLock::new(|| declared(SHA256_PATTERN));
/// [`SOURCE_DIGEST_PATTERN`] compiled.
pub static SOURCE_DIGEST_RE: LazyLock<Pattern> = LazyLock::new(|| declared(SOURCE_DIGEST_PATTERN));
/// [`MEDIA_TYPE_PATTERN`] compiled.
pub static MEDIA_TYPE_RE: LazyLock<Pattern> = LazyLock::new(|| declared(MEDIA_TYPE_PATTERN));
/// [`HELP_URL_PATTERN`] compiled.
pub static HELP_URL_RE: LazyLock<Pattern> = LazyLock::new(|| declared(HELP_URL_PATTERN));

/// The most files one chat turn carries.
pub const MAX_CHAT_FILES: usize = 8;
/// The most Assistants one Team may have installed.
pub const MAX_TEAM_ASSISTANTS: usize = 16;
/// A chat turn may name every installed Assistant.
pub const MAX_CHAT_ASSISTANTS: usize = MAX_TEAM_ASSISTANTS;
/// The largest answer of one Team's Assistant registry.
pub const MAX_TEAM_REGISTRY_BYTES: usize = 256 * 1024;
/// The most files of one Team.
pub const MAX_TEAM_FILES: usize = 256;
/// The longest Team name.
pub const MAX_TEAM_NAME_CHARS: usize = 80;
/// The largest uploaded file.
pub const MAX_FILE_UPLOAD_BYTES: i64 = 25 * 1024 * 1024;
/// The longest file name in UTF-8 bytes.
pub const MAX_FILENAME_BYTES: usize = 255;
/// The longest media type.
pub const MAX_MEDIA_TYPE_CHARS: usize = 127;
/// The most committed presentation history entries a chat turn carries.
pub const MAX_CONVERSATION_ENTRIES: usize = 8;
/// The longest history entry text.
pub const MAX_CONVERSATION_TEXT_CHARS: usize = 512;
/// The longest history text in all.
pub const MAX_CONVERSATION_CHARS: usize = 4_096;
/// The members of a chat body, sorted.
pub const CHAT_BODY_FIELDS: [&str; 8] = [
    "assistant_ids",
    "conversation",
    "files",
    "locale",
    "message",
    "refine",
    "request",
    "timezone",
];
/// The members of a request identity, sorted.
pub const REQUEST_IDENTITY_FIELDS: [&str; 2] = ["issued_at", "nonce"];
/// How long a request identity may mutate a Routine after Admin issued it.
pub const REQUEST_IDENTITY_SECONDS: i64 = 900;
/// How far ahead of Team's clock a request identity may be.
pub const REQUEST_IDENTITY_SKEW_SECONDS: i64 = 60;
/// A request nonce.
pub static REQUEST_NONCE_RE: LazyLock<Pattern> = LazyLock::new(|| declared(r"[0-9a-f]{32}\Z"));
/// The closed Admin interface languages, sorted.
pub const CHAT_LOCALES: [&str; 8] = ["ar", "de", "en", "es", "fr", "ja", "pt", "zh"];
/// The members of a snapshot summary, sorted.
pub const SNAPSHOT_SUMMARY_FIELDS: [&str; 2] = ["locale", "summary"];
/// The longest snapshot summary.
pub const MAX_SNAPSHOT_SUMMARY_CHARS: usize = 80;
/// How many chat turns the Space runs at once, at most.
pub const MAX_CHAT_LIMIT: i64 = 2;

/// `canonical_locale`: one closed interface language code.
#[must_use]
pub fn canonical_locale(value: &Value) -> Option<&str> {
    string(value).filter(|locale| CHAT_LOCALES.contains(locale))
}

/// `request_identity_fresh`: whether an identity issued at `issued_at` may still change a Routine at `now`.
#[must_use]
pub const fn request_identity_fresh(issued_at: i64, now: i64) -> bool {
    now - REQUEST_IDENTITY_SECONDS < issued_at && issued_at <= now + REQUEST_IDENTITY_SKEW_SECONDS
}

/// `canonical_request_identity`: a whole-second issue instant and a 32-hex nonce, as `{issued_at, nonce}`.
#[must_use]
pub fn canonical_request_identity(value: &Value) -> Option<Value> {
    let members = exact_object(value, &REQUEST_IDENTITY_FIELDS)?;
    let (issued_at, nonce) = (field(members, "issued_at"), field(members, "nonce"));
    let fresh = whole(issued_at, 1, (1 << 40) - 1);
    (fresh && REQUEST_NONCE_RE.is_full_value(nonce)).then(|| {
        object_of(vec![
            ("issued_at", issued_at.clone()),
            ("nonce", nonce.clone()),
        ])
    })
}

/// `canonical_source_digest`: one `sha256:` digest.
#[must_use]
pub fn canonical_source_digest(value: &Value) -> Option<&str> {
    string(value).filter(|digest| SOURCE_DIGEST_RE.is_full(digest))
}

/// `canonical_chat_limit`: exactly `{"limit": n}`, `n` an integer from 1 to [`MAX_CHAT_LIMIT`].
#[must_use]
pub fn canonical_chat_limit(value: &Value) -> Option<Value> {
    let members = exact_object(value, &["limit"])?;
    let limit = field(members, "limit");
    whole(limit, 1, MAX_CHAT_LIMIT).then(|| object_of(vec![("limit", limit.clone())]))
}
