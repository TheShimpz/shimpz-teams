//! A Routine's plan as a Supervisor inspects it: projected steps, their inputs, pages, and summaries (ADR-0092).

use std::fmt::Write as _;
use std::sync::LazyLock;

use strict_json::{Number, Value};

use super::{
    MAX_ROUTINE_STEPS, ROUTINE_ID_RE, encoded_bytes, position, valid_action, valid_assistant,
    valid_revision, whole,
};
use crate::identifiers::canonical_identifier;
use crate::python::json::{Options, dumps, write_string};
use crate::python::regex::{Pattern, declared};
use crate::python::text;
use crate::python::value::{array, eq_int, exact_object, field, py_eq, small_int, string};

/// The longest preview of a literal.
pub const MAX_PREVIEW_CHARS: usize = 120;
/// The most inputs of one step.
pub const MAX_STEP_INPUTS: usize = 64;
/// The most Stored Inputs one step names.
pub const MAX_STEP_STORED_INPUTS: usize = 8;
/// The longest input member name.
pub const MAX_MEMBER_CHARS: usize = 128;
/// The longest JSON pointer.
pub const MAX_POINTER_CHARS: usize = 256;
/// The longest selector constant as JSON text.
pub const MAX_WHERE_CHARS: usize = 4096;
/// One projected step's encoded size at most.
pub const MAX_STEP_VIEW_BYTES: usize = 24 * 1024;
/// The most steps of one page.
pub const MAX_PAGE_STEPS: usize = 64;
/// The most encoded bytes of one page's steps.
pub const MAX_PAGE_BYTES: usize = 96 * 1024;
/// The most runs of consecutive equal Actions a summary lists.
pub const MAX_SUMMARY_RUNS: usize = 16;
/// A plan digest.
pub static PLAN_DIGEST_RE: LazyLock<Pattern> = LazyLock::new(|| declared(r"sha256:[0-9a-f]{64}\Z"));
/// A JSON pointer.
pub static POINTER_RE: LazyLock<Pattern> = LazyLock::new(|| declared(r"(?:/(?:[^/~]|~[01])*)*\Z"));
/// Every control or invisible character, and a lone surrogate; [`plan_unsafe`] is the same set as a predicate.
pub static PLAN_UNSAFE_RE: LazyLock<Pattern> = LazyLock::new(|| {
    declared(
        "[\\u0000-\\u001f\\u007f-\\u009f\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ud800-\\udfff\\ufeff]",
    )
});

/// Whether `point` is in [`PLAN_UNSAFE_RE`]'s class (a lone surrogate included, which only a decoded JSON escape
/// holds).
#[must_use]
pub const fn plan_unsafe(point: u32) -> bool {
    matches!(
        point,
        0..=0x1f | 0x7f..=0x9f | 0x200b..=0x200f | 0x2028..=0x202e | 0x2060..=0x206f | 0xd800..=0xdfff | 0xfeff
    )
}

/// `escaped`: every control or invisible character written as its JSON unicode escape.
#[must_use]
pub fn escaped(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for character in text.chars() {
        push_escaped(u32::from(character), Some(character), &mut out);
    }
    out
}

fn push_escaped(point: u32, character: Option<char>, out: &mut String) {
    match character {
        Some(character) if !plan_unsafe(point) => out.push(character),
        _ => {
            let _ = write!(out, "\\u{point:04x}");
        }
    }
}

/// `literal_preview`: a literal's JSON text, escaped and cut to 120 characters.
#[must_use]
pub fn literal_preview(value: &Value) -> String {
    let options = Options {
        ensure_ascii: false,
        sort_keys: true,
        compact: true,
    };
    preview(&dumps(value, options))
}

/// `preview`: text with every control or invisible character escaped, cut to 120 characters.
#[must_use]
pub fn preview(text: &str) -> String {
    let escaped = escaped(text);
    if text::len(&escaped) <= MAX_PREVIEW_CHARS {
        return escaped;
    }
    let mut cut: String = escaped.chars().take(MAX_PREVIEW_CHARS - 1).collect();
    cut.push('…');
    cut
}

