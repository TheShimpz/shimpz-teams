//! `websocket`. A frame error is the object `{"frame_error": true, "status": s, "close_code": c}` on both sides.

use strict_json::{Number, Value};
use team_protocol::websocket::{self, FrameError};

use super::{Entry, Outcome, arguments, flag, integer, small, string, text};

fn frame(result: Result<Value, FrameError>) -> Outcome {
    Outcome::Value(result.unwrap_or_else(|error| {
        Value::Object(vec![
            ("frame_error".to_owned(), Value::Bool(true)),
            (
                "status".to_owned(),
                Value::Number(Number::Int(i64::from(error.status))),
            ),
            (
                "close_code".to_owned(),
                Value::Number(Number::Int(i64::from(error.close_code))),
            ),
        ])
    }))
}

fn origin(value: &Value) -> Outcome {
    match value {
        Value::Null => text(websocket::canonical_origin(None).as_deref()),
        Value::String(origin) => text(websocket::canonical_origin(Some(origin)).as_deref()),
        _ => Outcome::Refuse,
    }
}

fn public_text(value: &Value) -> Outcome {
    match arguments(value, ["value", "maximum"]) {
        Some([candidate, maximum]) => small(maximum).map_or(Outcome::Refuse, |maximum| {
            let maximum = usize::try_from(maximum).unwrap_or(0);
            text(websocket::public_text(candidate, maximum).ok())
        }),
        None => Outcome::Refuse,
    }
}

fn decode(value: &Value) -> Outcome {
    match arguments(value, ["message", "max_bytes"]) {
        Some([Value::Object(message), maximum]) => small(maximum)
            .map_or(Outcome::Refuse, |maximum| {
                frame(websocket::decode_bounded_json_frame(message, maximum))
            }),
        _ => Outcome::Refuse,
    }
}

fn terminal(value: &Value) -> Outcome {
    let Some([status, detail, fallback, maximum]) = arguments(
        value,
        ["status", "detail", "fallback_detail", "max_detail_chars"],
    ) else {
        return Outcome::Refuse;
    };
    match (string(detail), string(fallback), small(maximum)) {
        (Some(detail), Some(fallback), Some(maximum)) => {
            Outcome::Value(websocket::error_terminal(status, detail, fallback, maximum))
        }
        _ => Outcome::Refuse,
    }
}

fn identity(value: &Value) -> Outcome {
    match arguments(value, ["value", "expected_team_id"]) {
        Some([candidate, Value::String(team)]) => websocket::challenge_identity(candidate, team)
            .map_or(Outcome::Refuse, |(challenge, turn)| {
                Outcome::Value(Value::Array(vec![
                    Value::String(challenge.to_owned()),
                    Value::String(turn.to_owned()),
                ]))
            }),
        _ => Outcome::Refuse,
    }
}

/// The entries of `websocket`.
pub const ENTRIES: &[Entry] = &[
    ("websocket.canonical_origin", origin),
    ("websocket.public_text", public_text),
    ("websocket.decode_bounded_json_frame", decode),
    ("websocket.safe_status", |v| {
        integer(websocket::safe_status(v))
    }),
    ("websocket.error_terminal", terminal),
    ("websocket.valid_challenge_id", |v| {
        flag(websocket::valid_challenge_id(v))
    }),
    ("websocket.challenge_identity", identity),
    ("websocket.canonical_human_response", |v| {
        frame(websocket::canonical_human_response(v))
    }),
    ("websocket.human_value", |v| flag(websocket::human_value(v))),
];
