//! Regression tests for the first post-result audit: each test reproduces one finding's failure scenario.

#![cfg(all(test, feature = "testing"))]

#[cfg(test)]
mod common;

use std::sync::Arc;

use common::{Racing, Setup, file_store, memory_store, plan};
use holdpoint::testing::{Behavior, RecordingActivity, TestHost};
use holdpoint::{
    ActivityOptions, Driven, Error, Event, LimitKind, Limits, ManualClock, Outcome, Proof, RetryPolicy, RunOptions,
    Store, Wait, Worker,
};
use serde_json::{Value, json};

#[cfg(test)]
fn racing_worker(store: Arc<Store>, host: Racing, clock: ManualClock) -> Worker {
    Worker::builder(store, "audit", host)
        .definition(common::Plan { version: 1 })
        .activity("act", ActivityOptions::external(), RecordingActivity::new([Behavior::Echo]))
        .clock(clock)
        .options(common::fast_options())
        .build()
}

#[cfg(test)]
fn now() -> i64 {
    holdpoint::Clock::now_ms(&holdpoint::SystemClock)
}

#[test]
fn finding_01_deleting_a_run_proves_an_expired_session_first() {
    let dir = tempfile::tempdir().unwrap();
    let store = file_store(&dir);
    let stale = Setup::new(Arc::clone(&store));
    let mut stale = stale;
    stale.activity = RecordingActivity::new([Behavior::Sleep { ms: 200, progress: None }]);
    let stale = stale.build_with_clock(ManualClock::at(now() - 60_000));
    stale
        .worker
        .submit("plan", "d1", &plan(json!([{ "kind": "activity", "name": "act", "input": 1 }])), &RunOptions::default())
        .unwrap();
    let worker = Arc::clone(&stale.worker);
    let driving = std::thread::spawn(move || worker.drive("d1"));
    while !store.history("d1").unwrap().iter().any(|event| matches!(event, Event::StepStarted { .. })) {
        std::thread::sleep(std::time::Duration::from_millis(2));
    }
    let host = Racing::default();
    *host.proof.lock().unwrap() = Some(Proof::NotYet);
    let deleter = racing_worker(Arc::clone(&store), host.clone(), ManualClock::at(now()));
    assert!(matches!(deleter.delete("d1"), Err(Error::State(_))), "an expired session must be proven ended");
    *host.proof.lock().unwrap() = Some(Proof::Ended);
    assert_eq!(deleter.delete("d1"), Ok(()));
    let _ = driving.join();
}

#[test]
fn finding_02_a_deletion_is_refused_when_the_run_changed_during_its_proofs() {
    let store = memory_store();
    let failing = TestHost::default();
    failing.fail_releases();
    let parked = Worker::builder(Arc::clone(&store), "audit", failing)
        .definition(common::Plan { version: 1 })
        .activity("act", ActivityOptions::external(), RecordingActivity::new([Behavior::Echo]))
        .build();
    let steps = plan(
        json!([{ "kind": "activity", "name": "act", "input": 1 }, { "kind": "deferred", "name": "ask", "requests": [1] }]),
    );
    parked.submit("plan", "d3", &steps, &RunOptions::default()).unwrap();
    assert_eq!(parked.drive("d3").unwrap(), Driven::Suspended(Wait::Deferred));
    assert_eq!(store.obligations("d3").unwrap().len(), 1);
    let racer = racing_worker(Arc::clone(&store), Racing::default(), ManualClock::at(now()));
    let host = Racing::default();
    let resolver = Arc::clone(&store);
    *host.on_prove.lock().unwrap() = Some(Box::new(move || {
        resolver.resolve("d3", 1, &json!("go"), now()).unwrap();
        let _ = racer.drive("d3");
    }));
    let deleter = racing_worker(Arc::clone(&store), host, ManualClock::at(now()));
    assert!(matches!(deleter.delete("d3"), Err(Error::State(_))), "a run that moved on is not deleted");
    assert!(store.result("d3").is_ok());
}

