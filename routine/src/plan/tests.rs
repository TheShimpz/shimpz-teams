//! `test_routine_plan.py` and `test_routine_select.py`: admission, same-run references and selectors, the run date,
//! secret literals, bounds, and what a step record shows of an attempt's inputs.

use std::collections::BTreeMap;

use strict_json::Value;

use super::walk::{Sub, applicable, could_hold_secret, member_schemas};
use super::{
    ActionContract, Contracts, Held, Key, MAX_RESOLVED_INPUT_BYTES, MAX_RETAINED_BYTES,
    MAX_SAFE_OUTPUT_DEPTH, MAX_SECRET_DEPTH, MAX_STEPS, Plan, PlanError, VERSION, admit,
    commitment, holds_credential, input_preview, output_redacted, output_safe, resolve,
    retained_within, same, secret_literal, secret_literal_at, select, select_where, selections,
    well_formed,
};
use crate::dates::clock_value;
use crate::json::{as_str, at, elements, int, set, text};
use crate::schedule::zone_named;
use crate::testing::{epoch, json};

pub(crate) const PIN: &str =
    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
pub(crate) const OTHER_PIN: &str =
    "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const PUBLISH: &str = r#"{
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": 80},
        "day": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
        "count": {"type": "integer"},
        "note": {"type": ["string", "null"]}
    },
    "required": ["title"],
    "additionalProperties": false
}"#;
const SHARE: &str = r##"{
    "type": "object",
    "properties": {
        "post_id": {"type": "string"},
        "tags": {"type": "array", "items": {"$ref": "#/$defs/tag"}},
        "at": {"type": "integer"},
        "api_token": {"type": "string"},
        "secret_note": {"type": "string", "writeOnly": true},
        "code": {"type": "string", "format": "password"}
    },
    "required": ["post_id"],
    "additionalProperties": false,
    "$defs": {"tag": {"type": "string", "minLength": 1}}
}"##;
const DATED: &str = r#"{"days": 0, "format": "{dd}/{MM}/{yyyy}", "lang": null}"#;

/// One contract.
pub(crate) fn contract(pin: &str, schema: &str) -> ActionContract {
    ActionContract::new(pin.to_owned(), json(schema))
}

/// The contracts of the blog Assistant's two Actions.
pub(crate) fn contracts() -> Contracts {
    Contracts::from([
        (
            ("shimpz-blog".to_owned(), "publish-post".to_owned()),
            contract(PIN, PUBLISH),
        ),
        (
            ("shimpz-blog".to_owned(), "share-post".to_owned()),
            contract(OTHER_PIN, SHARE),
        ),
    ])
}

/// The two-step blog plan: publish a post, then share it.
pub(crate) fn document() -> Value {
    json(&format!(
        r#"{{
        "version": 4,
        "timezone": "America/Sao_Paulo",
        "steps": [
            {{"id": "publish", "assistant": "shimpz-blog", "action": "publish-post", "pin": "{PIN}",
              "input": {{"title": {{"kind": "literal", "value": "Weekly report"}},
                         "day": {{"kind": "run_clock", "format": "date"}}}}}},
            {{"id": "share", "assistant": "shimpz-blog", "action": "share-post", "pin": "{OTHER_PIN}",
              "input": {{"post_id": {{"kind": "step_output", "step": "publish", "pointer": "/id"}},
                         "tags": {{"kind": "step_output", "step": "publish", "pointer": "/meta/a~1b/0"}}}}}}
        ],
        "output": {{"mode": "show", "step": "share"}}
    }}"#
    ))
}

/// The blog plan with one top-level member replaced.
pub(crate) fn document_with(key: &str, value: Value) -> Value {
    let mut changed = document();
    set(&mut changed, key, value);
    changed
}

fn step_mut(document: &mut Value, index: usize) -> &mut Value {
    match document {
        Value::Object(found) => match &mut found
            .iter_mut()
            .find(|(name, _)| name == "steps")
            .expect("steps")
            .1
        {
            Value::Array(steps) => &mut steps[index],
            _ => panic!("steps"),
        },
        _ => panic!("a document"),
    }
}

fn input_mut(document: &mut Value, index: usize) -> &mut Value {
    match step_mut(document, index) {
        Value::Object(found) => {
            &mut found
                .iter_mut()
                .find(|(name, _)| name == "input")
                .expect("input")
                .1
        }
        _ => panic!("a step"),
    }
}

fn refused(document: &Value, code: &'static str) {
    assert_eq!(
        admit(document, &contracts()).map(|_| ()),
        Err(PlanError::new(code)),
        "{document:?}"
    );
}

fn validator(schema: &str) -> impl Fn(&Value) -> Result<(), ()> {
    let schema = json(schema);
    move |value| action_schema::validate_payload(&schema, value).map_err(|_| ())
}

fn key(step: &str, pointer: &str, where_text: &str, item: &str) -> Key {
    (
        step.to_owned(),
        pointer.to_owned(),
        where_text.to_owned(),
        item.to_owned(),
    )
}

#[test]
fn a_plan_is_admitted_against_the_exact_current_contracts() {
    let mut document = document();
    let plan = admit(&document, &contracts()).expect("an admitted plan");
    assert_eq!(
        plan.steps
            .iter()
            .map(|step| step.step_id.as_str())
            .collect::<Vec<_>>(),
        ["publish", "share"]
    );
    assert_eq!(
        plan.references("publish"),
        [
            ("/id".to_owned(), String::new(), String::new()),
            ("/meta/a~1b/0".to_owned(), String::new(), String::new())
        ]
    );
    assert_eq!(plan.references("share"), Vec::new());
    assert!(super::PIN_RE.is_full(&plan.digest));
    set(
        at_mut(input_mut(&mut document, 0), "title"),
        "value",
        text("Changed"),
    );
    assert_ne!(
        admit(&document, &contracts())
            .expect("an admitted plan")
            .digest,
        plan.digest
    );
}

fn at_mut<'v>(value: &'v mut Value, key: &str) -> &'v mut Value {
    match value {
        Value::Object(found) => {
            &mut found
                .iter_mut()
                .find(|(name, _)| name == key)
                .expect("a member")
                .1
        }
        _ => panic!("an object"),
    }
}

