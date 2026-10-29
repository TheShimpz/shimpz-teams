//! Python's `json.dumps` over a strict JSON value, byte for byte, and the Shimpz canonical JSON v1 writer.
//!
//! Strings escape `"`, `\`, and every control character below U+0020 (`\b \f \n \r \t` by name, the rest as
//! lowercase `\u00XX`); with `ensure_ascii` every other character outside printable ASCII becomes lowercase `\uXXXX`,
//! astral characters as a surrogate pair, U+007F included. Integers print exactly, big ones included. Floats print as
//! Python's `repr`: the shortest text that reads back as the same double, in positional notation with `.0` for a
//! whole number when its decimal exponent is from -4 to 15, and otherwise in scientific notation with a signed exponent
//! of at least two digits (`1e+16`, `1.5e-07`). A strict JSON value never holds a non-finite number, so `allow_nan`
//! never matters.

use std::fmt::Write as _;

use strict_json::{Number, Value};

/// The encoder options of one `json.dumps` call.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Options {
    /// `ensure_ascii`: escape every character outside printable ASCII.
    pub ensure_ascii: bool,
    /// `sort_keys`: write object members sorted by name (code point order).
    pub sort_keys: bool,
    /// `separators=(",", ":")`; otherwise Python's default `(", ", ": ")`.
    pub compact: bool,
}

/// The Shimpz canonical JSON v1 options: sorted keys, compact separators, UTF-8 without escapes for non-ASCII.
pub const CANONICAL: Options = Options {
    ensure_ascii: false,
    sort_keys: true,
    compact: true,
};

/// `json.dumps(value, ensure_ascii=..., sort_keys=..., separators=...)`.
#[must_use]
pub fn dumps(value: &Value, options: Options) -> String {
    let mut out = String::new();
    write_value(value, options, &mut out);
    out
}

/// The Shimpz canonical JSON v1 bytes of `value` (`teams/core/canonical_json.py` `encode`).
#[must_use]
pub fn canonical(value: &Value) -> Vec<u8> {
    dumps(value, CANONICAL).into_bytes()
}

/// The UTF-8 length of `json.dumps(value, ...)` under `options`.
#[must_use]
pub fn encoded_len(value: &Value, options: Options) -> usize {
    dumps(value, options).len()
}

fn write_value(value: &Value, options: Options, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(number) => out.push_str(&number_text(number)),
        Value::String(text) => write_string(text, options.ensure_ascii, out),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push_str(item_separator(options));
                }
                write_value(item, options, out);
            }
            out.push(']');
        }
        Value::Object(members) => {
            let mut ordered: Vec<&(String, Value)> = members.iter().collect();
            if options.sort_keys {
                ordered.sort_by(|left, right| left.0.cmp(&right.0));
            }
            out.push('{');
            for (index, (key, member)) in ordered.into_iter().enumerate() {
                if index > 0 {
                    out.push_str(item_separator(options));
                }
                write_string(key, options.ensure_ascii, out);
                out.push_str(if options.compact { ":" } else { ": " });
                write_value(member, options, out);
            }
            out.push('}');
        }
    }
}

const fn item_separator(options: Options) -> &'static str {
    if options.compact { "," } else { ", " }
}

/// The text Python writes for a number: an integer exactly, a float as its `repr`.
#[must_use]
pub fn number_text(number: &Number) -> String {
    match number {
        Number::Int(integer) => integer.to_string(),
        Number::BigInt(digits) => digits.clone(),
        Number::Float(float) => float_repr(*float),
    }
}

/// `repr(float)` for a finite float.
#[must_use]
pub fn float_repr(float: f64) -> String {
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

/// Writes one JSON string as Python's encoder does.
pub fn write_string(text: &str, ensure_ascii: bool, out: &mut String) {
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
            '\0'..='\u{1f}' => push_escape(u32::from(character), out),
            ' '..='~' => out.push(character),
            _ if !ensure_ascii => out.push(character),
            _ => {
                let mut units = [0_u16; 2];
                for unit in character.encode_utf16(&mut units) {
                    push_escape(u32::from(*unit), out);
                }
            }
        }
    }
    out.push('"');
}

fn push_escape(unit: u32, out: &mut String) {
    let _ = write!(out, "\\u{unit:04x}");
}

#[cfg(test)]
mod tests {
    use strict_json::{Number, Value};

    use super::{CANONICAL, Options, canonical, dumps, encoded_len, float_repr};

    fn parsed(text: &str) -> Value {
        strict_json::parse(text.as_bytes()).expect("test JSON")
    }

    #[test]
    fn floats_print_as_python_repr() {
        let cases = [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.5, "1.5"),
            (100.0, "100.0"),
            (1e15, "1000000000000000.0"),
            (1e16, "1e+16"),
            (1.5e16, "1.5e+16"),
            (0.0001, "0.0001"),
            (0.00012, "0.00012"),
            (0.00001, "1e-05"),
            (-1.5e-300, "-1.5e-300"),
            (1.5e300, "1.5e+300"),
            (123_456_789_012_345.6, "123456789012345.6"),
            (0.1, "0.1"),
            (5e-324, "5e-324"),
            (f64::MAX, "1.7976931348623157e+308"),
        ];
        for (float, text) in cases {
            assert_eq!(float_repr(float), text);
        }
    }

    #[test]
    fn strings_escape_as_python_does() {
        let value = Value::String("\"\\\n\r\t\u{8}\u{c}\u{1}\u{7f}é😀".to_owned());
        let ascii = Options {
            ensure_ascii: true,
            sort_keys: false,
            compact: true,
        };
        assert_eq!(
            dumps(&value, CANONICAL),
            "\"\\\"\\\\\\n\\r\\t\\b\\f\\u0001\u{7f}é😀\""
        );
        assert_eq!(
            dumps(&value, ascii),
            "\"\\\"\\\\\\n\\r\\t\\b\\f\\u0001\\u007f\\u00e9\\ud83d\\ude00\""
        );
    }

    #[test]
    fn containers_follow_the_separators_and_key_order() {
        let value = parsed(
            r#"{"b": [1, 2.5, null, true, false], "a": {}, "c": [], "é": 18446744073709551616}"#,
        );
        assert_eq!(
            String::from_utf8(canonical(&value)).expect("utf-8"),
            r#"{"a":{},"b":[1,2.5,null,true,false],"c":[],"é":18446744073709551616}"#
        );
        let spaced = Options {
            ensure_ascii: true,
            sort_keys: false,
            compact: false,
        };
        assert_eq!(
            dumps(&value, spaced),
            "{\"b\": [1, 2.5, null, true, false], \"a\": {}, \"c\": [], \"\\u00e9\": 18446744073709551616}"
        );
        assert_eq!(encoded_len(&Value::Number(Number::Int(-12)), CANONICAL), 3);
    }
}
