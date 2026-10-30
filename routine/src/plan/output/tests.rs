//! The projection parts of `test_routine_output.py`: a shown result is Team's bounded, redacted projection of one
//! step's validated result, compared on its exact safe form and cut down a fixed ladder of bounds.

use strict_json::Value;
use team_protocol::routine::{
    MAX_OUTPUT_BYTES, MAX_OUTPUT_KEY_CHARS, MAX_OUTPUT_TEXT_CHARS, canonical_output, encoded_bytes,
};

use super::{
    compared_within, node_fields, output_compared, output_label, output_labels, output_redacted,
    output_safe, output_safe_within, output_shown, output_state, shown_within,
};
use crate::json::{as_str, at, canonical, elements, int, object, text};
use crate::plan::tests::{contracts, document_with};
use crate::plan::{PlanError, admit, well_formed};
use crate::testing::json;

const ZONES: &str = r#"{"zones": [{"id": "z1", "name": "example.com", "paused": false, "status": "active"},
    {"id": "z2", "name": "example.org", "paused": true, "status": "pending"}], "pagination": {"page": 1, "per_page": 50}}"#;

fn shown_text(value: &str, cut: bool) -> Value {
    object(vec![
        ("kind", text("text")),
        ("value", text(value)),
        ("cut", Value::Bool(cut)),
    ])
}

fn shown_number(value: &str) -> Value {
    object(vec![("kind", text("number")), ("value", text(value))])
}

fn safe(result: &Value) -> Value {
    output_safe(result, &json("{}"), &[]).expect("a safe form")
}

fn shown(node: &Value) -> Value {
    output_shown(&int(1), node)
}

fn labels(output: &Value) -> Vec<String> {
    node_fields(at(output, "value"))
        .into_iter()
        .map(|(label, _)| label)
        .collect()
}

#[test]
fn a_plan_names_one_closed_disposition_whose_shown_step_is_its_last() {
    for output in [
        r#"{"mode": "show", "step": "share"}"#,
        r#"{"mode": "changes", "step": "share"}"#,
        r#"{"mode": "none", "step": null}"#,
    ] {
        let document = document_with("output", json(output));
        let plan = admit(&document, &contracts()).expect("a plan");
        assert_eq!(plan.output, json(output));
        assert_eq!(
            plan.shown().map_or(Value::Null, |step| text(&step.step_id)),
            at(&json(output), "step").clone()
        );
        assert!(well_formed(&document));
    }
    for output in [
        "null",
        r#"{"mode": "show"}"#,
        r#"{"mode": "show", "step": null}"#,
        r#"{"mode": "show", "step": "missing"}"#,
        r#"{"mode": "show", "step": "publish"}"#,
        r#"{"mode": "changes", "step": "publish"}"#,
        r#"{"mode": "changes", "step": 1}"#,
        r#"{"mode": "none", "step": "publish"}"#,
        r#"{"mode": "loud", "step": null}"#,
        r#"{"mode": "show", "step": "share", "extra": 1}"#,
    ] {
        let candidate = document_with("output", json(output));
        assert_eq!(
            admit(&candidate, &contracts()).map(|_| ()),
            Err(PlanError::new("plan-output-invalid")),
            "{output}"
        );
        assert!(!well_formed(&candidate));
    }
    let mut missing = document_with("output", Value::Null);
    if let Value::Object(members) = &mut missing {
        members.retain(|(name, _)| name != "output");
    }
    assert_eq!(
        admit(&missing, &contracts()).map(|_| ()),
        Err(PlanError::new("plan-invalid"))
    );
}

