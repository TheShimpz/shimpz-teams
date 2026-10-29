//! `routine_notice` and `routine_run`.

use strict_json::{Number, Value};
use team_protocol::{routine, routine_notice, routine_run};

use super::{big, first, int, member, object, refused, string, verdicts, with, without};
use crate::support::vectors::{admits_group, group, items, same};

const GROUPS: [(&str, &str); 19] = [
    (
        "routine_views.routine",
        "routine_notice.canonical_routine_view",
    ),
    ("routine_views.run", "routine_notice.canonical_run_view"),
    (
        "routine_views.notice_batch",
        "routine_notice.canonical_notice_batch",
    ),
    (
        "routine_views.incident",
        "routine_notice.canonical_incident_view",
    ),
    ("routine_views.claim", "routine_run.canonical_claim"),
    (
        "routine_views.claim_request",
        "routine_run.canonical_claim_request",
    ),
    ("routine_views.card", "routine_run.canonical_card"),
    (
        "routine_views.card_answer_request",
        "routine_run.canonical_card_answer_request",
    ),
    (
        "routine_views.card_answer",
        "routine_run.canonical_card_answer",
    ),
    (
        "routine_views.segment_request",
        "routine_run.canonical_segment_request",
    ),
    (
        "routine_views.segment_answer",
        "routine_run.canonical_segment_answer",
    ),
    ("routine_views.run_steps", "routine_run.canonical_run_steps"),
    ("routine_diagnostics", "routine_run.canonical_diagnostics"),
    (
        "routine_challenge_open",
        "routine_run.canonical_challenge_open",
    ),
    ("routine_proposal", "routine_proposal.canonical_proposal"),
    ("routine_refusal", "routine_proposal.canonical_refusal"),
    ("routine_question", "routine_proposal.canonical_question"),
    (
        "routine_proposal_answer",
        "routine_proposal.canonical_proposal_answer",
    ),
    (
        "routine_refinement",
        "routine_proposal.canonical_refinement",
    ),
];

#[test]
fn routine_form_vectors() {
    for (path, name) in GROUPS {
        admits_group(name, path);
    }
}

fn delivered(delivery: &Value) -> bool {
    let batch = object(vec![
        ("notices", delivery.clone()),
        ("more", Value::Bool(false)),
    ]);
    routine_notice::canonical_notice_batch(&batch).is_some_and(|admitted| same(&admitted, &batch))
        && items(delivery)
            .iter()
            .all(|notice| routine_notice::canonical_notice(notice) == Some(notice))
}

#[test]
fn rollup_deliveries_are_canonical_batches() {
    let Value::Object(cases) = group("routine_rollup_delivery") else {
        panic!("the rollup deliveries are an object");
    };
    let deliveries: Vec<&Value> = cases
        .iter()
        .flat_map(|(_, case)| items(member(case, "deliveries")))
        .collect();
    assert_eq!(deliveries.len(), 6);
    assert!(deliveries.into_iter().all(delivered));
}

#[test]
fn constants_keep_their_python_values() {
    let constants = [
        routine_notice::MAX_ROUTINE_LIST_BYTES,
        routine_notice::MAX_NOTICE_BATCH_BYTES,
        routine_notice::MAX_NOTICE_BATCH,
        routine_notice::MAX_CREATION_STEPS,
        routine_run::MAX_ACK_DELIVERIES,
        routine_run::RUN_STEP_FIELDS.len(),
    ];
    assert_eq!(constants, [241_664, 114_688, 1024, 64, 256, 7]);
    let seconds = [8, 228, 227, i64::MAX, -20, 0].map(routine_run::active_seconds);
    assert_eq!(seconds, [600, 7200, 7170, 7200, -240, 360]);
    assert_eq!(routine_run::SHORT_ACTIVE_SECONDS, 600);
}

#[test]
fn instants_are_real_utc_seconds() {
    verdicts(
        &[
            (string("2026-10-01T09:00:00Z"), true),
            (string("2024-02-29T00:00:00Z"), true),
            (string("2026-02-29T00:00:00Z"), false),
            (string("2026-10-01T09:00:00+00:00"), false),
            (string("2026-10-01T09:00:60Z"), false),
            (int(1), false),
        ],
        routine_notice::instant,
    );
    verdicts(
        &[
            (Value::Null, true),
            (string("x"), false),
            (string(&"a".repeat(32)), true),
        ],
        |value| routine_notice::optional(value, &routine::ROUTINE_ID_RE),
    );
}

