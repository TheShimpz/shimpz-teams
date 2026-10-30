//! What a completed run shows of its result (ADR-0092 amendment, 2026-10-05, output).
//!
//! A Routine that shows a result shows one step's validated result, never a model's summary of it: healthy runs stay
//! model-free. The result is first projected into its safe form: every value the step's reviewed output schema marks
//! as secret (a secret-named member, `writeOnly`, `format: password`, or a position an unmodelled applicator could make
//! secret, walked exactly as plan admission walks a literal), every credential-shaped string, and the value of every
//! credential-shaped key is redacted; every key and text is escaped; and an object's members are in sorted key order,
//! the canonical order of the pinned contract itself. Redaction always comes before any cut. The safe form is what a
//! change is compared on, so a change only inside redacted content is never shown; the shown form is the safe form cut
//! to a fixed ladder of bounds until its whole encoding fits [`MAX_OUTPUT_BYTES`], marking every cut and omission;
//! labels that read alike after escaping or shortening are numbered apart. Projection never refuses a result: anything
//! it cannot project is shown as unavailable instead, and nothing is replayed.

use std::fmt;

use strict_json::Value;
use team_protocol::python::json::{Options, dumps, number_text};
use team_protocol::python::text;
use team_protocol::routine::{
    MAX_OUTPUT_BYTES, MAX_OUTPUT_DEPTH, MAX_OUTPUT_FIELDS, MAX_OUTPUT_ITEMS, MAX_OUTPUT_KEY_CHARS,
    MAX_OUTPUT_NUMBER_CHARS, MAX_OUTPUT_TEXT_CHARS, encoded_bytes, escaped,
};

use super::PlanError;
use super::walk::{
    Held, MAX_SECRET_DEPTH, Sub, applicable, item_schemas, member_schemas, secret_position,
};
use crate::credential::resembles_credential;
use crate::json::{EMPTY, as_str, at, elements, int, object, sha256_hex, text as string};

/// The bounds a shown result is cut to, (items, fields, text characters), each tried in turn until its encoding fits.
pub const OUTPUT_LEVELS: [(usize, usize, usize); 7] = [
    (MAX_OUTPUT_ITEMS, MAX_OUTPUT_FIELDS, MAX_OUTPUT_TEXT_CHARS),
    (25, 24, 120),
    (12, 12, 60),
    (6, 8, 40),
    (3, 4, 20),
    (1, 2, 12),
    (0, 0, 0),
];
/// The safe form a change is compared on; a larger one always compares as changed.
pub const MAX_COMPARED_OUTPUT_BYTES: usize = 1024 * 1024;
/// Nesting the safe form follows; anything deeper is elided, never followed further.
pub const MAX_SAFE_OUTPUT_DEPTH: usize = MAX_SECRET_DEPTH;
/// The label of a redacted key.
pub const OUTPUT_REDACTED_KEY: &str = "[redacted]";

/// A redacted node.
#[must_use]
pub fn output_redacted() -> Value {
    object(vec![("kind", string("redacted"))])
}

/// An elided node.
#[must_use]
pub fn output_elided() -> Value {
    object(vec![("kind", string("elided"))])
}

/// A result could not be projected; the run shows its result as unavailable.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OutputError;

impl fmt::Display for OutputError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("routine-output-unavailable")
    }
}

impl std::error::Error for OutputError {}

/// What every node of one projection shares: the schema root, the values the run protects, and the depth bound.
struct Context<'a> {
    root: &'a Value,
    protected: Vec<&'a str>,
    depth: usize,
}

/// The complete safe form of one validated result under its Action's reviewed output schema.
///
/// It keeps every key, text, and number exactly as the result has them, apart from what it redacts: also every string
/// or key that holds a value the run protects (ADR-0101). A change is compared on the complete data; escaping,
/// shortening, and every cut belong to the shown form alone.
///
/// # Errors
///
/// [`OutputError`] when the schema cannot be walked.
pub fn output_safe(
    result: &Value,
    schema: &Value,
    protected: &[String],
) -> Result<Value, OutputError> {
    output_safe_within(result, schema, protected, MAX_SAFE_OUTPUT_DEPTH)
}