#[test]
fn a_result_is_shown_complete_in_sorted_order_when_it_fits() {
    let output = shown(&safe(&json(ZONES)));
    assert_eq!(
        (at(&output, "state"), at(&output, "truncated")),
        (&text("shown"), &Value::Bool(false))
    );
    let fields = node_fields(at(&output, "value"));
    assert_eq!(fields[0].0, "pagination");
    assert_eq!(
        node_fields(&fields[0].1),
        [
            ("page".to_owned(), shown_number("1")),
            ("per_page".to_owned(), shown_number("50"))
        ]
    );
    let first = &elements(at(&fields[1].1, "items"))[0];
    assert_eq!(
        node_fields(first),
        [
            ("id".to_owned(), shown_text("z1", false)),
            ("name".to_owned(), shown_text("example.com", false)),
            (
                "paused".to_owned(),
                object(vec![("kind", text("bool")), ("value", Value::Bool(false))])
            ),
            ("status".to_owned(), shown_text("active", false)),
        ]
    );
    assert_eq!(canonical_output(&output), Some(&output));
    assert_eq!(safe(&Value::Null), object(vec![("kind", text("null"))]));
    assert_eq!(
        safe(&json("1.5")),
        object(vec![("kind", text("number")), ("value", json("1.5"))])
    );
}

const SECRETS: &str = r#"{"sealed": "visible?", "nested": "also?", "items": ["a", "b"], "api_token": "tokenvalue",
    "note": "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789": "x",
    "Authorization: Bearer zyxwvutsrqponmlkjihgfedcba9876543210": "y", "plain": "ok"}"#;
const SECRET_SCHEMA: &str = r##"{"type": "object", "properties": {"sealed": {"type": "string", "writeOnly": true},
    "nested": {"$ref": "#/$defs/hidden"}, "items": {"type": "array", "items": {"type": "string", "format": "password"}}},
    "$defs": {"hidden": {"type": "string", "format": "password"}}}"##;

#[test]
fn secrets_are_redacted_before_any_cut_by_value_key_and_schema() {
    let node = output_safe(&json(SECRETS), &json(SECRET_SCHEMA), &[]).expect("a safe form");
    let fields = node_fields(&node);
    let field = |name: &str| {
        fields
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.clone())
    };
    for key in [
        "sealed",
        "nested",
        "api_token",
        "note",
        "[redacted]",
        "[redacted] 2",
    ] {
        assert_eq!(field(key), Some(output_redacted()), "{key}");
    }
    let items = Value::Array(vec![output_redacted(); 2]);
    assert_eq!(
        field("items"),
        Some(object(vec![
            ("kind", text("list")),
            ("items", items),
            ("omitted", int(0))
        ]))
    );
    assert_eq!(field("plain"), Some(shown_text("ok", false)));
    let encoded = String::from_utf8(canonical(&node)).expect("UTF-8");
    for secret in [
        "visible?",
        "also?",
        "abcdefghijklmnopqrstuvwxyz",
        "tokenvalue",
    ] {
        assert!(!encoded.contains(secret));
    }
}

