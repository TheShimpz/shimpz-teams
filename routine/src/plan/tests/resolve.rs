//! Pointers, retained selections, and the resolution of each step's input before dispatch.

use super::*;

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