/// `plain`: a string of 1 to `maximum` characters without a control or invisible character.
#[must_use]
pub fn plain(value: &Value, maximum: usize) -> bool {
    string(value).is_some_and(|text| {
        (1..=maximum).contains(&text::len(text)) && !text.chars().any(|c| plan_unsafe(u32::from(c)))
    })
}

fn pointer(value: &Value) -> bool {
    string(value).is_some_and(|text| {
        text::len(text) <= MAX_POINTER_CHARS
            && POINTER_RE.is_full(text)
            && !text.chars().any(|c| plan_unsafe(u32::from(c)))
    })
}

/// `where_text`: a selector constant (a string or an integer) as its JSON text with every control or invisible
/// character escaped; `None` for any other value.
#[must_use]
pub fn where_text(constant: &Value) -> Option<String> {
    match constant {
        Value::String(text) => Some(where_string(
            &text.chars().map(u32::from).collect::<Vec<_>>(),
        )),
        Value::Number(Number::Int(_) | Number::BigInt(_)) => {
            Some(dumps(constant, crate::python::json::CANONICAL))
        }
        _ => None,
    }
}

/// The JSON text of a decoded string (which may hold a lone surrogate), escaped as `where_text` escapes it.
fn where_string(points: &[u32]) -> String {
    let mut out = String::from("\"");
    for point in points {
        let character = char::from_u32(*point);
        match character {
            Some(character @ ('\0'..='\u{1f}' | '"' | '\\')) => {
                let mut quoted = String::new();
                write_string(&character.to_string(), false, &mut quoted);
                out.push_str(&quoted[1..quoted.len() - 1]);
            }
            _ => push_escaped(*point, character, &mut out),
        }
    }
    out.push('"');
    out
}

/// A JSON scalar Python's `json.loads` reads from a selector's text.
enum Scalar {
    String(Vec<u32>),
    Integer,
}

/// What `json.loads(text)` returns when it is a string (code points, lone surrogates kept) or an integer written
/// exactly as `text`; `None` for invalid JSON and every other value or spelling. The caller passes plain text, which
/// holds no control character, so the raw control characters Python's strict decoder refuses never occur here.
fn json_scalar(text: &str) -> Option<Scalar> {
    let bytes = text.as_bytes();
    let digits = text.strip_prefix('-').unwrap_or(text);
    if !digits.is_empty() && digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return (digits == "0" || !digits.starts_with('0')).then_some(Scalar::Integer);
    }
    if bytes.first() != Some(&b'"') {
        return None;
    }
    let characters: Vec<char> = text.chars().collect();
    let mut points = Vec::new();
    let mut index = 1;
    loop {
        let character = *characters.get(index)?;
        index += 1;
        match character {
            '"' => return (index == characters.len()).then_some(Scalar::String(points)),
            '\\' => index = json_escape(&characters, index, &mut points)?,
            other => points.push(u32::from(other)),
        }
    }
}

fn hex4(characters: &[char], index: usize) -> Option<u32> {
    let digits = characters.get(index..index + 4)?;
    digits
        .iter()
        .try_fold(0, |value, digit| Some(value * 16 + digit.to_digit(16)?))
}