fn notice(outcome: &str) -> Value {
    items(member(group("routine_views.notice_batch"), "valid"))
        .iter()
        .flat_map(|case| items(member(case, "notices")).iter())
        .find(|notice| *member(notice, "outcome") == string(outcome))
        .unwrap_or_else(|| panic!("a valid {outcome} notice"))
        .clone()
}

fn pair(outcome: Value, member: &str, value: Value) -> Value {
    object(vec![("outcome", outcome), (member, value)])
}

#[test]
fn notice_usage_follows_the_outcome() {
    let run = object(vec![("duration_ms", int(5))]);
    let turn = object(vec![
        ("duration_ms", int(5)),
        ("models", Value::Array(vec![])),
    ]);
    let created = notice("created");
    let usage = |outcome: &str, usage: &Value| pair(string(outcome), "usage", usage.clone());
    verdicts(
        &[
            (usage("created", &Value::Null), true),
            (usage("created", &run), false),
            (usage("created", &turn), false),
            (usage("created", member(&created, "usage")), true),
            (usage("deleted", &Value::Null), true),
            (usage("deleted", &run), false),
            (usage("healthy", &run), true),
            (usage("healthy", &Value::Null), false),
            (usage("done", &run), true),
            (pair(int(3), "usage", run.clone()), true),
        ],
        |value| routine_notice::notice_usage(member(value, "outcome"), member(value, "usage")),
    );
}

fn detail(outcome: &str, key: &str, value: Value) -> Value {
    pair(string(outcome), "detail", object(vec![(key, value)]))
}

#[test]
fn notice_details_are_exact_per_outcome() {
    let none = |outcome: &str| pair(string(outcome), "detail", object(vec![]));
    let steps = |count: usize| detail("created", "steps", Value::Array(vec![Value::Null; count]));
    verdicts(
        &[
            (detail("paused", "reason", string("policy")), true),
            (detail("paused", "reason", string("other")), false),
            (
                detail("skipped", "missed", big("100000000000000000000")),
                true,
            ),
            (detail("skipped", "missed", int(0)), false),
            (
                detail("skipped", "missed", big("-100000000000000000000")),
                false,
            ),
            (detail("healthy", "runs", int(60)), true),
            (detail("healthy", "runs", int(61)), false),
            (detail("healthy", "runs", int(-1)), false),
            (
                detail("frozen", "request_kind", string("integrations")),
                true,
            ),
            (detail("frozen", "request_kind", int(1)), false),
            (detail("failed", "code", string("a-1")), true),
            (detail("failed", "code", string("A")), false),
            (detail("streak-paused", "failures", int(3)), true),
            (detail("streak-paused", "failures", int(4)), false),
            (none("held"), true),
            (detail("held", "x", Value::Null), false),
            (detail("created", "steps", string("x")), false),
            (steps(0), true),
            (steps(65), false),
            (none("unknown"), false),
            (pair(int(1), "detail", object(vec![])), false),
        ],
        |value| {
            routine_notice::canonical_notice_detail(
                member(value, "outcome"),
                member(value, "detail"),
            )
            .is_some()
        },
    );
}

#[test]
fn creation_steps_are_finished_events_numbered_from_one() {
    let created = notice("created");
    let steps = items(member(member(&created, "detail"), "steps"));
    let created_with = |steps: Vec<Value>| detail("created", "steps", Value::Array(steps));
    let first_step = &steps[0];
    verdicts(
        &[
            (created_with(steps.to_vec()), true),
            (created_with(vec![with(first_step, "seq", int(2))]), false),
            (
                created_with(vec![with(first_step, "state", string("started"))]),
                false,
            ),
            (
                created_with(vec![with(first_step, "phase", string("unknown"))]),
                false,
            ),
        ],
        |value| {
            routine_notice::canonical_notice_detail(
                member(value, "outcome"),
                member(value, "detail"),
            )
            .is_some()
        },
    );
}

