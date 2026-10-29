//! `sign_in`.

use team_protocol::sign_in;

use super::{Entry, borrowed, text};

/// The entries of `sign_in`.
pub const ENTRIES: &[Entry] = &[
    ("sign_in.canonical_site", |v| {
        text(sign_in::canonical_site(v))
    }),
    ("sign_in.start_site", |v| {
        text(sign_in::start_site(v).as_deref())
    }),
    ("sign_in.canonical_origin", |v| {
        text(sign_in::canonical_origin(v))
    }),
    ("sign_in.canonical_start", |v| {
        borrowed(sign_in::canonical_start(v))
    }),
    ("sign_in.canonical_session", |v| {
        borrowed(sign_in::canonical_session(v))
    }),
    ("sign_in.canonical_delete", |v| {
        borrowed(sign_in::canonical_delete(v))
    }),
    ("sign_in.canonical_started", |v| {
        borrowed(sign_in::canonical_started(v))
    }),
    ("sign_in.canonical_sign_in", |v| {
        borrowed(sign_in::canonical_sign_in(v))
    }),
    ("sign_in.canonical_current", |v| {
        borrowed(sign_in::canonical_current(v))
    }),
    ("sign_in.canonical_saved", |v| {
        borrowed(sign_in::canonical_saved(v))
    }),
    ("sign_in.canonical_deleted", |v| {
        borrowed(sign_in::canonical_deleted(v))
    }),
    ("sign_in.canonical_cancelled", |v| {
        borrowed(sign_in::canonical_cancelled(v))
    }),
];
