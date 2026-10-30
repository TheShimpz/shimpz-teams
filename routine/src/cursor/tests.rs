//! `test_routine_cursor.py` and the cursor slot parts of `test_routine_output.py`: binding, durable prefix, stable
//! operations, recovery, bounds, protection, and the shown step's slot.

use strict_json::Value;

use super::{
    Binding, Cursor, CursorError, Episode, FAULTS, VERSION, checked_within, complete, continued,
    decode, dispatch, encode, failed, lose_protection, open_episode, proven_absent, retry, start,
};
use crate::json::{canonical, int, object, set, text};
use crate::plan::tests::{contracts, document, document_with};
use crate::plan::{self, MAX_RETAINED_BYTES, MAX_STEPS, Plan, admit};

const OPERATION: &str = "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6";
const OTHER_OPERATION: &str = "7a2d3c9f-4b5e-4d6f-8a70-829304b5c6d7";
const BOOT: &str = "ffffffffffffffffffffffffffffffff";

fn binding() -> Binding {
    Binding {
        incarnation: "a".repeat(64),
        routine_id: "b".repeat(32),
        revision: 1,
        run_id: "c".repeat(32),
    }
}

fn commitment() -> String {
    "d".repeat(64)
}

fn quiet_plan() -> Plan {
    admit(
        &document_with("output", json(r#"{"mode": "none", "step": null}"#)),
        &contracts(),
    )
    .expect("a plan")
}

fn json(text: &str) -> Value {
    crate::testing::json(text)
}

fn first(plan: &Plan) -> Cursor {
    start(plan, binding(), 1_800_000_000, BOOT).expect("a cursor")
}

fn dispatched(cursor: &Cursor, plan: &Plan, operation: &str) -> Cursor {
    dispatch(cursor, plan, operation, &commitment(), "", 0).expect("dispatched")
}

fn refused<T: std::fmt::Debug>(result: Result<T, CursorError>, code: &'static str) {
    assert_eq!(result.map(|_| ()).err(), Some(CursorError { code }));
}

fn key(step: &str, pointer: &str) -> (String, String, String, String) {
    (
        step.to_owned(),
        pointer.to_owned(),
        String::new(),
        String::new(),
    )
}

#[test]
fn a_completed_prefix_advances_and_keeps_only_selected_values() {
    let plan = quiet_plan();
    let cursor = first(&plan);
    assert_eq!(
        (cursor.step, cursor.operation_id.as_deref(), cursor.attempts),
        (0, None, 0)
    );
    let sent = dispatched(&cursor, &plan, OPERATION);
    let operation = (
        sent.operation_id.as_deref(),
        sent.attempts,
        sent.commitment.clone(),
    );
    assert_eq!(operation, (Some(OPERATION), 1, Some(commitment())));
    let body = "x".repeat(1000);
    let result = json(&format!(
        r#"{{"id": "post-1", "meta": {{"a/b": [["news"]]}}, "body": "{body}"}}"#
    ));
    let advanced = complete(&sent, &plan, &result, None).expect("advanced");
    let progress = (
        advanced.step,
        advanced.operation_id.as_deref(),
        advanced.attempts,
    );
    assert_eq!(
        (progress, advanced.commitment.as_deref()),
        ((1, None, 0), None)
    );
    let selections = advanced.selections();
    assert_eq!(
        selections.get(&key("publish", "/id")),
        Some(&text("post-1"))
    );
    assert_eq!(
        selections.get(&key("publish", "/meta/a~1b/0")),
        Some(&json(r#"["news"]"#))
    );
    assert_eq!(selections.len(), 2);
    let encoded = String::from_utf8(encode(&advanced).expect("encoded")).expect("UTF-8");
    assert!(!encoded.contains(&body));
}

#[test]
fn a_completed_step_is_never_run_again() {
    let plan = quiet_plan();
    let sent = dispatched(&first(&plan), &plan, OPERATION);
    let result = json(r#"{"id": "post-1", "meta": {"a/b": [["news"]]}}"#);
    let advanced = complete(&sent, &plan, &result, None).expect("advanced");
    let shared = json(r#"{"shared": true}"#);
    let last = complete(
        &dispatched(&advanced, &plan, OTHER_OPERATION),
        &plan,
        &shared,
        None,
    )
    .expect("done");
    assert!(last.done(&plan));
    assert_eq!(last.selections(), advanced.selections());
    let nothing = json("{}");
    refused(
        dispatch(&last, &plan, OPERATION, &commitment(), "", 0),
        "cursor-dispatch-invalid",
    );
    refused(
        complete(&last, &plan, &nothing, None),
        "cursor-not-dispatched",
    );
    refused(
        complete(&advanced, &plan, &nothing, None),
        "cursor-not-dispatched",
    );
    refused(
        complete(&sent, &plan, &json(r#"{"id": "post-1"}"#), None),
        "plan-reference-missing",
    );
}

#[test]
fn a_replay_keeps_its_logical_operation_and_exact_payload() {
    let plan = quiet_plan();
    let cursor = first(&plan);
    let sent = dispatched(&cursor, &plan, OPERATION);
    let again = dispatched(&sent, &plan, OPERATION);
    assert_eq!(
        (again.operation_id.as_deref(), again.attempts),
        (Some(OPERATION), 2)
    );
    refused(
        dispatch(&sent, &plan, OTHER_OPERATION, &commitment(), "", 0),
        "cursor-operation-changed",
    );
    refused(
        dispatch(&sent, &plan, OPERATION, &"e".repeat(64), "", 0),
        "cursor-operation-changed",
    );
    refused(
        dispatch(&cursor, &plan, "x", &commitment(), "", 0),
        "cursor-dispatch-invalid",
    );
    refused(
        dispatch(&cursor, &plan, OPERATION, "x", "", 0),
        "cursor-dispatch-invalid",
    );
    let other = admit(&document_with("timezone", text("UTC")), &contracts()).expect("a plan");
    refused(
        dispatch(&cursor, &other, OPERATION, &commitment(), "", 0),
        "cursor-plan-changed",
    );
    refused(
        complete(&sent, &other, &json("{}"), None),
        "cursor-plan-changed",
    );
}

#[test]
fn the_recovery_episode_opens_once_and_outlives_encoding_and_steps() {
    let plan = quiet_plan();
    let cursor = first(&plan);
    let opened = open_episode(&cursor).expect("opened");
    assert_eq!(
        (cursor.episode.opened(), opened.episode.opened()),
        (false, true)
    );
    assert!(
        decode(&encode(&opened).expect("encoded"), &binding())
            .expect("decoded")
            .episode
            .opened()
    );
    let advanced = complete(
        &dispatched(&opened, &plan, OPERATION),
        &plan,
        &json(r#"{"id": "1", "meta": {"a/b": [1]}}"#),
        None,
    )
    .expect("advanced");
    assert!(advanced.episode.opened());
    refused(open_episode(&advanced), "cursor-episode-spent");
}

fn tampered(document: &Value, key: &str, value: Option<Value>) -> Vec<u8> {
    let mut changed = document.clone();
    match value {
        Some(value) => set(&mut changed, key, value),
        None => {
            if let Value::Object(members) = &mut changed {
                members.retain(|(name, _)| name != key);
            }
        }
    }
    canonical(&changed)
}

/// Every encoding of `document` that differs from a valid cursor's canonical bytes in one way.
fn tampered_documents(document: &Value) -> Vec<Vec<u8>> {
    let mut spaced = crate::json::json_text(document).into_bytes();
    spaced.push(b' ');
    let past_steps = int(i64::try_from(MAX_STEPS).expect("a bound") + 1);
    let twice = r#"[["publish", "/id", "", "", 1], ["publish", "/id", "", "", 2]]"#;
    let mut cases = vec![b"not json".to_vec(), spaced];
    for (key, value) in [
        ("version", Some(int(VERSION + 1))),
        ("version", Some(json("7.0"))),
        ("shown", None),
        ("shown", Some(json(r#"{"step": "publish"}"#))),
        ("shown", Some(json("[]"))),
        ("episode", None),
        ("selected", Some(json("{}"))),
        ("selected", Some(json(r#"[["publish", "/id"]]"#))),
        (
            "selected",
            Some(json(r#"[["publish", "/id", "", "/x", 1]]"#)),
        ),
        ("selected", Some(json(r#"[["publish", "/id", 1, "", 1]]"#))),
        ("unexpected", Some(Value::Null)),
        ("boot", Some(text("x"))),
        ("episode", Some(int(1))),
        // Only an episode continues a run, and only a continuation carries an operation.
        ("continued", Some(Value::Bool(true))),
        ("carried", Some(Value::Bool(true))),
        ("attempts", Some(int(0))),
        ("commitment", Some(Value::Null)),
        ("step", Some(past_steps)),
        ("plan", Some(text("sha256:x"))),
        ("selected", Some(json(twice))),
        ("selected", Some(json(r#"[["Publish", "/id", "", "", 1]]"#))),
    ] {
        cases.push(tampered(document, key, value));
    }
    cases
}

#[test]
fn a_cursor_decodes_only_for_its_exact_binding_and_canonical_bytes() {
    let plan = quiet_plan();
    let raw = encode(&dispatched(&first(&plan), &plan, OPERATION)).expect("encoded");
    assert_eq!(
        decode(&raw, &binding())
            .expect("decoded")
            .operation_id
            .as_deref(),
        Some(OPERATION)
    );
    for other in [
        Binding {
            incarnation: "e".repeat(64),
            ..binding()
        },
        Binding {
            routine_id: "e".repeat(32),
            ..binding()
        },
        Binding {
            revision: 2,
            ..binding()
        },
        Binding {
            run_id: "e".repeat(32),
            ..binding()
        },
    ] {
        refused(decode(&raw, &other), "cursor-invalid");
    }
    for value in tampered_documents(&strict_json::parse(&raw).expect("a document")) {
        refused(decode(&value, &binding()), "cursor-invalid");
    }
}

#[test]
fn a_cursor_stays_within_its_bounds() {
    let plan = quiet_plan();
    for other in [
        Binding {
            incarnation: "x".to_owned(),
            ..binding()
        },
        Binding {
            revision: 0,
            ..binding()
        },
        Binding {
            run_id: String::new(),
            ..binding()
        },
    ] {
        refused(start(&plan, other, 0, BOOT), "cursor-invalid");
    }
    refused(start(&plan, binding(), -1, BOOT), "cursor-invalid");
    let sent = dispatched(&first(&plan), &plan, OPERATION);
    let large = json(&format!(
        r#"{{"id": "{}", "meta": {{"a/b": [1]}}}}"#,
        "x".repeat(MAX_RETAINED_BYTES)
    ));
    refused(complete(&sent, &plan, &large, None), "cursor-invalid");
    let mut oversized = first(&plan);
    oversized.selected = (0..5)
        .map(|index| {
            (
                format!("s{index}"),
                "/v".to_owned(),
                String::new(),
                String::new(),
                text(&"x".repeat(60_000)),
            )
        })
        .collect();
    refused(encode(&oversized), "cursor-invalid");
    refused(checked_within(oversized, usize::MAX), "cursor-too-large");
}

#[test]
fn a_classified_failure_is_sealed_and_a_policy_fault_never_admits_absence() {
    let plan = quiet_plan();
    let sent = dispatched(&first(&plan), &plan, OPERATION);
    for fault in &FAULTS[1..] {
        let classified = failed(&sent, fault).expect("classified");
        assert_eq!(
            decode(&encode(&classified).expect("encoded"), &binding())
                .expect("decoded")
                .fault,
            *fault
        );
    }
    let policy = failed(&sent, "policy").expect("classified");
    refused(proven_absent(&policy), "cursor-policy-hold");
    refused(
        encode(&Cursor {
            absent: true,
            ..policy.clone()
        }),
        "cursor-invalid",
    );
    // A new attempt of the operation starts unclassified.
    assert_eq!(dispatched(&policy, &plan, OPERATION).fault, "");
    refused(failed(&sent, "unknown"), "cursor-not-dispatched");
    refused(failed(&sent, ""), "cursor-not-dispatched");
    refused(failed(&first(&plan), "handled"), "cursor-not-dispatched");
    refused(
        encode(&Cursor {
            fault: "handled".to_owned(),
            ..first(&plan)
        }),
        "cursor-invalid",
    );
}

#[test]
fn an_attempt_keeps_its_workload_and_instant() {
    let plan = quiet_plan();
    let cursor = first(&plan);
    let attempt = dispatch(
        &cursor,
        &plan,
        OPERATION,
        &commitment(),
        "assistant-container",
        1_800_000_100,
    )
    .expect("dispatched");
    let decoded = decode(&encode(&attempt).expect("encoded"), &binding()).expect("decoded");
    assert_eq!(
        (decoded.workload.as_str(), decoded.dispatched_at),
        ("assistant-container", 1_800_000_100)
    );
    for changed in [
        Cursor {
            workload: "-bad".to_owned(),
            ..attempt.clone()
        },
        Cursor {
            dispatched_at: -1,
            ..attempt
        },
        Cursor {
            workload: "assistant-container".to_owned(),
            ..cursor.clone()
        },
        Cursor {
            dispatched_at: 5,
            ..cursor
        },
    ] {
        refused(encode(&changed), "cursor-invalid");
    }
}

#[test]
fn losing_protection_is_sealed_with_the_boot_it_was_bound_in() {
    let plan = quiet_plan();
    let cursor = first(&plan);
    let lost = lose_protection(&cursor).expect("lost");
    let decoded = decode(&encode(&lost).expect("encoded"), &binding()).expect("decoded");
    assert_eq!(
        (decoded.boot.as_str(), decoded.protection_lost),
        (BOOT, true)
    );
    for boot in ["", &"F".repeat(32)] {
        refused(
            encode(&Cursor {
                boot: boot.to_owned(),
                ..cursor.clone()
            }),
            "cursor-invalid",
        );
    }
}

#[test]
fn a_retry_follows_only_proven_absence_and_spends_it() {
    let plan = quiet_plan();
    let held = open_episode(&dispatched(&first(&plan), &plan, OPERATION)).expect("opened");
    refused(retry(&held), "cursor-operation-uncertain");
    let absent = proven_absent(&failed(&held, "transport").expect("classified")).expect("absent");
    assert!(!retry(&absent).expect("retried").absent);
    refused(proven_absent(&first(&plan)), "cursor-not-dispatched");
}

#[test]
fn the_one_continuation_carries_its_operation_into_its_own_generation() {
    let plan = quiet_plan();
    let held = open_episode(&dispatched(&first(&plan), &plan, OPERATION)).expect("opened");
    let next = continued(&held).expect("continued");
    assert_eq!(
        (
            next.episode.continued(),
            next.episode.carried(),
            next.generation_suffix()
        ),
        (true, true, "s1")
    );
    assert_eq!(held.generation_suffix(), "");
    refused(continued(&next), "cursor-continued");
    refused(
        continued(&Cursor {
            episode: Episode::Closed,
            ..held
        }),
        "cursor-invalid",
    );
}

// --- The shown step's slot -------------------------------------------------------------------------------------------

struct Slots {
    plan: Plan,
    first: Cursor,
    cursor: Cursor,
    result: Value,
    slot: Value,
}

fn slots() -> Slots {
    let plan = admit(&document(), &contracts()).expect("a plan");
    let first_cursor = dispatch(
        &start(&plan, binding(), 0, BOOT).expect("a cursor"),
        &plan,
        OPERATION,
        &commitment(),
        "",
        0,
    )
    .expect("dispatched");
    let result = json(r#"{"id": "post-1", "meta": {"a/b": [["news"]]}}"#);
    // Dispatched at the plan's last step, the one it shows.
    let cursor = dispatched(
        &complete(&first_cursor, &plan, &result, None).expect("advanced"),
        &plan,
        OTHER_OPERATION,
    );
    let node = plan::output_safe(&result, &json("{}"), &[]).expect("a safe form");
    // The slot keeps its step's id; its output names the step by position, as the wire does.
    let slot = object(vec![
        ("step", text("share")),
        ("output", plan::output_shown(&int(2), &node)),
        ("digest", text(&"e".repeat(64))),
    ]);
    Slots {
        plan,
        first: first_cursor,
        cursor,
        result,
        slot,
    }
}

#[test]
fn only_the_shown_step_keeps_its_slot_and_it_outlives_holds_and_continuations() {
    let Slots {
        plan,
        first: first_cursor,
        cursor,
        result,
        slot,
    } = slots();
    let last = complete(&cursor, &plan, &json("{}"), Some(&slot)).expect("done");
    assert_eq!(last.shown.as_ref(), Some(&slot));
    let decoded = decode(&encode(&last).expect("encoded"), &last.binding).expect("decoded");
    assert!(
        decoded
            .shown
            .as_ref()
            .is_some_and(|shown| crate::plan::same(shown, &slot))
    );
    let continued_cursor = continued(&open_episode(&last).expect("opened")).expect("continued");
    assert_eq!(continued_cursor.shown.as_ref(), Some(&slot));
    let mut publish = slot.clone();
    set(&mut publish, "step", text("publish"));
    refused(
        complete(&cursor, &plan, &json("{}"), None),
        "cursor-shown-invalid",
    );
    refused(
        complete(&cursor, &plan, &json("{}"), Some(&publish)),
        "cursor-shown-invalid",
    );
    refused(
        complete(&first_cursor, &plan, &result, Some(&publish)),
        "cursor-shown-invalid",
    );
}

#[test]
fn a_slot_outside_its_closed_shape_is_refused() {
    let Slots {
        plan, cursor, slot, ..
    } = slots();
    let unavailable = object(vec![
        ("step", text("share")),
        ("output", plan::output_state(&int(2), "unavailable")),
        ("digest", Value::Null),
    ]);
    assert_eq!(
        complete(&cursor, &plan, &json("{}"), Some(&unavailable))
            .expect("done")
            .shown,
        Some(unavailable.clone())
    );
    let with = |base: &Value, key: &str, value: Value| {
        let mut changed = base.clone();
        set(&mut changed, key, value);
        changed
    };
    for wrong in [
        with(&slot, "extra", int(1)),
        with(&slot, "digest", text(&"E".repeat(64))),
        with(&slot, "output", json(r#"{"step": "share"}"#)),
        with(&slot, "output", plan::output_state(&int(1), "unavailable")),
        with(&slot, "output", plan::output_state(&int(2), "unchanged")),
        with(&slot, "step", int(2)),
        with(&unavailable, "digest", text(&"e".repeat(64))),
        json("[]"),
    ] {
        assert!(
            complete(&cursor, &plan, &json("{}"), Some(&wrong)).is_err(),
            "{wrong:?}"
        );
    }
}
