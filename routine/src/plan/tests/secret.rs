//! A secret is never a literal: the walk of every admitted applicator from the input schema's root.

use super::*;

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