#[test]
fn finding_04_a_cancellation_committed_before_the_end_wins() {
    let store = memory_store();
    let host = Racing::default();
    let worker = racing_worker(Arc::clone(&store), host.clone(), ManualClock::at(now()));
    worker
        .submit("plan", "c1", &plan(json!([{ "kind": "activity", "name": "act", "input": 1 }])), &RunOptions::default())
        .unwrap();
    let canceller = Arc::clone(&store);
    *host.on_release.lock().unwrap() = Some(Box::new(move || {
        assert!(canceller.cancel("c1", now()).unwrap());
    }));
    let Driven::Ended(outcome) = worker.drive("c1").unwrap() else { panic!("the run did not end") };
    assert!(matches!(outcome, Outcome::Cancelled { .. }), "{outcome:?}");
    assert_eq!(store.result("c1").unwrap(), Some(outcome));
}

#[test]
fn finding_05_nothing_is_dispatched_once_a_slow_authorization_passes_the_deadline() {
    let clock = ManualClock::at(1_000);
    let host = Racing::default();
    let worker = racing_worker(memory_store(), host.clone(), clock.clone());
    let options = RunOptions { deadline_ms: Some(2_000), spend_ceiling: None };
    worker.submit("plan", "t1", &plan(json!([{ "kind": "activity", "name": "act", "input": 1 }])), &options).unwrap();
    let later = clock.clone();
    *host.on_authorize.lock().unwrap() = Some(Box::new(move || later.advance(5_000)));
    assert!(matches!(worker.drive("t1").unwrap(), Driven::Ended(Outcome::Expired { .. })));
    assert!(!worker.history("t1").unwrap().iter().any(|event| matches!(event, Event::StepStarted { .. })));
}

#[test]
fn finding_06_a_long_wait_observes_cancellation_and_expiry() {
    let clock = ManualClock::at(10_000);
    let worker = racing_worker(memory_store(), Racing::default(), clock.clone());
    let sleep = plan(json!([{ "kind": "sleep", "name": "later", "due_ms": 9_000_000 }]));
    worker.submit("plan", "w1", &sleep, &RunOptions::default()).unwrap();
    assert_eq!(worker.drive("w1").unwrap(), Driven::Suspended(Wait::Until(9_000_000)));
    worker.cancel("w1").unwrap();
    assert!(matches!(worker.drive("w1").unwrap(), Driven::Ended(Outcome::Cancelled { .. })));
    worker.submit("plan", "w2", &sleep, &RunOptions { deadline_ms: Some(20_000), spend_ceiling: None }).unwrap();
    assert_eq!(worker.drive("w2").unwrap(), Driven::Suspended(Wait::Until(9_000_000)));
    assert_eq!(worker.store().next_wake().unwrap(), Some(20_000), "the deadline wakes the run first");
    clock.advance(15_000);
    assert!(matches!(worker.drive("w2").unwrap(), Driven::Ended(Outcome::Expired { .. })));
}

#[test]
fn finding_08_values_too_deep_for_the_ledger_are_refused_before_they_are_written() {
    let worker = racing_worker(memory_store(), Racing::default(), ManualClock::at(now()));
    let mut deep = json!(1);
    for _ in 0..62 {
        deep = json!([deep]);
    }
    let input = json!({ "steps": [], "deep": deep });
    assert!(matches!(
        worker.submit("plan", "deep", &input, &RunOptions::default()),
        Err(Error::Limit(LimitKind::Value))
    ));
    let fine = plan(json!([{ "kind": "local", "name": "double", "input": 1 }]));
    worker.submit("plan", "fine", &fine, &RunOptions::default()).unwrap();
    assert!(matches!(worker.drive("fine").unwrap(), Driven::Ended(Outcome::Done { .. })));
}