#[test]
fn a_schema_the_walk_cannot_follow_redacts_that_position_instead_of_refusing_the_result() {
    let unreadable =
        json(r#"{"type": "object", "patternProperties": {"(?<=a)b": {"type": "string"}}}"#);
    let node = output_safe(&json(r#"{"ab": "x"}"#), &unreadable, &[]).expect("a safe form");
    assert_eq!(node_fields(&node), [("ab".to_owned(), output_redacted())]);
}

fn escaping_result() -> Value {
    let long = "k".repeat(100);
    Value::Object(vec![
        ("\u{202e}".to_owned(), text("a\u{0}b")),
        ("\\u202e".to_owned(), int(1)),
        (String::new(), int(2)),
        (long.clone(), int(3)),
        (format!("{long}x"), int(4)),
    ])
}

#[test]
fn the_safe_form_keeps_every_key_and_text_exactly() {
    let result = escaping_result();
    let node = safe(&result);
    let mut keys: Vec<String> = crate::json::members(&result)
        .iter()
        .map(|(key, _)| key.clone())
        .collect();
    keys.sort();
    assert_eq!(
        node_fields(&node)
            .into_iter()
            .map(|(key, _)| key)
            .collect::<Vec<_>>(),
        keys
    );
    assert!(node_fields(&node).contains(&("\u{202e}".to_owned(), shown_text("a\u{0}b", false))));
}

#[test]
fn keys_and_text_are_escaped_and_labels_stay_distinct_only_when_shown() {
    let output = shown(&safe(&escaping_result()));
    let shown_labels = labels(&output);
    let mut unique = shown_labels.clone();
    unique.sort();
    unique.dedup();
    assert_eq!(unique.len(), shown_labels.len());
    for label in ["\"\"", "\\u202e", "\\u202e (2)"] {
        assert!(
            shown_labels.iter().any(|shown_label| shown_label == label),
            "{label}"
        );
    }
    let escaped = ("\\u202e (2)".to_owned(), shown_text("a\\u0000b", false));
    assert!(node_fields(at(&output, "value")).contains(&escaped));
    assert!(
        shown_labels
            .iter()
            .all(|label| label.chars().count() <= MAX_OUTPUT_KEY_CHARS)
    );
    // A shortened key is a cut, so the output says it is truncated.
    assert_eq!(at(&output, "truncated"), &Value::Bool(true));
    assert_eq!(canonical_output(&output), Some(&output));
}

#[test]
fn labels_that_read_alike_are_numbered_apart_and_a_cut_says_so() {
    assert_eq!(
        at(&shown(&safe(&json(r#"{"k": 1}"#))), "truncated"),
        &Value::Bool(false)
    );
    let many = shown(&safe(&json(r#"{"same": 0, "same (2)": 1}"#)));
    assert_eq!(labels(&many), ["same", "same (2)"]);
    // Two keys whose labels read alike at the full label length are numbered apart, which cuts one: truncated.
    let alike = Value::Object(vec![
        (format!("\u{202e}{}", "x".repeat(58)), int(1)),
        (format!("\\u202e{}", "x".repeat(58)), int(2)),
    ]);
    let alike = output_shown(&text("zones"), &safe(&alike));
    let lengths: Vec<usize> = labels(&alike)
        .iter()
        .map(|label| label.chars().count())
        .collect();
    assert_eq!(lengths, [64, 62]);
    assert_eq!(at(&alike, "truncated"), &Value::Bool(true));
    let (numbered, shortened) = output_labels(&["same"; 5]);
    let expected = ["same", "same (2)", "same (3)", "same (4)", "same (5)"].map(str::to_owned);
    assert_eq!((numbered, shortened), (expected.to_vec(), false));
}

#[test]
fn a_change_is_compared_on_the_exact_data_never_its_shown_form() {
    let long = "k".repeat(100);
    let (label, _cut) = output_label(&long);
    let huge = format!("1{}", "0".repeat(400));
    let huger = format!("1{}1", "0".repeat(399));
    for (first, second) in [
        (json(r#"{"v": "\u202e"}"#), json(r#"{"v": "\\u202e"}"#)),
        (
            Value::Object(vec![(long.clone(), int(1))]),
            Value::Object(vec![(label, int(1))]),
        ),
        (json(r#"{"n": 0.1}"#), json(r#"{"n": 0.10000000000000002}"#)),
        (
            json(&format!(r#"{{"n": {huge}}}"#)),
            json(&format!(r#"{{"n": {huger}}}"#)),
        ),
    ] {
        assert_ne!(
            output_compared(&safe(&first)),
            output_compared(&safe(&second))
        );
    }
    // A change only inside redacted content compares equal.
    assert_eq!(
        output_compared(&safe(&json(r#"{"token": "a"}"#))),
        output_compared(&safe(&json(r#"{"token": "b"}"#)))
    );
}

#[test]
fn a_number_is_shown_as_its_exact_text() {
    let huge = format!("1{}", "0".repeat(400));
    let output = shown(&safe(&json(&format!(
        "[0.0001, 1.23456, 1000000000000000000000000000000, {huge}]"
    ))));
    let items = elements(at(at(&output, "value"), "items"));
    assert_eq!(
        &items[..3],
        [
            shown_number("0.0001"),
            shown_number("1.23456"),
            shown_number(&format!("1{}", "0".repeat(30)))
        ]
    );
    // A number too long for a number node is shown as its exact text, cut like any text.
    assert_eq!(items[3], shown_text(&format!("{}…", &huge[..299]), true));
    assert_eq!(at(&output, "truncated"), &Value::Bool(true));
    assert_eq!(canonical_output(&output), Some(&output));
    let sixty_five = shown(&safe(&json(&format!("1{}", "0".repeat(64)))));
    assert_eq!(
        at(&sixty_five, "value"),
        &shown_text(&format!("1{}", "0".repeat(64)), false)
    );
}

#[test]
fn a_large_result_is_cut_down_a_fixed_ladder_until_it_fits() {
    let rows: Vec<Value> = (0..400)
        .map(|index| {
            object(vec![
                ("name", text(&format!("zone-{index}.example"))),
                ("note", text(&"é".repeat(400))),
            ])
        })
        .collect();
    let output = shown(&safe(&object(vec![("zones", Value::Array(rows))])));
    assert_eq!(at(&output, "truncated"), &Value::Bool(true));
    assert!(encoded_bytes(&output) <= MAX_OUTPUT_BYTES);
    assert_eq!(canonical_output(&output), Some(&output));
    let zones = node_fields(at(&output, "value")).remove(0).1;
    let kept = elements(at(&zones, "items"));
    assert_eq!(
        at(&zones, "omitted"),
        &int(400 - i64::try_from(kept.len()).expect("a count"))
    );
    assert_eq!(at(&node_fields(&kept[0])[1].1, "cut"), &Value::Bool(true));
}

#[test]
fn deep_containers_are_elided_and_what_nothing_shrinks_is_elided_whole() {
    let deep = shown(&safe(&json(r#"{"a": {"b": {"c": {"d": {"e": 1}}}}}"#)));
    assert_eq!(
        String::from_utf8(canonical(&deep))
            .expect("UTF-8")
            .matches("\"elided\"")
            .count(),
        1
    );
    assert_eq!(at(&deep, "truncated"), &Value::Bool(true));
    let huge = shown_text(&"é".repeat(20_000), false);
    let cut = shown(&huge);
    assert!(
        as_str(at(at(&cut, "value"), "value"))
            .expect("text")
            .chars()
            .count()
            <= MAX_OUTPUT_TEXT_CHARS
    );
    let fields = (0..24).map(|index| {
        Value::Array(vec![
            text(&format!("{index:02}{}", "é".repeat(60))),
            huge.clone(),
        ])
    });
    let wide = object(vec![
        ("kind", text("fields")),
        ("fields", Value::Array(fields.collect())),
        ("omitted", int(0)),
    ]);
    let elided =
        json(r#"{"step": 1, "state": "shown", "value": {"kind": "elided"}, "truncated": true}"#);
    assert_eq!(shown_within(&int(1), &wide, &[(24, 24, 300)]), elided);
    assert_eq!(at(&safe(&json("[[[[[[1]]]]]]")), "kind"), &text("list"));
    let shallow = output_safe_within(&json("[[1]]"), &json("{}"), &[], 1).expect("a safe form");
    assert_eq!(
        elements(at(&elements(at(&shallow, "items"))[0], "items"))[0],
        json(r#"{"kind": "elided"}"#)
    );
}

#[test]
fn a_result_that_cannot_be_projected_is_unavailable_and_compared_only_when_small() {
    assert_eq!(
        output_state(&int(1), "unavailable"),
        json(r#"{"step": 1, "state": "unavailable", "value": null, "truncated": false}"#)
    );
    let small = safe(&json(ZONES));
    assert_eq!(output_compared(&small), Some(canonical(&small)));
    assert_eq!(compared_within(&small, 10), None);
    let mut chain = json(r#"{"type": "string"}"#);
    for index in 0..70 {
        chain = Value::Object(vec![
            ("allOf".to_owned(), Value::Array(vec![chain])),
            ("title".to_owned(), int(index)),
        ]);
    }
    assert!(output_safe(&text("x"), &chain, &[]).is_err());
}

#[test]
fn the_protocol_admits_exactly_closed_output_nodes() {
    let output = shown(&safe(&json(ZONES)));
    assert_eq!(canonical_output(&output), Some(&output));
    for state in ["unchanged", "unavailable"] {
        let value = output_state(&int(1), state);
        assert_eq!(canonical_output(&value), Some(&value));
    }
}
