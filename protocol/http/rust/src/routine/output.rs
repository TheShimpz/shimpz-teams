//! What a completed run does with its result, and the bounded projection of a shown result (ADR-0092, ADR-0101).

use std::sync::LazyLock;

use strict_json::Value;

use super::{MAX_ROUTINE_STEPS, encoded_bytes, plain, plan_unsafe, position, whole};
use crate::python::regex::{Pattern, declared};
use crate::python::text;
use crate::python::value::{
    array, exact_object, field, has_exactly, object, object_of, small_int, string,
};

/// The output modes: show the last step's result after every run, only when it changed, or none of it.
pub const OUTPUT_MODES: [&str; 3] = ["show", "changes", "none"];
/// The modes that name the shown step, sorted.
pub const SHOWN_MODES: [&str; 2] = ["changes", "show"];
/// The states of a run's output.
pub const OUTPUT_STATES: [&str; 3] = ["shown", "unchanged", "unavailable"];
/// Containers nest at most this deep.
pub const MAX_OUTPUT_DEPTH: usize = 4;
/// The most items of a list node.
pub const MAX_OUTPUT_ITEMS: usize = 50;
/// The most fields of a fields node.
pub const MAX_OUTPUT_FIELDS: usize = 24;
/// The longest text node.
pub const MAX_OUTPUT_TEXT_CHARS: usize = 300;
/// The longest field name.
pub const MAX_OUTPUT_KEY_CHARS: usize = 64;
/// The longest number node; a longer number is shown as text.
pub const MAX_OUTPUT_NUMBER_CHARS: usize = 64;
/// A number node's exact JSON text.
pub static OUTPUT_NUMBER_RE: LazyLock<Pattern> =
    LazyLock::new(|| declared(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z"));
/// The largest encoded output.
pub const MAX_OUTPUT_BYTES: usize = 16 * 1024;

/// `canonical_disposition`: a plan's output disposition, whose shown step is the last of its `total` steps.
#[must_use]
pub fn canonical_disposition(value: &Value, total: &Value) -> Option<Value> {
    let members = exact_object(value, &["mode", "step"])?;
    let mode = string(field(members, "mode")).filter(|mode| OUTPUT_MODES.contains(mode))?;
    let shown = field(members, "step");
    let total = small_int(total).filter(|total| (1..=MAX_ROUTINE_STEPS).contains(total))?;
    let valid = if SHOWN_MODES.contains(&mode) {
        small_int(shown) == Some(total)
    } else {
        *shown == Value::Null
    };
    valid.then(|| {
        object_of(vec![
            ("mode", Value::String(mode.to_owned())),
            ("step", shown.clone()),
        ])
    })
}

fn count(value: Option<&Value>) -> bool {
    value.is_some_and(|value| whole(value, 0, (1 << 31) - 1))
}

fn plain_text(value: Option<&Value>, maximum: usize) -> bool {
    value.and_then(string).is_some_and(|text| {
        text::len(text) <= maximum && !text.chars().any(|c| plan_unsafe(u32::from(c)))
    })
}

/// `_scalar_node`: null, redacted, elided, a boolean, a finite number's text, or escaped text of bounded length.
fn scalar_node(members: &[(String, Value)], kind: Option<&str>) -> bool {
    let item = members
        .iter()
        .find(|(key, _)| key == "value")
        .map(|(_, value)| value);
    match kind {
        Some("null" | "redacted" | "elided") => has_exactly(members, &["kind"]),
        Some("bool") => {
            has_exactly(members, &["kind", "value"]) && matches!(item, Some(Value::Bool(_)))
        }
        Some("number") => {
            has_exactly(members, &["kind", "value"])
                && item.and_then(string).is_some_and(|number| {
                    text::len(number) <= MAX_OUTPUT_NUMBER_CHARS && OUTPUT_NUMBER_RE.is_full(number)
                })
        }
        Some("text") => {
            has_exactly(members, &["kind", "value", "cut"])
                && matches!(field(members, "cut"), Value::Bool(_))
                && plain_text(item, MAX_OUTPUT_TEXT_CHARS)
        }
        _ => false,
    }
}

fn output_node(value: &Value, depth: usize) -> bool {
    let Some(members) = object(value) else {
        return false;
    };
    let kind = value.get("kind").and_then(string);
    if !matches!(kind, Some("list" | "fields")) {
        return scalar_node(members, kind);
    }
    if depth >= MAX_OUTPUT_DEPTH || !count(value.get("omitted")) {
        return false;
    }
    if kind == Some("list") {
        return has_exactly(members, &["kind", "items", "omitted"])
            && array(field(members, "items")).is_some_and(|items| {
                items.len() <= MAX_OUTPUT_ITEMS
                    && items.iter().all(|item| output_node(item, depth + 1))
            });
    }
    has_exactly(members, &["kind", "fields", "omitted"])
        && array(field(members, "fields")).is_some_and(|fields| {
            fields.len() <= MAX_OUTPUT_FIELDS
                && fields.iter().all(|entry| match array(entry) {
                    Some([key, node]) => {
                        plain(key, MAX_OUTPUT_KEY_CHARS) && output_node(node, depth + 1)
                    }
                    _ => false,
                })
                && fields.iter().enumerate().all(|(index, entry)| {
                    !fields[..index]
                        .iter()
                        .any(|other| key_of(other) == key_of(entry))
                })
        })
}

fn key_of(entry: &Value) -> Option<&Value> {
    array(entry).and_then(<[Value]>::first)
}

/// `canonical_output`: a completed run's result as shown: its step's position and state, and for a shown one its
/// bounded projection.
#[must_use]
pub fn canonical_output(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &["step", "state", "value", "truncated"])?;
    let state = string(field(members, "state")).filter(|state| OUTPUT_STATES.contains(state));
    let (node, truncated) = (field(members, "value"), field(members, "truncated"));
    let valid = position(field(members, "step"), MAX_ROUTINE_STEPS)
        && matches!(truncated, Value::Bool(_))
        && match state {
            Some("shown") => output_node(node, 0),
            Some(_) => *node == Value::Null && *truncated == Value::Bool(false),
            None => false,
        }
        && encoded_bytes(value) <= MAX_OUTPUT_BYTES;
    valid.then_some(value)
}
