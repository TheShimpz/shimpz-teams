//! Run control: cancellation, deadlines, spend, authority, definition changes, determinism, timers, and the
//! scheduler.

#![cfg(all(test, feature = "testing"))]

#[cfg(test)]
mod common;

use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::{Fixture, Plan, Setup, fixture, memory_store, plan};
use holdpoint::testing::{Behavior, HostAuthority, RecordingActivity};
use holdpoint::{
    ActivityOptions, Ctx, Definition, Driven, Error, Interrupt, LimitKind, ManualClock, ModelReply, Outcome,
    RunOptions, Store, Wait, Worker,
};
use serde_json::{Value, json};

#[cfg(test)]
fn submit(fixture: &Fixture, id: &str, steps: Value, options: &RunOptions) {
    fixture.worker.submit("plan", id, &plan(steps), options).unwrap();
}

#[test]
fn cancelling_a_waiting_run_ends_it_with_its_progress() {
    let fixture = fixture();
    let steps = json!([{ "kind": "local", "name": "double", "input": 2 }, { "kind": "deferred", "name": "ask", "requests": [1] }]);
    submit(&fixture, "c1", steps, &RunOptions::default());
    assert_eq!(fixture.worker.drive("c1").unwrap(), Driven::Suspended(Wait::Deferred));
    assert_eq!(fixture.worker.cancel("c1"), Ok(true));
    assert_eq!(fixture.worker.cancel("c1"), Ok(false));
    let outcome = Outcome::Cancelled { progress: json!({ "done": 1 }) };
    assert_eq!(fixture.worker.drive("c1").unwrap(), Driven::Ended(outcome));
    assert_eq!(fixture.worker.cancel("c1"), Ok(false));
}

#[test]
fn cancelling_a_running_attempt_abandons_it_promptly() {
    let mut setup = Setup::new(memory_store());
    setup.activity = RecordingActivity::new([Behavior::Sleep { ms: 5_000, progress: None }]);
    setup.activity_options = ActivityOptions::idempotent();
    let fixture = setup.build();
    submit(&fixture, "c2", json!([{ "kind": "activity", "name": "act", "input": 1 }]), &RunOptions::default());
    let worker = Arc::clone(&fixture.worker);
    let canceller = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(50));
        worker.cancel("c2").unwrap()
    });
    let started = std::time::Instant::now();
    let driven = fixture.worker.drive("c2").unwrap();
    assert!(canceller.join().unwrap());
    assert!(started.elapsed() < Duration::from_secs(2), "{:?}", started.elapsed());
    // The definition saw the abandoned step's result first, so its progress counts the step.
    assert_eq!(driven, Driven::Ended(Outcome::Cancelled { progress: json!({ "done": 1 }) }));
}

#[test]
fn a_run_past_its_deadline_expires_without_dispatching() {
    let clock = ManualClock::at(1_000);
    let fixture = Setup::new(memory_store()).build_with_clock(clock.clone());
    let steps = json!([{ "kind": "local", "name": "double", "input": 1 }, { "kind": "deferred", "name": "ask", "requests": [1] }, { "kind": "model", "name": "model" }]);
    submit(&fixture, "d1", steps, &RunOptions { deadline_ms: Some(2_000), spend_ceiling: None });
    assert_eq!(fixture.worker.drive("d1").unwrap(), Driven::Suspended(Wait::Deferred));
    fixture.worker.resolve("d1", 1, &json!("ok")).unwrap();
    clock.advance(5_000);
    let driven = fixture.worker.drive("d1").unwrap();
    assert_eq!(driven, Driven::Ended(Outcome::Expired { progress: json!({ "done": 2 }) }));
    assert_eq!(fixture.model.requests().len(), 0);
}

#[test]
fn a_deadline_wakes_a_run_waiting_for_the_host() {
    let clock = ManualClock::at(1_000);
    let fixture = Setup::new(memory_store()).build_with_clock(clock);
    submit(
        &fixture,
        "d2",
        json!([{ "kind": "deferred", "name": "ask", "requests": [1] }]),
        &RunOptions { deadline_ms: Some(9_000), spend_ceiling: None },
    );
    fixture.worker.drive("d2").unwrap();
    assert_eq!(fixture.store.next_wake().unwrap(), Some(9_000));
}

