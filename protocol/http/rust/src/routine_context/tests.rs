use super::{
    canonical_edits, canonical_refine, canonical_rerun, canonical_routine_listing,
    canonical_routine_listings, canonical_selected_routine, dates_written, json_literal,
    may_write_date, near, writes_date,
};
use crate::python::json::{CANONICAL, dumps};
use crate::python::regex::{MatchError, Pattern};
use crate::python::value::{int, object_of, text};
use strict_json::Value;

const TODAY: (i32, i32, i32) = (2026, 10, 16);

fn parse(json: &str) -> Value {
    strict_json::parse(json.as_bytes()).expect("strict JSON")
}

/// An object with one member replaced.
fn replaced(value: &Value, key: &str, wrong: &Value) -> Value {
    let Value::Object(members) = value else {
        unreachable!()
    };
    Value::Object(
        members
            .iter()
            .map(|(name, item)| {
                let item = if name == key { wrong } else { item };
                (name.clone(), item.clone())
            })
            .collect(),
    )
}

#[test]
fn dates_read_in_either_numeric_order_near_today() {
    let found = [
        "2026-10-15",
        "2026-12-15",
        "16.10.2026",
        "10/16/2026",
        "16/10",
        "10月15日",
        "2026年10月15日",
        "\u{9e8}\u{9e6}/10",
        "16/10.2026",
        "no date",
    ]
    .map(|text| may_write_date(text, TODAY));
    let expected = [
        true, false, true, true, true, true, true, false, false, false,
    ]
    .map(Ok);
    assert_eq!(found, expected);
}

#[test]
fn a_missing_year_is_read_as_the_nearest_existing_one() {
    let found = [
        may_write_date("31/12", (2027, 1, 1)),
        may_write_date("1/1", (9999, 12, 31)),
        may_write_date("31/12", (1, 1, 1)),
    ];
    assert_eq!(found, [Ok(true), Ok(false), Ok(false)]);
}

#[test]
fn a_written_date_must_exist() {
    let found = [
        near(Some("2026"), "02", "29", (2026, 2, 28)),
        near(Some("2028"), "02", "29", (2028, 2, 28)),
        near(Some("0000"), "01", "01", (1, 1, 1)),
        near(None, "13", "01", TODAY),
        near(None, "10", "00", TODAY),
        near(Some(""), "10", "17", TODAY),
    ];
    assert_eq!(found, [false, true, false, false, false, true]);
    assert_eq!(
        dates_written("numeric", &[Some("1"), None, Some("2"), Some("2026")]),
        [(Some("2026"), "2", "1"), (Some("2026"), "1", "2")]
    );
    assert_eq!(
        dates_written("short", &[Some("1"), Some("2")]),
        [(None, "2", "1"), (None, "1", "2")]
    );
}

#[test]
fn an_exhausted_date_search_refuses() {
    let forms = [("short", Pattern::new(r"(?:(?=a)a|a)*b").expect("pattern"))];
    let text = format!("{}c", "a".repeat(40));
    assert_eq!(writes_date(&forms, &text, TODAY), Err(MatchError::Engine));
}

#[test]
fn literals_are_exactly_python_default_json() {
    let valid = [
        "1.5",
        "-0.0",
        "1e+16",
        "[1, {\"a\": null}]",
        "{}",
        "\"\\u0001\"",
        "true",
    ];
    assert!(valid.iter().all(|literal| json_literal(&text(literal))));
    let invalid = [
        "1e16",
        "[1,2]",
        "\"\\u00e9\"",
        "{\"a\": 1, \"a\": 1}",
        "NaN",
        "",
        " 1",
    ];
    assert!(!invalid.iter().any(|literal| json_literal(&text(literal))));
    let bounds = [
        json_literal(&text(&format!("\"{}\"", "a".repeat(1022)))),
        json_literal(&text(&format!("\"{}\"", "a".repeat(1023)))),
        json_literal(&int(1)),
    ];
    assert_eq!(bounds, [true, false, false]);
}

fn rerun_input(kind: &str, value: Value, chosen: Value, source: Value) -> Value {
    object_of(vec![
        ("member", text("page")),
        ("kind", text(kind)),
        ("value", value),
        ("chosen", chosen),
        ("source", source),
    ])
}

fn entry(inputs: Vec<Value>, count: i64) -> Value {
    object_of(vec![
        ("assistant", text("shimpz-cloudflare")),
        ("action", text("list-dns-records")),
        ("count", int(count)),
        ("inputs", Value::Array(inputs)),
    ])
}