/// [`output_safe`] following nesting at most `depth` levels.
pub(crate) fn output_safe_within(
    result: &Value,
    schema: &Value,
    protected: &[String],
    depth: usize,
) -> Result<Value, OutputError> {
    let root = if matches!(schema, Value::Object(_)) {
        schema
    } else {
        &EMPTY
    };
    let context = Context {
        root,
        protected: protected
            .iter()
            .map(String::as_str)
            .filter(|item| !item.is_empty())
            .collect(),
        depth,
    };
    node(result, &context, &Sub::Node(root), "", 0).map_err(|_| OutputError)
}

fn protects(text: &str, protected: &[&str]) -> bool {
    protected.iter().any(|secret| text.contains(secret))
}

fn node<'a>(
    value: &Value,
    context: &Context<'a>,
    subschema: &Sub<'a>,
    name: &str,
    depth: usize,
) -> Result<Value, PlanError> {
    if depth > context.depth {
        return Ok(output_elided());
    }
    let candidates = applicable(context.root, subschema, 0, Held::Value(value))?;
    if secret_position(context.root, name, &candidates) {
        return Ok(output_redacted());
    }
    match value {
        Value::Object(found) => fields(found, context, &candidates, depth),
        Value::Array(items) => {
            let mut shown = Vec::with_capacity(items.len());
            for (index, item) in items.iter().enumerate() {
                shown.push(node(
                    item,
                    context,
                    &item_schemas(&candidates, index),
                    name,
                    depth + 1,
                )?);
            }
            Ok(object(vec![
                ("kind", string("list")),
                ("items", Value::Array(shown)),
                ("omitted", int(0)),
            ]))
        }
        Value::String(text) if protects(text, &context.protected) => Ok(output_redacted()),
        _ => Ok(scalar(value)),
    }
}

fn scalar(value: &Value) -> Value {
    match value {
        Value::Null => object(vec![("kind", string("null"))]),
        Value::Bool(flag) => object(vec![
            ("kind", string("bool")),
            ("value", Value::Bool(*flag)),
        ]),
        Value::String(text) if resembles_credential(text) => output_redacted(),
        Value::String(text) => object(vec![
            ("kind", string("text")),
            ("value", string(text)),
            ("cut", Value::Bool(false)),
        ]),
        _ => object(vec![("kind", string("number")), ("value", value.clone())]),
    }
}

/// An object's members in sorted key order, each under its member schema; a credential-shaped key is redacted.
///
/// So is a key that holds a value the run protects.
fn fields<'a>(
    found: &[(String, Value)],
    context: &Context<'a>,
    candidates: &[&'a Value],
    depth: usize,
) -> Result<Value, PlanError> {
    let mut sorted: Vec<&(String, Value)> = found.iter().collect();
    sorted.sort_by(|left, right| left.0.cmp(&right.0));
    let mut shown = Vec::with_capacity(sorted.len());
    let mut redacted = 0;
    for (key, item) in sorted {
        if resembles_credential(key) || protects(key, &context.protected) {
            redacted += 1;
            let label = if redacted == 1 {
                OUTPUT_REDACTED_KEY.to_owned()
            } else {
                format!("{OUTPUT_REDACTED_KEY} {redacted}")
            };
            shown.push(Value::Array(vec![Value::String(label), output_redacted()]));
            continue;
        }
        let Ok(member) = member_schemas(candidates, key) else {
            shown.push(Value::Array(vec![string(key), output_redacted()]));
            continue;
        };
        shown.push(Value::Array(vec![
            string(key),
            node(item, context, &member, key, depth + 1)?,
        ]));
    }
    Ok(object(vec![
        ("kind", string("fields")),
        ("fields", Value::Array(shown)),
        ("omitted", int(0)),
    ]))
}

