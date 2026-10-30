//! Human requests, their catalog copy references, and replay transcripts (`validators/human_request.py`, Assistant
//! Spec v1).
//!
//! An Action suspends with one closed request of a declared kind; every displayed text is a reference to a reviewed
//! catalog message with typed parameters. Team admits a response only for the exact request: same kind, ordinal, and
//! the SHA-256 fingerprint of the request's canonical JSON.

use std::collections::HashMap;

use strict_json::Value;

use crate::catalog::{Message, canonical_json, max_length};
use crate::python::{
    array, char_len, has_exactly, int_within, is_stripped, is_true, member, object, py_eq,
    sha256_hex, small_int, string,
};
use crate::unicode::printable;

/// The base members of every request.
pub const BASE: [&str; 4] = ["kind", "ordinal", "title", "description"];
/// The text request kinds and their largest `max_length`.
pub const LENGTH_KINDS: [(&str, i64); 4] = [
    ("input:text", 4096),
    ("input:textarea", 16_000),
    ("input:password", 1024),
    ("input:phone", 64),
];
/// The single-choice kinds.
pub const CHOICE_KINDS: [&str; 2] = ["input:select", "input:choice"];
/// The authentication ceremonies.
pub const AUTH_KINDS: [&str; 3] = ["auth:password", "auth:totp", "auth:passkey"];
/// The kinds that authorize an Action: approval or one authentication ceremony.
pub const AUTHORIZATION_KINDS: [&str; 4] =
    ["approval", "auth:password", "auth:totp", "auth:passkey"];
/// Each copy field and the largest `max_length` its message may have.
pub const COPY_BOUNDS: [(&str, usize); 6] = [
    ("title", 80),
    ("description", 500),
    ("label", 80),
    ("placeholder", 120),
    ("option_label", 80),
    ("option_description", 160),
];
/// The most requests one Action makes.
pub const MAX_REQUESTS: usize = 8;
/// The most options of one choice request.
pub const MAX_OPTIONS: usize = 32;

/// A reviewed catalog by message id.
pub type Catalog<'a> = HashMap<&'a str, &'a Message>;
type Members = [(String, Value)];

/// The lowercase SHA-256 of a request's canonical JSON.
#[must_use]
pub fn fingerprint(request: &Value) -> String {
    sha256_hex(&canonical_json(request))
}

/// The first semantic error of one request against the reviewed catalog, or `None`.
#[must_use]
pub fn request_error(request: &Value, catalog: &Catalog<'_>) -> Option<&'static str> {
    let members = match base_error(request, catalog) {
        Ok(members) => members,
        Err(error) => return Some(error),
    };
    let kind = member(members, "kind").and_then(string).unwrap_or_default();
    if kind == "approval" || AUTH_KINDS.contains(&kind) {
        return (members.len() != BASE.len()).then_some("request_shape");
    }
    if let Some((_, limit)) = LENGTH_KINDS.iter().find(|(name, _)| *name == kind) {
        return length_error(members, *limit, catalog);
    }
    if CHOICE_KINDS.contains(&kind) {
        return choice_error(members, catalog, false);
    }
    if kind == "input:choices" {
        return choice_error(members, catalog, true);
    }
    Some("request_kind")
}

/// The first error of one copy reference used by a field whose message may be at most `bound` long, or `None`.
#[must_use]
pub fn reference_error(
    reference: &Value,
    catalog: &Catalog<'_>,
    bound: usize,
) -> Option<&'static str> {
    let Some(members) =
        object(reference).filter(|members| has_exactly(members, &["message", "params"]))
    else {
        return Some("copy_reference");
    };
    let message = member(members, "message")
        .and_then(string)
        .and_then(|identifier| catalog.get(identifier));
    let (Some(message), Some(values)) = (message, member(members, "params").and_then(object))
    else {
        return Some("copy_reference");
    };
    if max_length(message).is_none_or(|length| length > bound) {
        return Some("copy_bound");
    }
    let declared = member(message, "params")
        .and_then(array)
        .unwrap_or_default();
    let names: Vec<&str> = declared
        .iter()
        .filter_map(|item| item.get("name").and_then(string))
        .collect();
    let same_names = values.len() == names.len()
        && values
            .iter()
            .all(|(name, _)| names.contains(&name.as_str()));
    let admitted = same_names
        && declared.iter().all(|item| {
            let name = item.get("name").and_then(string).unwrap_or_default();
            member(values, name).is_some_and(|value| param_value(item, value))
        });
    (!admitted).then_some("copy_params")
}