fn rerun(inputs: Vec<Value>, count: i64) -> Value {
    Value::Array(vec![entry(inputs, count)])
}

fn source(assistant: &str, action: &str) -> Value {
    object_of(vec![
        ("assistant", text(assistant)),
        ("action", text(action)),
    ])
}

#[test]
fn rerun_inputs_are_values_clocks_or_fresh_results() {
    let (yes, no, null) = (Value::Bool(true), Value::Bool(false), Value::Null);
    let good = source("shimpz-cloudflare", "list-zones");
    let valid = [
        rerun_input("value", text("1"), yes.clone(), null.clone()),
        rerun_input("value", null.clone(), no.clone(), null.clone()),
        rerun_input("fresh", null.clone(), no.clone(), good),
        rerun_input("fresh", null.clone(), no.clone(), null.clone()),
        rerun_input("clock", null.clone(), no, null),
    ];
    for input in valid {
        let value = rerun(vec![input], 1);
        assert_eq!(canonical_rerun(&value), Some(&value));
    }
}

#[test]
fn every_rerun_input_member_is_checked() {
    let (yes, no, null) = (Value::Bool(true), Value::Bool(false), Value::Null);
    let good = source("shimpz-cloudflare", "list-zones");
    let invalid = [
        rerun_input("value", text("1"), yes.clone(), good),
        rerun_input("value", text("01"), no.clone(), null.clone()),
        rerun_input("value", null.clone(), int(1), null.clone()),
        rerun_input("fresh", null.clone(), yes.clone(), null.clone()),
        rerun_input("fresh", null.clone(), no.clone(), text("x")),
        rerun_input(
            "fresh",
            null.clone(),
            no.clone(),
            source("shimpz-cloudflare", "Bad"),
        ),
        rerun_input(
            "fresh",
            null.clone(),
            no.clone(),
            source("Bad", "list-zones"),
        ),
        rerun_input("fresh", text("1"), no.clone(), null.clone()),
        rerun_input("clock", text("1"), no.clone(), null.clone()),
        rerun_input("clock", null.clone(), yes, null.clone()),
        rerun_input("guess", null.clone(), no, null),
        int(1),
    ];
    for input in invalid {
        assert_eq!(canonical_rerun(&rerun(vec![input], 1)), None);
    }
}

#[test]
fn every_rerun_entry_member_is_checked() {
    let clock = rerun_input("clock", Value::Null, Value::Bool(false), Value::Null);
    let wide = vec![clock.clone(); 65];
    let invalid = [
        rerun(vec![clock.clone(), clock], 1),
        rerun(wide, 1),
        rerun(vec![], 257),
        Value::Array(vec![entry(vec![], 200), entry(vec![], 57)]),
        Value::Array(vec![replaced(&entry(vec![], 1), "assistant", &text("Bad"))]),
        Value::Array(vec![replaced(&entry(vec![], 1), "action", &text("Bad"))]),
        Value::Array(vec![int(1)]),
        Value::Array(vec![]),
        int(1),
    ];
    for value in invalid {
        assert_eq!(canonical_rerun(&value), None, "{value:?}");
    }
}

fn step_object(assistant: &str, action: &str, inputs: Vec<Value>) -> Value {
    object_of(vec![
        ("id", text("s1")),
        ("assistant", text(assistant)),
        ("action", text(action)),
        ("inputs", Value::Array(inputs)),
    ])
}

fn listed(id: &str, steps: &Value) -> Value {
    parse(&format!(
        r#"{{"routine_id": "{id}", "name": "DNS", "schedule": {{"kind": "daily", "time": "08:00"}},
            "timezone": "UTC", "timezone_source": "none", "revision": 1, "daily_steps": 1,
            "output": {{"mode": "show"}}, "steps": {}}}"#,
        dumps(steps, CANONICAL)
    ))
}

fn one_step() -> Value {
    Value::Array(vec![step_object(
        "shimpz-cloudflare",
        "list-zones",
        vec![text("a"), text("b")],
    )])
}

#[test]
fn listings_are_admitted_whole_and_once() {
    let one = listed(&"a".repeat(32), &one_step());
    assert_eq!(canonical_routine_listing(&one), Some(&one));
    let both = Value::Array(vec![one.clone(), listed(&"b".repeat(32), &one_step())]);
    assert_eq!(canonical_routine_listings(&both), Some(&both));
    let invalid = [
        Value::Array(vec![one.clone(), one.clone()]),
        Value::Array(vec![int(1)]),
        Value::Array(vec![one; 9]),
    ];
    for value in invalid {
        assert_eq!(canonical_routine_listings(&value), None);
    }
}

