//! `progress`.

use strict_json::Value;
use team_protocol::progress;

use super::{Entry, Outcome, string};

fn result(result: Result<Value, progress::ProgressContractError>) -> Outcome {
    result.map_or(Outcome::Error("progress"), Outcome::Value)
}

fn encode(value: &Value) -> Outcome {
    match progress::encode_record(value) {
        Ok(line) => Outcome::Value(Value::String(String::from_utf8(line).expect("UTF-8 JSON"))),
        Err(_) => Outcome::Error("progress"),
    }
}

fn decode(value: &Value) -> Outcome {
    string(value).map_or(Outcome::Refuse, |line| {
        result(progress::decode_line(line.as_bytes()))
    })
}

/// The entries of `progress`.
pub const ENTRIES: &[Entry] = &[
    ("progress.canonical_event", |v| {
        result(progress::canonical_event(v))
    }),
    ("progress.canonical_record", |v| {
        result(progress::canonical_record(v))
    }),
    ("progress.encode_record", encode),
    ("progress.decode_line", decode),
];