/// Whether one parameter value satisfies its declaration's kind and `max_length`.
#[must_use]
pub fn param_value(declaration: &Value, value: &Value) -> bool {
    let Some(maximum) = declaration.get("max_length").and_then(small_int) else {
        return false;
    };
    let kind = declaration.get("kind").and_then(string).unwrap_or_default();
    if kind == "integer" {
        let limit = u32::try_from(maximum)
            .ok()
            .and_then(|exponent| 10_i64.checked_pow(exponent))
            .unwrap_or(i64::MAX);
        return int_within(value, 0, limit - 1);
    }
    let Some(text) = string(value)
        .filter(|text| i64::try_from(char_len(text)).is_ok_and(|length| length <= maximum))
    else {
        return false;
    };
    match kind {
        "domain" => domain(text),
        "dns_name" => dns_name(text),
        "identifier" => identifier_text(text),
        _ => false,
    }
}

fn label(text: &str, extra: impl Fn(u8) -> bool) -> bool {
    let bytes = text.as_bytes();
    let character = |byte: &u8| byte.is_ascii_lowercase() || byte.is_ascii_digit() || extra(*byte);
    (1..=63).contains(&bytes.len())
        && bytes.iter().all(|byte| character(byte) || *byte == b'-')
        && bytes.first().is_some_and(character)
        && bytes.last().is_some_and(character)
}

/// `(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?`: two or more labels.
fn domain(text: &str) -> bool {
    let labels: Vec<&str> = text.split('.').collect();
    labels.len() >= 2 && labels.iter().all(|part| label(part, |_| false))
}

/// One or more labels of `[a-z0-9_-]` that neither start nor end with a hyphen.
fn dns_name(text: &str) -> bool {
    text.split('.').all(|part| label(part, |byte| byte == b'_'))
}

/// `[A-Za-z0-9][A-Za-z0-9._:-]*`.
fn identifier_text(text: &str) -> bool {
    let bytes = text.as_bytes();
    bytes.first().is_some_and(u8::is_ascii_alphanumeric)
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

fn base_error<'a>(request: &'a Value, catalog: &Catalog<'_>) -> Result<&'a Members, &'static str> {
    let members = object(request)
        .filter(|members| BASE.iter().all(|key| member(members, key).is_some()))
        .ok_or("request_shape")?;
    let shaped = member(members, "kind").and_then(string).is_some()
        && member(members, "ordinal").is_some_and(|ordinal| int_within(ordinal, 0, 7));
    if !shaped {
        return Err("request_shape");
    }
    copy_error(
        members,
        catalog,
        &[("title", false), ("description", false)],
    )
    .map_or(Ok(members), Err)
}

/// The first error of the named copy references of `value`; a nullable field also admits null.
fn copy_error(
    value: &Members,
    catalog: &Catalog<'_>,
    fields: &[(&str, bool)],
) -> Option<&'static str> {
    fields.iter().find_map(|(field, nullable)| {
        let reference = member(value, field.strip_prefix("option_").unwrap_or(field))?;
        if *reference == Value::Null && *nullable {
            return None;
        }
        let bound = COPY_BOUNDS
            .iter()
            .find(|(name, _)| name == field)
            .map_or(0, |(_, bound)| *bound);
        reference_error(reference, catalog, bound)
    })
}