/// The exact canonical bytes a change is compared on, or `None` when they are too large to compare.
#[must_use]
pub fn output_compared(node: &Value) -> Option<Vec<u8>> {
    compared_within(node, MAX_COMPARED_OUTPUT_BYTES)
}

/// [`output_compared`] under its own bound.
pub(crate) fn compared_within(node: &Value, maximum: usize) -> Option<Vec<u8>> {
    let encoded = dumps(
        node,
        Options {
            ensure_ascii: true,
            sort_keys: true,
            compact: true,
        },
    )
    .into_bytes();
    (encoded.len() <= maximum).then_some(encoded)
}

/// The safe form as shown, cut to the first bounds whose whole output fits; the last step elides it entirely.
///
/// Every key and text is escaped, a long key shortened, a number written as its exact JSON text, and every cut,
/// shortening, omission, and elision marks the output truncated.
#[must_use]
pub fn output_shown(step: &Value, node: &Value) -> Value {
    shown_within(step, node, &OUTPUT_LEVELS)
}

/// [`output_shown`] over its own ladder of bounds.
pub(crate) fn shown_within(step: &Value, node: &Value, levels: &[(usize, usize, usize)]) -> Value {
    for limits in levels {
        let (value, truncated) = display(node, *limits, 0);
        let candidate = shown_output(step, value, truncated);
        if encoded_bytes(&candidate) <= MAX_OUTPUT_BYTES {
            return candidate;
        }
    }
    shown_output(step, output_elided(), true)
}

fn shown_output(step: &Value, value: Value, truncated: bool) -> Value {
    object(vec![
        ("step", step.clone()),
        ("state", string("shown")),
        ("value", value),
        ("truncated", Value::Bool(truncated)),
    ])
}

/// A completed run's output that shows no value: unchanged since the last shown one, or unavailable.
#[must_use]
pub fn output_state(step: &Value, state: &str) -> Value {
    object(vec![
        ("step", step.clone()),
        ("state", string(state)),
        ("value", Value::Null),
        ("truncated", Value::Bool(false)),
    ])
}

/// A key as shown, and whether it was shortened: escaped, quoted when empty, a long one cut with a digest.
#[must_use]
pub fn output_label(key: &str) -> (String, bool) {
    let escaped_key = escaped(key);
    let label = if escaped_key.is_empty() {
        "\"\"".to_owned()
    } else {
        escaped_key
    };
    if text::len(&label) <= MAX_OUTPUT_KEY_CHARS {
        return (label, false);
    }
    let digest = sha256_hex(key.as_bytes());
    let kept: String = label.chars().take(MAX_OUTPUT_KEY_CHARS - 8).collect();
    (format!("{kept}…{}", &digest[..7]), true)
}

/// Each key's label, numbering any that read alike after escaping or shortening, and whether any was shortened.
pub(crate) fn output_labels(keys: &[&str]) -> (Vec<String>, bool) {
    let mut labels: Vec<String> = Vec::with_capacity(keys.len());
    let mut shortened = false;
    for key in keys {
        let (label, cut) = output_label(key);
        shortened = shortened || cut;
        let mut shown = label.clone();
        let mut number = 1;
        while labels.contains(&shown) {
            number += 1;
            let kept: String = label.chars().take(MAX_OUTPUT_KEY_CHARS - 6).collect();
            shown = format!("{kept} ({number})");
            // Numbering a label that reads alike may cut it further, which is a cut like any other.
            shortened = shortened || text::len(&label) > MAX_OUTPUT_KEY_CHARS - 6;
        }
        labels.push(shown);
    }
    (labels, shortened)
}

