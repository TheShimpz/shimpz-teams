//! `vectors.json` of the protocol, read once, and the typed equality the vectors are checked with.

use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use strict_json::{Number, Value};

use super::registry::{Outcome, find};

/// The protocol's vector directory, `../v1` beside this crate; a copy of the crate (such as a mutation scratch tree)
/// holds it at the same relative path.
#[must_use]
pub fn v1() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../v1")
}

static VECTORS: LazyLock<Value> = LazyLock::new(|| {
    let bytes = std::fs::read(v1().join("vectors.json")).expect("a readable vectors.json");
    strict_json::parse(&bytes).expect("vectors.json is strict JSON")
});

/// The whole vector document.
#[must_use]
pub fn vectors() -> &'static Value {
    &VECTORS
}

/// The member `path` (dot-separated) of the vector document.
///
/// # Panics
///
/// Panics when the path is missing.
#[must_use]
pub fn group(path: &str) -> &'static Value {
    path.split('.').fold(vectors(), |value, key| {
        value
            .get(key)
            .unwrap_or_else(|| panic!("vectors.json has no {path}"))
    })
}

/// The items of an array.
///
/// # Panics
///
/// Panics for any other value.
#[must_use]
pub fn items(value: &Value) -> &[Value] {
    match value {
        Value::Array(items) => items,
        other => panic!("not an array: {other:?}"),
    }
}

/// The text of a string.
///
/// # Panics
///
/// Panics for any other value.
#[must_use]
pub fn text(value: &Value) -> &str {
    match value {
        Value::String(text) => text,
        other => panic!("not a string: {other:?}"),
    }
}

/// Equal values of the same JSON type; object members in any order.
#[must_use]
pub fn same(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len()
                && a.iter()
                    .all(|(key, x)| right.get(key).is_some_and(|y| same(x, y)))
                && b.iter().all(|(key, _)| left.get(key).is_some())
        }
        (Value::Array(a), Value::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(x, y)| same(x, y))
        }
        (Value::Number(Number::Float(a)), Value::Number(Number::Float(b))) => {
            a.to_bits() == b.to_bits()
        }
        _ => left == right,
    }
}

/// Calls the registered `name` on `value`.
///
/// # Panics
///
/// Panics when no entry has that name.
#[must_use]
pub fn call(name: &str, value: &Value) -> Outcome {
    find(name).unwrap_or_else(|| panic!("no registry entry {name}"))(value)
}

/// Asserts that `name` returns each case of `valid` exactly (typed) and refuses each of `invalid`.
///
/// # Panics
///
/// Panics on the first case that differs.
pub fn admits(name: &str, valid: &Value, invalid: &Value) {
    assert!(
        !items(valid).is_empty() && !items(invalid).is_empty(),
        "{name} has both outcomes"
    );
    for case in items(valid) {
        match call(name, case) {
            Outcome::Value(value) => {
                assert!(same(&value, case), "{name} changed {case:?} into {value:?}");
            }
            other => panic!("{name} refused {case:?}: {other:?}"),
        }
    }
    for case in items(invalid) {
        assert!(
            !matches!(call(name, case), Outcome::Value(_)),
            "{name} admitted {case:?}"
        );
    }
}

/// [`admits`] over the `valid` and `invalid` members of the group at `path`.
pub fn admits_group(name: &str, path: &str) {
    let cases = group(path);
    admits(
        name,
        group_member(cases, "valid"),
        group_member(cases, "invalid"),
    );
}

fn group_member<'a>(cases: &'a Value, key: &str) -> &'a Value {
    cases
        .get(key)
        .unwrap_or_else(|| panic!("the group has no {key}"))
}
