//! Plan admission: the closed document, its steps' pinned Actions, and every value source.

use super::*;

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
    assert!(crate::plan::PIN_RE.is_full(&plan.digest));
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
        text(&"x".repeat(crate::plan::MAX_PLAN_BYTES)),
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