#[test]
fn spend_never_exceeds_the_ceiling() {
    let mut setup = Setup::new(memory_store());
    setup.model = holdpoint::testing::ScriptedModel::new([json!("a"), json!("b")], 4).reserving(6);
    let fixture = setup.build();
    let steps = json!([{ "kind": "model", "name": "model" }, { "kind": "model", "name": "model" }]);
    submit(&fixture, "s1", steps, &RunOptions { deadline_ms: None, spend_ceiling: Some(9) });
    let driven = fixture.worker.drive("s1").unwrap();
    assert_eq!(driven, Driven::Ended(Outcome::Limited { kind: LimitKind::Spend, progress: json!({ "done": 1 }) }));
    assert_eq!(fixture.model.requests().len(), 1);
}

#[test]
fn authority_is_asked_once_per_session_and_can_make_the_run_wait() {
    let fixture = fixture();
    fixture.host.set_authority(HostAuthority::Wait);
    let steps =
        json!([{ "kind": "activity", "name": "act", "input": 1 }, { "kind": "activity", "name": "act", "input": 2 }]);
    submit(&fixture, "a1", steps, &RunOptions::default());
    assert_eq!(fixture.worker.drive("a1").unwrap(), Driven::Suspended(Wait::Authority));
    assert_eq!(fixture.activity.calls().len(), 0);
    fixture.host.set_authority(HostAuthority::Granted);
    fixture.worker.wake("a1").unwrap();
    assert_eq!(fixture.worker.drive("a1").unwrap(), Driven::Ended(Outcome::Done { result: json!([1, 2]) }));
    assert_eq!(fixture.host.authorized(), 2);
}

#[test]
fn revoked_authority_ends_the_run_before_any_dispatch() {
    let fixture = fixture();
    fixture.host.set_authority(HostAuthority::Revoked);
    submit(&fixture, "a2", json!([{ "kind": "activity", "name": "act", "input": 1 }]), &RunOptions::default());
    let outcome = Outcome::Unrecoverable { reason: "authority-revoked".into() };
    assert_eq!(fixture.worker.drive("a2").unwrap(), Driven::Ended(outcome));
    assert_eq!(fixture.activity.calls().len(), 0);
}

#[test]
fn a_changed_definition_version_is_never_replayed() {
    let store = memory_store();
    let first = Setup::new(Arc::clone(&store)).build();
    first
        .worker
        .submit(
            "plan",
            "v1",
            &plan(json!([{ "kind": "deferred", "name": "ask", "requests": [1] }])),
            &RunOptions::default(),
        )
        .unwrap();
    first.worker.drive("v1").unwrap();
    let mut setup = Setup::new(store);
    setup.version = 2;
    let second = setup.build();
    let outcome = Outcome::Unrecoverable { reason: "definition-changed".into() };
    assert_eq!(second.worker.drive("v1").unwrap(), Driven::Ended(outcome));
}

/// A definition whose step depends on state outside its context: forbidden, and detected.
#[cfg(test)]
struct Drifting {
    name: Arc<Mutex<&'static str>>,
}

#[cfg(test)]
impl Definition for Drifting {
    fn id(&self) -> &'static str {
        "drifting"
    }

    fn version(&self) -> u32 {
        1
    }

    fn run(&self, ctx: &mut Ctx<'_>) -> Result<Outcome, Interrupt> {
        let name = *self.name.lock().unwrap();
        ctx.local(name, &json!(1), |value| Ok(value.clone()))?;
        ctx.deferred(&[("ask", json!(1))])?;
        Ok(Outcome::Done { result: Value::Null })
    }
}

#[test]
fn a_step_that_differs_on_replay_is_nondeterminism() {
    let name = Arc::new(Mutex::new("first"));
    let worker = Worker::builder(memory_store(), "t", holdpoint::testing::TestHost::default())
        .definition(Drifting { name: Arc::clone(&name) })
        .build();
    worker.submit("drifting", "n1", &Value::Null, &RunOptions::default()).unwrap();
    assert_eq!(worker.drive("n1").unwrap(), Driven::Suspended(Wait::Deferred));
    *name.lock().unwrap() = "second";
    let outcome = Outcome::Unrecoverable { reason: "nondeterminism".into() };
    assert_eq!(worker.drive("n1").unwrap(), Driven::Ended(outcome));
}