#[test]
fn completed_runs_show_their_last_step() {
    let done = member(&notice("done"), "detail").clone();
    let output = member(&done, "output").clone();
    let steps = match member(member(&done, "plan"), "steps") {
        Value::Number(Number::Int(steps)) => *steps,
        other => panic!("a step count: {other:?}"),
    };
    assert_ne!(output, Value::Null, "the vector shows its output");
    verdicts(
        &[
            (done.clone(), true),
            (with(&done, "output", Value::Null), true),
            (with(&done, "plan", Value::Null), false),
            (with(&done, "output", object(vec![])), false),
            (
                with(&done, "output", with(&output, "step", int(steps + 1))),
                false,
            ),
        ],
        |value| routine_notice::canonical_notice_detail(&string("done"), value).is_some(),
    );
}

#[test]
fn notices_bind_their_run_and_outcome() {
    let (done, healthy) = (notice("done"), notice("healthy"));
    verdicts(
        &[
            (done.clone(), true),
            (with(&done, "run_id", Value::Null), false),
            (with(&done, "run_id", string(&"b".repeat(32))), false),
            (with(&done, "version", int(0)), false),
            (with(&done, "version", big("100000000000000000000")), true),
            (with(&done, "protection_lost", int(0)), false),
            (with(&done, "team_id", Value::Null), false),
            (with(&done, "notice_id", string("x")), false),
            (with(&done, "routine_id", string("x")), false),
            (with(&done, "name", Value::Null), false),
            (with(&done, "created_at", string("x")), false),
            (with(&healthy, "run_id", string("x")), false),
            (without(&done, "usage"), false),
            (with(&healthy, "protection_lost", Value::Bool(true)), false),
            (
                with(&healthy, "run_id", member(&healthy, "notice_id").clone()),
                false,
            ),
        ],
        |value| routine_notice::canonical_notice(value).is_some(),
    );
}

fn batch(notices: Vec<Value>, more: bool) -> Value {
    object(vec![
        ("notices", Value::Array(notices)),
        ("more", Value::Bool(more)),
    ])
}

/// 200 distinct copies of a run notice, beyond the batch's byte bound.
fn many(done: &Value) -> Vec<Value> {
    (0..200)
        .map(|index| {
            let id = string(&format!("{index:032x}"));
            with(&with(done, "notice_id", id.clone()), "run_id", id)
        })
        .collect()
}

#[test]
fn notice_batches_are_bounded_and_unique() {
    let done = notice("done");
    let wide = many(&done);
    assert!(
        routine::encoded_bytes(&Value::Array(wide.clone()))
            > routine_notice::MAX_NOTICE_BATCH_BYTES
    );
    verdicts(
        &[
            (batch(vec![done.clone()], true), true),
            (batch(wide[..2].to_vec(), false), true),
            (batch(vec![], false), true),
            (batch(vec![], true), false),
            (batch(vec![done.clone(), done.clone()], false), false),
            (batch(vec![Value::Null], false), false),
            (batch(vec![done.clone(); 1025], false), false),
            (batch(wide, false), false),
            (with(&batch(vec![], false), "more", int(0)), false),
            (with(&batch(vec![], false), "notices", Value::Null), false),
        ],
        |value| routine_notice::canonical_notice_batch(value).is_some(),
    );
}

fn view(path: &str, frozen: bool) -> Value {
    items(member(group(path), "valid"))
        .iter()
        .find(|view| (*member(view, "status") == string("frozen")) == frozen)
        .expect("a run vector")
        .clone()
}

#[test]
fn run_views_name_a_frozen_request_only() {
    let (frozen, leased) = (
        view("routine_views.run", true),
        view("routine_views.run", false),
    );
    verdicts(
        &[
            (frozen.clone(), true),
            (leased.clone(), true),
            (with(&frozen, "request_kind", Value::Null), false),
            (with(&frozen, "assistant_id", Value::Null), false),
            (with(&frozen, "action", Value::Null), false),
            (with(&leased, "action", string("x")), false),
            (with(&leased, "status", string("other")), false),
            (with(&leased, "run_id", string("x")), false),
        ],
        |value| routine_notice::canonical_run_view(value).is_some(),
    );
}

#[test]
fn incidents_name_a_held_call_or_nothing() {
    let incident = first("routine_views.incident.valid");
    let empty = ["assistant_id", "action", "position", "steps"]
        .into_iter()
        .fold(incident.clone(), |value, key| {
            with(&value, key, Value::Null)
        });
    verdicts(
        &[
            (incident.clone(), true),
            (empty.clone(), true),
            (with(&empty, "steps", int(1)), false),
            (with(&incident, "position", int(400)), false),
            (with(&incident, "action", Value::Null), false),
            (with(&incident, "assistant_id", string("X")), false),
            (with(&incident, "routine_id", Value::Null), false),
        ],
        |value| routine_notice::canonical_incident_view(value).is_some(),
    );
}

