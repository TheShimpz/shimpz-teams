//! `browser`. A byte measure takes its canonical input: a record, a one-step thread's entry, or a thread.

use strict_json::Value;
use team_protocol::browser;

use super::{Entry, Outcome, arguments, count, string, text, value};

fn authority(value: &Value) -> Outcome {
    string(value)
        .and_then(browser::web_authority)
        .map_or(Outcome::Refuse, |(scheme, netloc)| {
            Outcome::Value(Value::Array(vec![
                Value::String(scheme.to_owned()),
                Value::String(netloc.to_owned()),
            ]))
        })
}

fn field_check(value: &Value) -> Outcome {
    match arguments(value, ["name", "value"]) {
        Some([Value::String(name), candidate]) if browser::ALL_FIELDS.contains(&name.as_str()) => {
            super::value(browser::field_check(name, candidate))
        }
        _ => Outcome::Refuse,
    }
}

fn record_bytes(value: &Value) -> Outcome {
    browser::canonical_record(value).map_or(Outcome::Refuse, |record| {
        count(browser::record_bytes(&record))
    })
}

/// The thread holding `step` alone, as both sides build it.
fn single(step: &Value) -> Value {
    Value::Object(vec![
        ("folded".to_owned(), Value::Array(vec![])),
        (
            "folded_omitted".to_owned(),
            Value::Number(strict_json::Number::Int(0)),
        ),
        ("memory".to_owned(), Value::String(String::new())),
        (
            "unnoted".to_owned(),
            Value::Number(strict_json::Number::Int(0)),
        ),
        ("steps".to_owned(), Value::Array(vec![step.clone()])),
    ])
}

fn entry_bytes(value: &Value) -> Outcome {
    browser::canonical_thread(&single(value))
        .and_then(|thread| {
            thread.get("steps").and_then(|steps| match steps {
                Value::Array(items) => items.first().cloned(),
                _ => None,
            })
        })
        .map_or(Outcome::Refuse, |entry| count(browser::entry_bytes(&entry)))
}

fn history_bytes(value: &Value) -> Outcome {
    browser::canonical_thread(value).map_or(Outcome::Refuse, |thread| {
        count(browser::history_bytes(&thread))
    })
}

fn request_text_bytes(value: &Value) -> Outcome {
    let Some([objective, thread, observation, downloads]) =
        arguments(value, ["objective", "thread", "observation", "downloads"])
    else {
        return Outcome::Refuse;
    };
    match (
        string(objective),
        browser::canonical_thread(thread),
        string(observation),
    ) {
        (Some(objective), Some(thread), Some(observation)) => count(browser::request_text_bytes(
            objective,
            &thread,
            observation,
            downloads,
        )),
        _ => Outcome::Refuse,
    }
}

/// The entries of `browser`.
pub const ENTRIES: &[Entry] = &[
    ("browser.canonical_url", |v| text(browser::canonical_url(v))),
    ("browser.web_authority", authority),
    ("browser.canonical_objective", |v| {
        text(browser::canonical_objective(v))
    }),
    ("browser.canonical_step", |v| {
        value(browser::canonical_step(v))
    }),
    ("browser.field_check", field_check),
    ("browser.canonical_record", |v| {
        value(browser::canonical_record(v))
    }),
    ("browser.canonical_thread", |v| {
        value(browser::canonical_thread(v))
    }),
    ("browser.canonical_memory", |v| {
        text(browser::canonical_memory(v))
    }),
    ("browser.canonical_observation", |v| {
        text(browser::canonical_observation(v))
    }),
    ("browser.canonical_check", |v| {
        value(browser::canonical_check(v))
    }),
    ("browser.canonical_downloads", |v| {
        value(browser::canonical_downloads(v))
    }),
    ("browser.canonical_file_reference", |v| {
        value(browser::canonical_file_reference(v))
    }),
    ("browser.canonical_terminal", |v| {
        value(browser::canonical_terminal(v))
    }),
    ("browser.record_bytes", record_bytes),
    ("browser.entry_bytes", entry_bytes),
    ("browser.history_bytes", history_bytes),
    ("browser.request_text_bytes", request_text_bytes),
    ("browser.STEP_SCHEMA", |_| {
        Outcome::Value(browser::step_schema().clone())
    }),
];
