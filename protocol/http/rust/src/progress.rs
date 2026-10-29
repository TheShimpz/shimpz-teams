//! Closed Team chat progress and streamed-terminal framing (`progress.py`).
//!
//! An Action occurrence carries only its canonical Assistant and Action identifiers, never arguments, results, prompts,
//! or model output. Only the single terminal record determines a turn's outcome.

use strict_json::Value;

use crate::identifiers::{canonical_action_id, canonical_assistant_id};
use crate::python::json::{CANONICAL, dumps};
use crate::python::value::{field, has_exactly, object, small_int, string};

/// The progress phases, sorted.
pub const PHASES: [&str; 6] = [
    "action",
    "action-delivery",
    "action-preparation",
    "browser",
    "model",
    "team-context",
];
/// The progress states, sorted.
pub const STATES: [&str; 2] = ["finished", "started"];
/// The most progress events of one stream.
pub const MAX_EVENTS: i64 = 2_048;
/// The most Actions one progress record may count, its own index included.
pub const MAX_ACTION_COUNT: i64 = 512;
/// The longest elapsed time one finished record reports.
pub const MAX_ELAPSED_MS: i64 = 24 * 60 * 60 * 1_000;
/// Exact compact JSON size of the largest valid finished Action progress record, including newline.
pub const MAX_PROGRESS_LINE_BYTES: usize = 311;
/// The longest line of a stream.
pub const MAX_LINE_BYTES: usize = 256 * 1024;
/// The longest stream.
pub const MAX_STREAM_BYTES: usize = 2_048 * MAX_PROGRESS_LINE_BYTES + MAX_LINE_BYTES;

/// A streamed Team chat record violated the closed protocol (`ProgressContractError`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ProgressContractError;

impl std::fmt::Display for ProgressContractError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("the chat stream record violates the closed protocol")
    }
}

impl std::error::Error for ProgressContractError {}

fn integer(value: &Value, minimum: i64, maximum: i64) -> Result<Value, ProgressContractError> {
    small_int(value)
        .filter(|integer| (minimum..=maximum).contains(integer))
        .map(|_| value.clone())
        .ok_or(ProgressContractError)
}

fn identifier(value: Option<&str>) -> Result<Value, ProgressContractError> {
    value
        .map(|text| Value::String(text.to_owned()))
        .ok_or(ProgressContractError)
}

/// `canonical_event`: one metadata-only progress occurrence.
///
/// # Errors
///
/// Returns [`ProgressContractError`] for any other value.
pub fn canonical_event(value: &Value) -> Result<Value, ProgressContractError> {
    canonical_members(object(value).ok_or(ProgressContractError)?).map(Value::Object)
}

fn canonical_members(
    members: &[(String, Value)],
) -> Result<Vec<(String, Value)>, ProgressContractError> {
    let get = |key: &str| {
        members
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value)
    };
    let phase = get("phase")
        .and_then(string)
        .filter(|phase| PHASES.contains(phase));
    let state = get("state")
        .and_then(string)
        .filter(|state| STATES.contains(state));
    let (Some(phase), Some(state)) = (phase, state) else {
        return Err(ProgressContractError);
    };
    let (finished, action) = (state == "finished", phase == "action");
    let mut expected = vec!["seq", "phase", "state"];
    if finished {
        expected.push("elapsed_ms");
    }
    if action {
        expected.extend(["assistant_id", "index", "action", "total"]);
    }
    if !has_exactly(members, &expected) {
        return Err(ProgressContractError);
    }
    let mut event = vec![
        (
            "seq".to_owned(),
            integer(field(members, "seq"), 1, MAX_EVENTS)?,
        ),
        ("phase".to_owned(), Value::String(phase.to_owned())),
        ("state".to_owned(), Value::String(state.to_owned())),
    ];
    if finished {
        event.push((
            "elapsed_ms".to_owned(),
            integer(field(members, "elapsed_ms"), 0, MAX_ELAPSED_MS)?,
        ));
    }
    if action {
        push_action(members, &mut event)?;
    }
    Ok(event)
}

/// The members of an Action occurrence: its Assistant, its 1-based index among `total` Actions, and its Action.
fn push_action(
    members: &[(String, Value)],
    event: &mut Vec<(String, Value)>,
) -> Result<(), ProgressContractError> {
    let total = integer(field(members, "total"), 1, MAX_ACTION_COUNT)?;
    let count = small_int(&total).unwrap_or(1);
    let assistant = identifier(canonical_assistant_id(field(members, "assistant_id")))?;
    let index = integer(field(members, "index"), 1, count)?;
    let action = identifier(canonical_action_id(field(members, "action")))?;
    event.extend([
        ("assistant_id".to_owned(), assistant),
        ("index".to_owned(), index),
        ("action".to_owned(), action),
        ("total".to_owned(), total),
    ]);
    Ok(())
}

/// `canonical_record`: one exact progress or terminal stream record.
///
/// # Errors
///
/// Returns [`ProgressContractError`] for any other value.
pub fn canonical_record(value: &Value) -> Result<Value, ProgressContractError> {
    let members = object(value).ok_or(ProgressContractError)?;
    match value.get("type").and_then(string) {
        Some("progress") => {
            let event: Vec<(String, Value)> = members
                .iter()
                .filter(|(key, _)| key != "type")
                .cloned()
                .collect();
            let mut record = vec![("type".to_owned(), Value::String("progress".to_owned()))];
            record.extend(canonical_members(&event)?);
            Ok(Value::Object(record))
        }
        Some("terminal") => {
            let body = field(members, "body");
            if !has_exactly(members, &["type", "status", "body"]) || object(body).is_none() {
                return Err(ProgressContractError);
            }
            Ok(Value::Object(vec![
                ("type".to_owned(), Value::String("terminal".to_owned())),
                (
                    "status".to_owned(),
                    integer(field(members, "status"), 200, 599)?,
                ),
                ("body".to_owned(), body.clone()),
            ]))
        }
        _ => Err(ProgressContractError),
    }
}

/// `encode_record`: one canonical NDJSON record, sorted and compact, within its line bound.
///
/// # Errors
///
/// Returns [`ProgressContractError`] when the record is not canonical or exceeds its bound.
pub fn encode_record(value: &Value) -> Result<Vec<u8>, ProgressContractError> {
    let canonical = canonical_record(value)?;
    let mut encoded = dumps(&canonical, CANONICAL).into_bytes();
    encoded.push(b'\n');
    let progress = canonical.get("type") == Some(&Value::String("progress".to_owned()));
    let maximum = if progress {
        MAX_PROGRESS_LINE_BYTES
    } else {
        MAX_LINE_BYTES
    };
    if encoded.len() > maximum {
        return Err(ProgressContractError);
    }
    Ok(encoded)
}

/// `decode_line`: one unique-key UTF-8 NDJSON line, validated to its exact shape.
///
/// # Errors
///
/// Returns [`ProgressContractError`] for a line outside its bounds, invalid strict JSON, or a non-canonical record.
pub fn decode_line(raw: &[u8]) -> Result<Value, ProgressContractError> {
    if !raw.ends_with(b"\n") || raw.len() > MAX_LINE_BYTES {
        return Err(ProgressContractError);
    }
    let value = strict_json::parse(raw).map_err(|_| ProgressContractError)?;
    let record = canonical_record(&value)?;
    let progress = record.get("type") == Some(&Value::String("progress".to_owned()));
    if progress && raw.len() > MAX_PROGRESS_LINE_BYTES {
        return Err(ProgressContractError);
    }
    Ok(record)
}
