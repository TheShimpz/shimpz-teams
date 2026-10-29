//! Closed Team, Assistant, Action, and Assistant-declared identifiers (`identifiers.py`).
//!
//! Team ids and Action ids are Team's own. An Assistant id and the identifier of an Assistant's Integration, provider,
//! or Stored Input follow the Developers published-Assistant protocol: one grammar with two bounds.

use std::sync::LazyLock;

use strict_json::Value;

use crate::python::regex::{Pattern, declared};
use crate::python::text;

/// A Team id.
pub const TEAM_ID_PATTERN: &str = r"^[a-z0-9_]{1,40}$";
/// An Assistant, Integration, provider, or Stored Input identifier.
pub const IDENTIFIER_PATTERN: &str = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$";
/// An Assistant id (the identifier grammar).
pub const ASSISTANT_ID_PATTERN: &str = IDENTIFIER_PATTERN;
/// An Action id.
pub const ACTION_ID_PATTERN: &str = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$";
/// The longest Assistant id.
pub const MAX_ASSISTANT_ID_CHARS: usize = 40;
/// The longest identifier.
pub const MAX_IDENTIFIER_CHARS: usize = 64;
/// The longest Action id.
pub const MAX_ACTION_ID_CHARS: usize = 128;

/// [`TEAM_ID_PATTERN`] compiled.
pub static TEAM_ID_RE: LazyLock<Pattern> = LazyLock::new(|| declared(TEAM_ID_PATTERN));
/// [`IDENTIFIER_PATTERN`] compiled.
pub static IDENTIFIER_RE: LazyLock<Pattern> = LazyLock::new(|| declared(IDENTIFIER_PATTERN));
/// [`ASSISTANT_ID_PATTERN`] compiled (the identifier grammar).
pub static ASSISTANT_ID_RE: &LazyLock<Pattern> = &IDENTIFIER_RE;
/// [`ACTION_ID_PATTERN`] compiled.
pub static ACTION_ID_RE: LazyLock<Pattern> = LazyLock::new(|| declared(ACTION_ID_PATTERN));

fn bounded<'a>(value: &'a Value, pattern: &Pattern, maximum: usize) -> Option<&'a str> {
    match value {
        Value::String(text) if text::len(text) <= maximum && pattern.is_full(text) => Some(text),
        _ => None,
    }
}

/// `canonical_team_id`.
#[must_use]
pub fn canonical_team_id(value: &Value) -> Option<&str> {
    match value {
        Value::String(text) if TEAM_ID_RE.is_full(text) => Some(text),
        _ => None,
    }
}

/// `canonical_assistant_id`.
#[must_use]
pub fn canonical_assistant_id(value: &Value) -> Option<&str> {
    bounded(value, ASSISTANT_ID_RE, MAX_ASSISTANT_ID_CHARS)
}

/// `canonical_identifier`: one Integration, provider, or Stored Input identifier.
#[must_use]
pub fn canonical_identifier(value: &Value) -> Option<&str> {
    bounded(value, &IDENTIFIER_RE, MAX_IDENTIFIER_CHARS)
}

/// `canonical_action_id`.
#[must_use]
pub fn canonical_action_id(value: &Value) -> Option<&str> {
    bounded(value, &ACTION_ID_RE, MAX_ACTION_ID_CHARS)
}