/// Ends without consuming its recorded steps on the second execution.
#[cfg(test)]
struct Shortcut {
    calls: Arc<Mutex<u32>>,
}

#[cfg(test)]
impl Definition for Shortcut {
    fn id(&self) -> &'static str {
        "shortcut"
    }

    fn version(&self) -> u32 {
        1
    }

    fn run(&self, ctx: &mut Ctx<'_>) -> Result<Outcome, Interrupt> {
        let mut calls = self.calls.lock().unwrap();
        *calls += 1;
        if *calls > 1 {
            return Ok(Outcome::Done { result: Value::Null });
        }
        drop(calls);
        ctx.deferred(&[("ask", json!(1))])?;
        Ok(Outcome::Done { result: Value::Null })
    }
}

#[test]
fn ending_before_the_recorded_steps_is_nondeterminism() {
    let worker = Worker::builder(memory_store(), "t", holdpoint::testing::TestHost::default())
        .definition(Shortcut { calls: Arc::default() })
        .build();
    worker.submit("shortcut", "n2", &Value::Null, &RunOptions::default()).unwrap();
    worker.drive("n2").unwrap();
    let outcome = Outcome::Unrecoverable { reason: "nondeterminism".into() };
    assert_eq!(worker.drive("n2").unwrap(), Driven::Ended(outcome));
}

#[cfg(test)]
struct Panicking;

#[cfg(test)]
impl Definition for Panicking {
    fn id(&self) -> &'static str {
        "panicking"
    }

    fn version(&self) -> u32 {
        1
    }

    fn run(&self, _ctx: &mut Ctx<'_>) -> Result<Outcome, Interrupt> {
        std::panic::resume_unwind(Box::new("definition bug"))
    }
}

#[test]
fn a_panicking_definition_is_unrecoverable() {
    let worker =
        Worker::builder(memory_store(), "t", holdpoint::testing::TestHost::default()).definition(Panicking).build();
    worker.submit("panicking", "p1", &Value::Null, &RunOptions::default()).unwrap();
    let outcome = Outcome::Unrecoverable { reason: "definition-panicked".into() };
    assert_eq!(worker.drive("p1").unwrap(), Driven::Ended(outcome));
}

#[test]
fn a_short_timer_fires_inside_the_drive_and_a_long_one_suspends() {
    let clock = ManualClock::at(10_000);
    let fixture = Setup::new(memory_store()).build_with_clock(clock.clone());
    let steps = json!([{ "kind": "sleep", "name": "short", "due_ms": 10_100 }, { "kind": "sleep", "name": "long", "due_ms": 60_000 }]);
    submit(&fixture, "t1", steps, &RunOptions::default());
    assert_eq!(fixture.worker.drive("t1").unwrap(), Driven::Suspended(Wait::Until(60_000)));
    assert_eq!(fixture.worker.drive("t1").unwrap(), Driven::Suspended(Wait::Until(60_000)));
    clock.advance(60_000);
    assert_eq!(fixture.worker.drive("t1").unwrap(), Driven::Ended(Outcome::Done { result: json!(["slept", "slept"]) }));
}

#[test]
fn a_model_failure_is_retried_until_it_answers() {
    let fixture = fixture();
    fixture.model.push(Err(holdpoint::Failure::retryable("http-503", "unavailable").with_ended()));
    fixture.model.push(Ok(ModelReply { response: json!("back"), cost: 1 }));
    submit(&fixture, "m1", json!([{ "kind": "model", "name": "model" }]), &RunOptions::default());
    assert_eq!(fixture.worker.drive("m1").unwrap(), Driven::Ended(Outcome::Done { result: json!(["back"]) }));
    assert_eq!(fixture.model.requests().len(), 2);
}

