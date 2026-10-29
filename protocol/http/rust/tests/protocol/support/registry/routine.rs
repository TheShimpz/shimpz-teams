//! `routine`.

use strict_json::Value;
use team_protocol::routine;

use super::{
    Entry, Outcome, arguments, borrowed, count, flag, integer, small, string, text, value,
};

fn daily_cap(value: &Value) -> Outcome {
    routine::canonical_schedule(value).map_or(Outcome::Refuse, |schedule| {
        integer(routine::daily_cap(schedule))
    })
}

fn continuous_cap(value: &Value) -> Outcome {
    match small(value) {
        Some(gap) if gap >= 1 => integer(routine::continuous_cap(gap)),
        _ => Outcome::Refuse,
    }
}

fn zoned(value: &Value) -> Outcome {
    arguments(value, ["timezone", "source"]).map_or(Outcome::Refuse, |[timezone, source]| {
        flag(routine::zoned(timezone, source))
    })
}

fn position(value: &Value) -> Outcome {
    arguments(value, ["value", "steps"]).map_or(Outcome::Refuse, |[position, steps]| {
        borrowed(routine::canonical_position(position, steps))
    })
}

fn step(value: &Value) -> Outcome {
    match arguments(value, ["value", "position"]) {
        Some([step, at]) => small(at).map_or(Outcome::Refuse, |at| {
            borrowed(routine::canonical_step(step, at))
        }),
        None => Outcome::Refuse,
    }
}

fn disposition(value: &Value) -> Outcome {
    arguments(value, ["value", "total"]).map_or(Outcome::Refuse, |[disposition, total]| {
        super::value(routine::canonical_disposition(disposition, total))
    })
}

fn owned(text: String) -> Outcome {
    Outcome::Value(Value::String(text))
}

/// The entries of `routine`.
pub const ENTRIES: &[Entry] = &[
    ("routine.canonical_schedule", |v| {
        borrowed(routine::canonical_schedule(v))
    }),
    ("routine.exact_schedule", |v| {
        flag(routine::exact_schedule(v))
    }),
    ("routine.daily_cap", daily_cap),
    ("routine.continuous_cap", continuous_cap),
    ("routine.canonical_name", |v| {
        text(routine::canonical_name(v))
    }),
    ("routine.canonical_description", |v| {
        text(routine::canonical_description(v))
    }),
    ("routine.exact_name", |v| flag(routine::exact_name(v))),
    ("routine.exact_description", |v| {
        flag(routine::exact_description(v))
    }),
    ("routine.canonical_timezone", |v| {
        text(routine::canonical_timezone(v))
    }),
    ("routine.zoned", zoned),
    ("routine.canonical_position", position),
    ("routine.escaped", |v| {
        string(v).map_or(Outcome::Refuse, |t| owned(routine::escaped(t)))
    }),
    ("routine.literal_preview", |v| {
        owned(routine::literal_preview(v))
    }),
    ("routine.preview", |v| {
        string(v).map_or(Outcome::Refuse, |t| owned(routine::preview(t)))
    }),
    ("routine.where_text", |v| {
        text(routine::where_text(v).as_deref())
    }),
    ("routine.canonical_step", step),
    ("routine.canonical_page", |v| {
        borrowed(routine::canonical_page(v))
    }),
    ("routine.canonical_summary", |v| {
        borrowed(routine::canonical_summary(v))
    }),
    ("routine.canonical_disposition", disposition),
    ("routine.canonical_output", |v| {
        borrowed(routine::canonical_output(v))
    }),
    ("routine.encoded_bytes", |v| {
        count(routine::encoded_bytes(v))
    }),
    ("routine.valid_revision", |v| {
        flag(routine::valid_revision(v))
    }),
    ("routine.canonical_run_usage", |v| {
        value(routine::canonical_run_usage(v))
    }),
];