#[test]
fn every_listing_member_is_checked() {
    let good = listed(&"a".repeat(32), &one_step());
    let invalid = [
        ("routine_id", text("x")),
        ("name", text(" x")),
        ("schedule", object_of(vec![("kind", text("daily"))])),
        ("timezone_source", text("other")),
        ("revision", int(0)),
        ("daily_steps", int(0)),
        ("output", object_of(vec![("mode", text("other"))])),
        ("steps", int(1)),
    ];
    for (key, wrong) in invalid {
        assert_eq!(
            canonical_routine_listing(&replaced(&good, key, &wrong)),
            None,
            "{key}"
        );
    }
    assert_eq!(canonical_routine_listing(&int(1)), None);
}

#[test]
fn every_listed_step_is_checked() {
    let good = listed(&"a".repeat(32), &one_step());
    let step =
        |assistant, action, inputs| Value::Array(vec![step_object(assistant, action, inputs)]);
    let wide: Vec<Value> = (0..65).map(|name| text(&format!("{name:03}"))).collect();
    let invalid = [
        Value::Array(vec![
            step_object("shimpz-cloudflare", "list-zones", vec![]);
            257
        ]),
        Value::Array(vec![]),
        step("Bad", "list-zones", vec![]),
        step("shimpz-cloudflare", "Bad", vec![]),
        step("shimpz-cloudflare", "list-zones", wide),
        step("shimpz-cloudflare", "list-zones", vec![text("\u{85}")]),
        step(
            "shimpz-cloudflare",
            "list-zones",
            vec![text("b"), text("a")],
        ),
        parse(r#"[{"id": "S1", "assistant": "shimpz-cloudflare", "action": "a", "inputs": []}]"#),
        parse(r#"[{"id": "s1", "assistant": "shimpz-cloudflare", "action": "a", "inputs": {}}]"#),
        parse(r#"[{"id": "s1", "assistant": "shimpz-cloudflare", "action": "a"}]"#),
    ];
    for steps in invalid {
        assert_eq!(
            canonical_routine_listing(&replaced(&good, "steps", &steps)),
            None,
            "{steps:?}"
        );
    }
}

#[test]
fn a_listing_holds_its_steps_byte_bound() {
    let names: Vec<Value> = (0..64)
        .map(|name| text(&format!("{name:03}{}", "a".repeat(125))))
        .collect();
    let heavy: Vec<Value> = (0..40)
        .map(|number| {
            replaced(
                &step_object("shimpz-cloudflare", "list-zones", names.clone()),
                "id",
                &text(&format!("s{number}")),
            )
        })
        .collect();
    let large = listed(&"a".repeat(32), &Value::Array(heavy));
    assert_eq!(canonical_routine_listing(&large), Some(&large));
    assert_eq!(canonical_routine_listings(&Value::Array(vec![large])), None);
}

const STEP: &str = r#"{"id": "s1", "position": 1, "assistant": "shimpz-cloudflare", "action": "list-zones",
    "read_only": true, "inputs": [], "stored_inputs": []}"#;
const SHOWN: &str = r#"{"mode": "show", "step": "s1"}"#;
const DONE: &str = r#"{"revision": 1, "steps": [{"position": 1, "status": "done"}]}"#;

fn selected(steps: &str, output: &str, last_run: &str) -> Value {
    parse(&format!(
        r#"{{"routine_id": "{}", "name": "DNS", "description": "Lists records.", "revision": 2,
            "output": {output}, "steps": {steps}, "last_run": {last_run}}}"#,
        "1".repeat(32)
    ))
}

