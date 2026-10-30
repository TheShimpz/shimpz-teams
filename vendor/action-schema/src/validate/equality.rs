//! JSON Schema equality, as `jsonschema`'s `equal` decides it: numbers compare by value (`1 == 1.0`), a boolean
//! equals only a boolean, arrays compare in order, and objects compare by their members whatever the order.

use std::collections::HashSet;

use num_bigint::BigInt;
use strict_json::{Number, Value};

use crate::numbers;

/// Whether two values are equal under JSON Schema equality.
pub(super) fn equal(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Null, Value::Null) => true,
        (Value::Bool(left), Value::Bool(right)) => left == right,
        (Value::Number(left), Value::Number(right)) => numbers::equal(left, right),
        (Value::String(left), Value::String(right)) => left == right,
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right)
                    .all(|(left, right)| equal(left, right))
        }
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left.iter().all(|(name, value)| {
                    right
                        .iter()
                        .any(|(other, candidate)| other == name && equal(value, candidate))
                })
        }
        _ => false,
    }
}

/// A value's class under JSON Schema equality: two values are equal exactly when their keys are equal.
#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
enum Key {
    Null,
    Bool(bool),
    /// An integer, or a float without a fraction, by its exact value.
    Integer(BigInt),
    /// A float with a fraction, by its bits; it equals no integer and only floats of the same value.
    Fraction(u64),
    String(String),
    Array(Vec<Key>),
    /// Members sorted by name, since member order does not matter.
    Object(Vec<(String, Key)>),
}

fn key(value: &Value) -> Key {
    match value {
        Value::Null => Key::Null,
        Value::Bool(flag) => Key::Bool(*flag),
        Value::Number(number) => number_key(number),
        Value::String(text) => Key::String(text.clone()),
        Value::Array(items) => Key::Array(items.iter().map(key).collect()),
        Value::Object(members) => {
            let mut keyed: Vec<(String, Key)> = members
                .iter()
                .map(|(name, child)| (name.clone(), key(child)))
                .collect();
            keyed.sort();
            Key::Object(keyed)
        }
    }
}

fn number_key(number: &Number) -> Key {
    match number {
        Number::Int(value) => Key::Integer(BigInt::from(*value)),
        Number::BigInt(digits) => Key::Integer(digits.parse().unwrap_or_default()),
        Number::Float(float) if float.fract() == 0.0 => {
            Key::Integer(num_traits::FromPrimitive::from_f64(*float).unwrap_or_default())
        }
        Number::Float(float) => Key::Fraction(float.to_bits()),
    }
}

/// Whether no two items are equal under JSON Schema equality.
pub(super) fn unique(items: &[Value]) -> bool {
    let mut seen = HashSet::with_capacity(items.len());
    items.iter().all(|item| seen.insert(key(item)))
}

#[cfg(test)]
mod tests {
    use strict_json::{Number, Value};

    use super::{equal, unique};

    fn int(value: i64) -> Value {
        Value::Number(Number::Int(value))
    }

    fn float(value: f64) -> Value {
        Value::Number(Number::Float(value))
    }

    #[test]
    fn numbers_compare_by_value_and_booleans_only_with_booleans() {
        assert!(equal(&int(1), &float(1.0)));
        assert!(equal(&float(-0.0), &int(0)));
        assert!(!equal(&Value::Bool(true), &int(1)));
        assert!(equal(&Value::Bool(false), &Value::Bool(false)));
        assert!(equal(&Value::Null, &Value::Null));
        assert!(!equal(&Value::Null, &Value::Bool(false)));
        assert!(equal(
            &Value::Number(Number::BigInt("18446744073709551616".to_owned())),
            &float(18_446_744_073_709_551_616.0)
        ));
        assert!(!equal(&float(0.5), &int(0)));
    }

    #[test]
    fn containers_compare_members_whatever_their_order() {
        let first = Value::Object(vec![
            ("a".to_owned(), int(1)),
            ("b".to_owned(), Value::Array(vec![float(2.0)])),
        ]);
        let second = Value::Object(vec![
            ("b".to_owned(), Value::Array(vec![int(2)])),
            ("a".to_owned(), float(1.0)),
        ]);
        assert!(equal(&first, &second));
        let fewer = Value::Object(vec![("a".to_owned(), int(1))]);
        assert!(!equal(&first, &fewer));
        let renamed = Value::Object(vec![
            ("a".to_owned(), int(1)),
            ("c".to_owned(), Value::Array(vec![int(2)])),
        ]);
        assert!(!equal(&first, &renamed));
        assert!(!equal(&Value::Array(vec![int(1)]), &Value::Array(vec![])));
        assert!(!equal(
            &Value::String("a".to_owned()),
            &Value::String("b".to_owned())
        ));
    }

    #[test]
    fn uniqueness_uses_the_same_equality() {
        assert!(unique(&[
            int(1),
            Value::Bool(true),
            float(1.5),
            Value::String("1".to_owned())
        ]));
        assert!(!unique(&[int(1), float(1.0)]));
        assert!(!unique(&[
            Value::Array(vec![int(1)]),
            Value::Array(vec![Value::Bool(true)]),
            Value::Array(vec![float(1.0)]),
        ]));
        assert!(!unique(&[float(0.5), float(0.5)]));
        assert!(!unique(&[Value::Null, Value::Null]));
        assert!(!unique(&[
            Value::Number(Number::BigInt("-18446744073709551617".to_owned())),
            Value::Number(Number::BigInt("-18446744073709551617".to_owned())),
        ]));
        let object = |first: &str, second: &str| {
            Value::Object(vec![
                (first.to_owned(), int(1)),
                (second.to_owned(), int(2)),
            ])
        };
        assert!(unique(&[object("a", "b"), object("b", "a")]));
        assert!(unique(&[]));
    }
}
