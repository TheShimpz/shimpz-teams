use strict_json::Value;
use team_protocol::python::json::{CANONICAL, dumps};
use team_protocol::routine::encoded_bytes;
use team_protocol::routine_notice::{MAX_NOTICE_BATCH_BYTES, canonical_notice_batch};
use team_routine::record::{self, Notice, Routine, TeamRoutines};
use team_routine::runs;
use tempfile::TempDir;

use super::{AckError, acknowledge, batch, deliveries, wire};
use crate::routine::state::none_lost;
use crate::routine::store::{RoutineStore, StoreError};
use crate::routine::testing::{
    ENTROPY, NINE, added, claim_bound, confirmed, entropy, json, large_completion, plan_document,
};

const ROUTINE: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const NAME: &str = "Resumo das mudanças de DNS";
/// A minute boundary: 09:00:00.
const MINUTE: i64 = NINE;
/// The active time each run of these tests spends.
const RUN_MS: i64 = 1200;

/// A one-step continuous Routine that shows no result, which rolls its healthy runs up per minute (ADR-0101).
fn defined(gap: i64) -> Routine {
    let mut value = confirmed(Routine::new(
        ROUTINE,
        NAME,
        json(&format!(r#"{{"kind": "continuous", "gap": {gap}}}"#)),
        Vec::new(),
        plan_document("UTC", Some(r#"{"mode": "none", "step": null}"#)),
        crate::routine::testing::NINE - 86_400 * 30,
        0,
    ));
    value.next_run_at = record::first_run(&value).expect("a first firing");
    value
}

fn at(state: &TeamRoutines, next_run_at: i64) -> TeamRoutines {
    let mut next = state.clone();
    let mut value = record::routine(&next, ROUTINE).expect("a Routine").clone();
    value.next_run_at = next_run_at;
    record::replace_routine(&mut next, value);
    next
}

fn continuous() -> TeamRoutines {
    at(&added(&[defined(5)]), MINUTE)
}

/// Claims the Routine at `start` and lets its worker finish it at `end`.
fn run_once(state: &TeamRoutines, start: i64, end: i64) -> TeamRoutines {
    let (state, claim, lease) = claim_bound(&at(state, start), start);
    let state = runs::spend(&state, &claim.run.run_id, &lease, start, (1, RUN_MS)).expect("spent");
    runs::finish(&state, &claim.run.run_id, &lease, end, None, &ENTROPY).expect("finished")
}

/// The healthy rollups Admin receives in one delivery, exactly as Team serves them.
fn delivery(state: &TeamRoutines) -> Vec<Value> {
    state
        .notices
        .iter()
        .filter(|item| item.outcome == "healthy")
        .map(|item| wire("team_1", item))
        .collect()
}

fn acknowledged(state: &TeamRoutines, batch: &[Value]) -> TeamRoutines {
    let delivered: Vec<(String, i64)> = batch
        .iter()
        .map(|item| {
            let Some(Value::String(notice)) = item.get("notice_id") else {
                panic!("a notice id")
            };
            let Some(Value::Number(strict_json::Number::Int(version))) = item.get("version") else {
                panic!("a version")
            };
            (notice.clone(), *version)
        })
        .collect();
    record::acknowledge(state, &delivered)
}

/// The state as a restarted Team reads it back from its store.
fn restarted(state: &TeamRoutines) -> TeamRoutines {
    let directory = TempDir::new().expect("a directory");
    let store = RoutineStore::new(
        &directory.path().join("state"),
        &directory.path().join("key/k"),
        entropy(),
    )
    .expect("a store");
    let written = state.clone();
    store
        .update("team_1", |_before| Ok::<_, StoreError>((written, ())))
        .expect("written");
    store.load("team_1").expect("loaded")
}

fn vector(case: &str) -> Value {
    let vectors = strict_json::parse(include_bytes!("../../../../protocol/http/v1/vectors.json"))
        .expect("vectors");
    vectors
        .get("routine_rollup_delivery")
        .and_then(|delivery| delivery.get(case))
        .and_then(|case| case.get("deliveries"))
        .cloned()
        .expect("the vector")
}

fn canonical_deliveries(deliveries: &[Vec<Value>]) -> String {
    let value = Value::Array(
        deliveries
            .iter()
            .map(|batch| Value::Array(batch.clone()))
            .collect(),
    );
    dumps(&value, CANONICAL)
}

#[test]
fn a_change_restart_and_late_acknowledgment_in_one_minute_keep_counting_one_notice() {
    let state = run_once(&continuous(), MINUTE, MINUTE + 1);
    let first = delivery(&state);
    let mut state = restarted(&run_once(&state, MINUTE + 10, MINUTE + 11));
    let changed = record::scheduled(&defined(6), MINUTE + 20).expect("scheduled");
    state = record::update(&state, &changed, 1, MINUTE + 20, &ENTROPY).expect("updated");
    state = run_once(&state, MINUTE + 30, MINUTE + 31);
    // The first delivery's acknowledgment arrives only now, after the minute's count grew twice.
    state = acknowledged(&state, &first);
    let last = delivery(&state);
    let deliveries = vec![first, last.clone()];
    assert_eq!(
        canonical_deliveries(&deliveries),
        dumps(&vector("same_minute_change"), CANONICAL)
    );
    state = acknowledged(&state, &last);
    assert_eq!(
        state
            .notices
            .iter()
            .map(|item| item.outcome.as_str())
            .collect::<Vec<_>>(),
        ["changed"]
    );
    // Every delivery is exactly a batch the protocol admits.
    for batch in &deliveries {
        let body = super::object_of(vec![
            ("notices", Value::Array(batch.clone())),
            ("more", Value::Bool(false)),
        ]);
        assert_eq!(canonical_notice_batch(&body), Some(body));
    }
}

#[test]
fn a_clock_stepped_back_into_a_delivered_minute_never_republishes_it() {
    let mut deliveries = Vec::new();
    let mut state = continuous();
    for (start, end) in [
        (MINUTE, MINUTE + 1),
        (MINUTE + 10, MINUTE + 11),
        (MINUTE + 60, MINUTE + 61),
    ] {
        state = run_once(&state, start, end);
        if end != MINUTE + 1 {
            deliveries.push(delivery(&state));
            state = acknowledged(&state, &deliveries[deliveries.len() - 1]);
        }
    }
    // A run whose clock fell back into a delivered minute shows nothing, as a run of none does.
    state = run_once(&state, MINUTE + 30, MINUTE + 31);
    assert_eq!(state.notices.len(), 0);
    deliveries.push(delivery(&state));
    state = run_once(&state, MINUTE + 70, MINUTE + 71);
    deliveries.push(delivery(&state));
    assert_eq!(
        canonical_deliveries(&deliveries),
        dumps(&vector("backward_clock"), CANONICAL)
    );
}

fn store(directory: &TempDir) -> RoutineStore {
    RoutineStore::new(
        &directory.path().join("state"),
        &directory.path().join("key/k"),
        entropy(),
    )
    .expect("a store")
}

fn large_notices(count: usize) -> Vec<Notice> {
    (0..count)
        .map(|index| {
            let mut notice = Notice::new(
                &format!("{index:032x}"),
                ROUTINE,
                &format!("{index:032x}"),
                "done",
                NINE,
                large_completion(),
            );
            notice.name = NAME.to_owned();
            notice.usage = Some(json(r#"{"duration_ms": 1}"#));
            notice
        })
        .collect()
}

#[test]
fn a_backlog_of_maximum_notices_drains_in_bounded_batches() {
    let directory = TempDir::new().expect("a directory");
    let store = store(&directory);
    let mut state = added(&[defined(5)]);
    state.notices = large_notices(20);
    store
        .update("team_1", |_before| Ok::<_, StoreError>((state, ())))
        .expect("written");
    let (mut delivered, mut more) = (0, true);
    while more {
        let next = batch(&store).expect("a batch");
        assert_eq!(next.skipped, Vec::<String>::new());
        assert_eq!(
            canonical_notice_batch(&next.body).as_ref(),
            Some(&next.body)
        );
        let Some(Value::Array(notices)) = next.body.get("notices") else {
            panic!("notices")
        };
        assert!(
            !notices.is_empty()
                && encoded_bytes(&Value::Array(notices.clone())) <= MAX_NOTICE_BATCH_BYTES
        );
        let items: Vec<Value> = notices
            .iter()
            .map(|item| {
                let member = |name: &str| item.get(name).cloned().expect("a member");
                super::object_of(vec![
                    ("team_id", member("team_id")),
                    ("notice_id", member("notice_id")),
                    ("version", member("version")),
                ])
            })
            .collect();
        delivered += items.len();
        let body = super::object_of(vec![("deliveries", Value::Array(items))]);
        acknowledge(&store, &none_lost, &body).expect("acknowledged");
        more = next.body.get("more") == Some(&Value::Bool(true));
    }
    assert_eq!(delivered, 20);
    assert_eq!(store.load("team_1").expect("loaded").notices.len(), 0);
}

#[test]
fn notices_are_listed_for_every_readable_team_and_acknowledged_by_exact_version() {
    let directory = TempDir::new().expect("a directory");
    let store = store(&directory);
    let state = run_once(&continuous(), MINUTE, MINUTE + 1);
    for team in ["team_1", "team_2"] {
        let written = state.clone();
        store
            .update(team, |_before| Ok::<_, StoreError>((written, ())))
            .expect("written");
    }
    // A Team whose state is damaged is skipped and named, never blocking the others.
    let damaged = directory
        .path()
        .join("state")
        .join(RoutineStore::team_dir_name("team_2"))
        .join("state.json");
    std::fs::write(&damaged, br#"{"team_id": "team_2"}"#).expect("written");
    let listed = batch(&store).expect("a batch");
    assert_eq!(listed.skipped, ["team_2"]);
    let Some(Value::Array(notices)) = listed.body.get("notices") else {
        panic!("notices")
    };
    assert_eq!(notices.len(), 1);
    let notice = notices[0].get("notice_id").cloned().expect("an id");
    let ack = |version: i64| {
        json(&format!(
            r#"{{"deliveries": [{{"team_id": "team_1", "notice_id": {}, "version": {version}}}]}}"#,
            dumps(&notice, CANONICAL)
        ))
    };
    acknowledge(&store, &none_lost, &ack(2)).expect("acknowledged");
    assert_eq!(store.load("team_1").expect("loaded").notices.len(), 1);
    acknowledge(&store, &none_lost, &ack(1)).expect("acknowledged");
    assert_eq!(store.load("team_1").expect("loaded").notices.len(), 0);
}

#[test]
fn only_exact_bounded_deliveries_are_acknowledged() {
    for body in [
        "{}",
        r#"{"deliveries": []}"#,
        r#"{"deliveries": [{"team_id": "team_1"}]}"#,
        r#"{"deliveries": [{"team_id": "team_1", "notice_id": "n", "version": true}]}"#,
        r#"{"deliveries": [{"team_id": "team_1", "notice_id": "n", "version": 1, "extra": 1}]}"#,
        r#"{"deliveries": [], "more": 1}"#,
    ] {
        assert_eq!(deliveries(&json(body)), Err(AckError::Invalid), "{body}");
    }
    let invalid_team =
        json(r#"{"deliveries": [{"team_id": "Team", "notice_id": "n", "version": 1}]}"#);
    assert_eq!(deliveries(&invalid_team), Err(AckError::InvalidTeam));
    let item = r#"{"team_id": "team_1", "notice_id": "n", "version": 1}"#;
    let many = format!(r#"{{"deliveries": [{}]}}"#, vec![item; 257].join(","));
    assert_eq!(deliveries(&json(&many)), Err(AckError::Invalid));
    let duplicated = format!(r#"{{"deliveries": [{item}, {item}]}}"#);
    assert_eq!(
        deliveries(&json(&duplicated)).expect("admitted")["team_1"],
        [("n".to_owned(), 1)]
    );
}