/// Decodes one escape after its backslash at `index`, returning the index after it, or `None` where the selector
/// cannot be admitted.
///
/// Only escapes that `json.dumps` writes back the same way can round-trip, so `\/` (which reads `/`, written bare) is
/// refused at once, as is a surrogate pair: Python joins it into one supplementary character, which `where_text`
/// writes bare where the text spells twelve escaped characters. A high surrogate followed by any other `\u` escape
/// stays lone, as Python's decoder leaves it.
fn json_escape(characters: &[char], index: usize, points: &mut Vec<u32>) -> Option<usize> {
    let escape = *characters.get(index)?;
    let simple = match escape {
        '"' => Some(0x22),
        '\\' => Some(0x5c),
        'b' => Some(0x08),
        'f' => Some(0x0c),
        'n' => Some(0x0a),
        'r' => Some(0x0d),
        't' => Some(0x09),
        'u' => None,
        _ => return None,
    };
    if let Some(point) = simple {
        points.push(point);
        return Some(index + 1);
    }
    let high = hex4(characters, index + 1)?;
    let after = index + 5;
    let next_escape =
        characters.get(after) == Some(&'\\') && characters.get(after + 1) == Some(&'u');
    if (0xd800..=0xdbff).contains(&high) && next_escape {
        let low = hex4(characters, after + 2)?;
        if (0xdc00..=0xdfff).contains(&low) {
            return None;
        }
    }
    points.push(high);
    Some(after)
}

/// `_where`: a selector as shown, its member and its constant as JSON text, a string or an integer.
fn selector(value: &Value) -> bool {
    let Some(members) = exact_object(value, &["member", "value_json"]) else {
        return false;
    };
    let text = field(members, "value_json");
    if !plain(field(members, "member"), MAX_MEMBER_CHARS) || !plain(text, MAX_WHERE_CHARS) {
        return false;
    }
    let text = string(text).unwrap_or_default();
    match json_scalar(text) {
        Some(Scalar::String(points)) => where_string(&points) == text,
        Some(Scalar::Integer) => text != "-0",
        None => false,
    }
}

/// `reference`: a copied value: an earlier position and its pointer, and through an array item its selector and item
/// pointer.
#[must_use]
pub fn reference(members: &[(String, Value)], at: i64) -> bool {
    let selected = *field(members, "where") != Value::Null;
    position(field(members, "step"), at - 1)
        && pointer(field(members, "pointer"))
        && if selected {
            selector(field(members, "where")) && pointer(field(members, "item"))
        } else {
            *field(members, "item") == Value::Null
        }
}

fn input(value: &Value, at: i64) -> bool {
    let source = value.get("source").and_then(string);
    let fields: &[&str] = match source {
        Some("literal" | "run_clock") => &["member", "source", "value"],
        Some("step_output") => &["member", "source", "step", "pointer", "where", "item"],
        _ => return false,
    };
    let Some(members) = exact_object(value, fields) else {
        return false;
    };
    if !plain(field(members, "member"), MAX_MEMBER_CHARS) {
        return false;
    }
    if source == Some("step_output") {
        reference(members, at)
    } else {
        plain(field(members, "value"), MAX_PREVIEW_CHARS)
    }
}

/// `members`: a step's inputs, at most [`MAX_STEP_INPUTS`], each admitted, in member order with no member twice.
#[must_use]
pub fn members(items: &Value, admit: impl Fn(&Value) -> bool) -> bool {
    array(items).is_some_and(|items| {
        items.len() <= MAX_STEP_INPUTS
            && items.iter().all(&admit)
            && items.windows(2).all(|pair| {
                string(field_of(&pair[0], "member")) < string(field_of(&pair[1], "member"))
            })
    })
}

fn field_of<'a>(value: &'a Value, key: &str) -> &'a Value {
    value.get(key).unwrap_or(&Value::Null)
}

/// `canonical_step`: one projected step at exactly `at`, referring only to earlier positions, within its byte bound.
#[must_use]
pub fn canonical_step(value: &Value, at: i64) -> Option<&Value> {
    let fields = [
        "position",
        "assistant",
        "action",
        "read_only",
        "inputs",
        "stored_inputs",
    ];
    let members = exact_object(value, &fields)?;
    let stored = field(members, "stored_inputs");
    let valid = (1..=MAX_ROUTINE_STEPS).contains(&at)
        && small_int(field(members, "position")) == Some(at)
        && valid_assistant(field(members, "assistant"))
        && valid_action(field(members, "action"))
        && matches!(field(members, "read_only"), Value::Bool(_))
        && members_of(field(members, "inputs"), at)
        && array(stored).is_some_and(|stored| {
            stored.len() <= MAX_STEP_STORED_INPUTS
                && stored
                    .iter()
                    .map(canonical_identifier)
                    .collect::<Option<Vec<_>>>()
                    .is_some_and(|names| names.windows(2).all(|pair| pair[0] < pair[1]))
        })
        && encoded_bytes(value) <= MAX_STEP_VIEW_BYTES;
    valid.then_some(value)
}

