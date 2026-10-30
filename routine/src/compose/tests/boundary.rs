//! What a recording replays and refuses at its bounds: every work call in order, a run's own verification, a kept
//! replacement, and a question's exact targets.

use strict_json::Value;

use super::{
    DELETE_IN, DRIFTED_PIN, EVERY_HOUR, PIN, Span, actions, call, call_value, contracts, hourly,
    last_input, records, send, zones_call,
};
use crate::json::{as_str, at, json_text, object, text};
use crate::plan::{ActionContract, MAX_RESOLVED_INPUT_BYTES, MAX_RETAINED_BYTES, VERSION};
use crate::recording::{Existing, Question, Target};
use crate::testing::json;
use crate::trace::{MAX_KEPT_BYTES, Protection};

/// The plan a replacement keeps: a zone listing and its records, selected by name.
pub(super) fn kept_plan() -> Value {
    json(&format!(
        r#"{{"version": {VERSION}, "timezone": "UTC", "steps": [
            {{"id": "s1", "assistant": "cloudflare", "action": "list-zones", "pin": "{PIN}", "input": {{}}}},
            {{"id": "s2", "assistant": "cloudflare", "action": "list-dns-records", "pin": "{PIN}", "input": {{
                "zone_id": {{"kind": "step_output", "step": "s1", "pointer": "/result", "where": {{"name": "a.com"}}, "item": "/id"}},
                "type": {{"kind": "literal", "value": "MX"}},
                "per_page": {{"kind": "literal", "value": 50}}}}}}],
            "output": {{"mode": "show", "step": "s2"}}}}"#
    ))
}

