//! `routine_context`.

use strict_json::Value;
use team_protocol::python::regex::MatchError;
use team_protocol::routine_context;

use super::{Entry, Outcome, arguments, borrowed, flag, string};

/// A `today` argument: an ISO `YYYY-MM-DD` date of ASCII digits that is a real date.
fn today(value: &Value) -> Option<(i32, i32, i32)> {
    let text = string(value)?;
    let bytes = text.as_bytes();
    let shaped = bytes.len() == 10
        && bytes.iter().enumerate().all(|(index, byte)| {
            if index == 4 || index == 7 {
                *byte == b'-'
            } else {
                byte.is_ascii_digit()
            }
        });
    if !shaped {
        return None;
    }
    let number = |range: std::ops::Range<usize>| text[range].parse::<i32>().unwrap_or(0);
    let (year, month, day) = (number(0..4), number(5..7), number(8..10));
    let valid = (1..=9999).contains(&year)
        && (1..=12).contains(&month)
        && (1..=team_protocol::python::datetime::days_in_month(year, month)).contains(&day);
    valid.then_some((year, month, day))
}

fn may_write_date(value: &Value) -> Outcome {
    match arguments(value, ["text", "today"]) {
        Some([text, date]) => match (string(text), today(date)) {
            (Some(text), Some(date)) => {
                routine_context::may_write_date(text, date).map_or(Outcome::Error("match"), flag)
            }
            _ => Outcome::Refuse,
        },
        None => Outcome::Refuse,
    }
}

fn number_tokens(value: &Value) -> Outcome {
    string(value).map_or(Outcome::Refuse, |text| {
        let found: Result<Vec<Value>, MatchError> = routine_context::NUMBER_TOKEN_RE
            .finditer(text)
            .map(|tokens| {
                tokens
                    .iter()
                    .map(|token| Value::String(text[token.start..token.end].to_owned()))
                    .collect()
            });
        found.map_or(Outcome::Error("match"), |tokens| {
            Outcome::Value(Value::Array(tokens))
        })
    })
}

/// The entries of `routine_context`.
pub const ENTRIES: &[Entry] = &[
    ("routine_context.canonical_routine_listing", |v| {
        borrowed(routine_context::canonical_routine_listing(v))
    }),
    ("routine_context.canonical_routine_listings", |v| {
        borrowed(routine_context::canonical_routine_listings(v))
    }),
    ("routine_context.canonical_rerun", |v| {
        borrowed(routine_context::canonical_rerun(v))
    }),
    ("routine_context.json_literal", |v| {
        flag(routine_context::json_literal(v))
    }),
    ("routine_context.canonical_selected_routine", |v| {
        borrowed(routine_context::canonical_selected_routine(v))
    }),
    ("routine_context.canonical_edits", |v| {
        borrowed(routine_context::canonical_edits(v))
    }),
    ("routine_context.canonical_refine", |v| {
        borrowed(routine_context::canonical_refine(v))
    }),
    ("routine_context.may_write_date", may_write_date),
    ("routine_context.number_tokens", number_tokens),
];
