//! `test_routine_trace.py`: a recording turn's memory-only trace, its kept values, the turn protection set, and their
//! bounds.

use std::collections::BTreeSet;

use strict_json::Value;

use super::{
    Kept, MAX_KEPT_BYTES, MAX_PROTECTED_BYTES, MAX_PROTECTED_VALUES, Occurrence, Protection,
    exposes, keep, kept_bytes, secret_values,
};
use crate::json::{canonical, object, text};
use crate::testing::json;

const SECRET_SCHEMA: &str = r#"{"type": "object", "properties": {
    "zones": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}}},
    "account": {"type": "object", "properties": {"id": {"type": "string"}, "token": {"type": "string"}}},
    "hidden": {"type": "string", "writeOnly": true}}}"#;

fn credential() -> String {
    format!("sk-{}", "A1b2C3d4".repeat(6))
}

fn pointers(items: &[&str]) -> BTreeSet<String> {
    items.iter().map(|item| (*item).to_owned()).collect()
}

fn protected(items: &[&str]) -> BTreeSet<String> {
    pointers(items)
}

/// A schema of 200 nested `allOf` levels, deeper than any walk follows.
fn unwalkable() -> Value {
    let mut deep = json("{}");
    for _ in 0..200 {
        deep = Value::Object(vec![("allOf".to_owned(), Value::Array(vec![deep]))]);
    }
    deep
}

#[test]
fn secret_positions_credentials_and_protected_values_are_withheld_whole() {
    let value = Value::Object(vec![
        (
            "zones".to_owned(),
            json(r#"[{"id": "z1"}, {"id": "holds injected-value here"}]"#),
        ),
        ("account".to_owned(), json(r#"{"id": "acc", "token": "t"}"#)),
        ("hidden".to_owned(), text("h")),
        ("note".to_owned(), text(&credential())),
        (
            "map".to_owned(),
            Value::Object(vec![(credential(), json("1"))]),
        ),
        ("plain".to_owned(), json("5")),
    ]);
    let kept = keep(
        &value,
        &json(SECRET_SCHEMA),
        &["injected-value".to_owned(), String::new()],
    )
    .expect("kept");
    assert_eq!(
        kept.withheld,
        pointers(&["/zones/1/id", "/account/token", "/hidden", "/note", "/map"])
    );
    assert_eq!(
        kept.value
            .get("zones")
            .map(|zones| crate::json::elements(zones)[0].clone()),
        Some(json(r#"{"id": "z1"}"#))
    );
    assert_eq!(kept.value.get("note"), Some(&Value::Null));
    assert_eq!(kept.value.get("plain"), Some(&json("5")));
    assert!(!kept.oversize);
}

#[test]
fn available_sees_withheld_ancestors_descendants_and_the_root() {
    let kept = Kept {
        value: json(r#"{"a": {"b": null}}"#),
        withheld: pointers(&["/a/b"]),
        oversize: false,
    };
    assert!(kept.available("/a/c"));
    assert!(kept.available("/ab"));
    for pointer in ["/a/b", "/a/b/c", "/a", ""] {
        assert!(!kept.available(pointer), "{pointer}");
    }
    assert!(
        !Kept {
            value: Value::Null,
            withheld: pointers(&[""]),
            oversize: false
        }
        .available("/x")
    );
}

#[test]
fn an_oversize_value_is_withheld_whole_and_an_unwalkable_schema_withholds_its_position() {
    let kept = keep(&text(&"x".repeat(MAX_KEPT_BYTES + 1)), &json("{}"), &[]).expect("kept");
    assert_eq!(
        (kept.value, kept.withheld, kept.oversize),
        (Value::Null, pointers(&[""]), true)
    );
    let kept = keep(&json(r#"{"a": 1}"#), &unwalkable(), &[]).expect("kept");
    assert_eq!(kept.withheld, pointers(&[""]));
    let escaped = keep(
        &Value::Object(vec![(
            "a/b".to_owned(),
            object(vec![("c~d", text(&credential()))]),
        )]),
        &json("{}"),
        &[],
    );
    assert_eq!(escaped.expect("kept").withheld, pointers(&["/a~1b/c~0d"]));
}

#[test]
fn strings_at_secret_positions_are_collected() {
    let value = json(
        r#"{"account": {"token": "tok-1", "id": "acc"}, "hidden": "h-1", "zones": [{"id": "z"}]}"#,
    );
    let mut found = secret_values(&value, &json(SECRET_SCHEMA)).expect("collected");
    found.sort();
    assert_eq!(found, ["h-1", "tok-1"]);
    let nested = json(r#"{"hidden": {"inner": ["deep-1", 3]}}"#);
    assert_eq!(
        secret_values(&nested, &json(SECRET_SCHEMA)),
        Ok(vec!["deep-1".to_owned()])
    );
    assert_eq!(
        secret_values(&json(r#"{"a": "b"}"#), &Value::Null),
        Ok(Vec::new())
    );
    // An unwalkable schema yields every string.
    assert_eq!(
        secret_values(&json(r#"{"a": "b"}"#), &unwalkable()),
        Ok(vec!["b".to_owned()])
    );
}

#[test]
fn any_string_or_key_holding_a_protected_value_exposes_it() {
    let token = protected(&["tok-1"]);
    assert!(exposes(&text("x tok-1 y"), &token));
    assert!(exposes(&json(r#"{"a": ["tok-1"]}"#), &token));
    assert!(exposes(&json(r#"{"tok-1": 1}"#), &token));
    assert!(!exposes(&json(r#"{"a": [1, null, true, "tok"]}"#), &token));
    assert!(!exposes(&text("tok-1"), &BTreeSet::new()));
}

#[test]
fn protection_grows_and_saturation_is_irreversible() {
    let grown = Protection::default().grow(["a", "", "b", "a"]);
    assert_eq!((grown.values, grown.lost), (protected(&["a", "b"]), false));
    let names: Vec<String> = (0..MAX_PROTECTED_VALUES)
        .map(|index| format!("v{index}"))
        .collect();
    let full = Protection::default().grow(names.iter().map(String::as_str));
    assert!(!full.lost);
    let lost = full.grow(["one-more"]);
    assert!(lost.lost);
    assert!(lost.grow([]).lost);
    assert!(
        Protection::default()
            .grow([&*"x".repeat(MAX_PROTECTED_BYTES + 1)])
            .lost
    );
    assert_eq!(full.grow(["v1"]), full);
}

#[test]
fn an_occurrence_counts_its_kept_values_and_every_pointer_they_withhold() {
    let empty = keep(&json("{}"), &json("{}"), &[]).expect("kept");
    let zones = keep(&json(r#"{"zones": []}"#), &json(SECRET_SCHEMA), &[]).expect("kept");
    let first = Occurrence::new("cloudflare", "list-zones", PIN, true, empty.clone(), zones);
    assert_eq!(
        first.size,
        kept_bytes(&first.input) + kept_bytes(&first.result)
    );
    // A value of many withheld members carries a pointer for each, which a span's byte bound counts.
    let members = Value::Object(
        (0..9000)
            .map(|index| (format!("token_{index:05}"), text("x")))
            .collect(),
    );
    let kept = keep(&members, &json("{}"), &[]).expect("kept");
    assert!(kept_bytes(&kept) > canonical(&kept.value).len() + 9000 * 12);
    let large = Occurrence::new("cloudflare", "list-zones", PIN, true, empty, kept.clone());
    assert!(large.size > kept_bytes(&kept));
}

const PIN: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