#[test]
fn the_selected_routine_names_its_steps_output_and_last_run() {
    let steps = format!("[{STEP}]");
    for (output, last_run) in [(SHOWN, "null"), (r#"{"mode": "none", "step": null}"#, DONE)] {
        let value = selected(&steps, output, last_run);
        assert_eq!(
            canonical_selected_routine(&value),
            Some(&value),
            "{output} {last_run}"
        );
    }
    let value = selected(&steps, SHOWN, "null");
    let wrong = [
        ("routine_id", text("x")),
        ("name", text(" x")),
        ("description", text("")),
        ("revision", int(0)),
    ];
    for (key, wrong) in wrong {
        assert_eq!(
            canonical_selected_routine(&replaced(&value, key, &wrong)),
            None,
            "{key}"
        );
    }
    assert_eq!(canonical_selected_routine(&int(1)), None);
}

#[test]
fn every_selected_output_and_last_run_is_checked() {
    let steps = format!("[{STEP}]");
    let outputs = [
        r#"{"mode": "show", "step": null}"#,
        r#"{"mode": "none", "step": "s1"}"#,
        r#"{"mode": "other", "step": null}"#,
        "[]",
    ];
    for output in outputs {
        assert_eq!(
            canonical_selected_routine(&selected(&steps, output, "null")),
            None,
            "{output}"
        );
    }
    let last_runs = [
        "[]",
        r#"{"revision": 0, "steps": [{"position": 1, "status": "done"}]}"#,
        r#"{"revision": 1, "steps": []}"#,
        r#"{"revision": 1, "steps": [{"position": 2, "status": "done"}]}"#,
        r#"{"revision": 1, "steps": [{"position": 1.0, "status": "done"}]}"#,
        r#"{"revision": 1, "steps": [{"position": 1, "status": "late"}]}"#,
        r#"{"revision": 1, "steps": [1]}"#,
    ];
    for last_run in last_runs {
        assert_eq!(
            canonical_selected_routine(&selected(&steps, SHOWN, last_run)),
            None,
            "{last_run}"
        );
    }
}

#[test]
fn every_selected_step_is_checked() {
    let second = STEP.replace("\"position\": 1", "\"position\": 2");
    let renamed = STEP.replace("\"s1\"", "\"S1\"");
    let steps = [
        "[]".to_owned(),
        format!("[{STEP}, {second}]"),
        "[1]".to_owned(),
        "{}".to_owned(),
        format!("[{second}]"),
        format!("[{renamed}]"),
    ];
    for steps in steps {
        assert_eq!(
            canonical_selected_routine(&selected(&steps, SHOWN, "null")),
            None,
            "{steps}"
        );
    }
}

const EDITS: &str = r#"[{"kind": "insert", "step": "s2", "calls": ["c2", "c3"]},
    {"kind": "replace", "step": "s1", "calls": ["c1"]}, {"kind": "remove", "step": "s3", "calls": []}]"#;

#[test]
fn refinements_name_each_step_and_call_once() {
    let valid = parse(EDITS);
    assert_eq!(canonical_edits(&valid), Some(&valid));
    let invalid = [
        r#"[{"kind": "insert", "step": "s2", "calls": []}]"#,
        r#"[{"kind": "replace", "step": "s2", "calls": ["c1", "c2"]}]"#,
        r#"[{"kind": "remove", "step": "s2", "calls": ["c1"]}]"#,
        r#"[{"kind": "move", "step": "s2", "calls": []}]"#,
        r#"[{"kind": "remove", "step": "s2", "calls": {}}]"#,
        r#"[{"kind": "insert", "step": "s2", "calls": ["c0"]}]"#,
        r#"[{"kind": "remove", "step": "S2", "calls": []}]"#,
        r#"[{"kind": "remove", "step": "s2", "calls": []}, {"kind": "remove", "step": "s2", "calls": []}]"#,
        r#"[{"kind": "insert", "step": "s1", "calls": ["c1"]}, {"kind": "insert", "step": "s2", "calls": ["c1"]}]"#,
        r#"[{"kind": "remove", "step": "s2"}]"#,
        "{}",
    ];
    for edits in invalid {
        assert_eq!(canonical_edits(&parse(edits)), None, "{edits}");
    }
    let remove = parse(r#"{"kind": "remove", "step": "s1", "calls": []}"#);
    assert_eq!(canonical_edits(&Value::Array(vec![remove; 257])), None);
}

#[test]
fn every_refinement_member_is_checked() {
    let refine = object_of(vec![
        ("routine_id", text(&"1".repeat(32))),
        ("base_revision", int(2)),
        ("description", text("Lists records.")),
        ("edits", parse(EDITS)),
    ]);
    assert_eq!(canonical_refine(&refine), Some(&refine));
    let wrong = [
        ("routine_id", text("x")),
        ("base_revision", int(0)),
        ("description", text("")),
        ("edits", int(1)),
    ];
    for (key, wrong) in wrong {
        assert_eq!(
            canonical_refine(&replaced(&refine, key, &wrong)),
            None,
            "{key}"
        );
    }
    assert_eq!(canonical_refine(&int(1)), None);
}