fn members_of(inputs: &Value, at: i64) -> bool {
    members(inputs, |item| input(item, at))
}

/// `paged`: whole consecutive entries of `total` from `offset`, the next offset or null, within the page's bounds.
#[must_use]
pub fn paged(members: &[(String, Value)], step: impl Fn(&Value, i64) -> bool) -> bool {
    let (total, offset) = (field(members, "total"), field(members, "offset"));
    let Some(steps) = array(field(members, "steps")) else {
        return false;
    };
    let (Some(total), Some(offset)) = (
        small_int(total).filter(|_| whole(total, 1, MAX_ROUTINE_STEPS)),
        small_int(offset),
    ) else {
        return false;
    };
    let count = i64::try_from(steps.len()).unwrap_or(i64::MAX);
    let next = field(members, "next");
    (0..total).contains(&offset)
        && count > 0
        && steps.len() <= MAX_PAGE_STEPS
        && count <= total - offset
        && steps
            .iter()
            .zip(offset + 1..)
            .all(|(item, at)| step(item, at))
        && if offset + count == total {
            *next == Value::Null
        } else {
            eq_int(next, offset + count)
        }
        && encoded_bytes(field(members, "steps")) <= MAX_PAGE_BYTES
}

/// `canonical_page`: one page of a revision's projected steps, naming the revision and digest.
#[must_use]
pub fn canonical_page(value: &Value) -> Option<&Value> {
    let fields = [
        "routine_id",
        "revision",
        "plan_digest",
        "total",
        "offset",
        "steps",
        "next",
    ];
    let members = exact_object(value, &fields)?;
    let valid = ROUTINE_ID_RE.is_full_value(field(members, "routine_id"))
        && valid_revision(field(members, "revision"))
        && PLAN_DIGEST_RE.is_full_value(field(members, "plan_digest"))
        && paged(members, |item, at| canonical_step(item, at).is_some());
    valid.then_some(value)
}

/// `canonical_summary`: a plan summary whose runs cover the first steps in order, each differing from the one before.
#[must_use]
pub fn canonical_summary(value: &Value) -> Option<&Value> {
    let members = exact_object(
        value,
        &["revision", "plan_digest", "steps", "actions", "more"],
    )?;
    let runs = array(field(members, "actions"))?;
    let (more, total) = (field(members, "more"), field(members, "steps"));
    let parsed: Option<Vec<(&Value, &Value, i64)>> = runs
        .iter()
        .map(|run| match array(run) {
            Some([assistant, action, count])
                if valid_assistant(assistant)
                    && valid_action(action)
                    && position(count, MAX_ROUTINE_STEPS) =>
            {
                small_int(count).map(|count| (assistant, action, count))
            }
            _ => None,
        })
        .collect();
    let valid = valid_revision(field(members, "revision"))
        && PLAN_DIGEST_RE.is_full_value(field(members, "plan_digest"))
        && whole(total, 1, MAX_ROUTINE_STEPS)
        && (1..=MAX_SUMMARY_RUNS).contains(&runs.len())
        && parsed.as_ref().is_some_and(|parsed| {
            parsed
                .windows(2)
                .all(|pair| !(py_eq(pair[0].0, pair[1].0) && py_eq(pair[0].1, pair[1].1)))
                && small_int(more).is_some_and(|more| {
                    more >= 0
                        && (more == 0 || runs.len() == MAX_SUMMARY_RUNS)
                        && small_int(total)
                            == Some(parsed.iter().map(|run| run.2).sum::<i64>() + more)
                })
        });
    valid.then_some(value)
}
