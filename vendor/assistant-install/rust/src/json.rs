//! The Python value semantics the reference verifier relies on: member access, `==` between decoded values, and the
//! sorted-key JSON text it compares values by.

use std::fmt::Write as _;

use strict_json::{Number, Value};

/// An object's members.
pub type Members = [(String, Value)];

/// The members of an object, or `None`.
pub(crate) fn object(value: &Value) -> Option<&Members> {
    match value {
        Value::Object(members) => Some(members),
        _ => None,
    }
}

/// The items of an array, or `None`.
pub(crate) fn array(value: &Value) -> Option<&[Value]> {
    match value {
        Value::Array(items) => Some(items),
        _ => None,
    }
}

/// The text of a string, or `None`.
pub(crate) fn string(value: &Value) -> Option<&str> {
    match value {
        Value::String(text) => Some(text),
        _ => None,
    }
}

/// Member `key` of an object value.
pub(crate) fn get<'a>(value: &'a Value, key: &str) -> Option<&'a Value> {
    value.get(key)
}

/// Python `==` between two strings or between two values of other kinds compared by their sorted-key text; the
/// verifier compares only strings, identifiers, and closed JSON literals this way.
pub(crate) fn text_eq(value: Option<&Value>, text: &str) -> bool {
    value.and_then(string) == Some(text)
}

/// `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`.
#[must_use]
pub fn canonical(value: &Value) -> String {
    let mut out = String::new();
    write_value(value, &mut out);
    out
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
        Value::Object(members) => {
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
    }
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
    let point = exponent + 1;
    let count = i32::try_from(digits.len()).unwrap_or(i32::MAX);
    let body = if !(-4..16).contains(&exponent) {
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
    } else if point <= 0 {
        format!("0.{}{digits}", "0".repeat(point.unsigned_abs() as usize))
    } else if point >= count {
        format!(
            "{digits}{}.0",
            "0".repeat((point - count).unsigned_abs() as usize)
        )
    } else {
        let (whole, fraction) = digits.split_at(point.unsigned_abs() as usize);
        format!("{whole}.{fraction}")
    };
    format!("{sign}{body}")
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

#[cfg(test)]
mod tests {
    use strict_json::{Number, Value};

    use super::canonical;

    #[test]
    fn values_compare_by_python_sorted_key_text() {
        let value =
            strict_json::parse(r#"{"b": [1, 1.0, 1e16, true], "a": "\u00e9\n"}"#.as_bytes())
                .expect("JSON");
        assert_eq!(
            canonical(&value),
            "{\"a\":\"é\\n\",\"b\":[1,1.0,1e+16,true]}"
        );
        assert_eq!(canonical(&Value::Number(Number::Float(0.0001))), "0.0001");
        assert_eq!(canonical(&Value::Number(Number::Float(0.00001))), "1e-05");
    }
}
