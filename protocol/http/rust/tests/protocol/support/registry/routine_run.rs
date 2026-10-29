//! `routine_run`.

use strict_json::Value;
use team_protocol::routine_run;

use super::{Entry, Outcome, arguments, borrowed, integer, small, value};

/// The units `active_seconds` is called with: any count Team can hold, within 2^40 either way.
const UNITS: i64 = 1 << 40;

fn active_seconds(value: &Value) -> Outcome {
    match small(value) {
        Some(units) if (-UNITS..=UNITS).contains(&units) => {
            integer(routine_run::active_seconds(units))
        }
        _ => Outcome::Refuse,
    }
}

fn run_step(value: &Value) -> Outcome {
    match arguments(value, ["value", "position", "steps"]) {
        Some([step, position, steps]) => small(position).map_or(Outcome::Refuse, |position| {
            borrowed(routine_run::canonical_run_step(step, position, steps))
        }),
        None => Outcome::Refuse,
    }
}

/// The entries of `routine_run`.
pub const ENTRIES: &[Entry] = &[
    ("routine_run.canonical_card", |v| {
        borrowed(routine_run::canonical_card(v))
    }),
    ("routine_run.canonical_card_answer_request", |v| {
        value(routine_run::canonical_card_answer_request(v))
    }),
    ("routine_run.canonical_card_answer", |v| {
        borrowed(routine_run::canonical_card_answer(v))
    }),
    ("routine_run.canonical_challenge_open", |v| {
        value(routine_run::canonical_challenge_open(v))
    }),
    ("routine_run.canonical_claim_request", |v| {
        value(routine_run::canonical_claim_request(v))
    }),
    ("routine_run.active_seconds", active_seconds),
    ("routine_run.canonical_claim", |v| {
        value(routine_run::canonical_claim(v))
    }),
    ("routine_run.canonical_segment_request", |v| {
        value(routine_run::canonical_segment_request(v))
    }),
    ("routine_run.canonical_segment_answer", |v| {
        borrowed(routine_run::canonical_segment_answer(v))
    }),
    ("routine_run.canonical_failure", |v| {
        borrowed(routine_run::canonical_failure(v))
    }),
    ("routine_run.canonical_diagnostic", |v| {
        borrowed(routine_run::canonical_diagnostic(v))
    }),
    ("routine_run.canonical_diagnostics", |v| {
        borrowed(routine_run::canonical_diagnostics(v))
    }),
    ("routine_run.canonical_run_step", run_step),
    ("routine_run.canonical_run_steps", |v| {
        borrowed(routine_run::canonical_run_steps(v))
    }),
];