#[test]
fn an_unknown_model_or_activity_is_unrecoverable() {
    let fixture = fixture();
    submit(&fixture, "u1", json!([{ "kind": "model", "name": "nope" }]), &RunOptions::default());
    let outcome = Outcome::Unrecoverable { reason: "unknown-model:nope".into() };
    assert_eq!(fixture.worker.drive("u1").unwrap(), Driven::Ended(outcome));
    submit(&fixture, "u2", json!([{ "kind": "activity", "name": "nope", "input": 1 }]), &RunOptions::default());
    let outcome = Outcome::Unrecoverable { reason: "unknown-activity:nope".into() };
    assert_eq!(fixture.worker.drive("u2").unwrap(), Driven::Ended(outcome));
}

#[test]
fn the_scheduler_resumes_every_due_run() {
    let fixture = fixture();
    for id in ["s-a", "s-b", "s-c"] {
        submit(&fixture, id, json!([{ "kind": "activity", "name": "act", "input": id }]), &RunOptions::default());
    }
    fixture.worker.start().unwrap();
    fixture.worker.start().unwrap();
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    while ["s-a", "s-b", "s-c"].iter().any(|id| fixture.worker.result(id).unwrap().is_none()) {
        assert!(std::time::Instant::now() < deadline, "the scheduler did not finish the runs");
        std::thread::sleep(Duration::from_millis(5));
    }
    fixture.worker.stop();
    assert_eq!(fixture.worker.result("s-b").unwrap(), Some(Outcome::Done { result: json!(["s-b"]) }));
}

#[test]
fn a_run_driven_twice_at_once_is_busy() {
    let mut setup = Setup::new(memory_store());
    setup.activity = RecordingActivity::new([Behavior::Sleep { ms: 200, progress: None }]);
    let fixture = setup.build();
    submit(&fixture, "b1", json!([{ "kind": "activity", "name": "act", "input": 1 }]), &RunOptions::default());
    let worker = Arc::clone(&fixture.worker);
    let first = std::thread::spawn(move || worker.drive("b1"));
    std::thread::sleep(Duration::from_millis(50));
    assert!(matches!(fixture.worker.drive("b1"), Err(Error::Busy(_))));
    assert!(matches!(first.join().unwrap(), Ok(Driven::Ended(_))));
    assert_eq!(fixture.activity.calls().len(), 1);
}

#[test]
fn unknown_runs_are_state_errors() {
    let fixture = fixture();
    assert!(matches!(fixture.worker.drive("ghost"), Err(Error::State(_))));
    assert!(matches!(fixture.worker.cancel("ghost"), Err(Error::State(_))));
    assert!(matches!(fixture.worker.result("ghost"), Err(Error::State(_))));
    assert!(matches!(fixture.worker.drive("bad id"), Err(Error::Contract(_))));
}

#[test]
fn a_store_holds_only_its_schema_version() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("ledger.sqlite3");
    drop(Store::open(&path, holdpoint::Limits::default()).unwrap());
    rusqlite::Connection::open(&path).unwrap().pragma_update(None, "user_version", 99).unwrap();
    assert!(matches!(Store::open(&path, holdpoint::Limits::default()), Err(Error::Storage(_))));
}

#[test]
fn every_store_file_is_private() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("private.sqlite3");
    drop(Store::open(&path, holdpoint::Limits::default()).unwrap());
    assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
}

#[test]
fn plans_run_through_the_plan_definition() {
    assert_eq!(Plan { version: 1 }.id(), "plan");
}

/// A definition whose first step changes kind, or input, on its second execution.
#[cfg(test)]
struct Shifting {
    calls: Arc<Mutex<u32>>,
    change: &'static str,
}

#[cfg(test)]
impl Definition for Shifting {
    fn id(&self) -> &'static str {
        "shifting"
    }

    fn version(&self) -> u32 {
        1
    }

    fn run(&self, ctx: &mut Ctx<'_>) -> Result<Outcome, Interrupt> {
        let second = {
            let mut calls = self.calls.lock().unwrap();
            *calls += 1;
            *calls > 1
        };
        if second && self.change == "kind" {
            ctx.deferred(&[("step", json!(1))])?;
        } else {
            let input = if second { json!(2) } else { json!(1) };
            ctx.local("step", &input, |value| Ok(value.clone()))?;
        }
        ctx.deferred(&[("ask", json!(1))])?;
        Ok(Outcome::Done { result: Value::Null })
    }
}

