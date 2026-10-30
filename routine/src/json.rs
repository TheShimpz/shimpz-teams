//! The JSON operations the Routine logic shares: canonical bytes, digests, Python's default `json.dumps`, and building
//! and reading `strict_json` values with Python's `dict` and `list` meaning.

use sha2::{Digest, Sha256};
use strict_json::{Number, Value};
use team_protocol::python::json::{Options, dumps};

pub use team_protocol::python::json::canonical;

/// An empty JSON object, which stands for a schema that is not an object.
pub static EMPTY: Value = Value::Object(Vec::new());

/// The lowercase hexadecimal SHA-256 of `bytes`.
#[must_use]
pub fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    let mut text = String::with_capacity(64);
    for byte in digest {
        text.push(char::from_digit(u32::from(byte >> 4), 16).unwrap_or('0'));
        text.push(char::from_digit(u32::from(byte & 15), 16).unwrap_or('0'));
    }
    text
}

/// `json.dumps(value, ensure_ascii=False)`: insertion order and Python's default separators.
#[must_use]
pub fn json_text(value: &Value) -> String {
    dumps(
        value,
        Options {
            ensure_ascii: false,
            sort_keys: false,
            compact: false,
        },
    )
}

/// A string value.
#[must_use]
pub fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

/// An integer value.
#[must_use]
pub const fn int(value: i64) -> Value {
    Value::Number(Number::Int(value))
}

/// An object of these members, in this order.
#[must_use]
pub fn object(members: Vec<(&str, Value)>) -> Value {
    Value::Object(
        members
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    )
}

/// The members of an object, or none for any other value.
#[must_use]
pub fn members(value: &Value) -> &[(String, Value)] {
    match value {
        Value::Object(members) => members,
        _ => &[],
    }
}

/// The elements of an array, or none for any other value.
#[must_use]
pub fn elements(value: &Value) -> &[Value] {
    match value {
        Value::Array(elements) => elements,
        _ => &[],
    }
}

/// `isinstance(value, dict)`.
#[must_use]
pub const fn is_object(value: &Value) -> bool {
    matches!(value, Value::Object(_))
}

/// `isinstance(value, str)`, as its text.
#[must_use]
pub fn as_str(value: &Value) -> Option<&str> {
    match value {
        Value::String(text) => Some(text),
        _ => None,
    }
}

/// `value.get(key)` on an object, `None` for a missing member or any other value.
#[must_use]
pub fn get<'a>(value: &'a Value, key: &str) -> Option<&'a Value> {
    value.get(key)
}

/// `value[key]` on an object, `null` when it is missing (only for values whose shape was already admitted).
#[must_use]
pub fn at<'a>(value: &'a Value, key: &str) -> &'a Value {
    value.get(key).unwrap_or(&Value::Null)
}

/// `value.get(key) == expected` for a string `expected`.
#[must_use]
pub fn is_text(value: Option<&Value>, expected: &str) -> bool {
    matches!(value, Some(Value::String(text)) if text == expected)
}

/// `set(value) == set(fields)` for an object: exactly these member names.
#[must_use]
pub fn has_fields(value: &Value, fields: &[&str]) -> bool {
    match value {
        Value::Object(members) => {
            members.len() == fields.len()
                && members
                    .iter()
                    .all(|(name, _)| fields.contains(&name.as_str()))
        }
        _ => false,
    }
}

/// Sets one member of an object, replacing it in place or appending it, as `dict[key] = value` does.
pub fn set(value: &mut Value, key: &str, member: Value) {
    if let Value::Object(members) = value {
        if let Some(slot) = members.iter_mut().find(|(name, _)| name == key) {
            slot.1 = member;
        } else {
            members.push((key.to_owned(), member));
        }
    }
}

/// The object's members sorted by name, as `sorted(value.items())` visits them.
#[must_use]
pub fn sorted_members(value: &Value) -> Vec<&(String, Value)> {
    let mut sorted: Vec<&(String, Value)> = members(value).iter().collect();
    sorted.sort_by(|left, right| left.0.cmp(&right.0));
    sorted
}

/// `type(value) is int`: an integer of any size, never a boolean or a float.
#[must_use]
pub const fn is_integer(value: &Value) -> bool {
    matches!(value, Value::Number(Number::Int(_) | Number::BigInt(_)))
}
