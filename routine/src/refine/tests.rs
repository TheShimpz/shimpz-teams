//! `test_routine_refine.py`: a Routine's next revision from its plan and the agent's explicit edits, and its
//! confirmed write.

use strict_json::Value;

use super::{DEPENDENCY, INPUT, INVALID, Refined, refine, verified};
use crate::compose::tests::{PIN, SHIMPZ_ID, call, contracts, send, zones_call};
use crate::json::{as_str, at, elements, object, set, text};
use crate::plan::VERSION;
use crate::record::{self, RoutineStateError, Run, RunStatus, TeamRoutines};
use crate::recording::{Context, RecordingError, Send, known_texts, segment_lines, span_calls};
use crate::testing::{ENTROPY, Fixture, added, default_plan, json};
use crate::trace::{Kept, Occurrence, Protection};

const ZONE: &str = "America/Sao_Paulo";
const SELECTED: &str = r#"{"kind": "step_output", "step": "s1", "pointer": "/result", "where": {"name": "shimpz.com"}, "item": "/id"}"#;

fn base() -> Value {
    json(&format!(
        r#"{{"version": {VERSION}, "timezone": "{ZONE}", "steps": [
            {{"id": "s1", "assistant": "cloudflare", "action": "list-zones", "pin": "{PIN}", "input": {{}}}},
            {{"id": "s2", "assistant": "cloudflare", "action": "list-dns-records", "pin": "{PIN}", "input": {{"zone_id": {SELECTED}}}}},
            {{"id": "s3", "assistant": "reports", "action": "fetch", "pin": "{PIN}",
              "input": {{"q": {{"kind": "step_output", "step": "s2", "pointer": "/result"}}}}}}],
            "output": {{"mode": "show", "step": "s3"}}}}"#
    ))
}

fn fetch_a() -> Occurrence {
    call(
        "reports/fetch",
        r#"{"q": "dns", "page": 2}"#,
        r#"{"result": []}"#,
    )
}

fn fetch_b() -> Occurrence {
    call(
        "reports/fetch",
        r#"{"q": "zones", "page": 3}"#,
        r#"{"result": []}"#,
    )
}

fn records() -> Occurrence {
    call(
        "cloudflare/list-dns-records",
        &format!(r#"{{"zone_id": "{SHIMPZ_ID}"}}"#),
        r#"{"result": []}"#,
    )
}

fn insert(step: &str, calls: &[&str]) -> Value {
    object(vec![
        ("kind", text("insert")),
        ("step", text(step)),
        (
            "calls",
            Value::Array(calls.iter().map(|call| text(call)).collect()),
        ),
    ])
}

fn replace(step: &str, handle: &str) -> Value {
    object(vec![
        ("kind", text("replace")),
        ("step", text(step)),
        ("calls", Value::Array(vec![text(handle)])),
    ])
}

fn remove(step: &str) -> Value {
    object(vec![
        ("kind", text("remove")),
        ("step", text(step)),
        ("calls", Value::Array(Vec::new())),
    ])
}

/// A refinement of a base plan by edits over one send's traced calls.
struct Refinement {
    edits: Vec<Value>,
    calls: Vec<Occurrence>,
    message: &'static str,
    base: Value,
}

impl Refinement {
    fn of(edits: Vec<Value>) -> Self {
        Self {
            edits,
            calls: vec![fetch_a(), fetch_b()],
            message: "Também busca relatórios",
            base: base(),
        }
    }

    fn run(&self) -> Result<Refined, RecordingError> {
        let sends = vec![send(self.calls.clone(), self.message)];
        let current = contracts();
        refine_span(&sends, &current, &self.base, &self.edits, |span| {
            span.to_vec()
        })
    }

    fn refined(&self) -> Refined {
        self.run().expect("a refinement")
    }

    fn code(&self) -> &'static str {
        self.run().map(|_| ()).expect_err("a refusal").code
    }
}

