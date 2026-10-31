//! Every `vectors.json` group of `routine_notice`, `routine_run`, and `routine_proposal`, checked with typed equality,
//! and the edges of each validator the vectors leave open.

mod cards;
mod views;

use strict_json::{Number, Value};

use crate::support::vectors::{group, items};

/// An object of these members, in this order.
pub fn object(members: Vec<(&str, Value)>) -> Value {
    Value::Object(
        members
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    )
}

/// A string value.
pub fn string(text: &str) -> Value {
    Value::String(text.to_owned())
}

/// An integer value.
pub fn int(value: i64) -> Value {
    Value::Number(Number::Int(value))
}

/// An integer beyond 64 bits, by its digits.
pub fn big(digits: &str) -> Value {
    Value::Number(Number::BigInt(digits.to_owned()))
}

/// `value` with member `key` set to `member`, added last when it is new.
pub fn with(value: &Value, key: &str, member: Value) -> Value {
    let Value::Object(members) = value else {
        panic!("not an object: {value:?}");
    };
    let mut members = members.clone();
    match members.iter_mut().find(|(name, _)| name == key) {
        Some(slot) => slot.1 = member,
        None => members.push((key.to_owned(), member)),
    }
    Value::Object(members)
}

/// `value` without member `key`.
pub fn without(value: &Value, key: &str) -> Value {
    let Value::Object(members) = value else {
        panic!("not an object: {value:?}");
    };
    Value::Object(
        members
            .iter()
            .filter(|(name, _)| name != key)
            .cloned()
            .collect(),
    )
}

fn member<'a>(value: &'a Value, key: &str) -> &'a Value {
    value
        .get(key)
        .unwrap_or_else(|| panic!("no member {key} in {value:?}"))
}

/// The first case of the vector group at `path`.
pub fn first(path: &str) -> Value {
    items(group(path))[0].clone()
}

/// Asserts that `check` answers each case's verdict.
pub fn verdicts(cases: &[(Value, bool)], check: impl Fn(&Value) -> bool) {
    for (case, expected) in cases {
        assert_eq!(check(case), *expected, "{case:?}");
    }
}

/// `value` with each of `changes` applied alone, each refused.
pub fn refused(value: &Value, changes: Vec<(&str, Value)>) -> Vec<(Value, bool)> {
    let mut cases = vec![(value.clone(), true)];
    cases.extend(
        changes
            .into_iter()
            .map(|(key, bad)| (with(value, key, bad), false)),
    );
    cases
}
