//! Validity against the Draft 2020-12 meta-schema, checked without retrieving anything.

use serde_json::{Map, Number as JsonNumber};
use strict_json::{Number, Value};

/// Whether a schema is valid against the Draft 2020-12 meta-schema.
pub(crate) fn valid(schema: &Value) -> bool {
    jsonschema::draft202012::meta::is_valid(&json(schema))
}

/// The same value for the meta-schema check. An integer beyond 64 bits keeps its sign and stays integral, which is all
/// the meta-schema reads of a number: it becomes the nearest float, or the largest finite one in its direction.
fn json(value: &Value) -> serde_json::Value {
    match value {
        Value::Null => serde_json::Value::Null,
        Value::Bool(flag) => serde_json::Value::Bool(*flag),
        Value::Number(number) => serde_json::Value::Number(number_json(number)),
        Value::String(text) => serde_json::Value::String(text.clone()),
        Value::Array(items) => serde_json::Value::Array(items.iter().map(json).collect()),
        Value::Object(members) => serde_json::Value::Object(
            members
                .iter()
                .map(|(key, child)| (key.clone(), json(child)))
                .collect::<Map<String, serde_json::Value>>(),
        ),
    }
}

fn number_json(number: &Number) -> JsonNumber {
    match number {
        Number::Int(integer) => JsonNumber::from(*integer),
        Number::BigInt(digits) => digits.parse::<u64>().map_or_else(
            |_| finite(digits.parse::<f64>().unwrap_or(f64::MAX)),
            JsonNumber::from,
        ),
        Number::Float(float) => finite(*float),
    }
}

/// A float the meta-schema check can hold: an overflowed integer becomes the largest finite float of its sign.
fn finite(float: f64) -> JsonNumber {
    JsonNumber::from_f64(float.clamp(f64::MIN, f64::MAX)).unwrap_or(JsonNumber::from(0))
}

#[cfg(test)]
mod tests {
    use strict_json::{Number, Value};

    use super::{json, valid};

    fn number(number: Number) -> Value {
        Value::Number(number)
    }

    #[test]
    fn big_integers_keep_their_sign_and_stay_integral() {
        let huge = format!("1{}", "0".repeat(400));
        let cases = [
            (
                Number::BigInt("18446744073709551615".to_owned()),
                "18446744073709551615",
            ),
            (
                Number::BigInt("-9223372036854775809".to_owned()),
                "-9.223372036854776e+18",
            ),
            (Number::BigInt(huge.clone()), "1.7976931348623157e+308"),
            (
                Number::BigInt(format!("-{huge}")),
                "-1.7976931348623157e+308",
            ),
            (Number::Float(0.5), "0.5"),
            (Number::Int(-3), "-3"),
        ];
        for (value, expected) in cases {
            assert_eq!(json(&number(value)).to_string(), expected);
        }
        let schema =
            |bound: Number| Value::Object(vec![("minLength".to_owned(), Value::Number(bound))]);
        assert!(valid(&schema(Number::BigInt(huge.clone()))));
        assert!(!valid(&schema(Number::BigInt(format!("-{huge}")))));
        assert!(valid(&schema(Number::Float(1.0))));
        assert!(!valid(&schema(Number::Float(1.5))));
    }
}