#[test]
fn routine_views_check_every_member() {
    let view = first("routine_views.routine.valid");
    let cases = refused(
        &view,
        vec![
            ("routine_id", Value::Null),
            ("name", string(" x")),
            ("plan", Value::Null),
            ("output", Value::Null),
            ("schedule", Value::Null),
            ("timezone", Value::Null),
            ("next_run_at", string("2026-02-30T00:00:00Z")),
            ("needs_reconfirm", int(0)),
            ("deleting", int(1)),
            ("state", string("stopped")),
        ],
    );
    verdicts(&cases, |value| {
        routine_notice::canonical_routine_view(value).is_some()
    });
}

fn no_run(hint: Value) -> Value {
    object(vec![("run", Value::Null), ("next_due_at", hint)])
}

#[test]
fn claims_without_a_run_carry_a_wake_hint() {
    assert_eq!(
        routine_run::canonical_claim(&no_run(int(5))),
        Some(no_run(int(5)))
    );
    verdicts(
        &[
            (no_run(big("100000000000000000000")), true),
            (no_run(Value::Null), true),
            (no_run(int(0)), false),
            (no_run(Value::Bool(true)), false),
        ],
        |value| routine_run::canonical_claim(value).is_some(),
    );
}

#[test]
fn claimed_runs_carry_their_lease() {
    let claimed = items(member(group("routine_views.claim"), "valid"))
        .iter()
        .find(|claim| *member(claim, "run") != Value::Null)
        .expect("a claimed run vector")
        .clone();
    assert_eq!(
        routine_run::canonical_claim(&claimed),
        Some(claimed.clone())
    );
    let run = member(&claimed, "run").clone();
    let mut cases: Vec<(Value, bool)> = [
        ("active_seconds", int(0)),
        ("active_seconds", int(7201)),
        ("team_id", Value::Null),
        ("run_id", Value::Null),
        ("lease_token", string("x")),
        ("revision", int(0)),
        ("plan_digest", string("x")),
    ]
    .into_iter()
    .map(|(key, bad)| (with(&claimed, "run", with(&run, key, bad)), false))
    .collect();
    cases.push((with(&claimed, "next_due_at", int(5)), false));
    cases.push((with(&claimed, "run", int(1)), false));
    verdicts(&cases, |value| {
        routine_run::canonical_claim(value).is_some()
    });
}

#[test]
fn small_answers_rebuild_their_members() {
    let one = |key: &str, value: Value| object(vec![(key, value)]);
    let digest = string(&format!("sha256:{}", "0".repeat(64)));
    let segment = object(vec![("revision", int(2)), ("plan_digest", digest)]);
    let rebuilt = [
        (
            one("tag", string(&"a".repeat(64))),
            routine_run::canonical_card_answer_request as fn(&Value) -> Option<Value>,
        ),
        (
            one("long", Value::Bool(false)),
            routine_run::canonical_claim_request,
        ),
        (
            one("locale", string("pt")),
            routine_run::canonical_challenge_open,
        ),
        (segment.clone(), routine_run::canonical_segment_request),
    ];
    assert!(
        rebuilt
            .iter()
            .all(|(value, admit)| admit(value).as_ref() == Some(value))
    );
    let refused = [
        (
            one("tag", string("A")),
            routine_run::canonical_card_answer_request as fn(&Value) -> Option<Value>,
        ),
        (one("long", int(0)), routine_run::canonical_claim_request),
        (
            one("locale", string("xx")),
            routine_run::canonical_challenge_open,
        ),
        (
            with(&segment, "revision", int(0)),
            routine_run::canonical_segment_request,
        ),
        (
            with(&segment, "plan_digest", int(0)),
            routine_run::canonical_segment_request,
        ),
    ];
    assert!(refused.iter().all(|(value, admit)| admit(value).is_none()));
}

