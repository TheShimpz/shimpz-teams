//! Python's view of a decoded JSON value: its type tests, equality, and the member and number checks the validators
//! repeat.
//!
//! Python decodes a JSON integer to `int`, a fraction or exponent to `float`, `true`/`false` to `bool` (a subclass of
//! `int` equal to 1 and 0), objects to `dict` and arrays to `list`. `type(x) is int` excludes `bool`;
//! `isinstance(x, int)` includes it; `==` compares numbers by value across `bool`, `int`, and `float`, exactly.

use std::cmp::Ordering;

use strict_json::{Number, Value};

/// The members of an object, or `None` for any other value (`isinstance(x, dict)`).
#[must_use]
pub fn object(value: &Value) -> Option<&[(String, Value)]> {
    match value {
        Value::Object(members) => Some(members),
        _ => None,
    }
}

/// The items of an array, or `None` for any other value (`isinstance(x, list)`).
#[must_use]
pub fn array(value: &Value) -> Option<&[Value]> {
    match value {
        Value::Array(items) => Some(items),
        _ => None,
    }
}

/// The text of a string, or `None` for any other value (`isinstance(x, str)`).
#[must_use]
pub fn string(value: &Value) -> Option<&str> {
    match value {
        Value::String(text) => Some(text),
        _ => None,
    }
}

/// `value[key]` of an object, or `None` when it is no object or has no such member (`dict.get`).
#[must_use]
pub fn member<'a>(value: &'a Value, key: &str) -> Option<&'a Value> {
    value.get(key)
}

/// `set(value) == set(fields)` for an object: exactly these member names, in any order.
#[must_use]
pub fn has_exactly(members: &[(String, Value)], fields: &[&str]) -> bool {
    members.len() == fields.len()
        && members
            .iter()
            .all(|(key, _)| fields.contains(&key.as_str()))
}

/// The object's members when it is an object with exactly these member names.
#[must_use]
pub fn exact_object<'a>(value: &'a Value, fields: &[&str]) -> Option<&'a [(String, Value)]> {
    object(value).filter(|members| has_exactly(members, fields))
}

/// `value[key]` of an object known to hold `key`; `Null` stands in only where a caller proved the member exists.
#[must_use]
pub fn field<'a>(members: &'a [(String, Value)], key: &str) -> &'a Value {
    members
        .iter()
        .find(|(name, _)| name == key)
        .map_or(&Value::Null, |(_, value)| value)
}

/// The integer of a value whose `type(x) is int`, when it fits `i64` (a larger one is `None` too, see [`is_int`]).
#[must_use]
pub const fn small_int(value: &Value) -> Option<i64> {
    match value {
        Value::Number(Number::Int(integer)) => Some(*integer),
        _ => None,
    }
}

/// `type(x) is int`: an integer, never a boolean or a float.
#[must_use]
pub const fn is_int(value: &Value) -> bool {
    matches!(value, Value::Number(Number::Int(_) | Number::BigInt(_)))
}

/// `type(x) is int and low <= x <= high`.
#[must_use]
pub fn whole(value: &Value, low: i64, high: i64) -> bool {
    small_int(value).is_some_and(|integer| (low..=high).contains(&integer))
}

/// `isinstance(x, int)`: an integer or a boolean.
#[must_use]
pub const fn is_int_like(value: &Value) -> bool {
    matches!(value, Value::Bool(_)) || is_int(value)
}

/// `type(x) is bool`.
#[must_use]
pub const fn is_bool(value: &Value) -> bool {
    matches!(value, Value::Bool(_))
}

/// A number's exact comparison key: an integer as decimal text with its sign, or a float.
enum Numeric<'a> {
    Integer(i128),
    Big(&'a str),
    Float(f64),
}

fn numeric(value: &Value) -> Option<Numeric<'_>> {
    match value {
        Value::Bool(flag) => Some(Numeric::Integer(i128::from(*flag))),
        Value::Number(Number::Int(integer)) => Some(Numeric::Integer(i128::from(*integer))),
        Value::Number(Number::BigInt(digits)) => Some(Numeric::Big(digits)),
        Value::Number(Number::Float(float)) => Some(Numeric::Float(*float)),
        _ => None,
    }
}

/// The exact decimal text of a whole float, sign included (zero unsigned), or `None` when it has a fraction.
fn whole_float_text(float: f64) -> Option<String> {
    (float.fract() == 0.0).then(|| format!("{:.0}", float + 0.0))
}

