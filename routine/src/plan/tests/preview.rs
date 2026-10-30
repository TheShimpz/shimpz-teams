//! What a step record shows of an attempt's inputs: redacted under every schema before any cut.

use super::*;

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