#[test]
fn answers_name_their_team_and_run() {
    let answer = object(vec![
        ("team_id", string("team_1")),
        ("run_id", string(&"a".repeat(32))),
        ("status", string("held")),
    ]);
    verdicts(
        &[
            (answer.clone(), true),
            (with(&answer, "status", string("waiting")), false),
            (with(&answer, "team_id", Value::Null), false),
        ],
        |value| routine_run::canonical_segment_answer(value).is_some(),
    );
    verdicts(
        &[
            (without(&answer, "status"), true),
            (
                without(&with(&answer, "run_id", Value::Null), "status"),
                false,
            ),
        ],
        |value| routine_run::canonical_card_answer(value).is_some(),
    );
}

fn diagnostic() -> Value {
    items(member(group("routine_diagnostics"), "valid"))
        .iter()
        .flat_map(|case| items(member(case, "diagnostics")).iter())
        .find(|diagnostic| *member(diagnostic, "failure") != Value::Null)
        .expect("a failure diagnostic vector")
        .clone()
}

#[test]
fn failures_are_sanitized() {
    let failure = member(&diagnostic(), "failure").clone();
    let mut cases: Vec<(Value, bool)> = [
        ("provider", Value::Null, true),
        (
            "provider",
            string(&format!("{}.com", "a.".repeat(125))),
            false,
        ),
        (
            "provider",
            string(&format!("{}com", "a.".repeat(125))),
            true,
        ),
        (
            "provider",
            string(&format!("{}com", "a.".repeat(126))),
            false,
        ),
        ("provider", string("A"), false),
        ("http_status", Value::Null, true),
        ("http_status", int(599), true),
        ("http_status", int(600), false),
        ("http_status", Value::Bool(true), false),
        ("response_excerpt", Value::Null, true),
        ("response_excerpt", string("a\u{202e}"), false),
        ("message", string("tab\tand\nline"), true),
        ("message", string("\u{feff}"), false),
        ("message", string(&"é".repeat(1024)), true),
        ("message", string(&"é".repeat(1025)), false),
        ("message", int(1), false),
        ("error_type", string(" "), false),
        ("redacted", int(0), false),
        ("truncated", Value::Null, false),
    ]
    .into_iter()
    .map(|(key, value, admitted)| (with(&failure, key, value), admitted))
    .collect();
    cases.push((without(&failure, "truncated"), false));
    verdicts(&cases, |value| {
        routine_run::canonical_failure(value).is_some()
    });
}

#[test]
fn diagnostics_hold_one_failure_or_condition() {
    let item = diagnostic();
    let condition = with(&item, "failure", Value::Null);
    let mut cases = refused(
        &item,
        vec![
            ("operation_id", string("x")),
            ("attempt", int(65)),
            ("attempt", int(0)),
            ("assistant_id", Value::Null),
            ("action", Value::Null),
            ("position", int(257)),
            ("recorded_at", string("x")),
            ("condition", string("timeout")),
            ("failure", object(vec![])),
            ("failure", Value::Null),
        ],
    );
    cases.push((
        with(&condition, "condition", string("exit-status:-12")),
        true,
    ));
    cases.push((
        with(&condition, "condition", string("exit-status:12345678901")),
        false,
    ));
    verdicts(&cases, |value| {
        routine_run::canonical_diagnostic(value).is_some()
    });
}

fn diagnostics(entries: Vec<Value>) -> Value {
    object(vec![
        ("team_id", string("team_1")),
        ("run_id", string(&"a".repeat(32))),
        ("diagnostics", Value::Array(entries)),
    ])
}

#[test]
fn diagnostics_are_oldest_first_and_unique() {
    let item = diagnostic();
    let later = with(&item, "attempt", int(2));
    let earlier = with(&later, "recorded_at", string("2000-01-01T00:00:00Z"));
    verdicts(
        &[
            (diagnostics(vec![]), true),
            (diagnostics(vec![item.clone()]), true),
            (diagnostics(vec![item.clone(), later]), true),
            (diagnostics(vec![item.clone(), earlier]), false),
            (diagnostics(vec![item.clone(), item.clone()]), false),
            (diagnostics(vec![Value::Null]), false),
            (diagnostics(vec![item; 33]), false),
            (with(&diagnostics(vec![]), "team_id", Value::Null), false),
            (
                with(&diagnostics(vec![]), "diagnostics", Value::Null),
                false,
            ),
        ],
        |value| routine_run::canonical_diagnostics(value).is_some(),
    );
}

