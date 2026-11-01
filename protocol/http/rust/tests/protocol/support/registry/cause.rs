//! `cause`.

use team_protocol::cause;

use super::{Entry, value};

/// Every `cause` entry.
pub const ENTRIES: &[Entry] = &[("cause.canonical_cause", |v| {
    value(cause::canonical_cause(v))
})];
