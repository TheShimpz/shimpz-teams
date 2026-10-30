//! The Python semantics the reference validators rely on, over strict JSON values: `type(x) is int`, `==` across
//! numbers and booleans, `str.strip`, `len` in code points, and `json.dumps` with sorted keys.

use std::fmt::Write as _;

use sha2::{Digest, Sha256};
use strict_json::{Number, Value};

/// The members of an object, or `None` for another value (`isinstance(x, dict)`).
#[must_use]
pub fn object(value: &Value) -> Option<&[(String, Value)]> {
    match value {
        Value::Object(members) => Some(members),
        _ => None,
    }
}

/// The items of an array, or `None` for another value (`isinstance(x, list)`).
#[must_use]
pub fn array(value: &Value) -> Option<&[Value]> {
    match value {
        Value::Array(items) => Some(items),
        _ => None,
    }
}

/// The text of a string, or `None` for another value (`isinstance(x, str)`).
#[must_use]
pub fn string(value: &Value) -> Option<&str> {
    match value {
        Value::String(text) => Some(text),
        _ => None,
    }
}

/// The value of member `key`, if present.
#[must_use]
pub fn member<'a>(members: &'a [(String, Value)], key: &str) -> Option<&'a Value> {
    members
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value)
}

/// `set(d) == set(keys)` for an object whose names are unique.
#[must_use]
pub fn has_exactly(members: &[(String, Value)], keys: &[&str]) -> bool {
    members.len() == keys.len() && keys.iter().all(|key| member(members, key).is_some())
}

/// `type(x) is int` (a JSON integer of any size; never a boolean or a float).
#[must_use]
pub const fn is_int(value: &Value) -> bool {
    matches!(value, Value::Number(Number::Int(_) | Number::BigInt(_)))
}

/// `type(x) is int and low <= x <= high`.
#[must_use]
pub fn int_within(value: &Value, low: i64, high: i64) -> bool {
    matches!(value, Value::Number(Number::Int(integer)) if (low..=high).contains(integer))
}

/// The integer of `type(x) is int` when it fits 64 bits.
#[must_use]
pub const fn small_int(value: &Value) -> Option<i64> {
    match value {
        Value::Number(Number::Int(integer)) => Some(*integer),
        _ => None,
    }
}

/// `value is True`.
#[must_use]
pub const fn is_true(value: &Value) -> bool {
    matches!(value, Value::Bool(true))
}

/// Python's numeric view of a number or boolean, for `==` across `bool`, `int`, and `float`.
enum Numeric<'a> {
    Int(i64),
    Big(&'a str),
    Float(f64),
}

fn numeric(value: &Value) -> Option<Numeric<'_>> {
    match value {
        Value::Bool(flag) => Some(Numeric::Int(i64::from(*flag))),
        Value::Number(Number::Int(integer)) => Some(Numeric::Int(*integer)),
        Value::Number(Number::BigInt(digits)) => Some(Numeric::Big(digits)),
        Value::Number(Number::Float(float)) => Some(Numeric::Float(*float)),
        _ => None,
    }
}

/// The exact decimal digits of an integral float, as Python compares it with an int.
fn integral_digits(float: f64) -> Option<String> {
    (float.fract() == 0.0).then(|| format!("{float:.0}").replace("-0", "0"))
}

fn numeric_eq(left: &Numeric<'_>, right: &Numeric<'_>) -> bool {
    match (left, right) {
        (Numeric::Int(left), Numeric::Int(right)) => left == right,
        (Numeric::Big(left), Numeric::Big(right)) => left == right,
        (Numeric::Float(left), Numeric::Float(right)) => left == right,
        (Numeric::Int(_) | Numeric::Big(_), Numeric::Float(float))
        | (Numeric::Float(float), Numeric::Int(_) | Numeric::Big(_)) => {
            let integer = match (left, right) {
                (Numeric::Int(integer), _) | (_, Numeric::Int(integer)) => integer.to_string(),
                (Numeric::Big(digits), _) | (_, Numeric::Big(digits)) => (*digits).to_owned(),
                _ => return false,
            };
            integral_digits(*float).is_some_and(|digits| digits == integer)
        }
        _ => false,
    }
}

/// Python `left == right` between two decoded JSON values: `True == 1 == 1.0`, objects regardless of member order.
#[must_use]
pub fn py_eq(left: &Value, right: &Value) -> bool {
    if let (Some(left), Some(right)) = (numeric(left), numeric(right)) {
        return numeric_eq(&left, &right);
    }
    match (left, right) {
        (Value::Null, Value::Null) => true,
        (Value::String(left), Value::String(right)) => left == right,
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len() && left.iter().zip(right).all(|(a, b)| py_eq(a, b))
        }
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .all(|(key, value)| member(right, key).is_some_and(|other| py_eq(value, other)))
        }
        _ => false,
    }
}

/// `value in items` for a list: any item Python-equal to it.
#[must_use]
pub fn contains(items: &[Value], value: &Value) -> bool {
    items.iter().any(|item| py_eq(item, value))
}

/// `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`: the shared canonical encoding.
#[must_use]
pub fn canonical_json(value: &Value) -> String {
    let mut out = String::new();
    write_value(value, &mut out);
    out
}

/// Two values compared as their sorted-key JSON encodings: booleans, integers, and floats never equal one another.
#[must_use]
pub fn same(left: &Value, right: &Value) -> bool {
    canonical_json(left) == canonical_json(right)
}