#[test]
fn cards_show_only_their_own_diagnostic() {
    let recorded = items(member(group("routine_views.card"), "valid"))
        .iter()
        .find(|card| *member(card, "evidence") == string("recorded"))
        .expect("a recorded card vector")
        .clone();
    let absent = with(
        &with(&recorded, "evidence", string("absent")),
        "diagnostic",
        Value::Null,
    );
    let moved = with(
        member(&recorded, "diagnostic"),
        "action",
        string("other-action"),
    );
    let mut cases = refused(
        &recorded,
        vec![
            ("evidence", string("absent")),
            ("diagnostic", object(vec![])),
            ("evidence", int(1)),
            ("position", int(400)),
            ("diagnostic", moved),
        ],
    );
    cases.extend(refused(
        &absent,
        vec![
            ("team_id", Value::Null),
            ("tag", string("x")),
            ("assistant_id", Value::Null),
            ("run_id", Value::Null),
            ("action", Value::Null),
        ],
    ));
    verdicts(&cases, |value| routine_run::canonical_card(value).is_some());
}

fn run_step(position: i64) -> Value {
    object(vec![
        ("position", int(position)),
        ("status", string("done")),
        ("assistant_id", string("shimpz-cloudflare")),
        ("action", string("list-zones")),
        ("attempt", int(1)),
        ("duration_ms", int(10)),
        ("inputs", Value::Null),
    ])
}

fn step_at(value: &Value) -> bool {
    routine_run::canonical_run_step(value, 1, &int(3)).is_some()
}

#[test]
fn run_steps_say_what_each_attempt_did() {
    let step = run_step(1);
    let gap = ["assistant_id", "action", "attempt", "duration_ms", "inputs"]
        .into_iter()
        .fold(with(&step, "status", string("not_run")), |value, key| {
            with(&value, key, Value::Null)
        });
    let recovered = with(&step, "status", string("recovered"));
    let mut cases = refused(
        &step,
        vec![
            ("position", int(2)),
            ("position", Value::Number(Number::Float(1.0))),
            ("status", string("other")),
            ("assistant_id", Value::Null),
            ("action", Value::Null),
            ("attempt", int(65)),
            ("duration_ms", int(1 << 53)),
            ("duration_ms", int(-1)),
        ],
    );
    cases.push((gap.clone(), true));
    cases.push((with(&gap, "attempt", int(1)), false));
    cases.push((recovered.clone(), false));
    cases.push((with(&recovered, "duration_ms", Value::Null), true));
    verdicts(&cases, step_at);
    assert!(routine_run::canonical_run_step(&step, 1, &int(0)).is_none());
}

fn input(value: Value) -> Value {
    object(vec![("member", string("zone")), ("value", value)])
}

/// A step whose 64 widest inputs exceed the step view's byte bound.
fn wide_step() -> Value {
    let inputs = (0..64)
        .map(|index| {
            object(vec![
                ("member", string(&format!("{index:03}{}", "m".repeat(125)))),
                ("value", string(&"é".repeat(120))),
            ])
        })
        .collect();
    with(&run_step(1), "inputs", Value::Array(inputs))
}

#[test]
fn run_step_inputs_are_redacted_previews() {
    let step = run_step(1);
    let wide = wide_step();
    assert!(routine::encoded_bytes(&wide) > routine::MAX_STEP_VIEW_BYTES);
    let inputs = |inputs: Vec<Value>| with(&step, "inputs", Value::Array(inputs));
    verdicts(
        &[
            (inputs(vec![input(Value::Null)]), true),
            (inputs(vec![input(string("a"))]), true),
            (inputs(vec![input(string(""))]), false),
            (
                inputs(vec![object(vec![
                    ("member", string("")),
                    ("value", Value::Null),
                ])]),
                false,
            ),
            (inputs(vec![Value::Null]), false),
            (wide, false),
        ],
        step_at,
    );
}

#[test]
fn run_step_pages_check_every_member() {
    let page = first("routine_views.run_steps.valid");
    let cases = refused(
        &page,
        vec![
            ("team_id", Value::Null),
            ("run_id", Value::Null),
            ("routine_id", Value::Null),
            ("revision", int(0)),
            ("plan_digest", Value::Null),
            ("snapshot", string("x")),
            ("steps", Value::Array(vec![Value::Null])),
        ],
    );
    verdicts(&cases, |value| {
        routine_run::canonical_run_steps(value).is_some()
    });
}