fn length_error(request: &Members, limit: i64, catalog: &Catalog<'_>) -> Option<&'static str> {
    let password = member(request, "kind").and_then(string) == Some("input:password");
    let mut expected = BASE.to_vec();
    expected.extend([
        "label",
        "required",
        "placeholder",
        "min_length",
        "max_length",
    ]);
    if password {
        expected.push("stored_input");
    }
    if !has_exactly(request, &expected)
        || !matches!(member(request, "required"), Some(Value::Bool(_)))
    {
        return Some("request_shape");
    }
    if member(request, "stored_input").is_some_and(|slot| !string(slot).is_some_and(identifier)) {
        return Some("stored_input");
    }
    if let Some(error) = copy_error(request, catalog, &[("label", false), ("placeholder", true)]) {
        return Some(error);
    }
    let (minimum, maximum) = (member(request, "min_length"), member(request, "max_length"));
    let bounded = minimum
        .and_then(small_int)
        .zip(maximum.and_then(small_int))
        .is_some_and(|(minimum, maximum)| 0 <= minimum && minimum <= maximum && maximum <= limit);
    (!bounded).then_some("length_bounds")
}

fn choice_error(request: &Members, catalog: &Catalog<'_>, multiple: bool) -> Option<&'static str> {
    let mut expected = BASE.to_vec();
    expected.extend(["label", "required", "options"]);
    if multiple {
        expected.extend(["min_selections", "max_selections"]);
    }
    if !has_exactly(request, &expected)
        || !matches!(member(request, "required"), Some(Value::Bool(_)))
    {
        return Some("request_shape");
    }
    let options = member(request, "options")
        .and_then(array)
        .unwrap_or_default();
    if !(2..=MAX_OPTIONS).contains(&options.len()) || !options.iter().all(option) {
        return Some("options");
    }
    let values: Vec<&str> = options
        .iter()
        .filter_map(|item| item.get("value").and_then(string))
        .collect();
    if values
        .iter()
        .enumerate()
        .any(|(index, value)| values[..index].contains(value))
    {
        return Some("options");
    }
    let copy = copy_error(request, catalog, &[("label", false)]).or_else(|| {
        options.iter().filter_map(object).find_map(|item| {
            copy_error(
                item,
                catalog,
                &[("option_label", false), ("option_description", true)],
            )
        })
    });
    if copy.is_some() || !multiple {
        return copy;
    }
    let (minimum, maximum) = (
        member(request, "min_selections"),
        member(request, "max_selections"),
    );
    let count = i64::try_from(options.len()).unwrap_or(i64::MAX);
    let bounded = minimum
        .and_then(small_int)
        .zip(maximum.and_then(small_int))
        .is_some_and(|(minimum, maximum)| 0 <= minimum && minimum <= maximum && maximum <= count);
    (!bounded).then_some("selection_bounds")
}

fn option(value: &Value) -> bool {
    object(value).is_some_and(|members| {
        has_exactly(members, &["value", "label", "description"])
            && member(members, "value")
                .and_then(string)
                .is_some_and(|text| choice_text(text, 128))
    })
}

fn choice_text(text: &str, maximum: usize) -> bool {
    is_stripped(text) && (1..=maximum).contains(&char_len(text)) && printable(text)
}

/// A Stored Input slot: 1 to 64 lowercase ASCII letters, digits, and single inner hyphens, starting with a letter.
fn identifier(text: &str) -> bool {
    let bytes = text.as_bytes();
    (1..=64).contains(&bytes.len())
        && bytes[0].is_ascii_lowercase()
        && bytes.last() != Some(&b'-')
        && !text.contains("--")
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
}

/// The first deterministic error of a replay transcript, or `None`.
#[must_use]
pub fn transcript_error(
    requests: &Value,
    responses: &Value,
    catalog: &Catalog<'_>,
) -> Option<&'static str> {
    let (Some(requests), Some(responses)) = (array(requests), array(responses)) else {
        return Some("transcript_shape");
    };
    if requests.len() > MAX_REQUESTS {
        return Some("transcript_shape");
    }
    let in_sequence = requests.iter().enumerate().all(|(index, request)| {
        let expected = Value::Number(strict_json::Number::Int(
            i64::try_from(index).unwrap_or(i64::MAX),
        ));
        request
            .get("ordinal")
            .is_some_and(|ordinal| py_eq(ordinal, &expected))
    });
    if !in_sequence {
        return Some("ordinal_sequence");
    }
    if let Some(error) = requests
        .iter()
        .find_map(|request| request_error(request, catalog))
    {
        return Some(error);
    }
    transcript_responses_error(requests, responses)
}

fn kind_of(request: &Value) -> &str {
    request.get("kind").and_then(string).unwrap_or_default()
}

fn transcript_responses_error(requests: &[Value], responses: &[Value]) -> Option<&'static str> {
    if requests
        .iter()
        .filter(|request| AUTHORIZATION_KINDS.contains(&kind_of(request)))
        .count()
        > 1
    {
        return Some("authorization_once");
    }
    if requests
        .iter()
        .any(|request| kind_of(request) == "input:password")
    {
        return Some("stored_input_response");
    }
    if responses.len() != requests.len() {
        return Some("response_count");
    }
    requests
        .iter()
        .zip(responses)
        .find_map(|(request, response)| response_error(request, response))
}

