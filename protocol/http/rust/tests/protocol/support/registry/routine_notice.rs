//! `routine_notice`.

use strict_json::Value;
use team_protocol::routine_notice;

use super::{Entry, Outcome, arguments, borrowed, flag, value};

fn notice_usage(value: &Value) -> Outcome {
    arguments(value, ["outcome", "usage"]).map_or(Outcome::Refuse, |[outcome, usage]| {
        flag(routine_notice::notice_usage(outcome, usage))
    })
}

fn notice_detail(value: &Value) -> Outcome {
    arguments(value, ["outcome", "detail"]).map_or(Outcome::Refuse, |[outcome, detail]| {
        borrowed(routine_notice::canonical_notice_detail(outcome, detail))
    })
}

/// The entries of `routine_notice`.
pub const ENTRIES: &[Entry] = &[
    ("routine_notice.instant", |v| {
        flag(routine_notice::instant(v))
    }),
    ("routine_notice.notice_usage", notice_usage),
    ("routine_notice.canonical_notice_detail", notice_detail),
    ("routine_notice.canonical_routine_view", |v| {
        borrowed(routine_notice::canonical_routine_view(v))
    }),
    ("routine_notice.canonical_run_view", |v| {
        borrowed(routine_notice::canonical_run_view(v))
    }),
    ("routine_notice.canonical_incident_view", |v| {
        borrowed(routine_notice::canonical_incident_view(v))
    }),
    ("routine_notice.canonical_notice", |v| {
        borrowed(routine_notice::canonical_notice(v))
    }),
    ("routine_notice.canonical_notice_batch", |v| {
        value(routine_notice::canonical_notice_batch(v))
    }),
];