fn write_value(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
        Value::Number(Number::Int(integer)) => {
            let _ = write!(out, "{integer}");
        }
        Value::Number(Number::BigInt(digits)) => out.push_str(digits),
        Value::Number(Number::Float(float)) => out.push_str(&float_repr(*float)),
        Value::String(text) => write_string(text, out),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                write_value(item, out);
            }
            out.push(']');
        }
        Value::Object(members) => write_object(members, out),
    }
}

fn write_object(members: &[(String, Value)], out: &mut String) {
    let mut ordered: Vec<&(String, Value)> = members.iter().collect();
    ordered.sort_by(|left, right| left.0.cmp(&right.0));
    out.push('{');
    for (index, (key, item)) in ordered.into_iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        write_string(key, out);
        out.push(':');
        write_value(item, out);
    }
    out.push('}');
}

/// `repr(float)`: the shortest round-trip digits, positional for a decimal exponent from -4 to 15.
fn float_repr(float: f64) -> String {
    let scientific = format!("{:e}", float.abs());
    let (mantissa, exponent) = scientific
        .split_once('e')
        .unwrap_or((scientific.as_str(), "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let sign = if float.is_sign_negative() { "-" } else { "" };
    let body = if (-4..16).contains(&exponent) {
        positional(&digits, exponent)
    } else {
        let (first, rest) = digits.split_at(1);
        let fraction = if rest.is_empty() {
            String::new()
        } else {
            format!(".{rest}")
        };
        let exponent_sign = if exponent.is_negative() { '-' } else { '+' };
        format!(
            "{first}{fraction}e{exponent_sign}{:02}",
            exponent.unsigned_abs()
        )
    };
    format!("{sign}{body}")
}

fn positional(digits: &str, exponent: i32) -> String {
    let point = exponent + 1;
    let count = i32::try_from(digits.len()).unwrap_or(i32::MAX);
    if point <= 0 {
        format!("0.{}{digits}", "0".repeat(point.unsigned_abs() as usize))
    } else if point >= count {
        format!(
            "{digits}{}.0",
            "0".repeat((point - count).unsigned_abs() as usize)
        )
    } else {
        let (whole, fraction) = digits.split_at(point.unsigned_abs() as usize);
        format!("{whole}.{fraction}")
    }
}

fn write_string(text: &str, out: &mut String) {
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            '\0'..='\u{1f}' => {
                let _ = write!(out, "\\u{:04x}", u32::from(character));
            }
            _ => out.push(character),
        }
    }
    out.push('"');
}

/// The lowercase hexadecimal SHA-256 of `bytes`.
#[must_use]
pub fn sha256_hex(bytes: &[u8]) -> String {
    let mut hex = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        let _ = write!(hex, "{byte:02x}");
    }
    hex
}

/// `str.isspace` for one character.
#[must_use]
pub const fn is_space(character: char) -> bool {
    matches!(
        character,
        '\t'..='\r'
            | '\u{1c}'..='\u{20}'
            | '\u{85}'
            | '\u{a0}'
            | '\u{1680}'
            | '\u{2000}'..='\u{200a}'
            | '\u{2028}'
            | '\u{2029}'
            | '\u{202f}'
            | '\u{205f}'
            | '\u{3000}'
    )
}

/// `text == text.strip()`.
#[must_use]
pub fn is_stripped(text: &str) -> bool {
    !text.starts_with(is_space) && !text.ends_with(is_space)
}

/// `len(text)` in code points.
#[must_use]
pub fn char_len(text: &str) -> usize {
    text.chars().count()
}

#[cfg(test)]
mod tests {
    use strict_json::{Number, Value};

    use super::{canonical_json, float_repr, is_stripped, py_eq};

    fn parsed(text: &str) -> Value {
        strict_json::parse(text.as_bytes()).expect("test JSON")
    }

    #[test]
    fn floats_print_as_python_repr() {
        for (float, text) in [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (100.0, "100.0"),
            (1e16, "1e+16"),
            (0.00001, "1e-05"),
            (0.1, "0.1"),
        ] {
            assert_eq!(float_repr(float), text);
        }
    }

    #[test]
    fn canonical_json_sorts_keys_and_keeps_text() {
        let value = parsed(r#"{"b": [1, 2.5, null, true], "a": "é\n\u0001"}"#);
        assert_eq!(
            canonical_json(&value),
            "{\"a\":\"é\\n\\u0001\",\"b\":[1,2.5,null,true]}"
        );
    }

    #[test]
    fn equality_follows_python() {
        let one = Value::Number(Number::Int(1));
        assert!(py_eq(&Value::Bool(true), &one));
        assert!(py_eq(&Value::Number(Number::Float(1.0)), &one));
        assert!(!py_eq(&Value::Number(Number::Float(1.5)), &one));
        assert!(py_eq(
            &parsed(r#"{"a": 1, "b": 2}"#),
            &parsed(r#"{"b": 2, "a": true}"#)
        ));
        assert!(!py_eq(&Value::String("1".to_owned()), &one));
        let big = Value::Number(Number::BigInt("18446744073709551616".to_owned()));
        assert!(py_eq(
            &Value::Number(Number::Float(18_446_744_073_709_551_616.0)),
            &big
        ));
    }

    #[test]
    fn strip_uses_python_whitespace() {
        assert!(is_stripped("a b"));
        assert!(!is_stripped("a\u{1f}"));
        assert!(!is_stripped("\u{3000}a"));
        assert!(is_stripped("a\u{200b}"));
    }
}
