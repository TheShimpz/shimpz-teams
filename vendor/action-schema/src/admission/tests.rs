use strict_json::Value;

use super::{Refusal, admit_schema, closed, node_admitted, permits_object};

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("strict JSON")
}

fn admit(text: &str) -> Result<(), Refusal> {
    admit_schema(&json(text))
}

const CLOSED: &str = r#""type": "object", "additionalProperties": false"#;

fn closed_with(rest: &str) -> String {
    format!("{{{CLOSED}, {rest}}}")
}

#[test]
fn each_rule_refuses_with_its_reason() {
    let cases = [
        (r#"{"type": "string"}"#.to_owned(), Refusal::NotAnObject),
        (r"[1]".to_owned(), Refusal::NotAnObject),
        (closed_with(r#""minLength": -1"#), Refusal::Invalid),
        (
            closed_with(r##""properties": {"a": {"$ref": "#/default"}}"##),
            Refusal::Reference,
        ),
        (
            closed_with(r##""properties": {"a": {"$dynamicRef": "#a"}}"##),
            Refusal::Reference,
        ),
        (
            closed_with(r#""$schema": "http://json-schema.org/draft-07/schema#""#),
            Refusal::Dialect,
        ),
        (
            closed_with(r#""properties": {"a": {"$id": "a.json"}}"#),
            Refusal::NestedIdentifier,
        ),
        (
            closed_with(r#""unevaluatedProperties": false"#),
            Refusal::UnevaluatedProperties,
        ),
        (
            closed_with(r#""properties": {"a": {"pattern": "a{01}"}}"#),
            Refusal::Pattern,
        ),
        (
            closed_with(r#""patternProperties": {"\\pL": {}}"#),
            Refusal::Pattern,
        ),
        (
            closed_with(r##""properties": {"a": {"$ref": "#"}}"##),
            Refusal::Cycle,
        ),
        (
            closed_with(r#""properties": {"a": {"type": "object"}}"#),
            Refusal::Open,
        ),
        (
            closed_with(r#""properties": {"a": true}"#),
            Refusal::BooleanSubschema,
        ),
    ];
    for (schema, refusal) in cases {
        assert_eq!(admit(&schema), Err(refusal), "{schema}");
    }
}

#[test]
fn the_byte_bound_counts_ascii_escapes() {
    let base = crate::size::encoded_len(&json(&closed_with(r#""description": """#)));
    let fill = crate::MAX_BYTES - base;
    let at_limit = closed_with(&format!(r#""description": "{}""#, "a".repeat(fill)));
    assert_eq!(crate::size::encoded_len(&json(&at_limit)), crate::MAX_BYTES);
    assert_eq!(admit(&at_limit), Ok(()));
    let over = closed_with(&format!(r#""description": "{}""#, "a".repeat(fill + 1)));
    assert_eq!(admit(&over), Err(Refusal::TooLarge));
    let escaped = closed_with(&format!(r#""description": "{}""#, "é".repeat(fill / 6 + 1)));
    assert_eq!(admit(&escaped), Err(Refusal::TooLarge));
}

#[test]
fn the_value_bound_counts_every_annotation() {
    // The root, `type`, `additionalProperties`, and the `enum` array hold four values.
    let values = vec!["0"; crate::MAX_NODES - 4].join(",");
    assert_eq!(
        admit(&closed_with(&format!(r#""enum": [{values}]"#))),
        Ok(())
    );
    let more = vec!["0"; crate::MAX_NODES - 3].join(",");
    assert_eq!(
        admit(&closed_with(&format!(r#""enum": [{more}]"#))),
        Err(Refusal::TooLarge)
    );
}

fn doubling(levels: usize) -> String {
    let mut definitions = vec![r#""d0": {"type": "string"}"#.to_owned()];
    for level in 1..=levels {
        let previous = level - 1;
        definitions.push(format!(
            r##""d{level}": {{"allOf": [{{"$ref": "#/$defs/d{previous}"}}, {{"$ref": "#/$defs/d{previous}"}}]}}"##
        ));
    }
    closed_with(&format!(
        r##""$defs": {{{}}}, "properties": {{"a": {{"$ref": "#/$defs/d{levels}"}}}}"##,
        definitions.join(", ")
    ))
}

#[test]
fn references_expand_within_their_count_and_depth() {
    assert_eq!(admit(&doubling(8)), Ok(()));
    assert_eq!(admit(&doubling(30)), Err(Refusal::Expanded));
    let mut chain = vec![r#""d15": {"type": "string"}"#.to_owned()];
    for link in 0..15 {
        let next = link + 1;
        chain.push(format!(
            r##""d{link}": {{"allOf": [{{"$ref": "#/$defs/d{next}"}}]}}"##
        ));
    }
    let defs = chain.join(", ");
    let reach = |first: &str| {
        closed_with(&format!(
            r#""$defs": {{{defs}}}, "properties": {{"a": {first}}}"#
        ))
    };
    assert_eq!(admit(&reach(r##"{"$ref": "#/$defs/d0"}"##)), Ok(()));
    assert_eq!(
        admit(&reach(r##"{"allOf": [{"$ref": "#/$defs/d0"}]}"##)),
        Err(Refusal::TooDeep)
    );
}

#[test]
fn structural_nesting_and_legacy_dependencies_are_bounded() {
    let nested = |levels: usize| {
        let mut inner = r#"{"type": "string"}"#.to_owned();
        for _ in 0..levels {
            inner = format!(r#"{{"not": {inner}}}"#);
        }
        inner
    };
    assert_eq!(
        admit(&closed_with(&format!(
            r#""properties": {{"a": {}}}"#,
            nested(31)
        ))),
        Ok(())
    );
    assert_eq!(
        admit(&closed_with(&format!(
            r#""properties": {{"a": {}}}"#,
            nested(32)
        ))),
        Err(Refusal::TooDeep)
    );
    assert_eq!(
        admit(&closed_with(&format!(
            r#""dependencies": {{"a": {}}}"#,
            nested(31)
        ))),
        Ok(())
    );
    assert_eq!(
        admit(&closed_with(&format!(
            r#""dependencies": {{"a": {}}}"#,
            nested(32)
        ))),
        Err(Refusal::TooDeep)
    );
    // A legacy dependency's references and identifiers are never followed, only its patterns.
    assert_eq!(
        admit(&closed_with(
            r#""dependencies": {"a": {"$id": "x.json", "$ref": "https://e.com"}}"#
        )),
        Ok(())
    );
    assert_eq!(
        admit(&closed_with(
            r#""dependencies": {"a": {"pattern": "a{01}"}}"#
        )),
        Err(Refusal::Pattern)
    );
}

#[test]
fn references_name_the_root_or_one_definition() {
    for reference in ["#", "#/$defs/a", "#/definitions/a~1b"] {
        let node = json(&format!(r#"{{"$ref": "{reference}"}}"#));
        assert_eq!(node_admitted(&node, true), Ok(()), "{reference}");
    }
    for reference in [
        "#/$defs/",
        "#/$defs/a/b",
        "#/$defs/a%2F",
        "#/properties/a",
        "other.json#",
    ] {
        let node = json(&format!(r#"{{"$ref": "{reference}"}}"#));
        assert_eq!(
            node_admitted(&node, true),
            Err(Refusal::Reference),
            "{reference}"
        );
    }
    assert_eq!(
        node_admitted(&json(r#"{"$ref": 5}"#), false),
        Err(Refusal::Reference)
    );
    assert_eq!(node_admitted(&json(r#"{"$id": "a.json"}"#), false), Ok(()));
    let dialect = r#"{"$schema": "https://json-schema.org/draft/2020-12/schema"}"#;
    assert_eq!(node_admitted(&json(dialect), true), Ok(()));
}

#[test]
fn every_closed_position_holds_a_closed_object() {
    assert_eq!(closed(&json(r#"{"not": null}"#)), Ok(()));
    assert_eq!(closed(&json(r#"{"not": 3}"#)), Err(Refusal::Invalid));
    assert_eq!(
        closed(&json(r#"{"items": false}"#)),
        Err(Refusal::BooleanSubschema)
    );
    assert_eq!(
        closed(&json(
            r#"{"additionalProperties": false, "propertyNames": {}}"#
        )),
        Ok(())
    );
    assert_eq!(
        closed(&json(r#"{"anyOf": [{"type": ["null", "object"]}]}"#)),
        Err(Refusal::Open)
    );
    assert_eq!(
        closed(&json(r#"{"$defs": {"a": {"required": ["x"]}}}"#)),
        Err(Refusal::Open)
    );
}

#[test]
fn a_subschema_permits_an_object_by_type_or_keyword() {
    assert!(permits_object(&json(r#"{"type": "object"}"#)));
    assert!(!permits_object(&json(r#"{"type": "string"}"#)));
    assert!(permits_object(&json(r#"{"type": ["string", "object"]}"#)));
    assert!(!permits_object(&json(r#"{"type": ["string"]}"#)));
    assert!(permits_object(&json(
        r#"{"type": null, "maxProperties": 1}"#
    )));
    assert!(!permits_object(&json(r#"{"minLength": 1}"#)));
    assert!(!permits_object(&json(r#"{"type": 3}"#)));
}

/// `count` properties, each with the case-folded wide class under its own capture name, or all under one name when
/// `distinct` is false.
fn wide_properties(count: usize, distinct: bool) -> String {
    let properties: Vec<String> = (0..count)
        .map(|index| {
            let name = if distinct { index } else { 0 };
            format!("\"p{index}\": {{\"pattern\": \"(?P<p{name}>(?i:[A-\\uffff]))?\"}}")
        })
        .collect();
    format!("\"properties\": {{{}}}", properties.join(", "))
}

#[test]
fn the_distinct_patterns_prepare_within_half_the_matching_budget() {
    // Each such pattern prepares about 4.3 million units: 15 fit 2^26, 16 do not.
    assert_eq!(admit(&closed_with(&wide_properties(15, true))), Ok(()));
    assert_eq!(
        admit(&closed_with(&wide_properties(16, true))),
        Err(Refusal::Preparation)
    );
    // One pattern is prepared once however many positions hold it.
    assert_eq!(admit(&closed_with(&wide_properties(512, false))), Ok(()));
    // A legacy dependency's pattern is prepared like any other.
    let legacy = format!(
        r#"{}, "dependencies": {{"a": {{"pattern": "(?i:[A-￿])"}}}}"#,
        wide_properties(15, true)
    );
    assert_eq!(admit(&closed_with(&legacy)), Err(Refusal::Preparation));
}