#[test]
fn a_step_whose_kind_or_input_changes_on_replay_is_nondeterminism() {
    for change in ["kind", "input"] {
        let worker = Worker::builder(memory_store(), "t", holdpoint::testing::TestHost::default())
            .definition(Shifting { calls: Arc::default(), change })
            .build();
        worker.submit("shifting", "s", &Value::Null, &RunOptions::default()).unwrap();
        assert_eq!(worker.drive("s").unwrap(), Driven::Suspended(Wait::Deferred));
        let outcome = Outcome::Unrecoverable { reason: "nondeterminism".into() };
        assert_eq!(worker.drive("s").unwrap(), Driven::Ended(outcome), "{change}");
    }
}

#[test]
fn a_retry_past_the_schedule_to_close_bound_fails_the_step() {
    let mut setup = Setup::new(memory_store());
    setup.activity =
        RecordingActivity::new([Behavior::Fail(holdpoint::Failure::retryable("busy", "later").with_ended())]);
    setup.activity_options = ActivityOptions {
        idempotent: true,
        retry: holdpoint::RetryPolicy { initial_ms: 500, max_attempts: None, ..holdpoint::RetryPolicy::default() },
        timeouts: holdpoint::Timeouts { schedule_to_close_ms: Some(100), ..holdpoint::Timeouts::default() },
        ..ActivityOptions::external()
    };
    let fixture = setup.build();
    submit(&fixture, "stc", json!([{ "kind": "activity", "name": "act", "input": 1 }]), &RunOptions::default());
    assert_eq!(
        fixture.worker.drive("stc").unwrap(),
        Driven::Ended(Outcome::Done { result: json!([{ "failed": "busy" }]) })
    );
    assert_eq!(fixture.activity.calls().len(), 1);
}

#[test]
fn a_failed_model_call_without_confirmation_keeps_its_reservation() {
    let mut setup = Setup::new(memory_store());
    setup.model = holdpoint::testing::ScriptedModel::default().reserving(5);
    setup.model.push(Err(holdpoint::Failure::retryable("timeout", "no answer")));
    setup.model.push(Ok(ModelReply { response: json!("ok"), cost: 1 }));
    let fixture = setup.build();
    let steps = json!([{ "kind": "model", "name": "model" }, { "kind": "spent" }]);
    submit(&fixture, "keep", steps, &RunOptions::default());
    assert_eq!(fixture.worker.drive("keep").unwrap(), Driven::Ended(Outcome::Done { result: json!(["ok", 6]) }));
}

#[test]
fn a_long_attempt_keeps_its_lease_renewed() {
    let mut setup = Setup::new(memory_store());
    setup.activity = RecordingActivity::new([Behavior::Sleep { ms: 250, progress: None }]);
    setup.options = holdpoint::WorkerOptions { lease_ms: 60, inline_wait_ms: 10, ..common::fast_options() };
    let fixture = setup.build();
    submit(&fixture, "renew", json!([{ "kind": "activity", "name": "act", "input": 1 }]), &RunOptions::default());
    let before = fixture.store.commits();
    assert!(matches!(fixture.worker.drive("renew").unwrap(), Driven::Ended(Outcome::Done { .. })));
    assert!(fixture.store.commits() - before >= 4, "renewals commit while the attempt runs");
}

#[test]
fn run_ids_are_bounded_and_identities_compare_definition_and_version() {
    let fixture = fixture();
    for id in [String::new(), "x".repeat(129)] {
        assert!(matches!(
            fixture.worker.submit("plan", &id, &plan(json!([])), &RunOptions::default()),
            Err(Error::Contract(_))
        ));
    }
    let store = memory_store();
    let first = Setup::new(Arc::clone(&store)).build();
    first.worker.submit("plan", "same", &plan(json!([])), &RunOptions::default()).unwrap();
    let mut newer = Setup::new(Arc::clone(&store));
    newer.version = 2;
    let newer = newer.build();
    assert!(matches!(
        newer.worker.submit("plan", "same", &plan(json!([])), &RunOptions::default()),
        Err(Error::Conflict(_))
    ));
    let other = Worker::builder(store, "t", holdpoint::testing::TestHost::default())
        .definition(Shifting { calls: Arc::default(), change: "input" })
        .build();
    assert!(matches!(
        other.submit("shifting", "same", &plan(json!([])), &RunOptions::default()),
        Err(Error::Conflict(_))
    ));
}