#[test]
fn finding_09_resolutions_respect_the_history_bounds_and_a_full_run_still_ends() {
    let limits = Limits { max_events_per_run: 4, ..Limits::default() };
    let store = Arc::new(Store::open_in_memory(limits).unwrap());
    let worker = racing_worker(Arc::clone(&store), Racing::default(), ManualClock::at(now()));
    let steps = plan(json!([{ "kind": "deferred", "name": "ask", "requests": [1, 2, 3] }]));
    worker.submit("plan", "h1", &steps, &RunOptions::default()).unwrap();
    assert_eq!(worker.drive("h1").unwrap(), Driven::Suspended(Wait::Deferred));
    assert!(matches!(worker.resolve("h1", 0, &json!("a")), Err(Error::Limit(LimitKind::History))));
    worker.cancel("h1").unwrap();
    assert!(matches!(worker.drive("h1").unwrap(), Driven::Ended(Outcome::Cancelled { .. })));
    let long =
        plan(json!((0..40).map(|n| json!({ "kind": "local", "name": "double", "input": n })).collect::<Vec<_>>()));
    worker.submit("plan", "h2", &long, &RunOptions::default()).unwrap();
    assert!(matches!(worker.drive("h2").unwrap(), Driven::Ended(Outcome::Limited { kind: LimitKind::History, .. })));
}

#[test]
fn finding_10_a_run_is_bound_to_its_options_and_its_steps_to_their_policies() {
    let store = memory_store();
    let worker = racing_worker(Arc::clone(&store), Racing::default(), ManualClock::at(now()));
    let input = plan(
        json!([{ "kind": "deferred", "name": "ask", "requests": [1] }, { "kind": "activity", "name": "act", "input": 1 }]),
    );
    worker.submit("plan", "o1", &input, &RunOptions { deadline_ms: Some(i64::MAX), spend_ceiling: None }).unwrap();
    let changed = RunOptions { deadline_ms: Some(i64::MAX - 1), spend_ceiling: None };
    assert!(matches!(worker.submit("plan", "o1", &input, &changed), Err(Error::Conflict(_))));
    let ceiling = RunOptions { deadline_ms: Some(i64::MAX), spend_ceiling: Some(1) };
    assert!(matches!(worker.submit("plan", "o1", &input, &ceiling), Err(Error::Conflict(_))));

    let first = Worker::builder(Arc::clone(&store), "audit", TestHost::default())
        .definition(common::Plan { version: 1 })
        .activity("act", ActivityOptions::external(), RecordingActivity::new([Behavior::Hang { linger_ms: 0 }]))
        .options(holdpoint::WorkerOptions { abandon_grace_ms: 10, ..common::fast_options() })
        .build();
    let hang = plan(json!([{ "kind": "activity", "name": "act", "input": 1 }]));
    first.submit("plan", "o2", &hang, &RunOptions::default()).unwrap();
    std::thread::spawn(move || {
        let _ = first.drive("o2");
    });
    while !store.history("o2").unwrap().iter().any(|event| matches!(event, Event::StepStarted { .. })) {
        std::thread::sleep(std::time::Duration::from_millis(2));
    }
    let retried = ActivityOptions { idempotent: true, retry: RetryPolicy::default(), ..ActivityOptions::external() };
    let second = Worker::builder(Arc::clone(&store), "audit", TestHost::default())
        .definition(common::Plan { version: 1 })
        .activity("act", retried, RecordingActivity::new([Behavior::Echo]))
        .clock(ManualClock::at(now() + 60_000))
        .build();
    let outcome = second.drive("o2").unwrap();
    assert_eq!(outcome, Driven::Ended(Outcome::Unrecoverable { reason: "definition-changed".into() }));
}

#[test]
fn finding_09b_a_run_at_its_cap_ends_with_bounded_headroom() {
    let limits = Limits { max_events_per_run: 3, ..Limits::default() };
    let store = Arc::new(Store::open_in_memory(limits).unwrap());
    let worker = racing_worker(store, Racing::default(), ManualClock::at(now()));
    worker.submit("plan", "cap", &plan(json!([])), &RunOptions::default()).unwrap();
    assert_eq!(worker.drive("cap").unwrap(), Driven::Ended(Outcome::Done { result: Value::Array(vec![]) }));
}