#[test]
fn every_work_call_replays_in_order() {
    let message = format!("shimpz.com\n{EVERY_HOUR}");
    let calls = vec![
        call("reports/fetch", "{}", r#"{"unused": 1}"#),
        zones_call(),
        call("reports/post", r#"{"text": "hello"}"#, r#"{"ok": true}"#),
        call("reports/fetch", r#"{"q": 1}"#, "{}"),
        records(),
    ];
    let recorded = Span {
        mode: Some("changes"),
        ..Span::of(vec![send(calls, &message)])
    }
    .recorded();
    assert_eq!(
        actions(&recorded),
        ["fetch", "list-zones", "post", "fetch", "list-dns-records"]
    );
    assert_eq!(
        at(at(last_input(&recorded), "zone_id"), "step"),
        &text("s2")
    );
    assert_eq!(
        at(&recorded.document, "output"),
        &json(r#"{"mode": "changes", "step": "s5"}"#)
    );
    let quiet = Span {
        mode: Some("none"),
        ..Span::of(vec![hourly(vec![call("reports/fetch", "{}", "{}")])])
    }
    .recorded();
    assert_eq!(
        at(&quiet.document, "output"),
        &json(r#"{"mode": "none", "step": null}"#)
    );
}

#[test]
fn a_recording_needs_a_call_and_current_contracts() {
    assert_eq!(
        Span::of(vec![hourly(Vec::new())]).code(),
        "routine-recording-empty"
    );
    let work = hourly(vec![
        call("reports/fetch", "{}", "{}"),
        call("cloudflare/delete-dns-record", r#"{"zone_id": "z"}"#, "{}"),
    ]);
    let mut drifted = contracts();
    drifted.insert(
        ("cloudflare".to_owned(), "delete-dns-record".to_owned()),
        ActionContract::new(DRIFTED_PIN.to_owned(), json(DELETE_IN)),
    );
    assert_eq!(
        Span {
            contracts: drifted,
            ..Span::of(vec![work.clone()])
        }
        .code(),
        "plan-pin-drift"
    );
    let mut missing = contracts();
    missing.remove(&("reports".to_owned(), "fetch".to_owned()));
    assert_eq!(
        Span {
            contracts: missing,
            ..Span::of(vec![work])
        }
        .code(),
        "plan-pin-drift"
    );
}

#[test]
fn an_input_past_the_dispatch_bound_refuses_instead_of_failing_every_run() {
    // Within what a recording keeps (256 KiB), past what a run dispatches (128 KiB): no card, never a dead Routine.
    let large = "word ".repeat(MAX_RESOLVED_INPUT_BYTES / 5 + 1);
    assert!(large.len() < MAX_KEPT_BYTES);
    let post = call_value(
        "reports/post",
        &object(vec![("text", text(&large))]),
        &json("{}"),
    );
    assert_eq!(
        Span::of(vec![hourly(vec![post])]).code(),
        "routine-recording-too-large"
    );
    let fits = call_value(
        "reports/post",
        &object(vec![(
            "text",
            text(&"word ".repeat(MAX_RESOLVED_INPUT_BYTES / 10)),
        )]),
        &json("{}"),
    );
    assert_eq!(
        actions(&Span::of(vec![hourly(vec![fits])]).recorded()),
        ["post"]
    );
}

#[test]
fn selections_past_what_a_run_retains_refuse() {
    // Each input alone dispatches, but together a run could not keep what later steps select.
    let half = MAX_RETAINED_BYTES / 2 + 1024;
    let (first, second) = ("a".repeat(half), "b".repeat(half));
    let fetch = call_value(
        "reports/fetch",
        &json("{}"),
        &object(vec![("first", text(&first)), ("second", text(&second))]),
    );
    let posts = [("a", &first), ("b", &second)].map(|(member, value)| {
        call_value(
            "reports/post",
            &object(vec![(member, text(value))]),
            &json("{}"),
        )
    });
    let calls = vec![fetch, posts[0].clone(), posts[1].clone()];
    assert_eq!(
        Span::of(vec![hourly(calls)]).code(),
        "routine-recording-too-large"
    );
}

#[test]
fn a_dated_text_is_verified_in_the_routines_own_zone() {
    // 01:00 UTC on 6 October is still 5 October in Sao Paulo, where the send wrote its date and each run reads it.
    let mut dated = hourly(vec![call(
        "reports/post",
        r#"{"text": "Resumo de 05/10/2026"}"#,
        "{}",
    )]);
    dated.started_at = crate::testing::epoch(2026, 10, 6, 1, 0, 0);
    let recorded = Span::of(vec![dated]).recorded();
    assert_eq!(
        at(&recorded.document, "timezone"),
        &text("America/Sao_Paulo")
    );
    assert_eq!(
        at(at(last_input(&recorded), "text"), "kind"),
        &text("run_clock")
    );
}

fn keep_span(plan: Value, message: &str) -> Span {
    let existing = Existing {
        plan,
        schedule: json(r#"{"kind": "daily", "time": "08:00"}"#),
    };
    Span {
        mode: Some("changes"),
        existing: Some(existing),
        ..Span::of(vec![send(Vec::new(), message)])
    }
}

#[test]
fn a_replacement_that_ran_no_action_keeps_the_steps_and_changes_only_how_and_when_they_run() {
    let kept = keep_span(kept_plan(), "now with 50 per page").recorded();
    assert_eq!(at(&kept.document, "steps"), at(&kept_plan(), "steps"));
    assert_eq!(
        (at(&kept.document, "timezone"), &kept.schedule),
        (
            &text("America/Sao_Paulo"),
            &json(r#"{"kind": "daily", "time": "08:00"}"#)
        )
    );
    assert_eq!(
        at(&kept.document, "output"),
        &json(r#"{"mode": "changes", "step": "s2"}"#)
    );
    let s2: Vec<(String, String)> = ["zone_id", "type", "per_page"]
        .iter()
        .zip(["selector", "assistant", "request"])
        .map(|(member, origin)| ((*member).to_owned(), origin.to_owned()))
        .collect();
    assert_eq!(
        kept.origins,
        [("s1".to_owned(), Vec::new()), ("s2".to_owned(), s2)]
    );
    let permitted: Vec<&str> = kept
        .permitted
        .iter()
        .filter_map(|item| as_str(at(item, "action")))
        .collect();
    assert_eq!(permitted, ["list-dns-records", "list-zones"]);
    assert_eq!(
        keep_span(kept_plan(), "a cada hora").recorded().schedule,
        json(r#"{"kind": "continuous", "gap": 3600}"#)
    );
}

#[test]
fn a_kept_reference_and_run_date_keep_their_origins() {
    let mut plain = kept_plan();
    let mut steps = crate::json::elements(at(&plain, "steps")).to_vec();
    crate::json::set(
        &mut steps[1],
        "input",
        json(
            r#"{"zone_id": {"kind": "step_output", "step": "s1", "pointer": "/zone"}, "day": {"kind": "run_clock", "format": "date"}}"#,
        ),
    );
    crate::json::set(&mut plain, "steps", Value::Array(steps));
    let kept = keep_span(plain, "now").recorded();
    assert_eq!(
        (kept.origin("s2", "zone_id"), kept.origin("s2", "day")),
        (Some("step"), Some("clock"))
    );
}

#[test]
fn a_lost_protection_or_a_drifted_pin_refuses_a_kept_plan() {
    let lost = Span {
        protection: Protection {
            lost: true,
            ..Protection::default()
        },
        ..keep_span(kept_plan(), "now")
    };
    assert_eq!(lost.code(), "routine-recording-unavailable");
    let mut drifted = kept_plan();
    let mut first = crate::json::elements(at(&drifted, "steps"))[0].clone();
    crate::json::set(&mut first, "pin", text(DRIFTED_PIN));
    crate::json::set(&mut drifted, "steps", Value::Array(vec![first]));
    assert_eq!(keep_span(drifted, "now").code(), "plan-pin-drift");
}

#[test]
fn a_question_carries_each_target_as_its_exact_json_text() {
    let big = json("9007199254740993");
    let question = Question::new(
        "routine-binding-ambiguous",
        vec![
            Target::new(text("a"), None),
            Target::new(big.clone(), Some("big")),
        ],
    );
    let wire = question.wire();
    let expected = object(vec![
        ("code", text("routine-binding-ambiguous")),
        (
            "options",
            Value::Array(vec![
                object(vec![
                    ("value", text(&json_text(&text("a")))),
                    ("label", Value::Null),
                ]),
                object(vec![
                    ("value", text("9007199254740993")),
                    ("label", text("big")),
                ]),
            ]),
        ),
        ("value", Value::Null),
    ]);
    assert_eq!(wire, expected);
    assert_eq!(
        team_protocol::routine_proposal::canonical_question(&wire),
        Some(&wire)
    );
}

#[test]
fn more_targets_than_a_question_shows_offer_none() {
    let items: Vec<Value> = (0..9)
        .map(|index| {
            object(vec![
                ("name", text("beta")),
                ("id", text(&format!("id-{index:04}"))),
            ])
        })
        .collect();
    let fetch = call_value(
        "reports/fetch",
        &json("{}"),
        &object(vec![("items", Value::Array(items))]),
    );
    let reading = |zone: &str| {
        call(
            "cloudflare/list-dns-records",
            &format!(r#"{{"zone_id": "{zone}"}}"#),
            "{}",
        )
    };
    let message = format!("beta\n{EVERY_HOUR}");
    let empty = Question::new("routine-binding-ambiguous", Vec::new());
    assert_eq!(
        Span::of(vec![send(vec![fetch, reading("id-0003")], &message)]).question(),
        empty
    );
    let (long_x, long_y) = ("x".repeat(200), "y".repeat(200));
    let unshowable = format!(
        r#"{{"items": [{{"name": "beta", "id": "{long_x}"}}, {{"name": "beta", "id": "{long_y}"}}]}}"#
    );
    let calls = vec![call("reports/fetch", "{}", &unshowable), reading(&long_x)];
    assert_eq!(Span::of(vec![send(calls, &message)]).question(), empty);
}
