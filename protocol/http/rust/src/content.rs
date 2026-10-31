//! A Team file's original: the framing of the bytes Team sends a Supervisor's browser to download.
//!
//! Team sends a listed file's stored bytes only after they matched their stored digest, as an octet stream of exactly
//! `length` bytes whose SHA-256 is `sha256`; Admin verifies both before it sends a byte on.

use std::sync::LazyLock;

use strict_json::Value;

use crate::payload::MAX_FILE_UPLOAD_BYTES;
use crate::python::regex::{Pattern, declared};
use crate::python::value::{exact_object, field, whole};

/// The largest original Team sends.
pub const MAX_CONTENT_BYTES: i64 = MAX_FILE_UPLOAD_BYTES;
/// A lowercase SHA-256 digest, matched whole.
pub static SHA256_RE: LazyLock<Pattern> = LazyLock::new(|| declared("[0-9a-f]{64}"));

/// `canonical_framing`: exactly `{"length", "sha256"}` of an admissible file content response, or `None`.
#[must_use]
pub fn canonical_framing(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &["length", "sha256"])?;
    (whole(field(members, "length"), 1, MAX_CONTENT_BYTES)
        && SHA256_RE.is_full_value(field(members, "sha256")))
    .then_some(value)
}
