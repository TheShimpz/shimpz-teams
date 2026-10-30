//! Which occurrence a copied value comes from, how an array item is selected, and the secrets no plan holds.

use std::collections::BTreeMap;

use strict_json::Value;

use super::{
    EVERY_HOUR, PIN, SHIMPZ_ID, Span, actions, call, call_kept, call_value, call_with_input,
    contracts, hourly, input_of, last_input, owner, records, send, then_hourly, zones_call,
};
use crate::compose::{source_classes, verify};
use crate::json::{at, object, text};
use crate::plan::{ActionContract, MAX_POINTER, MAX_SECRET_DEPTH};
use crate::recording::{Context, Question, Target, known_texts, span_calls};
use crate::testing::json;
use crate::trace::{Kept, Protection};

fn selecting(value: &str, known: &str, earlier: &str) -> Span {
    let calls = vec![
        call("reports/fetch", "{}", earlier),
        call_value(
            "cloudflare/list-dns-records",
            &object(vec![("zone_id", text(value))]),
            &json("{}"),
        ),
    ];
    Span::of(vec![send(calls, &then_hourly(known))])
}

fn selector_of(span: &Span) -> Value {
    at(last_input(&span.recorded()), "zone_id").clone()
}

#[test]
fn one_array_reads_only_by_a_named_unique_member() {
    let items = r#"{"items": [{"name": "alpha", "id": "id-0001"}, {"name": "beta", "id": "id-0002", "rank": 70}]}"#;
    let source = json(
        r#"{"kind": "step_output", "step": "s1", "pointer": "/items", "where": {"name": "beta"}, "item": "/id"}"#,
    );
    let recorded = selecting("id-0002", "use beta", items).recorded();
    assert_eq!(
        (
            at(last_input(&recorded), "zone_id"),
            recorded.origin("s2", "zone_id")
        ),
        (&source, Some("selector"))
    );
    assert_eq!(
        at(
            &selector_of(&selecting("id-0002", "rank 70", items)),
            "where"
        ),
        &json(r#"{"rank": 70}"#)
    );
    // Two named members, each unique: the name-like one selects the item, in whatever order the person wrote them.
    for known in ["beta rank 70", "rank 70 beta"] {
        assert_eq!(
            at(&selector_of(&selecting("id-0002", known, items)), "where"),
            &json(r#"{"name": "beta"}"#)
        );
    }
    for (known, earlier) in [
        ("nothing named", items),
        ("b", items),
        ("rank -70", items),
        ("x", r#"{"items": ["id-0002"]}"#),
        ("true", r#"{"items": [{"flag": true, "id": "id-0002"}]}"#),
        ("2.5", r#"{"items": [{"score": 2.5, "id": "id-0002"}]}"#),
    ] {
        assert_eq!(
            selecting("id-0002", known, earlier).question().code,
            "routine-binding-ambiguous",
            "{known} {earlier}"
        );
    }
}

#[test]
fn an_array_item_nothing_separates_is_asked_about() {
    let shared =
        r#"{"items": [{"name": "beta", "id": "id-0001"}, {"name": "beta", "id": "id-0002"}]}"#;
    let targets = vec![
        Target::new(text("id-0001"), Some("beta")),
        Target::new(text("id-0002"), Some("beta")),
    ];
    assert_eq!(
        selecting("id-0002", "beta", shared).question(),
        Question::new("routine-binding-ambiguous", targets)
    );
    // Two unique name-like members, or two unique members of which none is name-like: nothing separates them.
    let titled = r#"{"items": [{"name": "alpha", "title": "First", "id": "id-0001"}, {"name": "beta", "title": "Second", "id": "id-0002"}]}"#;
    assert_eq!(
        selecting("id-0002", "beta Second", titled).question().code,
        "routine-binding-ambiguous"
    );
    let ranked = r#"{"items": [{"rank": 1, "size": 10, "id": "id-0001"}, {"rank": 70, "size": 2000, "id": "id-0002"}]}"#;
    assert_eq!(
        selecting("id-0002", "rank 70 size 2000", ranked)
            .question()
            .code,
        "routine-binding-ambiguous"
    );
}

#[test]
fn the_selected_member_itself_is_never_its_own_key() {
    let value = json(r#"{"name": "beta", "id": "id-0002"}"#);
    let calls = vec![
        call(
            "reports/fetch",
            "{}",
            r#"{"items": [{"name": "alpha"}, {"name": "beta", "id": "id-0002"}]}"#,
        ),
        call_value(
            "cloudflare/list-dns-records",
            &object(vec![("zone_id", text("z")), ("match", value)]),
            &json("{}"),
        ),
    ];
    let recorded = Span::of(vec![send(calls, &then_hourly("beta"))]).recorded();
    let source = at(last_input(&recorded), "match");
    assert_eq!(
        (at(source, "where"), at(source, "item")),
        (&json(r#"{"name": "beta"}"#), &text(""))
    );
}

#[test]
fn a_withheld_sibling_could_equal_the_name_so_it_never_selects() {
    let withheld_member = Kept {
        value: json(
            r#"{"items": [{"name": null, "id": "id-0001"}, {"name": "beta", "id": "id-0002"}]}"#,
        ),
        withheld: ["/items/0/name".to_owned()].into(),
        oversize: false,
    };
    let withheld_item = Kept {
        value: json(r#"{"items": [null, {"name": "beta", "id": "id-0002"}]}"#),
        withheld: ["/items/0".to_owned()].into(),
        oversize: false,
    };
    for kept in [withheld_member, withheld_item] {
        let calls = vec![
            call_kept("reports/fetch", &json("{}"), kept),
            call(
                "cloudflare/list-dns-records",
                r#"{"zone_id": "id-0002"}"#,
                "{}",
            ),
        ];
        let question = Span::of(vec![send(calls, &format!("beta\n{EVERY_HOUR}"))]).question();
        assert_eq!(question.code, "routine-binding-ambiguous");
    }
}

#[test]
fn a_value_a_change_returned_binds_to_it_and_still_replays_where_it_ran() {
    let calls = [
        call("reports/post", "{}", r#"{"post_id": "post-12345"}"#),
        call(
            "cloudflare/list-dns-records",
            r#"{"zone_id": "post-12345"}"#,
            "{}",
        ),
    ];
    let recorded = Span::of(vec![hourly(calls.to_vec())]).recorded();
    assert_eq!(actions(&recorded), ["post", "list-dns-records"]);
    assert_eq!(
        at(last_input(&recorded), "zone_id"),
        &json(r#"{"kind": "step_output", "step": "s1", "pointer": "/post_id"}"#)
    );
    // In an earlier send, the change still replays as the work's source, where it ran.
    let earlier = Span::of(vec![
        hourly(vec![calls[0].clone()]),
        hourly(vec![calls[1].clone()]),
    ])
    .recorded();
    assert_eq!(actions(&earlier), ["post", "list-dns-records"]);
}

#[test]
fn identical_reads_with_no_change_between_are_one_source_and_replay_once() {
    let message = format!("shimpz.com\n{EVERY_HOUR}");
    let recorded = Span::of(vec![send(
        vec![zones_call(), zones_call(), records()],
        &message,
    )])
    .recorded();
    assert_eq!(actions(&recorded), ["list-zones", "list-dns-records"]);
    // Reads equal as JSON numbers are one source.
    let calls = vec![
        call(
            "reports/fetch",
            r#"{"page": 1}"#,
            r#"{"id": "zone-12345", "count": 2}"#,
        ),
        call(
            "reports/fetch",
            r#"{"page": 1.0}"#,
            r#"{"id": "zone-12345", "count": 2.0}"#,
        ),
        call("reports/post", r#"{"z": "zone-12345"}"#, "{}"),
    ];
    let recorded = Span::of(vec![hourly(calls)]).recorded();
    assert_eq!(actions(&recorded), ["fetch", "post"]);
    assert_eq!(
        at(last_input(&recorded), "z"),
        &json(r#"{"kind": "step_output", "step": "s1", "pointer": "/id"}"#)
    );
}

#[test]
fn reads_of_different_results_or_across_a_change_stay_distinct_sources() {
    let calls = vec![
        call("reports/fetch", "{}", r#"{"id": "old-id-123"}"#),
        call("reports/fetch", "{}", r#"{"id": "new-id-456"}"#),
        call("reports/post", r#"{"a": "old-id-123"}"#, "{}"),
        call("reports/post", r#"{"b": "new-id-456"}"#, "{}"),
    ];
    let recorded = Span::of(vec![hourly(calls)]).recorded();
    assert_eq!(actions(&recorded), ["fetch", "fetch", "post", "post"]);
    assert_eq!(
        (
            at(at(input_of(&recorded, 2), "a"), "step"),
            at(at(input_of(&recorded, 3), "b"), "step")
        ),
        (&text("s1"), &text("s2"))
    );
    // Each read holds the zone, and a change may have changed what the first one read: the person is asked.
    let separated = vec![
        zones_call(),
        call("reports/post", "{}", "{}"),
        zones_call(),
        records(),
    ];
    let question = Span::of(vec![send(separated, "shimpz.com\na cada hora")]).question();
    assert_eq!(
        question,
        Question::new(
            "routine-binding-ambiguous",
            vec![Target::new(text(SHIMPZ_ID), None)]
        )
    );
}

#[test]
fn an_order_that_would_move_a_read_across_a_change_refuses() {
    let conflict = vec![
        call("reports/post", r#"{"name": "renamed-1"}"#, "{}"),
        call("reports/fetch", "{}", r#"{"name": "renamed-1"}"#),
    ];
    assert_eq!(
        Span::of(vec![hourly(conflict)]).code(),
        "routine-recording-conflict"
    );
    let cyclic = vec![
        call(
            "reports/fetch",
            r#"{"x": "value-one"}"#,
            r#"{"y": "value-two"}"#,
        ),
        call(
            "reports/fetch",
            r#"{"y": "value-two"}"#,
            r#"{"x": "value-one"}"#,
        ),
    ];
    assert_eq!(
        Span::of(vec![hourly(cyclic)]).code(),
        "routine-recording-cyclic"
    );
}

#[test]
fn the_latest_send_that_ran_actions_is_the_whole_work() {
    let reading = |index: usize| {
        call(
            "cloudflare/list-dns-records",
            &format!(r#"{{"zone_id": "{}"}}"#, super::zone_id(index)),
            "{}",
        )
    };
    let named = "DNS de example.com e other.org a cada hora";
    let latest = Span::of(vec![
        send(vec![zones_call(), reading(0)], named),
        hourly(vec![reading(1)]),
    ])
    .recorded();
    assert_eq!(actions(&latest), ["list-zones", "list-dns-records"]);
    let whole = Span::of(vec![
        send(vec![zones_call(), reading(0)], named),
        hourly(vec![reading(0), reading(1)]),
    ])
    .recorded();
    assert_eq!(
        actions(&whole),
        ["list-zones", "list-dns-records", "list-dns-records"]
    );
    // An earlier identical call the work ran again is one source.
    let again = call(
        "cloudflare/list-dns-records",
        &format!(r#"{{"zone_id": "{SHIMPZ_ID}"}}"#),
        r#"{"result": [{"type": "MX"}]}"#,
    );
    let recorded = Span::of(vec![
        send(vec![zones_call(), records()], "shimpz.com"),
        hourly(vec![zones_call(), again]),
    ])
    .recorded();
    assert_eq!(actions(&recorded), ["list-zones", "list-dns-records"]);
}

#[test]
fn a_named_member_separates_one_position_and_a_non_scalar_target_is_never_offered() {
    let message = format!("beta\n{EVERY_HOUR}");
    let reading = call(
        "cloudflare/list-dns-records",
        r#"{"zone_id": "id-0002"}"#,
        "{}",
    );
    let result = r#"{"items": [{"name": "beta", "id": "id-0002"}], "default": "id-0002"}"#;
    let recorded = Span::of(vec![send(
        vec![call("reports/fetch", "{}", result), reading.clone()],
        &message,
    )])
    .recorded();
    assert_eq!(
        at(at(last_input(&recorded), "zone_id"), "where"),
        &json(r#"{"name": "beta"}"#)
    );
    let mixed = r#"{"items": [{"name": "beta", "id": true}, {"name": "beta"}, {"name": "beta", "id": "id-0002"}]}"#;
    let question = Span::of(vec![send(
        vec![call("reports/fetch", "{}", mixed), reading],
        &message,
    )])
    .question();
    assert_eq!(
        question.options,
        [Target::new(text("id-0002"), Some("beta"))]
    );
}

/// A span's context with its classes and classified inputs, as the recorder leaves them before verification.
fn planned_context(span: &Span) -> Context<'_> {
    let calls = span_calls(&span.sends, &span.contracts).expect("calls");
    let texts: Vec<String> = span
        .sends
        .iter()
        .flat_map(|send| send.person.clone())
        .collect();
    let mut context = Context::new(
        &span.sends,
        calls,
        known_texts(&texts, &span.protection),
        crate::recording::zone(&span.sends),
        None,
        &span.contracts,
    );
    source_classes(&mut context);
    context
}

#[test]
fn a_plan_that_does_not_resend_what_each_call_sent_is_unverified() {
    // Were two reads of different input one source, the later one's input would not be the step's.
    let span = Span::of(vec![hourly(vec![
        call("reports/fetch", "{}", r#"{"a": 1}"#),
        call("reports/fetch", r#"{"extra": 1}"#, r#"{"a": 1}"#),
    ])]);
    let mut context = planned_context(&span);
    context.classes.insert(1, 0);
    let nodes = BTreeMap::from([(0, context.calls[0].clone())]);
    let document = super::super::document(
        vec![json(&format!(
            r#"{{"id": "s1", "assistant": "reports", "action": "fetch", "pin": "{PIN}", "input": {{}}}}"#
        ))],
        "s1",
        "show",
        "America/Sao_Paulo",
    );
    let names = BTreeMap::from([(0, "s1".to_owned())]);
    assert_eq!(
        verify(&context, &nodes, &document, &names).map_err(|error| error.code),
        Err("routine-recording-unverified")
    );
    // A reference no recorded result can resolve is unverified too.
    let owner_span = Span::of(vec![send(vec![zones_call(), records()], &owner())]);
    let context = planned_context(&owner_span);
    let recorded = owner_span.recorded();
    let nodes = BTreeMap::from([(0, context.calls[0].clone()), (1, context.calls[1].clone())]);
    let names = BTreeMap::from([(0, "s1".to_owned()), (1, "s2".to_owned())]);
    let mut wrong = recorded.document.clone();
    let mut steps = crate::json::elements(at(&wrong, "steps")).to_vec();
    crate::json::set(
        &mut steps[1],
        "input",
        json(r#"{"zone_id": {"kind": "step_output", "step": "s1", "pointer": "/missing"}}"#),
    );
    crate::json::set(&mut wrong, "steps", Value::Array(steps));
    assert_eq!(
        verify(&context, &nodes, &wrong, &names).map_err(|error| error.code),
        Err("routine-recording-unverified")
    );
    assert_eq!(verify(&context, &nodes, &recorded.document, &names), Ok(()));
}

#[test]
fn secret_values_and_destinations_refuse_the_recording() {
    let credential = format!("sk-{}", "A1b2C3d4".repeat(6));
    let withheld = Kept {
        value: json(r#"{"zone_id": null}"#),
        withheld: ["/zone_id".to_owned()].into(),
        oversize: false,
    };
    let whole = Kept {
        value: Value::Null,
        withheld: [String::new()].into(),
        oversize: false,
    };
    for occurrence in [
        call_value(
            "cloudflare/list-dns-records",
            &object(vec![("zone_id", text(&credential))]),
            &json("{}"),
        ),
        call(
            "cloudflare/delete-dns-record",
            r#"{"zone_id": "z", "api_token": "plain"}"#,
            "{}",
        ),
        call_with_input("cloudflare/list-dns-records", withheld, "{}"),
        call_with_input("cloudflare/list-dns-records", whole, "{}"),
    ] {
        assert_eq!(
            Span::of(vec![hourly(vec![occurrence])]).code(),
            "routine-secret-literal"
        );
    }
    // A value the span protects never enters the plan.
    let protection = Protection::default().grow(["tok-secret-1"]);
    let exposed = call(
        "cloudflare/list-dns-records",
        r#"{"zone_id": "a tok-secret-1"}"#,
        "{}",
    );
    assert_eq!(
        Span {
            protection,
            ..Span::of(vec![hourly(vec![exposed])])
        }
        .code(),
        "routine-secret-literal"
    );
}

#[test]
fn a_protected_value_in_any_reference_path_refuses() {
    for (result, known, protected) in [
        (
            r#"{"protected-key": {"id": "safe-id123"}}"#,
            "x",
            "protected-key",
        ),
        (
            r#"{"protected-list": [{"name": "beta", "id": "safe-id123"}]}"#,
            "beta",
            "protected-list",
        ),
        (
            r#"{"items": [{"name": "beta", "protected-item": "safe-id123"}]}"#,
            "beta",
            "protected-item",
        ),
        (
            r#"{"items": [{"name": "shimpz.com", "id": "safe-id123"}]}"#,
            "shimpz.com",
            "shimpz.com",
        ),
    ] {
        let calls = vec![
            call("reports/fetch", "{}", result),
            call(
                "cloudflare/list-dns-records",
                r#"{"zone_id": "safe-id123"}"#,
                "{}",
            ),
        ];
        let open = Span::of(vec![send(calls.clone(), &then_hourly(known))]);
        assert_eq!(
            at(at(last_input(&open.recorded()), "zone_id"), "kind"),
            &text("step_output")
        );
        let protection = Protection::default().grow([protected]);
        assert_eq!(
            Span {
                protection,
                ..Span::of(vec![send(calls, &then_hourly(known))])
            }
            .code(),
            "routine-secret-literal",
            "{protected}"
        );
    }
}

#[test]
fn a_question_never_offers_a_target_or_label_the_span_protects() {
    let protection = Protection::default().grow(["tok-secret-2", "tok-label-1"]);
    let reading = call(
        "cloudflare/list-dns-records",
        r#"{"zone_id": "id-0003"}"#,
        "{}",
    );
    let items = r#"{"items": [{"name": "same", "id": "id-0001", "title": "tok-label-1"}, {"name": "same", "id": "tok-secret-2"},
        {"name": "same", "id": "id-0003"}]}"#;
    let calls = vec![call("reports/fetch", "{}", items), reading.clone()];
    let asked = Span {
        protection: protection.clone(),
        ..Span::of(vec![send(calls, &then_hourly("same"))])
    }
    .question();
    assert_eq!(
        asked.options,
        [
            Target::new(text("id-0001"), Some("same")),
            Target::new(text("id-0003"), Some("same"))
        ]
    );
    let unnamed = r#"{"items": [{"id": "id-0001", "title": "tok-label-1"}, {"id": "id-0003", "title": "plain"}]}"#;
    let calls = vec![call("reports/fetch", "{}", unnamed), reading];
    let asked = Span {
        protection,
        ..Span::of(vec![send(calls, &then_hourly("x"))])
    }
    .question();
    assert_eq!(
        asked.options,
        [
            Target::new(text("id-0001"), None),
            Target::new(text("id-0003"), Some("plain"))
        ]
    );
}

#[test]
fn a_path_longer_than_a_plan_pointer_or_an_oversize_input_is_too_large() {
    let long_key = "k".repeat(MAX_POINTER);
    let result = Value::Object(vec![(long_key, json(r#"{"id": "safe-id123"}"#))]);
    let calls = vec![
        call_value("reports/fetch", &json("{}"), &result),
        call(
            "cloudflare/list-dns-records",
            r#"{"zone_id": "safe-id123"}"#,
            "{}",
        ),
    ];
    assert_eq!(
        Span::of(vec![hourly(calls)]).code(),
        "routine-recording-too-large"
    );
    let oversize = Kept {
        value: Value::Null,
        withheld: [String::new()].into(),
        oversize: true,
    };
    assert_eq!(
        Span::of(vec![hourly(vec![call_with_input(
            "reports/fetch",
            oversize,
            "{}"
        )])])
        .code(),
        "routine-recording-too-large"
    );
    let lost = Protection {
        lost: true,
        ..Protection::default()
    };
    assert_eq!(
        Span {
            protection: lost,
            ..Span::of(vec![hourly(vec![zones_call()])])
        }
        .code(),
        "routine-recording-unavailable"
    );
}

#[test]
fn the_whole_input_is_checked_against_its_schema_even_when_kept_unwithheld() {
    let mut deep = json(r#"{"type": "object"}"#);
    for _ in 0..MAX_SECRET_DEPTH + 2 {
        deep = Value::Object(vec![("allOf".to_owned(), Value::Array(vec![deep]))]);
    }
    let mut unwalkable = contracts();
    let mut fetch = ActionContract::new(PIN.to_owned(), deep);
    fetch.read_only = true;
    unwalkable.insert(("reports".to_owned(), "fetch".to_owned()), fetch);
    for (kept, contracts) in [
        (Kept::whole(json(r#"{"api_token": "plain"}"#)), contracts()),
        (Kept::whole(json(r#"{"q": "plain"}"#)), unwalkable),
    ] {
        let span = Span {
            contracts,
            ..Span::of(vec![hourly(vec![call_with_input(
                "reports/fetch",
                kept,
                "{}",
            )])])
        };
        assert_eq!(span.code(), "routine-secret-literal");
    }
}