#[test]
fn a_dispatch_that_would_overfill_the_history_ends_the_run_limited() {
    let limits = holdpoint::Limits { max_events_per_run: 6, ..holdpoint::Limits::default() };
    let fixture = Setup::new(Arc::new(Store::open_in_memory(limits).unwrap())).build();
    let steps = json!((0..5).map(|n| json!({ "kind": "activity", "name": "act", "input": n })).collect::<Vec<_>>());
    submit(&fixture, "full", steps, &RunOptions::default());
    assert!(matches!(
        fixture.worker.drive("full").unwrap(),
        Driven::Ended(Outcome::Limited { kind: LimitKind::History, .. })
    ));
}

#[test]
fn a_history_over_its_byte_bound_ends_the_run_limited() {
    let limits = holdpoint::Limits { max_run_bytes: 1_500, ..holdpoint::Limits::default() };
    let mut setup = Setup::new(Arc::new(Store::open_in_memory(limits).unwrap()));
    setup.activity = RecordingActivity::new([Behavior::Value(json!("b".repeat(400)))]);
    let fixture = setup.build();
    let steps = json!((0..5).map(|n| json!({ "kind": "activity", "name": "act", "input": n })).collect::<Vec<_>>());
    submit(&fixture, "bytes", steps, &RunOptions::default());
    assert!(matches!(
        fixture.worker.drive("bytes").unwrap(),
        Driven::Ended(Outcome::Limited { kind: LimitKind::History, .. })
    ));
}

#[test]
fn round3_03_an_effect_receipt_lands_even_past_the_history_bound() {
    let limits = holdpoint::Limits { max_events_per_run: 3, ..holdpoint::Limits::default() };
    let fixture = Setup::new(Arc::new(Store::open_in_memory(limits).unwrap())).build();
    let steps =
        json!([{ "kind": "activity", "name": "act", "input": 1 }, { "kind": "activity", "name": "act", "input": 2 }]);
    submit(&fixture, "receipt", steps, &RunOptions::default());
    assert!(matches!(
        fixture.worker.drive("receipt").unwrap(),
        Driven::Ended(Outcome::Limited { kind: LimitKind::History, .. })
    ));
    let history = fixture.worker.history("receipt").unwrap();
    assert!(
        history.iter().any(|event| matches!(event, holdpoint::Event::StepCompleted { step: 0, .. })),
        "{history:?}"
    );
    assert_eq!(fixture.activity.calls().len(), 1);
}

#[test]
fn round3_02_a_model_that_cannot_estimate_its_cost_is_refused_under_a_ceiling() {
    struct Unpriced;
    impl holdpoint::Model for Unpriced {
        fn reserve(&self, _request: &Value) -> Option<u64> {
            None
        }
        fn call(&self, _call: holdpoint::ModelCall<'_>) -> Result<ModelReply, holdpoint::Failure> {
            Ok(ModelReply { response: json!("paid"), cost: 100 })
        }
    }
    let worker = Worker::builder(memory_store(), "t", holdpoint::testing::TestHost::default())
        .definition(Plan { version: 1 })
        .model("model", holdpoint::ModelOptions::default(), Unpriced)
        .build();
    let steps = plan(json!([{ "kind": "model", "name": "model" }]));
    worker.submit("plan", "ceiling", &steps, &RunOptions { deadline_ms: None, spend_ceiling: Some(1_000) }).unwrap();
    assert!(matches!(worker.drive("ceiling").unwrap(), Driven::Ended(Outcome::Limited { kind: LimitKind::Spend, .. })));
    worker.submit("plan", "free", &steps, &RunOptions::default()).unwrap();
    assert_eq!(worker.drive("free").unwrap(), Driven::Ended(Outcome::Done { result: json!(["paid"]) }));
}