fn numbers_equal(left: &Numeric<'_>, right: &Numeric<'_>) -> bool {
    match (left, right) {
        (Numeric::Integer(a), Numeric::Integer(b)) => a == b,
        (Numeric::Big(a), Numeric::Big(b)) => a == b,
        (Numeric::Float(a), Numeric::Float(b)) => a.partial_cmp(b) == Some(Ordering::Equal),
        (Numeric::Integer(_), Numeric::Big(_)) | (Numeric::Big(_), Numeric::Integer(_)) => false,
        (Numeric::Integer(integer), Numeric::Float(float))
        | (Numeric::Float(float), Numeric::Integer(integer)) => {
            whole_float_text(*float).is_some_and(|text| text == integer.to_string())
        }
        (Numeric::Big(digits), Numeric::Float(float))
        | (Numeric::Float(float), Numeric::Big(digits)) => {
            whole_float_text(*float).is_some_and(|text| text == *digits)
        }
    }
}

/// Python `left == right` for decoded JSON values.
#[must_use]
pub fn py_eq(left: &Value, right: &Value) -> bool {
    if let (Some(a), Some(b)) = (numeric(left), numeric(right)) {
        return numbers_equal(&a, &b);
    }
    match (left, right) {
        (Value::Null, Value::Null) => true,
        (Value::String(a), Value::String(b)) => a == b,
        (Value::Array(a), Value::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(x, y)| py_eq(x, y))
        }
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len()
                && a.iter().all(|(key, x)| {
                    b.iter()
                        .find(|(name, _)| name == key)
                        .is_some_and(|(_, y)| py_eq(x, y))
                })
        }
        _ => false,
    }
}

/// Python `left == right` against an integer.
#[must_use]
pub fn eq_int(value: &Value, integer: i64) -> bool {
    py_eq(value, &Value::Number(Number::Int(integer)))
}

/// Python `left == right` against a string.
#[must_use]
pub fn eq_str(value: &Value, text: &str) -> bool {
    string(value) == Some(text)
}

/// Strings sorted and deduplicated, as `sorted(set(strings))` orders them (by code point).
#[must_use]
pub fn sorted_unique(strings: &[&str]) -> Vec<String> {
    let mut sorted: Vec<String> = strings.iter().map(|text| (*text).to_owned()).collect();
    sorted.sort();
    sorted.dedup();
    sorted
}

/// Whether `strings == sorted(set(strings))`: strictly increasing by code point.
#[must_use]
pub fn strictly_sorted(strings: &[&str]) -> bool {
    strings
        .windows(2)
        .all(|pair| pair[0].cmp(pair[1]) == Ordering::Less)
}

/// The decimal digits of a non-negative integer value (`Int` or `BigInt`), or `None` for anything else.
#[must_use]
pub fn natural_digits(value: &Value) -> Option<String> {
    match value {
        Value::Number(Number::Int(integer)) if *integer >= 0 => Some(integer.to_string()),
        Value::Number(Number::BigInt(digits)) if !digits.starts_with('-') => Some(digits.clone()),
        _ => None,
    }
}

/// Compares two non-negative decimal digit strings without leading zeros.
#[must_use]
pub fn natural_cmp(left: &str, right: &str) -> Ordering {
    left.len().cmp(&right.len()).then_with(|| left.cmp(right))
}

/// `left - right` of two non-negative decimal digit strings with `left >= right`, without leading zeros.
#[must_use]
pub fn natural_sub(left: &str, right: &str) -> String {
    let mut digits: Vec<u8> = Vec::with_capacity(left.len());
    let mut borrow = 0;
    let mut right = right.bytes().rev();
    for digit in left.bytes().rev() {
        let subtrahend = right.next().map_or(0, |byte| byte - b'0') + borrow;
        let minuend = digit - b'0';
        borrow = u8::from(minuend < subtrahend);
        digits.push(minuend + borrow * 10 - subtrahend + b'0');
    }
    while digits.len() > 1 && digits.last() == Some(&b'0') {
        digits.pop();
    }
    digits.reverse();
    String::from_utf8(digits).unwrap_or_default()
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
pub fn object_of(members: Vec<(&str, Value)>) -> Value {
    Value::Object(
        members
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    )
}

#[cfg(test)]
mod tests;
