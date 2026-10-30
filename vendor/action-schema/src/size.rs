//! The length of Team's encoding of a schema: Python `json.dumps` with compact separators and ASCII escapes.

use strict_json::{Number, Value};

/// The byte length of `json.dumps(value, separators=(",", ":"))`: compact separators, every character outside
/// printable ASCII escaped, integers with every digit, and floats in Python's `repr`.
#[must_use]
pub fn encoded_len(value: &Value) -> usize {
    match value {
        Value::Null | Value::Bool(true) => 4,
        Value::Bool(false) => 5,
        Value::Number(number) => number_len(number),
        Value::String(text) => string_len(text),
        Value::Array(items) => {
            2 + items.len().saturating_sub(1) + items.iter().map(encoded_len).sum::<usize>()
        }
        Value::Object(members) => {
            2 + members.len().saturating_sub(1)
                + members
                    .iter()
                    .map(|(key, child)| string_len(key) + 1 + encoded_len(child))
                    .sum::<usize>()
        }
    }
}

fn string_len(text: &str) -> usize {
    2 + text
        .chars()
        .map(|character| match character {
            '"' | '\\' | '\n' | '\r' | '\t' | '\u{8}' | '\u{c}' => 2,
            ' '..='~' => 1,
            '\u{10000}'.. => 12,
            _ => 6,
        })
        .sum::<usize>()
}

fn number_len(number: &Number) -> usize {
    match number {
        Number::Int(integer) => integer.to_string().len(),
        Number::BigInt(digits) => digits.len(),
        Number::Float(float) => python_float_repr(*float).len(),
    }
}

/// Python's `repr` of a finite float: the shortest digits that round-trip, in fixed notation while the decimal point
/// falls within 16 places after or 4 places before the first digit, else as `d.ddde±XX`, with `.0` added to a fixed
/// integral value.
#[must_use]
pub(crate) fn python_float_repr(float: f64) -> String {
    let sign = if float.is_sign_negative() { "-" } else { "" };
    let scientific = format!("{:e}", float.abs());
    let (mantissa, exponent) = scientific.split_once('e').unwrap_or((&scientific, "0"));
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let point = exponent.parse::<i32>().unwrap_or(0) + 1;
    let count = i32::try_from(digits.len()).unwrap_or(i32::MAX);
    let body = if -4 < point && point <= 16 {
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
    } else {
        let (first, rest) = digits.split_at(1);
        let fraction = if rest.is_empty() {
            String::new()
        } else {
            format!(".{rest}")
        };
        // A signed exponent of at least two digits: `e+16`, `e-05`, `e+300`.
        format!("{first}{fraction}e{:+03}", point - 1)
    };
    format!("{sign}{body}")
}

#[cfg(test)]
mod tests {
    use strict_json::{Number, Value};

    use super::{encoded_len, python_float_repr};

    #[test]
    fn floats_are_spelled_as_python_repr() {
        for (float, expected) in [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.5, "1.5"),
            (100.0, "100.0"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1.5e-5, "1.5e-05"),
            (1e15, "1000000000000000.0"),
            (1e16, "1e+16"),
            (1.234_567_890_123_456_7e16, "1.2345678901234568e+16"),
            (123_456_789_012_345.6, "123456789012345.6"),
            (-1.5e300, "-1.5e+300"),
            (5e-324, "5e-324"),
            (1.797_693_134_862_315_7e308, "1.7976931348623157e+308"),
            (0.1, "0.1"),
        ] {
            assert_eq!(python_float_repr(float), expected, "{float}");
        }
    }

    #[test]
    fn encoded_length_matches_python_json_dumps() {
        let value = Value::Object(vec![
            (
                "a".to_owned(),
                Value::Array(vec![
                    Value::Number(Number::Int(1)),
                    Value::Number(Number::Int(-2)),
                    Value::Bool(true),
                    Value::Bool(false),
                    Value::Null,
                ]),
            ),
            ("é".to_owned(), Value::Array(vec![])),
        ]);
        // json.dumps({"a": [1, -2, True, False, None], "é": []}, separators=(",", ":"))
        assert_eq!(encoded_len(&value), 40);
        let text = Value::String("é\u{7f}😀\"\\\n\u{1}".to_owned());
        assert_eq!(encoded_len(&text), 38);
        let big = Value::Number(Number::BigInt("-100000000000000000000000".to_owned()));
        assert_eq!(encoded_len(&big), 25);
        assert_eq!(encoded_len(&Value::Number(Number::Float(-1.5e300))), 9);
        assert_eq!(encoded_len(&Value::Object(vec![])), 2);
        let three = (1..=3)
            .map(|value| Value::Number(Number::Int(value)))
            .collect();
        assert_eq!(encoded_len(&Value::Array(three)), 7);
    }
}
