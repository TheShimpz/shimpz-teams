//! Item selectors, exact equality, and output dispositions.

use super::*;

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
    let fields = crate::plan::output::node_fields(&node);
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