fn refine_span(
    sends: &[Send],
    current: &crate::plan::Contracts,
    base: &Value,
    edits: &[Value],
    mine: impl Fn(&[crate::recording::Call]) -> Vec<crate::recording::Call>,
) -> Result<Refined, RecordingError> {
    let span = span_calls(sends, current)?;
    let texts: Vec<String> = sends
        .iter()
        .flat_map(|send| segment_lines(&send.person))
        .collect();
    let known = known_texts(&texts, &Protection::default());
    let calls = mine(&span);
    let mut context = Context::new(
        sends,
        span,
        known,
        (ZONE.to_owned(), "browser".to_owned()),
        None,
        current,
    );
    refine(base, edits, &mut context, &calls)
}

fn steps(refined: &Refined) -> &[Value] {
    elements(at(&refined.document, "steps"))
}

fn field<'v>(step: &'v Value, key: &str) -> &'v Value {
    at(step, key)
}

fn changes(refined: &Refined) -> Vec<(&str, Option<usize>)> {
    refined
        .changes
        .iter()
        .map(|(change, base)| (change.as_str(), *base))
        .collect()
}

fn origins(refined: &Refined, step: &str) -> Vec<(String, String)> {
    refined
        .origins
        .iter()
        .find(|(id, _)| id == step)
        .map(|(_, found)| found.clone())
        .unwrap_or_default()
}