/// One safe node as shown within `limits`, and whether anything was cut, shortened, omitted, or elided.
fn display(node: &Value, limits: (usize, usize, usize), depth: usize) -> (Value, bool) {
    let (items, fields, chars) = limits;
    let kind = as_str(at(node, "kind")).unwrap_or_default();
    if kind == "text" || kind == "number" {
        return text_node(node, chars);
    }
    if kind != "list" && kind != "fields" {
        return (node.clone(), false);
    }
    if depth >= MAX_OUTPUT_DEPTH {
        return (output_elided(), true);
    }
    let list = kind == "list";
    let key = if list { "items" } else { "fields" };
    let entries = elements(at(node, key));
    let kept = &entries[..entries.len().min(if list { items } else { fields })];
    let (shown, truncated) = if list {
        let mut truncated = kept.len() < entries.len();
        let shown = kept
            .iter()
            .map(|entry| {
                let (child, cut) = display(entry, limits, depth + 1);
                truncated |= cut;
                child
            })
            .collect();
        (shown, truncated)
    } else {
        let (shown, cut) = display_fields(kept, limits, depth);
        (shown, cut || kept.len() < entries.len())
    };
    let earlier = at(node, "omitted").clone();
    let more = i64::try_from(entries.len() - kept.len()).unwrap_or(i64::MAX);
    let omitted = team_protocol::python::value::small_int(&earlier)
        .map_or(earlier, |count| int(count + more));
    (
        object(vec![
            ("kind", string(kind)),
            (key, Value::Array(shown)),
            ("omitted", omitted),
        ]),
        truncated,
    )
}

/// The kept fields of a node as shown, each under its numbered label, and whether any was cut or shortened.
fn display_fields(
    kept: &[Value],
    limits: (usize, usize, usize),
    depth: usize,
) -> (Vec<Value>, bool) {
    let keys: Vec<&str> = kept
        .iter()
        .map(|entry| as_str(elements(entry).first().unwrap_or(&Value::Null)).unwrap_or_default())
        .collect();
    let (labels, mut truncated) = output_labels(&keys);
    let shown = kept
        .iter()
        .zip(labels)
        .map(|(entry, label)| {
            let (child, cut) = display(
                elements(entry).get(1).unwrap_or(&Value::Null),
                limits,
                depth + 1,
            );
            truncated |= cut;
            Value::Array(vec![Value::String(label), child])
        })
        .collect();
    (shown, truncated)
}

/// Escaped text or a number's exact JSON text, cut to `chars`; a number too long for its node is shown as text.
fn text_node(node: &Value, chars: usize) -> (Value, bool) {
    let shown = if as_str(at(node, "kind")) == Some("number") {
        let written = match at(node, "value") {
            Value::Number(number) => number_text(number),
            other => dumps(other, team_protocol::python::json::CANONICAL),
        };
        if text::len(&written) <= MAX_OUTPUT_NUMBER_CHARS {
            return (
                object(vec![
                    ("kind", string("number")),
                    ("value", Value::String(written)),
                ]),
                false,
            );
        }
        written
    } else {
        escaped(as_str(at(node, "value")).unwrap_or_default())
    };
    if text::len(&shown) <= chars {
        return (
            object(vec![
                ("kind", string("text")),
                ("value", Value::String(shown)),
                ("cut", Value::Bool(false)),
            ]),
            false,
        );
    }
    let mut cut: String = shown.chars().take(chars.saturating_sub(1)).collect();
    cut.push('…');
    (
        object(vec![
            ("kind", string("text")),
            ("value", Value::String(cut)),
            ("cut", Value::Bool(true)),
        ]),
        true,
    )
}

/// The members of a node, for the tests.
#[cfg(test)]
pub(crate) fn node_fields(node: &Value) -> Vec<(String, Value)> {
    elements(at(node, "fields"))
        .iter()
        .map(|entry| {
            let pair = elements(entry);
            (
                as_str(&pair[0]).unwrap_or_default().to_owned(),
                pair[1].clone(),
            )
        })
        .collect()
}

#[cfg(test)]
mod tests;