#[test]
fn the_closed_shape_version_timezone_and_bounds_are_enforced() {
    let first = elements(at(&document(), "steps"))[0].clone();
    let too_many: Vec<Value> = (0..=MAX_STEPS)
        .map(|index| {
            let mut step = first.clone();
            set(&mut step, "id", text(&format!("step{index}")));
            step
        })
        .collect();
    let mut extra = document();
    set(&mut extra, "extra", int(1));
    for (candidate, code) in [
        (json("[]"), "plan-invalid"),
        (extra, "plan-invalid"),
        (document_with("version", int(VERSION + 1)), "plan-invalid"),
        (document_with("steps", json("[]")), "plan-output-invalid"),
        (
            document_with("steps", Value::Array(too_many)),
            "plan-invalid",
        ),
        (document_with("steps", json("{}")), "plan-invalid"),
        (
            document_with("timezone", text("Mars/Olympus")),
            "plan-timezone-invalid",
        ),
        (
            document_with("timezone", Value::Null),
            "plan-timezone-invalid",
        ),
        (
            document_with("steps", json(r#"[{"id": "x"}]"#)),
            "plan-step-invalid",
        ),
        (document_with("version", json("4.0")), "plan-invalid"),
    ] {
        refused(&candidate, code);
    }
    let mut large = document();
    set(
        at_mut(input_mut(&mut large, 0), "title"),
        "value",
        text(&"x".repeat(super::MAX_PLAN_BYTES)),
    );
    refused(&large, "plan-invalid");
}

#[test]
fn each_step_names_one_exact_pinned_action_and_closed_inputs() {
    let step = |key: &str, value: Value| {
        let mut changed = document();
        set(step_mut(&mut changed, 0), key, value);
        changed
    };
    for (candidate, code) in [
        (step("id", text("Publish")), "plan-step-invalid"),
        (step("assistant", text("Shimpz")), "plan-step-invalid"),
        (step("action", text("Publish")), "plan-step-invalid"),
        (step("pin", text("sha256:x")), "plan-step-invalid"),
        (step("input", json("[]")), "plan-step-invalid"),
        (step("pin", text(OTHER_PIN)), "plan-pin-drift"),
        (step("action", text("delete-post")), "plan-pin-drift"),
        (
            step(
                "input",
                json(
                    r#"{"title": {"kind": "literal", "value": "x"}, "extra": {"kind": "literal", "value": 1}}"#,
                ),
            ),
            "plan-input-mismatch",
        ),
        (
            step(
                "input",
                json(r#"{"day": {"kind": "run_clock", "format": "date"}}"#),
            ),
            "plan-input-mismatch",
        ),
    ] {
        refused(&candidate, code);
    }
    let mut duplicate = document();
    set(step_mut(&mut duplicate, 1), "id", text("publish"));
    refused(&duplicate, "plan-step-invalid");
}

#[test]
fn an_action_that_takes_a_file_cannot_be_compiled() {
    // A Routine holds no file grant, so even a literal id never stands for an attached file (ADR-0093).
    let mut contracts = contracts();
    let mut publish = contract(PIN, PUBLISH);
    publish.input_files = vec!["title".to_owned()];
    contracts.insert(
        ("shimpz-blog".to_owned(), "publish-post".to_owned()),
        publish,
    );
    assert_eq!(
        admit(&document(), &contracts).map(|_| ()),
        Err(PlanError::new("plan-file-input"))
    );
}

fn with_source(name: &str, value: &str, step: usize) -> Value {
    let mut changed = document();
    set(input_mut(&mut changed, step), name, json(value));
    changed
}

#[test]
fn every_value_source_is_closed() {
    let dated_date = format!(r#"{{"kind": "run_clock", "format": "date", "parts": [{DATED}]}}"#);
    let sources = [
        ("title", r#"{"kind": "literal"}"#, 0),
        ("title", r#"{"kind": "template", "value": "{x}"}"#, 0),
        ("title", r#""Weekly""#, 0),
        ("day", r#"{"kind": "run_clock", "format": "weekday"}"#, 0),
        ("day", r#"{"kind": "run_clock", "format": "time"}"#, 0),
        ("title", r#"{"kind": "run_clock", "format": "text"}"#, 0),
        ("title", dated_date.as_str(), 0),
        (
            "title",
            r#"{"kind": "run_clock", "format": "text", "parts": ["no date"]}"#,
            0,
        ),
        (
            "title",
            r#"{"kind": "step_text", "step": "publish", "pointer": "/id"}"#,
            1,
        ),
        (
            "post_id",
            r#"{"kind": "step_output", "step": "publish", "pointer": "/a", "where": {}}"#,
            1,
        ),
    ];
    for (name, source, step) in sources {
        refused(&with_source(name, source, step), "plan-input-invalid");
    }
}

#[test]
fn every_reference_names_an_earlier_step_by_a_valid_pointer() {
    for (name, source, step) in [
        (
            "post_id",
            r#"{"kind": "step_output", "step": "share", "pointer": "/id"}"#,
            1,
        ),
        (
            "post_id",
            r#"{"kind": "step_output", "step": "later", "pointer": "/id"}"#,
            1,
        ),
        (
            "post_id",
            r#"{"kind": "step_output", "step": "publish", "pointer": "id"}"#,
            1,
        ),
        (
            "post_id",
            r#"{"kind": "step_output", "step": "publish", "pointer": "/a~2"}"#,
            1,
        ),
        (
            "title",
            r#"{"kind": "step_output", "step": "publish", "pointer": "/id"}"#,
            0,
        ),
    ] {
        refused(&with_source(name, source, step), "plan-reference-invalid");
    }
}

#[test]
fn every_literal_and_run_clock_value_is_typed_at_creation() {
    let long_title = format!(r#"{{"kind": "literal", "value": "{}"}}"#, "x".repeat(81));
    let long_template = format!(
        r#"{{"kind": "run_clock", "format": "text", "parts": ["{}", {DATED}]}}"#,
        "x".repeat(80)
    );
    for (name, source, code) in [
        (
            "title",
            r#"{"kind": "literal", "value": 7}"#,
            "plan-input-type",
        ),
        ("title", long_title.as_str(), "plan-input-type"),
        (
            "count",
            r#"{"kind": "run_clock", "format": "date"}"#,
            "plan-input-type",
        ),
        ("title", long_template.as_str(), "plan-input-type"),
        (
            "title",
            r#"{"kind": "literal", "value": null}"#,
            "plan-input-type",
        ),
    ] {
        refused(&with_source(name, source, 0), code);
    }
    let sealed = format!(r#"{{"kind": "run_clock", "format": "text", "parts": [{DATED}]}}"#);
    refused(
        &with_source("secret_note", &sealed, 1),
        "plan-secret-literal",
    );
    let nullable = admit(
        &with_source("note", r#"{"kind": "literal", "value": null}"#, 0),
        &contracts(),
    )
    .expect("a plan");
    assert_eq!(
        at(nullable.steps[0].input("note").expect("note"), "value"),
        &Value::Null
    );
}

#[test]
fn a_secret_is_never_a_literal() {
    let literal = |name: &str, value: &str, step: usize| {
        with_source(
            name,
            &format!(r#"{{"kind": "literal", "value": {value}}}"#),
            step,
        )
    };
    for candidate in [
        literal("api_token", r#""plain""#, 1),
        literal("secret_note", r#""plain""#, 1),
        literal("code", r#""plain""#, 1),
        literal("title", r#""Bearer abcdefghijklmnop""#, 0),
        literal("title", r#""api_key=abc""#, 0),
        literal("tags", r#"["ok", "sk-abcdefghijklmnop"]"#, 1),
        literal(
            "note",
            r#""eyJhbGciOiJIUzI1.eyJzdWIiOjEyMzQ1.c2lnbmF0dXJlMTIz""#,
            0,
        ),
        with_source(
            "title",
            &format!(
                r#"{{"kind": "run_clock", "format": "text", "parts": ["Bearer abcdefghijklmnop ", {DATED}]}}"#
            ),
            0,
        ),
    ] {
        refused(&candidate, "plan-secret-literal");
    }
    assert!(!holds_credential(&json(
        r#"{"tags": [1, null, {"k": "v"}]}"#
    )));
    assert!(holds_credential(&json(r#"{"password=abc": "v"}"#)));
}

const NESTED: &str = r##"{
    "type": "object",
    "properties": {
        "options": {
            "type": "object",
            "properties": {
                "password": {"type": "string"},
                "label": {"type": "string"},
                "hidden": {"type": "string", "writeOnly": true},
                "ref": {"$ref": "#/$defs/sealed"},
                "either": {"anyOf": [{"type": "string", "format": "password"}, {"type": "integer"}]},
                "pairs": {"type": "array", "items": {"$ref": "#/$defs/pair"}},
                "tuple": {"type": "array", "prefixItems": [{"type": "string"}, {"$ref": "#/$defs/sealed"}]}
            },
            "additionalProperties": false
        }
    },
    "required": ["options"],
    "additionalProperties": false,
    "$defs": {
        "sealed": {"type": "string", "writeOnly": true},
        "pair": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "value": {"$ref": "#/$defs/sealed"}},
            "additionalProperties": false
        }
    }
}"##;

/// A one-step publish plan of these inputs under this schema.
fn single(inputs: &str, schema: &Value) -> Result<Plan, PlanError> {
    let mut document = document_with("output", json(r#"{"mode": "show", "step": "publish"}"#));
    let step = json(&format!(
        r#"{{"id": "publish", "assistant": "shimpz-blog", "action": "publish-post", "pin": "{PIN}", "input": {inputs}}}"#
    ));
    set(&mut document, "steps", Value::Array(vec![step]));
    let contracts = Contracts::from([(
        ("shimpz-blog".to_owned(), "publish-post".to_owned()),
        ActionContract::new(PIN.to_owned(), schema.clone()),
    )]);
    admit(&document, &contracts)
}

fn options(value: &str) -> String {
    format!(r#"{{"options": {{"kind": "literal", "value": {value}}}}}"#)
}

#[test]
fn a_secret_nested_anywhere_in_a_literal_is_refused() {
    let nested = json(NESTED);
    for value in [
        r#"{"password": "hunter2"}"#,
        r#"{"hidden": "plain"}"#,
        r#"{"ref": "plain"}"#,
        r#"{"either": "plain"}"#,
        r#"{"pairs": [{"name": "a"}, {"name": "b", "value": "plain"}]}"#,
        r#"{"tuple": ["first", "plain"]}"#,
    ] {
        assert_eq!(
            single(&options(value), &nested).map(|_| ()),
            Err(PlanError::new("plan-secret-literal")),
            "{value}"
        );
    }
    let admitted = single(
        &options(r#"{"label": "x", "pairs": [{"name": "a"}], "tuple": ["first"]}"#),
        &nested,
    )
    .expect("an admitted plan");
    let value = at(
        admitted.steps[0].input("options").expect("options"),
        "value",
    );
    assert_eq!(at(value, "label"), &text("x"));
}

#[test]
fn a_literal_or_a_reference_chain_deeper_than_the_bound_is_refused() {
    let nested = json(NESTED);
    let mut deep = text("x");
    for _ in 0..=MAX_SECRET_DEPTH {
        deep = Value::Array(vec![deep]);
    }
    let array = json(r#"{"type": "array"}"#);
    assert_eq!(
        secret_literal_at(&nested, "items", Some(&deep), &Sub::Node(&array), 0),
        Ok(true)
    );
    let chain = Value::Object(vec![(
        "$defs".to_owned(),
        Value::Object(
            (0..70)
                .map(|index| {
                    (
                        format!("d{index}"),
                        json(&format!(r##"{{"$ref": "#/$defs/d{}"}}"##, index + 1)),
                    )
                })
                .collect(),
        ),
    )]);
    let start = json(r##"{"$ref": "#/$defs/d0"}"##);
    assert_eq!(
        applicable(&chain, &Sub::Node(&start), 0, Held::Value(&Value::Null))
            .map(|found| found.len()),
        Err(PlanError::new("plan-secret-literal"))
    );
    assert_eq!(
        applicable(
            &nested,
            &Sub::Node(&Value::Null),
            0,
            Held::Value(&Value::Null)
        ),
        Ok(Vec::new())
    );
}

/// A schema whose one `options` member is `options`, admitted as normal Action admission admits it.
fn options_schema(options: &str, defs: Option<&str>) -> Value {
    let defs = defs.map_or_else(String::new, |defs| format!(r#", "$defs": {defs}"#));
    let schema = json(&format!(
        r#"{{"type": "object", "properties": {{"options": {options}}}, "required": ["options"],
            "additionalProperties": false{defs}}}"#
    ));
    action_schema::admit_schema(&schema).expect("an admitted schema");
    schema
}

const SEALED: &str = r#"{"type": "string", "writeOnly": true}"#;
const PLAIN: &str = r#"{"type": "string"}"#;

fn pattern_schema() -> Value {
    options_schema(
        &format!(
            r#"{{"type": "object", "properties": {{}}, "patternProperties": {{"^value$": {SEALED}, "^note$": {PLAIN}}},
                "additionalProperties": false}}"#
        ),
        None,
    )
}

fn dependent_schema() -> Value {
    options_schema(
        &format!(
            r#"{{"type": "object", "properties": {{"mode": {PLAIN}, "x": {PLAIN}}},
                "dependentSchemas": {{"mode": {{"type": "object", "properties": {{"mode": {PLAIN}, "x": {SEALED}}},
                "additionalProperties": false}}}}, "additionalProperties": false}}"#
        ),
        None,
    )
}

fn conditional_schema(then: &str) -> Value {
    options_schema(
        &format!(
            r#"{{"type": "object", "properties": {{"x": {PLAIN}}},
                "if": {{"type": "object", "properties": {{"x": {{"const": "a"}}}}, "additionalProperties": false}},
                "then": {{"type": "object", "properties": {{"x": {then}}}, "additionalProperties": false}},
                "additionalProperties": false}}"#
        ),
        None,
    )
}

#[test]
fn every_admitted_applicator_is_followed_or_refused_for_secret_literals() {
    let negated = options_schema(
        r##"{"type": "string", "not": {"$ref": "#/$defs/sealed"}}"##,
        Some(&format!(r#"{{"sealed": {SEALED}}}"#)),
    );
    let contained = options_schema(
        &format!(r#"{{"type": "array", "items": {PLAIN}, "contains": {SEALED}}}"#),
        None,
    );
    let named = options_schema(
        &format!(
            r#"{{"type": "array", "items": {PLAIN},
                "contains": {{"type": "object", "properties": {{"api_key": {PLAIN}}}, "additionalProperties": false}}}}"#
        ),
        None,
    );
    for (schema, value) in [
        (pattern_schema(), r#"{"value": "hunter2"}"#),
        (dependent_schema(), r#"{"mode": "a", "x": "hunter2"}"#),
        (conditional_schema(SEALED), r#"{"x": "b"}"#),
        (negated, r#""plain""#),
        (contained, r#"["plain"]"#),
        (named, r#"["plain"]"#),
    ] {
        assert_eq!(
            single(&options(value), &schema).map(|_| ()),
            Err(PlanError::new("plan-secret-literal")),
            "{value}"
        );
    }
    for (schema, value) in [
        (pattern_schema(), r#"{"note": "n"}"#),
        (dependent_schema(), r#"{"x": "plain"}"#),
        (conditional_schema(r#"{"maxLength": 4}"#), r#"{"x": "a"}"#),
    ] {
        let admitted = single(&options(value), &schema).expect("an admitted plan");
        assert_eq!(
            at(
                admitted.steps[0].input("options").expect("options"),
                "value"
            ),
            &json(value)
        );
    }
}

#[test]
fn member_schemas_apply_properties_patterns_and_else_additional_properties() {
    // An additional-properties schema applies only to members no property or pattern names.
    let extra = json(&format!(
        r#"{{"properties": {{"a": {PLAIN}}}, "patternProperties": {{"^b$": {PLAIN}}}, "additionalProperties": {SEALED}}}"#
    ));
    let (sealed, plain) = (json(SEALED), json(PLAIN));
    assert_eq!(
        member_schemas(&[&extra], "c"),
        Ok(Sub::AllOf(vec![&sealed]))
    );
    assert_eq!(member_schemas(&[&extra], "a"), Ok(Sub::AllOf(vec![&plain])));
    assert_eq!(member_schemas(&[&extra], "b"), Ok(Sub::AllOf(vec![&plain])));
    let unmatchable = json(r#"{"patternProperties": {"(?<=a)b": {"type": "string"}}}"#);
    assert_eq!(
        member_schemas(&[&unmatchable], "ab"),
        Err(PlanError::new("plan-secret-literal"))
    );
    let mut deep = json(r#"{"type": "string"}"#);
    for _ in 0..MAX_SECRET_DEPTH + 2 {
        deep = Value::Object(vec![("not".to_owned(), deep)]);
    }
    assert!(could_hold_secret(&json("{}"), Some(&deep), 0));
    assert!(!could_hold_secret(&json("{}"), Some(&Value::Null), 0));
}

fn closed_root(value: &str) -> String {
    format!(
        r#"{{"type": "object", "properties": {{"value": {value}, "mode": {PLAIN}}}, "additionalProperties": false}}"#
    )
}

fn rooted(members: &str) -> Value {
    let schema = json(&format!(
        r#"{{"type": "object", "properties": {{"value": {PLAIN}, "mode": {PLAIN}}}, "required": ["value"],
            "additionalProperties": false{members}}}"#
    ));
    action_schema::admit_schema(&schema).expect("an admitted schema");
    schema
}

const SECRET_VALUE: &str = r#"{"value": {"kind": "literal", "value": "hunter2"}}"#;

#[test]
fn the_input_schema_root_is_the_first_position_of_the_secret_check() {
    let with_mode = r#"{"value": {"kind": "literal", "value": "hunter2"}, "mode": {"kind": "literal", "value": "a"}}"#;
    let conditional = format!(
        r#", "if": {{"type": "object", "properties": {{"mode": {{"const": "a"}}}}, "additionalProperties": false}},
            "then": {}"#,
        closed_root(SEALED)
    );
    let cases = [
        (
            rooted(&format!(r#", "allOf": [{}]"#, closed_root(SEALED))),
            SECRET_VALUE,
        ),
        (
            rooted(&format!(r#", "anyOf": [{}]"#, closed_root(SEALED))),
            SECRET_VALUE,
        ),
        (
            rooted(&format!(r#", "patternProperties": {{"^val": {SEALED}}}"#)),
            SECRET_VALUE,
        ),
        (
            rooted(&format!(
                r#", "dependentSchemas": {{"mode": {}}}"#,
                closed_root(SEALED)
            )),
            with_mode,
        ),
        (rooted(&conditional), SECRET_VALUE),
        (
            rooted(&format!(
                r##", "$ref": "#/$defs/sealed_value", "$defs": {{"sealed_value": {}}}"##,
                closed_root(SEALED)
            )),
            SECRET_VALUE,
        ),
    ];
    for (schema, inputs) in &cases {
        assert_eq!(
            single(inputs, schema).map(|_| ()),
            Err(PlanError::new("plan-secret-literal")),
            "{inputs}"
        );
    }
}

#[test]
fn a_member_filled_at_run_time_counts_only_for_presence() {
    let dependent = rooted(&format!(
        r#", "dependentSchemas": {{"mode": {}}}"#,
        closed_root(SEALED)
    ));
    let admitted = single(SECRET_VALUE, &dependent).expect("an admitted plan");
    assert_eq!(
        admitted.steps[0].inputs,
        crate::json::members(&json(SECRET_VALUE)).to_vec()
    );
    // A member filled at run time counts for presence: it triggers the dependent schema but is never a literal.
    let filled = r#"{"value": {"kind": "run_clock", "format": "date"}, "mode": {"kind": "literal", "value": "a"}}"#;
    let clocked = single(filled, &dependent).expect("an admitted plan");
    assert_eq!(
        at(clocked.steps[0].input("value").expect("value"), "kind"),
        &text("run_clock")
    );
    let harmless = rooted(&format!(
        r#", "allOf": [{}]"#,
        closed_root(r#"{"maxLength": 40}"#)
    ));
    assert!(single(SECRET_VALUE, &harmless).is_ok());
}

#[test]
fn whole_input_context_activates_dependent_schemas() {
    let schema = json(
        r#"{"type": "object", "properties": {"mode": {"type": "string"}, "value": {"type": "string"}},
            "dependentSchemas": {"mode": {"properties": {"value": {"writeOnly": true}}}}}"#,
    );
    assert_eq!(
        secret_literal(&schema, &json(r#"{"mode": "x", "value": "y"}"#)),
        Ok(true)
    );
    assert_eq!(
        secret_literal(&schema, &json(r#"{"value": "y"}"#)),
        Ok(false)
    );
    assert_eq!(
        secret_literal(
            &json(r#"{"type": "object"}"#),
            &json(r#"{"api_token": "y"}"#)
        ),
        Ok(true)
    );
    assert_eq!(
        secret_literal(&json(r#"{"type": "object"}"#), &json(r#"{"zone_id": "y"}"#)),
        Ok(false)
    );
}

// --- Resolution ------------------------------------------------------------------------------------------------------

fn result() -> Value {
    json(r#"{"id": "post-1", "meta": {"a/b": [["news"], "x"], "~k": 1}, "list": [10, 20]}"#)
}

fn started_at() -> i64 {
    epoch(2026, 1, 1, 2, 30, 0)
}

#[test]
fn a_pointer_selects_exactly_one_value_and_fails_closed_otherwise() {
    let result = result();
    assert_eq!(
        select(&result, &text("/meta/a~1b/0")),
        Ok(json(r#"["news"]"#))
    );
    assert_eq!(select(&result, &text("/meta/~0k")), Ok(int(1)));
    assert_eq!(select(&result, &text("/list/1")), Ok(int(20)));
    assert_eq!(select(&result, &text("")), Ok(result.clone()));
    for pointer in [
        "/missing",
        "/list/2",
        "/list/01",
        "/list/-",
        "/list/x",
        "/id/0",
        "/meta/a/b",
    ] {
        assert_eq!(
            select(&result, &text(pointer)),
            Err(PlanError::new("plan-reference-missing")),
            "{pointer}"
        );
    }
    for pointer in [
        text("id"),
        text("/~2"),
        int(7),
        text(&format!("/{}", "x".repeat(256))),
    ] {
        assert_eq!(
            select(&result, &pointer),
            Err(PlanError::new("plan-reference-invalid")),
            "{pointer:?}"
        );
    }
}

#[test]
fn only_selected_values_are_retained_within_their_bound() {
    let plan = admit(&document(), &contracts()).expect("a plan");
    let chosen = selections(&plan, "publish", &result()).expect("selections");
    assert_eq!(
        chosen,
        BTreeMap::from([
            (
                ("/id".to_owned(), String::new(), String::new()),
                text("post-1")
            ),
            (
                ("/meta/a~1b/0".to_owned(), String::new(), String::new()),
                json(r#"["news"]"#)
            ),
        ])
    );
    assert_eq!(
        selections(&plan, "share", &json(r#"{"anything": 1}"#)),
        Ok(BTreeMap::new())
    );
    assert!(retained_within(&BTreeMap::from([(
        key("publish", "/id", "", ""),
        text("x")
    )])));
    assert!(!retained_within(&BTreeMap::from([(
        key("publish", "/id", "", ""),
        text(&"x".repeat(MAX_RETAINED_BYTES))
    )])));
}

#[test]
fn resolution_copies_literals_renders_one_run_clock_and_validates_the_whole_input() {
    let plan = admit(&document(), &contracts()).expect("a plan");
    let (publish, share) = (&plan.steps[0], &plan.steps[1]);
    let resolved = resolve(
        &plan,
        publish,
        &BTreeMap::new(),
        started_at(),
        validator(PUBLISH),
    );
    // 02:30 UTC on 1 January is still 31 December in Sao Paulo.
    assert_eq!(
        resolved,
        Ok(json(r#"{"title": "Weekly report", "day": "2025-12-31"}"#))
    );
    let selected = BTreeMap::from([
        (key("publish", "/id", "", ""), text("post-1")),
        (key("publish", "/meta/a~1b/0", "", ""), json(r#"["news"]"#)),
    ]);
    assert_eq!(
        resolve(&plan, share, &selected, started_at(), validator(SHARE)),
        Ok(json(r#"{"post_id": "post-1", "tags": ["news"]}"#))
    );
    let only = BTreeMap::from([(key("publish", "/id", "", ""), text("post-1"))]);
    assert_eq!(
        resolve(&plan, share, &only, started_at(), validator(SHARE)),
        Err(PlanError::new("plan-reference-missing"))
    );
    for wrong in ["null", "7", r#"[""]"#] {
        let selected = BTreeMap::from([
            (key("publish", "/id", "", ""), text("post-1")),
            (key("publish", "/meta/a~1b/0", "", ""), json(wrong)),
        ]);
        assert_eq!(
            resolve(&plan, share, &selected, started_at(), validator(SHARE)),
            Err(PlanError::new("plan-input-type")),
            "{wrong}"
        );
    }
}

#[test]
fn clock_tokens_and_commitments_are_deterministic() {
    let instant = epoch(2026, 7, 4, 15, 5, 9);
    let tokyo = zone_named("Asia/Tokyo").expect("a zone");
    let date = json(r#"{"kind": "run_clock", "format": "date"}"#);
    assert_eq!(
        clock_value(&date, instant, &tokyo),
        Some(text("2026-07-05"))
    );
    assert_eq!(
        clock_value(&date, instant, &jiff::tz::TimeZone::UTC),
        Some(text("2026-07-04"))
    );
    let template = json(
        r#"{"kind": "run_clock", "format": "text", "parts": ["news ", {"days": 1, "format": "{d}/{M}", "lang": null}]}"#,
    );
    assert_eq!(
        clock_value(&template, instant, &tokyo),
        Some(text("news 6/7"))
    );
    assert_eq!(
        commitment(&json(r#"{"b": 1, "a": "é"}"#)),
        commitment(&json(r#"{"a": "é", "b": 1}"#))
    );
    assert_ne!(
        commitment(&json(r#"{"a": 1}"#)),
        commitment(&json(r#"{"a": "1"}"#))
    );
}

fn publish(step_id: &str, inputs: &str) -> Value {
    json(&format!(
        r#"{{"id": "{step_id}", "assistant": "shimpz-blog", "action": "publish-post", "pin": "{PIN}", "input": {inputs}}}"#
    ))
}

#[test]
fn one_action_runs_many_times_each_with_its_own_required_and_optional_inputs() {
    let steps = vec![
        publish(
            "first",
            r#"{"title": {"kind": "literal", "value": "One"}, "count": {"kind": "literal", "value": 1}}"#,
        ),
        publish(
            "second",
            r#"{"title": {"kind": "literal", "value": "Two"}, "note": {"kind": "literal", "value": null}}"#,
        ),
        publish(
            "third",
            r#"{"title": {"kind": "step_output", "step": "first", "pointer": "/id"}}"#,
        ),
    ];
    let mut document = document_with("steps", Value::Array(steps.clone()));
    set(
        &mut document,
        "output",
        json(r#"{"mode": "show", "step": "third"}"#),
    );
    let plan = admit(&document, &contracts()).expect("a plan");
    assert!(plan.steps.iter().all(|step| step.action == "publish-post"));
    // An optional member is present only where the plan sets it; nothing fills one in.
    let mut members: Vec<Vec<&str>> = plan
        .steps
        .iter()
        .map(|step| step.inputs.iter().map(|(name, _)| name.as_str()).collect())
        .collect();
    for names in &mut members {
        names.sort_unstable();
    }
    assert_eq!(
        members,
        [vec!["count", "title"], vec!["note", "title"], vec!["title"]]
    );
    // A required member is never defaulted: a step without it is refused.
    let mut missing = document.clone();
    let mut more = steps;
    more.push(publish(
        "fourth",
        r#"{"count": {"kind": "literal", "value": 4}}"#,
    ));
    set(&mut missing, "steps", Value::Array(more));
    assert_eq!(
        admit(&missing, &contracts()).map(|_| ()),
        Err(PlanError::new("plan-input-mismatch"))
    );
}

#[test]
fn a_plan_of_the_most_steps_is_admitted_and_one_more_is_refused() {
    let mut steps: Vec<Value> = (0..MAX_STEPS)
        .map(|index| {
            publish(
                &format!("s{index}"),
                &format!(r#"{{"title": {{"kind": "literal", "value": "Post {index}"}}}}"#),
            )
        })
        .collect();
    let mut document = document_with("steps", Value::Array(steps.clone()));
    set(
        &mut document,
        "output",
        json(r#"{"mode": "changes", "step": "s255"}"#),
    );
    let plan = admit(&document, &contracts()).expect("a plan");
    assert_eq!(
        (
            plan.steps.len(),
            plan.shown().map(|step| step.step_id.as_str())
        ),
        (256, Some("s255"))
    );
    assert!(well_formed(&document));
    steps.push(publish(
        "s256",
        r#"{"title": {"kind": "literal", "value": "x"}}"#,
    ));
    let mut over = document_with("steps", Value::Array(steps));
    set(
        &mut over,
        "output",
        json(r#"{"mode": "changes", "step": "s256"}"#),
    );
    assert!(!well_formed(&over));
}

#[test]
fn a_resolved_input_over_its_bound_is_refused_before_any_dispatch() {
    let steps = vec![
        publish("earlier", r#"{"title": {"kind": "literal", "value": "x"}}"#),
        publish(
            "only",
            r#"{"title": {"kind": "step_output", "step": "earlier", "pointer": ""}}"#,
        ),
    ];
    let mut document = document_with("steps", Value::Array(steps));
    set(
        &mut document,
        "output",
        json(r#"{"mode": "none", "step": null}"#),
    );
    let open = Contracts::from([(
        ("shimpz-blog".to_owned(), "publish-post".to_owned()),
        contract(
            PIN,
            r#"{"type": "object", "properties": {"title": {"type": "string"}}}"#,
        ),
    )]);
    let plan = admit(&document, &open).expect("a plan");
    let validated = std::cell::Cell::new(0);
    let counting = |_: &Value| {
        validated.set(validated.get() + 1);
        Ok(())
    };
    let large = BTreeMap::from([(
        key("earlier", "", "", ""),
        text(&"x".repeat(MAX_RESOLVED_INPUT_BYTES)),
    )]);
    assert_eq!(
        resolve(&plan, &plan.steps[1], &large, 0, counting),
        Err(PlanError::new("plan-input-too-large"))
    );
    assert_eq!(validated.get(), 0);
    let fits = "x".repeat(MAX_RESOLVED_INPUT_BYTES - 64);
    let selected = BTreeMap::from([(key("earlier", "", "", ""), text(&fits))]);
    assert_eq!(
        resolve(&plan, &plan.steps[1], &selected, 0, counting),
        Ok(json(&format!(r#"{{"title": "{fits}"}}"#)))
    );
}

// --- Previews --------------------------------------------------------------------------------------------------------

/// The output schema of the step an input copies its value from: one member secret by name, one inside an object.
const SOURCE: &str = r#"{
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "token": {"type": "string"},
        "meta": {"type": "object", "properties": {"api_key": {"type": "string"}, "label": {"type": "string"}}}
    }
}"#;

fn preview_plan(inputs: &str, source: &Value, share: &Value) -> Plan {
    let mut publish_contract = contract(PIN, PUBLISH);
    publish_contract.output_schema = source.clone();
    let contracts = Contracts::from([
        (
            ("shimpz-blog".to_owned(), "publish-post".to_owned()),
            publish_contract,
        ),
        (
            ("shimpz-blog".to_owned(), "share-post".to_owned()),
            ActionContract::new(OTHER_PIN.to_owned(), share.clone()),
        ),
    ]);
    let steps = json(&format!(
        r#"[{{"id": "publish", "assistant": "shimpz-blog", "action": "publish-post", "pin": "{PIN}",
              "input": {{"title": {{"kind": "literal", "value": "Weekly report"}}}}}},
            {{"id": "share", "assistant": "shimpz-blog", "action": "share-post", "pin": "{OTHER_PIN}", "input": {inputs}}}]"#
    ));
    let mut document = document_with("steps", steps);
    set(
        &mut document,
        "output",
        json(r#"{"mode": "none", "step": null}"#),
    );
    admit(&document, &contracts).expect("a plan")
}

fn preview(
    inputs: &str,
    resolved: &str,
    selected: &[(Key, &str)],
    protected: &[&str],
    source: &str,
    share: &str,
) -> Value {
    let plan = preview_plan(inputs, &json(source), &json(share));
    let selected: BTreeMap<Key, Value> = selected
        .iter()
        .map(|(key, value)| (key.clone(), json(value)))
        .collect();
    let protected: Vec<String> = protected.iter().map(|item| (*item).to_owned()).collect();
    input_preview(
        &plan,
        &plan.steps[1],
        &json(resolved),
        &selected,
        &protected,
    )
}

fn shown_value(previews: &Value, index: usize) -> String {
    as_str(at(&elements(previews)[index], "value"))
        .unwrap_or("<null>")
        .to_owned()
}

#[test]
fn a_value_copied_from_a_secret_source_position_is_withheld_whole() {
    for pointer in ["/token", "/meta/api_key"] {
        let inputs = format!(
            r#"{{"post_id": {{"kind": "step_output", "step": "publish", "pointer": "{pointer}"}}}}"#
        );
        let shown = preview(
            &inputs,
            r#"{"post_id": "s3cr3t"}"#,
            &[(key("publish", pointer, "", ""), r#""s3cr3t""#)],
            &[],
            SOURCE,
            SHARE,
        );
        assert_eq!(
            shown,
            json(r#"[{"member": "post_id", "value": null}]"#),
            "{pointer}"
        );
    }
}

#[test]
fn a_secret_root_or_a_dependent_schema_on_the_way_withholds_the_copied_value() {
    let sources = [
        r#"{"type": "object", "writeOnly": true, "properties": {"meta": {"type": "object"}}}"#,
        r#"{"type": "object", "properties": {"meta": {"type": "object", "properties": {"note": {"type": "string"}},
            "dependentSchemas": {"flag": {"properties": {"note": {"writeOnly": true}}}}}}}"#,
        r#"{"type": "object", "dependentSchemas": {"flag": {"properties": {"meta": {"format": "password"}}}}}"#,
    ];
    let inputs =
        r#"{"post_id": {"kind": "step_output", "step": "publish", "pointer": "/meta/note"}}"#;
    for source in sources {
        let shown = preview(
            inputs,
            r#"{"post_id": "private"}"#,
            &[(key("publish", "/meta/note", "", ""), r#""private""#)],
            &[],
            source,
            SHARE,
        );
        assert_eq!(
            shown,
            json(r#"[{"member": "post_id", "value": null}]"#),
            "{source}"
        );
    }
}

#[test]
fn a_schema_secret_is_redacted_by_its_original_key_before_any_key_is_renamed() {
    let source = r#"{"type": "object", "properties": {"meta": {"type": "object",
        "properties": {"entry": {"type": "string", "writeOnly": true}, "flag": {"type": "string"}},
        "dependentSchemas": {"flag": {"properties": {"other": {"format": "password"}}}}}}}"#;
    let value =
        r#"{"entry": "private-ordinary-value", "flag": "on", "other": "second-private-value"}"#;
    let inputs = r#"{"post_id": {"kind": "step_output", "step": "publish", "pointer": "/meta"}}"#;
    for protected in [&[][..], &["entry"][..], &["other", "flag"][..]] {
        let resolved = format!(r#"{{"post_id": {value}}}"#);
        let shown = preview(
            inputs,
            &resolved,
            &[(key("publish", "/meta", "", ""), value)],
            protected,
            source,
            SHARE,
        );
        assert!(!shown_value(&shown, 0).contains("private"), "{protected:?}");
    }
}

#[test]
fn no_key_is_renamed_before_the_destination_schema_is_walked() {
    let share = r#"{"type": "object", "properties": {"post_id": {"type": "string"}, "meta": {"type": "object",
        "properties": {"api_key": {"type": "string"}, "other": {"type": "string"}},
        "dependentSchemas": {"api_key": {"properties": {"other": {"writeOnly": true}}}}}},
        "required": ["post_id"], "additionalProperties": false}"#;
    let value = r#"{"api_key": "k-1", "other": "private-other-value"}"#;
    let inputs = r#"{"meta": {"kind": "step_output", "step": "publish", "pointer": "/meta"},
                    "post_id": {"kind": "literal", "value": "p-1"}}"#;
    for protected in [&[][..], &["api_key"][..]] {
        let resolved = format!(r#"{{"meta": {value}, "post_id": "p-1"}}"#);
        let shown = preview(
            inputs,
            &resolved,
            &[(key("publish", "/meta", "", ""), value)],
            protected,
            SOURCE,
            share,
        );
        assert!(!shown_value(&shown, 0).contains("private"));
        assert!(!shown_value(&shown, 0).contains("k-1"));
    }
}

#[test]
fn a_key_holding_an_injected_value_is_renamed_without_colliding() {
    let injected = "quote\"and\ncontrol";
    let value = Value::Object(vec![
        (format!("k {injected}"), text("v")),
        ("[redacted] 0".to_owned(), text("kept")),
        ("plain".to_owned(), text("ok")),
    ]);
    let plan = preview_plan(
        r#"{"post_id": {"kind": "step_output", "step": "publish", "pointer": "/id"}}"#,
        &json(SOURCE),
        &json(SHARE),
    );
    let resolved = Value::Object(vec![("post_id".to_owned(), value.clone())]);
    let selected = BTreeMap::from([(key("publish", "/id", "", ""), value)]);
    let shown = input_preview(
        &plan,
        &plan.steps[1],
        &resolved,
        &selected,
        &[injected.to_owned()],
    );
    assert_eq!(
        shown_value(&shown, 0),
        r#"{"[redacted] 0":"kept","[redacted] 1":"v","plain":"ok"}"#
    );
}

#[test]
fn secret_positions_inside_a_copied_value_and_at_its_destination_are_redacted() {
    let meta = r#"{"api_key": "k-123", "label": "public"}"#;
    let inputs = r#"{"post_id": {"kind": "step_output", "step": "publish", "pointer": "/meta"},
                    "tags": {"kind": "literal", "value": ["a"]}}"#;
    let resolved = format!(r#"{{"post_id": {meta}, "tags": ["a"]}}"#);
    let shown = preview(
        inputs,
        &resolved,
        &[(key("publish", "/meta", "", ""), meta)],
        &[],
        SOURCE,
        SHARE,
    );
    assert_eq!(
        shown_value(&shown, 0),
        r#"{"api_key":"[redacted]","label":"public"}"#
    );
    assert_eq!(
        elements(&shown)[1],
        json(r#"{"member": "tags", "value": "[\"a\"]"}"#)
    );
    let destination = r#"{"post_id": {"kind": "literal", "value": "p-1"},
                         "secret_note": {"kind": "step_output", "step": "publish", "pointer": "/id"}}"#;
    let shown = preview(
        destination,
        r#"{"post_id": "p-1", "secret_note": "plain"}"#,
        &[(key("publish", "/id", "", ""), r#""plain""#)],
        &[],
        SOURCE,
        SHARE,
    );
    assert_eq!(
        elements(&shown)[1],
        json(r#"{"member": "secret_note", "value": "\"[redacted]\""}"#)
    );
}

#[test]
fn credentials_and_injected_values_are_redacted_before_any_cut() {
    let credential = format!("sk-{}", "a".repeat(40));
    let injected = "quote\"and\ncontrol";
    let value = Value::Object(vec![
        ("label".to_owned(), text(&format!("see {injected} here"))),
        ("nested".to_owned(), Value::Array(vec![text(&credential)])),
        (credential.clone(), text("x")),
        ("ok".to_owned(), text("fine")),
    ]);
    let plan = preview_plan(
        r#"{"post_id": {"kind": "step_output", "step": "publish", "pointer": "/id"}}"#,
        &json(SOURCE),
        &json(SHARE),
    );
    let resolved = Value::Object(vec![("post_id".to_owned(), value.clone())]);
    let selected = BTreeMap::from([(key("publish", "/id", "", ""), value)]);
    let shown = shown_value(
        &input_preview(
            &plan,
            &plan.steps[1],
            &resolved,
            &selected,
            &[injected.to_owned()],
        ),
        0,
    );
    assert!(!shown.contains(&credential[..12]));
    assert!(!shown.contains("quote"));
    assert!(shown.contains(r#""ok":"fine""#));
    assert!(shown.chars().count() <= 120);
}

#[test]
fn an_unreadable_value_is_withheld_and_a_clock_shows_its_rendered_value() {
    let share = r#"{"type": "object", "properties": {"post_id": {"type": "string"}, "title": {"type": "string"}},
                   "required": ["post_id"], "additionalProperties": false}"#;
    let inputs = r#"{"post_id": {"kind": "step_output", "step": "publish", "pointer": "/id"},
                    "title": {"kind": "run_clock", "format": "date"}}"#;
    let shown = preview(
        inputs,
        r#"{"post_id": "x", "title": "2026-10-05"}"#,
        &[],
        &[],
        SOURCE,
        share,
    );
    assert_eq!(
        shown,
        json(
            r#"[{"member": "post_id", "value": null}, {"member": "title", "value": "\"2026-10-05\""}]"#
        )
    );
}

#[test]
fn a_value_nested_past_the_depth_bound_is_redacted_whole() {
    let mut deep = text("leaf");
    for _ in 0..MAX_SAFE_OUTPUT_DEPTH + 2 {
        deep = Value::Array(vec![deep]);
    }
    let plan = preview_plan(
        r#"{"post_id": {"kind": "step_output", "step": "publish", "pointer": "/id"}}"#,
        &json(SOURCE),
        &json(SHARE),
    );
    let resolved = Value::Object(vec![("post_id".to_owned(), deep.clone())]);
    let selected = BTreeMap::from([(key("publish", "/id", "", ""), deep)]);
    let shown = shown_value(
        &input_preview(&plan, &plan.steps[1], &resolved, &selected, &[]),
        0,
    );
    assert!(shown.contains("[redacted]"));
    assert!(!shown.contains("leaf"));
}

// --- Selectors and dispositions --------------------------------------------------------------------------------------

const ZONES_OUT: &str = r#"{"type": "object", "properties": {"result": {"type": "array", "prefixItems": [{"type": "object"}],
    "items": {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}}}}}}"#;

fn selector_plan(source: &str, output: &str, out: &str) -> Result<Plan, PlanError> {
    let mut publish_contract = contract(PIN, PUBLISH);
    publish_contract.output_schema = json(out);
    let contracts = Contracts::from([
        (
            ("shimpz-blog".to_owned(), "publish-post".to_owned()),
            publish_contract,
        ),
        (
            ("shimpz-blog".to_owned(), "share-post".to_owned()),
            contract(OTHER_PIN, SHARE),
        ),
    ]);
    let steps = json(&format!(
        r#"[{{"id": "publish", "assistant": "shimpz-blog", "action": "publish-post", "pin": "{PIN}",
              "input": {{"title": {{"kind": "literal", "value": "x"}}}}}},
            {{"id": "share", "assistant": "shimpz-blog", "action": "share-post", "pin": "{OTHER_PIN}", "input": {source}}}]"#
    ));
    let mut document = document_with("steps", steps);
    set(&mut document, "output", json(output));
    admit(&document, &contracts)
}

const SHOW_SHARE: &str = r#"{"mode": "show", "step": "share"}"#;

fn selected_source(changes: &str) -> String {
    format!(
        r#"{{"post_id": {{"kind": "step_output", "step": "publish", "pointer": "/result", "where": {{"name": "shimpz.com"}},
             "item": "/id"{changes}}}}}"#
    )
}

#[test]
fn a_pointer_through_an_index_and_scalars_beside_injected_values_preview_safely() {
    let out = r#"{"type": "object", "properties": {"zones": {"type": "array", "prefixItems": [{"type": "object"}]}}}"#;
    let plan = selector_plan(
        r#"{"post_id": {"kind": "step_output", "step": "publish", "pointer": "/zones/0"}}"#,
        r#"{"mode": "none", "step": null}"#,
        out,
    )
    .expect("a plan");
    let value = json(r#"{"id": 7, "flag": true, "note": null}"#);
    let resolved = Value::Object(vec![("post_id".to_owned(), value.clone())]);
    let selected = BTreeMap::from([(key("publish", "/zones/0", "", ""), value)]);
    let shown = input_preview(
        &plan,
        &plan.steps[1],
        &resolved,
        &selected,
        &["secret".to_owned()],
    );
    assert_eq!(
        shown_value(&shown, 0),
        r#"{"flag":true,"id":7,"note":null}"#
    );
}

#[test]
fn a_selector_is_admitted_keyed_whole_and_resolved_through_its_one_item() {
    let plan = selector_plan(&selected_source(""), SHOW_SHARE, ZONES_OUT).expect("a plan");
    let selector = (
        "/result".to_owned(),
        r#"{"name":"shimpz.com"}"#.to_owned(),
        "/id".to_owned(),
    );
    assert_eq!(plan.references("publish"), vec![selector.clone()]);
    let result = json(
        r#"{"result": [{"name": "other.org", "id": "z1"}, {"name": "shimpz.com", "id": "z2"}]}"#,
    );
    assert_eq!(
        selections(&plan, "publish", &result),
        Ok(BTreeMap::from([(selector, text("z2"))]))
    );
    let selected = BTreeMap::from([(
        key("publish", "/result", r#"{"name":"shimpz.com"}"#, "/id"),
        text("z2"),
    )]);
    assert_eq!(
        resolve(&plan, &plan.steps[1], &selected, 0, validator(SHARE)),
        Ok(json(r#"{"post_id": "z2"}"#))
    );
    let twice = json(
        r#"{"result": [{"name": "shimpz.com", "id": "a"}, {"name": "shimpz.com", "id": "a"}]}"#,
    );
    assert_eq!(
        selections(&plan, "publish", &twice),
        Err(PlanError::new("plan-reference-ambiguous"))
    );
}

#[test]
fn a_selector_out_of_its_closed_shape_is_refused() {
    for (where_, item) in [
        (r#"{"name": true}"#, r#""/id""#),
        (r#"{"a": "x", "b": "y"}"#, r#""/id""#),
        (r#"{"name": 1.5}"#, r#""/id""#),
        (r#"{"name": "shimpz.com"}"#, r#""id""#),
    ] {
        let source = format!(
            r#"{{"post_id": {{"kind": "step_output", "step": "publish", "pointer": "/result", "where": {where_}, "item": {item}}}}}"#
        );
        assert_eq!(
            selector_plan(&source, SHOW_SHARE, ZONES_OUT).map(|_| ()),
            Err(PlanError::new("plan-reference-invalid")),
            "{source}"
        );
    }
    let without_item = r#"{"post_id": {"kind": "step_output", "step": "publish", "pointer": "/result", "where": {"name": "shimpz.com"}}}"#;
    assert_eq!(
        selector_plan(without_item, SHOW_SHARE, ZONES_OUT).map(|_| ()),
        Err(PlanError::new("plan-input-invalid"))
    );
}

#[test]
fn a_selected_value_is_previewed_under_every_schema_any_item_may_have() {
    let plan = selector_plan(&selected_source(""), SHOW_SHARE, ZONES_OUT).expect("a plan");
    let resolved = json(r#"{"post_id": "z2"}"#);
    let selected = BTreeMap::from([(
        key("publish", "/result", r#"{"name":"shimpz.com"}"#, "/id"),
        text("z2"),
    )]);
    let shown = input_preview(&plan, &plan.steps[1], &resolved, &selected, &[]);
    assert_eq!(shown, json(r#"[{"member": "post_id", "value": "\"z2\""}]"#));
    let secret = r#"{"type": "object", "properties": {"result": {"type": "array", "items": {"type": "object", "writeOnly": true}}}}"#;
    let withheld = selector_plan(&selected_source(""), SHOW_SHARE, secret).expect("a plan");
    let shown = input_preview(&withheld, &withheld.steps[1], &resolved, &selected, &[]);
    assert_eq!(shown, json(r#"[{"member": "post_id", "value": null}]"#));
}

#[test]
fn every_plan_runs_a_step_and_chain_is_no_disposition() {
    let none = selector_plan(
        &selected_source(""),
        r#"{"mode": "none", "step": null}"#,
        ZONES_OUT,
    )
    .expect("a plan");
    assert!(none.shown().is_none());
    let mut empty = document_with("steps", json("[]"));
    set(
        &mut empty,
        "output",
        json(r#"{"mode": "none", "step": null}"#),
    );
    assert_eq!(
        admit(&empty, &contracts()).map(|_| ()),
        Err(PlanError::new("plan-output-invalid"))
    );
    assert_eq!(
        selector_plan(
            &selected_source(""),
            r#"{"mode": "other", "step": null}"#,
            ZONES_OUT
        )
        .map(|_| ()),
        Err(PlanError::new("plan-output-invalid"))
    );
    assert!(!well_formed(&empty));
}

#[test]
fn a_shown_result_redacts_every_value_and_key_the_run_protects() {
    let node = output_safe(
        &json(r#"{"note": "has tok-1 inside", "tok-1": "x", "ok": "fine"}"#),
        &json("{}"),
        &["tok-1".to_owned()],
    )
    .expect("a safe form");
    let fields = super::output::node_fields(&node);
    let field = |name: &str| {
        fields
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.clone())
    };
    assert_eq!(field("note"), Some(output_redacted()));
    assert_eq!(field("[redacted]"), Some(output_redacted()));
    assert_eq!(
        field("ok"),
        Some(json(r#"{"kind": "text", "value": "fine", "cut": false}"#))
    );
    let plain = output_safe(&text("plain"), &json("{}"), &[String::new()]).expect("a safe form");
    assert_eq!(at(&plain, "kind"), &text("text"));
}

// --- Exact equality and the item selector (`test_routine_select.py`) -------------------------------------------------

#[test]
fn equality_is_type_sensitive_and_exact() {
    for (left, right, expected) in [
        ("true", "1", false),
        ("1", "true", false),
        ("false", "0", false),
        ("true", "true", true),
        ("1", "1", true),
        ("12345678901234567890", "12345678901234567890", true),
        ("12345678901234567890", "12345678901234567891", false),
        ("2.0", "2", true),
        ("2", "2.0", true),
        ("2.5", "2", false),
        ("1152921504606846976.0", "1152921504606846977", false),
        ("1.5", "1.5", true),
        (r#""1""#, "1", false),
        ("null", "null", true),
        ("null", "false", false),
        (r#"[1, "a"]"#, r#"[1, "a"]"#, true),
        (r#"[1, "a"]"#, r#"[1, "a", null]"#, false),
        ("[true]", "[1]", false),
        (r#"{"a": 1, "b": [2]}"#, r#"{"b": [2.0], "a": 1}"#, true),
        (r#"{"a": 1}"#, r#"{"a": 1, "b": 2}"#, false),
        (r#"{"a": 1}"#, r#"{"b": 1}"#, false),
        (r#"{"a": 1}"#, "[1]", false),
        (r#""x""#, r#""x""#, true),
    ] {
        assert_eq!(same(&json(left), &json(right)), expected, "{left} {right}");
    }
}

fn zones() -> Value {
    json(
        r#"{"result": [
            {"id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "name": "example.com", "paused": false},
            {"id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "name": "shimpz.com", "paused": true},
            {"id": "cccccccccccccccccccccccccccccccc", "name": "other.org", "rank": 7},
            "not-an-object",
            {"id": "dddddddddddddddddddddddddddddddd"}
        ]}"#,
    )
}

fn select_in(value: &Value, pointer: &str, where_: &str, item: &str) -> Result<Value, PlanError> {
    select_where(value, &text(pointer), &json(where_), &text(item))
}

#[test]
fn exactly_one_matching_item_is_selected() {
    let zones = zones();
    assert_eq!(
        select_in(&zones, "/result", r#"{"name": "shimpz.com"}"#, "/id"),
        Ok(text(&"b".repeat(32)))
    );
    assert_eq!(
        select_in(&zones, "/result", r#"{"rank": 7}"#, ""),
        Ok(elements(at(&zones, "result"))[2].clone())
    );
    assert_eq!(
        select_in(&zones, "/result", r#"{"name": "example.com"}"#, "/id"),
        Ok(text(&"a".repeat(32)))
    );
    let doubled = json(r#"{"items": [{"name": "x", "id": 1}, {"name": "x", "id": 2}]}"#);
    assert_eq!(
        select_in(&doubled, "/items", r#"{"name": "x"}"#, "/id"),
        Err(PlanError::new("plan-reference-ambiguous"))
    );
}

#[test]
fn zero_matches_and_non_matches_fail_closed() {
    let zones = zones();
    for where_ in [
        r#"{"name": "absent.com"}"#,
        r#"{"missing": "x"}"#,
        r#"{"rank": "7"}"#,
        r#"{"paused": 1}"#,
    ] {
        assert_eq!(
            select_in(&zones, "/result", where_, "/id"),
            Err(PlanError::new("plan-reference-missing")),
            "{where_}"
        );
    }
    for (pointer, item) in [
        ("/result/0", "/id"),
        ("/absent", "/id"),
        ("", "/id"),
        ("/result", "/absent"),
    ] {
        assert_eq!(
            select_in(&zones, pointer, r#"{"name": "shimpz.com"}"#, item),
            Err(PlanError::new("plan-reference-missing")),
            "{pointer} {item}"
        );
    }
}

#[test]
fn malformed_selectors_are_invalid() {
    let zones = zones();
    for (pointer, where_, item) in [
        ("/result", "{}", "/id"),
        ("/result", r#"{"name": "a", "id": "b"}"#, "/id"),
        ("/result", r#"{"paused": true}"#, "/id"),
        ("/result", r#"{"rank": 7.0}"#, "/id"),
        ("/result", r#"{"name": null}"#, "/id"),
        ("/result", r#"["name"]"#, "/id"),
        ("/result", r#"{"name": "shimpz.com"}"#, "id"),
        ("result", r#"{"name": "shimpz.com"}"#, "/id"),
    ] {
        assert_eq!(
            select_in(&zones, pointer, where_, item),
            Err(PlanError::new("plan-reference-invalid")),
            "{pointer} {where_} {item}"
        );
    }
}

#[test]
fn deep_member_walks_stay_within_the_depth_bound() {
    let deep_schema = json(r#"{"type": "object"}"#);
    let candidates =
        applicable(&deep_schema, &Sub::Node(&deep_schema), 0, Held::Unknown).expect("candidates");
    assert_eq!(candidates.len(), 1);
}