/// The error of one response to one admitted request, or `None`.
#[must_use]
pub fn response_error(request: &Value, response: &Value) -> Option<&'static str> {
    let Some(members) = object(response)
        .filter(|members| has_exactly(members, &["kind", "ordinal", "fingerprint", "value"]))
    else {
        return Some("response_shape");
    };
    let equal = |key: &str| {
        member(members, key)
            .zip(request.get(key))
            .is_some_and(|(left, right)| py_eq(left, right))
    };
    let fingerprinted =
        member(members, "fingerprint").and_then(string) == Some(fingerprint(request).as_str());
    if !equal("kind") || !equal("ordinal") || !fingerprinted {
        return Some("response_match");
    }
    let value = member(members, "value").unwrap_or(&Value::Null);
    (!response_value_admitted(request, value)).then_some("response_value")
}

fn response_value_admitted(request: &Value, value: &Value) -> bool {
    let kind = kind_of(request);
    let options = request.get("options").and_then(array).unwrap_or_default();
    let allowed: Vec<&Value> = options
        .iter()
        .filter_map(|item| item.get("value"))
        .collect();
    if kind == "approval" || AUTH_KINDS.contains(&kind) {
        return is_true(value);
    }
    if CHOICE_KINDS.contains(&kind) {
        let optional = request.get("required") == Some(&Value::Bool(false));
        return string(value)
            .is_some_and(|text| allowed.iter().any(|item| string(item) == Some(text)))
            || (string(value) == Some("") && optional);
    }
    if kind == "input:choices" {
        return choices_admitted(request, value, &allowed);
    }
    text_response_admitted(request, value)
}

fn choices_admitted(request: &Value, value: &Value, allowed: &[&Value]) -> bool {
    let Some(items) = array(value) else {
        return false;
    };
    let texts: Vec<&str> = items.iter().filter_map(string).collect();
    let unique = texts
        .iter()
        .enumerate()
        .all(|(index, text)| !texts[..index].contains(text));
    let count = i64::try_from(items.len()).unwrap_or(i64::MAX);
    let within = |key: &str| request.get(key).and_then(small_int);
    texts.len() == items.len()
        && unique
        && items
            .iter()
            .all(|item| allowed.iter().any(|option| py_eq(option, item)))
        && within("min_selections").is_some_and(|minimum| minimum <= count)
        && within("max_selections").is_some_and(|maximum| count <= maximum)
}

fn text_response_admitted(request: &Value, value: &Value) -> bool {
    let Some(text) = string(value) else {
        return false;
    };
    if request.get("required").is_some_and(is_true) && text.is_empty() {
        return false;
    }
    let length = i64::try_from(char_len(text)).unwrap_or(i64::MAX);
    let bound = |key: &str| request.get(key).and_then(small_int);
    bound("min_length").is_some_and(|minimum| minimum <= length)
        && bound("max_length").is_some_and(|maximum| length <= maximum)
}
