use strict_json::Value;

use super::{Expansion, applied, expand, nodes_within, reference_target};

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("strict JSON")
}

#[test]
fn the_value_bound_counts_every_value_but_member_names() {
    let value = json(r#"{"a": [1, {"b": "c"}]}"#);
    assert!(nodes_within(&value, 5));
    assert!(!nodes_within(&value, 4));
    assert!(nodes_within(&json("1"), 1));
    assert!(!nodes_within(&json("1"), 0));
}

#[test]
fn applied_subschemas_cover_every_applicator() {
    let node = json(
        r#"{"not": {"a": 1}, "allOf": [{"b": 1}, {"c": 1}], "properties": {"x": {"d": 1}}, "const": {"e": 1},
            "anyOf": 3, "$defs": 4}"#,
    );
    let applied: Vec<&Value> = applied(&node).collect();
    assert_eq!(applied.len(), 4);
}

#[test]
fn references_name_the_root_or_one_definition() {
    let root =
        json(r#"{"$defs": {"a/b~": {"x": 1}, "c": {"y": 1}}, "definitions": {"d": {"z": 1}}}"#);
    assert_eq!(reference_target(&root, "#"), Some(&root));
    assert_eq!(
        reference_target(&root, "#/$defs/a~1b~0"),
        Some(&json(r#"{"x": 1}"#))
    );
    assert_eq!(
        reference_target(&root, "#/definitions/d"),
        Some(&json(r#"{"z": 1}"#))
    );
    assert_eq!(reference_target(&root, "#/$defs/missing"), None);
    assert_eq!(reference_target(&root, "#/definitions/c"), None);
    assert_eq!(reference_target(&root, "#/other/c"), None);
    assert_eq!(reference_target(&root, "$defs/c"), None);
    assert_eq!(reference_target(&root, "#/$defsc"), None);
}

#[test]
fn references_expand_at_every_holder() {
    // The root, its definition, and each reference with its target counted again.
    let shared = json(
        r##"{"$defs": {"a/b~": {"type": "string"}},
            "properties": {"x": {"$ref": "#/$defs/a~1b~0"}, "y": {"$ref": "#/$defs/a~1b~0"}}}"##,
    );
    assert_eq!(
        expand(&shared),
        Some(Expansion {
            subschemas: 6,
            depth: 2
        })
    );
    let leaves = json(r#"{"not": true, "items": {"not": false}}"#);
    assert_eq!(
        expand(&leaves),
        Some(Expansion {
            subschemas: 4,
            depth: 1
        })
    );
}

#[test]
fn a_missing_or_cyclic_reference_does_not_expand() {
    for schema in [
        r##"{"$ref": "#"}"##,
        r##"{"properties": {"a": {"$ref": "#"}}}"##,
        r##"{"$defs": {"a": {"not": {"$ref": "#/definitions/b"}}}, "definitions": {"b": {"allOf": [{"$ref": "#/$defs/a"}]}}}"##,
        r##"{"properties": {"a": {"$ref": "#/$defs/missing"}}}"##,
    ] {
        assert_eq!(expand(&json(schema)), None, "{schema}");
    }
}

#[test]
fn a_non_string_reference_expands_nothing() {
    assert_eq!(
        expand(&json(r#"{"$ref": 5}"#)),
        Some(Expansion {
            subschemas: 1,
            depth: 0
        })
    );
}