#[test]
fn an_insertion_keeps_its_traced_order_and_every_reference_follows_its_renumbered_step() {
    let refined = Refinement::of(vec![insert("s1", &["c1", "c2"])]).refined();
    let found = steps(&refined);
    let ids: Vec<&str> = found
        .iter()
        .filter_map(|step| as_str(field(step, "id")))
        .collect();
    assert_eq!(ids, ["s1", "s2", "s3", "s4", "s5"]);
    let names: Vec<&str> = found
        .iter()
        .filter_map(|step| as_str(field(step, "action")))
        .collect();
    assert_eq!(
        names,
        ["list-zones", "fetch", "fetch", "list-dns-records", "fetch"]
    );
    assert_eq!(
        field(&found[1], "input"),
        &json(
            r#"{"q": {"kind": "literal", "value": "dns"}, "page": {"kind": "literal", "value": 2}}"#
        )
    );
    assert_eq!(
        at(field(&found[2], "input"), "q"),
        &json(r#"{"kind": "literal", "value": "zones"}"#)
    );
    assert_eq!(at(field(&found[3], "input"), "zone_id"), &json(SELECTED));
    assert_eq!(at(at(field(&found[4], "input"), "q"), "step"), &text("s4"));
    assert_eq!(
        at(&refined.document, "output"),
        &json(r#"{"mode": "show", "step": "s5"}"#)
    );
}

#[test]
fn an_insertion_names_its_changes_and_origins() {
    let refined = Refinement::of(vec![insert("s1", &["c1", "c2"])]).refined();
    assert_eq!(
        changes(&refined),
        [
            ("kept", Some(1)),
            ("added", None),
            ("added", None),
            ("kept", Some(2)),
            ("kept", Some(3))
        ]
    );
    assert_eq!((refined.removed.len(), refined.last_changed), (0, false));
    let assistant = |member: &str| (member.to_owned(), "assistant".to_owned());
    assert_eq!(origins(&refined, "s2"), [assistant("q"), assistant("page")]);
    assert_eq!(
        origins(&refined, "s4"),
        [("zone_id".to_owned(), "selector".to_owned())]
    );
}

#[test]
fn inserted_calls_keep_their_traced_order_whatever_order_the_edit_names() {
    let refined = Refinement::of(vec![insert("s3", &["c2", "c1"])]).refined();
    let queries: Vec<&Value> = steps(&refined)[3..]
        .iter()
        .map(|step| at(at(field(step, "input"), "q"), "value"))
        .collect();
    assert_eq!(queries, [&text("dns"), &text("zones")]);
}

#[test]
fn edits_apply_in_plan_order_whatever_order_they_are_given_in() {
    let later = Refinement::of(vec![insert("s3", &["c2"]), insert("s1", &["c1"])]).refined();
    assert_eq!(
        at(field(&steps(&later)[1], "input"), "q"),
        &json(r#"{"kind": "literal", "value": "dns"}"#)
    );
    assert_eq!(
        later
            .changes
            .last()
            .map(|(change, base)| (change.as_str(), *base)),
        Some(("added", None))
    );
    assert!(later.last_changed);
    assert_eq!(at(at(&later.document, "output"), "step"), &text("s5"));
}

#[test]
fn replacing_or_removing_the_last_step_says_the_shown_result_changes() {
    let replaced = Refinement::of(vec![replace("s3", "c1")]).refined();
    assert_eq!(
        changes(&replaced),
        [("kept", Some(1)), ("kept", Some(2)), ("replaced", Some(3))]
    );
    assert_eq!(
        (replaced.removed.clone(), replaced.last_changed),
        (vec![(3, "replaced".to_owned())], true)
    );
    let removed = Refinement::of(vec![remove("s3")]).refined();
    assert_eq!(changes(&removed), [("kept", Some(1)), ("kept", Some(2))]);
    assert_eq!(
        (removed.removed.clone(), removed.last_changed),
        (vec![(3, "removed".to_owned())], true)
    );
    assert_eq!(
        at(&removed.document, "output"),
        &json(r#"{"mode": "show", "step": "s2"}"#)
    );
}

#[test]
fn a_quiet_plan_stays_quiet_a_description_alone_keeps_every_step_and_a_group_may_go_together() {
    let mut quiet = base();
    set(
        &mut quiet,
        "output",
        json(r#"{"mode": "none", "step": null}"#),
    );
    let refined = Refinement {
        base: quiet,
        ..Refinement::of(vec![insert("s3", &["c1"])])
    }
    .refined();
    assert_eq!(
        at(&refined.document, "output"),
        &json(r#"{"mode": "none", "step": null}"#)
    );
    let alone = Refinement::of(Vec::new()).refined();
    assert_eq!(
        (alone.document.clone(), alone.last_changed),
        (base(), false)
    );
    assert_eq!(
        changes(&alone),
        [("kept", Some(1)), ("kept", Some(2)), ("kept", Some(3))]
    );
    let group = Refinement::of(vec![remove("s2"), remove("s3")]).refined();
    assert_eq!(steps(&group).len(), 1);
    assert_eq!(
        group.removed,
        [(2, "removed".to_owned()), (3, "removed".to_owned())]
    );
}

#[test]
fn removing_a_read_step_an_unknown_step_or_call_or_every_step_is_refused() {
    for edit in [
        remove("s2"),
        replace("s2", "c1"),
        replace("s1", "c1"),
        remove("s1"),
    ] {
        assert_eq!(Refinement::of(vec![edit]).code(), DEPENDENCY);
    }
    assert_eq!(Refinement::of(vec![insert("s9", &["c1"])]).code(), INVALID);
    assert_eq!(Refinement::of(vec![insert("s1", &["c3"])]).code(), INVALID);
    assert_eq!(
        Refinement::of(vec![remove("s1"), remove("s2"), remove("s3")]).code(),
        INVALID
    );
}

const NAMED: &str = "Também leia os registros de shimpz.com";

#[test]
fn a_value_copied_from_a_traced_call_joins_with_that_call_as_an_added_step() {
    let refined = Refinement {
        calls: vec![zones_call(), records()],
        message: NAMED,
        ..Refinement::of(vec![insert("s3", &["c2"])])
    }
    .refined();
    let found = steps(&refined);
    let tail: Vec<&Value> = found[3..]
        .iter()
        .map(|step| field(step, "action"))
        .collect();
    assert_eq!(tail, [&text("list-zones"), &text("list-dns-records")]);
    // The copy names its new source step by name, never the demonstrated id.
    let mut expected = json(SELECTED);
    set(&mut expected, "step", text("s4"));
    assert_eq!(at(field(&found[4], "input"), "zone_id"), &expected);
    assert_eq!(&changes(&refined)[3..], [("added", None), ("added", None)]);
    // A zone the person never named is a binding the recorder would ask about, and so is a value no call holds.
    let unnamed = Refinement {
        calls: vec![zones_call(), records()],
        ..Refinement::of(vec![insert("s3", &["c2"])])
    };
    assert_eq!(unnamed.code(), INPUT);
    assert_eq!(
        Refinement {
            calls: vec![records()],
            ..Refinement::of(vec![insert("s3", &["c1"])])
        }
        .code(),
        INPUT
    );
}

#[test]
fn a_source_two_edits_read_joins_once_before_both_and_a_named_source_read_earlier_is_refused() {
    let again = call(
        "cloudflare/list-dns-records",
        &format!(r#"{{"zone_id": "{SHIMPZ_ID}"}}"#),
        r#"{"result": [{"id": "r2"}]}"#,
    );
    let refined = Refinement {
        calls: vec![zones_call(), records(), again],
        message: NAMED,
        ..Refinement::of(vec![insert("s1", &["c2"]), insert("s3", &["c3"])])
    }
    .refined();
    let found = steps(&refined);
    assert_eq!(
        found
            .iter()
            .filter(|step| field(step, "action") == &text("list-zones"))
            .count(),
        2
    );
    let zones = found
        .iter()
        .filter(|step| field(step, "action") == &text("list-zones"))
        .nth(1)
        .map(|step| field(step, "id").clone());
    let readers = found[2..].iter().filter(|step| {
        field(step, "action") == &text("list-dns-records") && field(step, "id") != &text("s4")
    });
    assert!(
        readers
            .into_iter()
            .all(|step| Some(at(at(field(step, "input"), "zone_id"), "step").clone()) == zones)
    );
    // The source is named after the step whose edit reads it first: it would run after its reader.
    let backwards = Refinement {
        calls: vec![zones_call(), records()],
        message: NAMED,
        ..Refinement::of(vec![insert("s1", &["c2"]), insert("s3", &["c1"])])
    };
    assert_eq!(backwards.code(), INVALID);
}

#[test]
fn changes_placed_against_the_order_they_ran_in_are_refused() {
    let enable = call("reports/post", r#"{"text": "enable"}"#, r#"{"result": []}"#);
    let disable = call(
        "reports/post",
        r#"{"text": "disable"}"#,
        r#"{"result": []}"#,
    );
    let message = "Também envie \"enable\" e depois \"disable\"";
    let kept = Refinement {
        calls: vec![enable.clone(), disable.clone()],
        message,
        ..Refinement::of(vec![insert("s1", &["c1"]), insert("s3", &["c2"])])
    }
    .refined();
    let posts: Vec<&Value> = steps(&kept)
        .iter()
        .filter(|step| field(step, "action") == &text("post"))
        .map(|step| at(at(field(step, "input"), "text"), "value"))
        .collect();
    assert_eq!(posts, [&text("enable"), &text("disable")]);
    let reversed = Refinement {
        calls: vec![enable, disable],
        message,
        ..Refinement::of(vec![insert("s3", &["c1"]), insert("s1", &["c2"])])
    };
    assert_eq!(reversed.code(), INVALID);
}

#[test]
fn a_named_value_is_a_literal_and_the_send_date_is_each_runs_date() {
    let named = Refinement {
        calls: vec![records()],
        message: "Inclua a zona 023e105f4ecef8ad9ca31a8372d0c353",
        ..Refinement::of(vec![insert("s3", &["c1"])])
    }
    .refined();
    assert_eq!(
        at(field(&steps(&named)[3], "input"), "zone_id"),
        &json(&format!(r#"{{"kind": "literal", "value": "{SHIMPZ_ID}"}}"#))
    );
    assert_eq!(
        origins(&named, "s4"),
        [("zone_id".to_owned(), "request".to_owned())]
    );
    // 15:00 UTC is 12:00 in Sao Paulo: the send's local date is each run's date.
    let today = crate::provenance::date_at(crate::compose::tests::STARTED_AT, ZONE)
        .expect("a date")
        .isoformat();
    let fetch = call(
        "reports/fetch",
        &format!(r#"{{"t": "{today}"}}"#),
        r#"{"result": []}"#,
    );
    let dated = Refinement {
        calls: vec![fetch],
        ..Refinement::of(vec![insert("s3", &["c1"])])
    }
    .refined();
    assert_eq!(
        at(field(&steps(&dated)[3], "input"), "t"),
        &json(r#"{"kind": "run_clock", "format": "date"}"#)
    );
    assert_eq!(
        origins(&dated, "s4"),
        [("t".to_owned(), "clock".to_owned())]
    );
}

#[test]
fn new_steps_whose_selections_outgrow_their_bound_are_refused() {
    let half = crate::plan::MAX_RETAINED_BYTES / 2 + 1024;
    let (first, second) = ("a".repeat(half), "b".repeat(half));
    let fetch = crate::compose::tests::call_value(
        "reports/fetch",
        &json("{}"),
        &object(vec![("first", text(&first)), ("second", text(&second))]),
    );
    let posts = [("a", &first), ("b", &second)].map(|(member, value)| {
        crate::compose::tests::call_value(
            "reports/post",
            &object(vec![(member, text(value))]),
            &json("{}"),
        )
    });
    let large = Refinement {
        calls: vec![fetch, posts[0].clone(), posts[1].clone()],
        ..Refinement::of(vec![insert("s3", &["c2", "c3"])])
    };
    assert_eq!(large.code(), "routine-recording-too-large");
}

#[test]
fn a_withheld_input_refuses_as_a_secret_and_a_drifted_pin_refuses() {
    let withheld = Kept {
        value: json(r#"{"q": null}"#),
        withheld: ["/q".to_owned()].into(),
        oversize: false,
    };
    let hidden =
        crate::compose::tests::call_with_input("reports/fetch", withheld, r#"{"result": []}"#);
    assert_eq!(
        Refinement {
            calls: vec![hidden],
            ..Refinement::of(vec![insert("s3", &["c1"])])
        }
        .code(),
        "routine-secret-literal"
    );
    let mut drifted = base();
    let mut found = elements(at(&drifted, "steps")).to_vec();
    set(
        &mut found[0],
        "pin",
        text(&format!("sha256:{}", "f".repeat(64))),
    );
    set(&mut drifted, "steps", Value::Array(found));
    assert_eq!(
        Refinement {
            base: drifted,
            ..Refinement::of(Vec::new())
        }
        .code(),
        "plan-pin-drift"
    );
}

#[test]
fn each_new_step_must_resolve_to_what_its_call_sent() {
    let sends = vec![send(vec![fetch_a()], "Também busca relatórios")];
    let current = contracts();
    let span = span_calls(&sends, &current).expect("calls");
    let mut context = Context::new(
        &sends,
        span,
        known_texts(&[], &Protection::default()),
        (ZONE.to_owned(), "browser".to_owned()),
        None,
        &current,
    );
    crate::compose::source_classes(&mut context);
    let mut document = base();
    let mut found = elements(at(&document, "steps")).to_vec();
    found.push(json(&format!(r#"{{"id": "s4", "assistant": "reports", "action": "fetch", "pin": "{PIN}", "input": {{"q": {{"kind": "literal", "value": "other"}}}}}}"#)));
    set(&mut document, "steps", Value::Array(found));
    set(
        &mut document,
        "output",
        json(r#"{"mode": "show", "step": "s4"}"#),
    );
    let entries: Vec<super::Entry> = (0..4)
        .map(|index| super::Entry {
            step: Value::Null,
            change: "kept",
            base: None,
            origins: Vec::new(),
            node: (index == 3).then_some(0),
        })
        .collect();
    assert_eq!(
        verified(&document, &context, &entries).map_err(|error| error.code),
        Err("routine-recording-unverified")
    );
}

#[test]
fn a_call_of_an_earlier_send_is_never_a_handle_of_this_one() {
    let sends = vec![
        send(vec![fetch_a()], "Busca relatórios"),
        send(vec![fetch_b()], "Também busca zonas"),
    ];
    let current = contracts();
    let only_mine = |span: &[crate::recording::Call]| {
        span.iter().filter(|call| call.send == 1).cloned().collect()
    };
    let refined = refine_span(
        &sends,
        &current,
        &base(),
        &[insert("s3", &["c1"])],
        only_mine,
    )
    .expect("a refinement");
    assert_eq!(
        at(field(&steps(&refined)[3], "input"), "q"),
        &json(r#"{"kind": "literal", "value": "zones"}"#)
    );
    assert!(
        refine_span(
            &sends,
            &current,
            &base(),
            &[insert("s3", &["c2"])],
            only_mine
        )
        .is_err()
    );
}

// --- The confirmed write -------------------------------------------------------------------------------------------

fn refinement_of(current: &record::Routine) -> record::Routine {
    let mut card = current.clone();
    card.description = "Checks the DNS zone twice.".to_owned();
    let mut plan = default_plan();
    let mut second = elements(at(&plan, "steps"))[0].clone();
    set(&mut second, "id", text("s2"));
    let mut found = elements(at(&plan, "steps")).to_vec();
    found.push(second);
    set(&mut plan, "steps", Value::Array(found));
    set(
        &mut plan,
        "output",
        json(r#"{"mode": "show", "step": "s2"}"#),
    );
    card.plan = plan;
    card.permitted = crate::testing::permitted(&card.plan, true);
    card
}

/// The Routine as a write finds it after a run ended, it was paused, missed a firing, and a run was asked for.
fn moved(current: &record::Routine) -> record::Routine {
    let mut moved = current.clone();
    moved.next_run_at += 86_400;
    (
        moved.paused,
        moved.gap_started_at,
        moved.missed,
        moved.reported_missed,
    ) = (true, 5, 2, 1);
    (
        moved.failures,
        moved.run_requested,
        moved.rollup_minute,
        moved.rollup_runs,
    ) = (2, 7, 60, 3);
    moved.output_digest = "d".repeat(64);
    moved
}

#[test]
fn a_refinement_keeps_the_scheduling_state_its_write_finds() {
    let state = added(&[Fixture::default().build()]);
    let card = refinement_of(&state.routines[0]);
    // Meanwhile a run ended, the Routine was paused, missed a firing, and a run was asked for.
    let moved = moved(&state.routines[0]);
    let mut state = state;
    record::replace_routine(&mut state, moved.clone());
    let refined = record::refine(&state, &card, 1, 100, &ENTROPY).expect("refined");
    let value = &refined.routines[0];
    assert_eq!(
        (value.revision, value.description.as_str(), &value.plan),
        (2, "Checks the DNS zone twice.", &card.plan)
    );
    let scheduling = |item: &record::Routine| {
        (
            item.anchor,
            item.next_run_at,
            item.paused,
            item.gap_started_at,
            item.missed,
            item.reported_missed,
            item.run_requested,
        )
    };
    assert_eq!(scheduling(value), scheduling(&moved));
    assert_eq!((value.rollup_minute, value.rollup_runs), (60, 3));
    assert_eq!(
        (
            value.failures,
            value.output_digest.as_str(),
            value.needs_reconfirm
        ),
        (0, "", false)
    );
    assert_eq!(
        refined
            .notices
            .iter()
            .map(|item| (item.outcome.as_str(), item.detail.clone()))
            .collect::<Vec<_>>(),
        [("changed", object(vec![]))]
    );
}

#[test]
fn a_stale_busy_deleting_rezoned_or_oversized_refinement_is_refused() {
    let state = added(&[Fixture::default().build()]);
    let card = refinement_of(&state.routines[0]);
    let refused = |given: &TeamRoutines, value: &record::Routine, revision: i64| {
        record::refine(given, value, revision, 100, &ENTROPY)
            .map(|_| ())
            .err()
    };
    assert_eq!(
        refused(&state, &card, 2),
        Some(RoutineStateError::new("routine-revision-changed"))
    );
    let deleting = crate::testing::edit(&state, &card.routine_id, |value| value.deleting = true);
    assert_eq!(
        refused(&deleting, &card, 1),
        Some(RoutineStateError::new("routine-not-found"))
    );
    let mut rezoned = card.clone();
    set(&mut rezoned.plan, "timezone", text("Asia/Tokyo"));
    assert_eq!(
        refused(&state, &rezoned, 1),
        Some(RoutineStateError::new("routine-invalid"))
    );
    let busy = TeamRoutines {
        runs: vec![Run::new(
            &"f".repeat(32),
            &card.routine_id,
            RunStatus::Frozen,
        )],
        ..state
    };
    assert_eq!(
        refused(&busy, &card, 1),
        Some(RoutineStateError::new("routine-busy"))
    );
    let often = added(&[Fixture {
        schedule: json(r#"{"kind": "continuous", "gap": 5}"#),
        ..Fixture::default()
    }
    .build()]);
    assert_eq!(
        refused(&often, &refinement_of(&often.routines[0]), 1),
        Some(RoutineStateError::new("routine-step-budget"))
    );
}
