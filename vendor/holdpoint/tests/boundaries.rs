//! Exact boundaries and cadences: bounds that admit exactly their limit, renewals at their rate, timers at their
//! time, the scheduler's wakeups, cross-process cancellation, and the round-4 audit's scenarios.

#![cfg(all(test, feature = "testing"))]

#[cfg(test)]
mod common;

use std::sync::Arc;
use std::time::{Duration, Instant};

use common::{Plan, Setup, file_store, memory_store, plan};
use holdpoint::testing::{Behavior, RecordingActivity, ScriptedModel, TestHost};
use holdpoint::{
    ActivityOptions, Driven, Error, Event, LimitKind, Limits, ManualClock, ModelOptions, ModelReply, Outcome,
    RetryPolicy, RunOptions, Store, Timeouts, Wait, Worker, WorkerOptions,
};
use serde_json::{Value, json};

#[cfg(test)]
fn act(input: &Value) -> Value {
    json!({ "kind": "activity", "name": "act", "input": input })
}

#[cfg(test)]
fn wait_for(mut done: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !done() {
        assert!(Instant::now() < deadline, "timed out waiting");
        std::thread::sleep(Duration::from_millis(2));
    }
}

#[test]
fn leases_renew_at_a_third_of_their_length() {
    let mut setup = Setup::new(memory_store());
    setup.activity = RecordingActivity::new([Behavior::Sleep { ms: 300, progress: None }]);
    setup.options = WorkerOptions { lease_ms: 90, inline_wait_ms: 10, poll_ms: 5, ..common::fast_options() };
    let fixture = setup.build();
    fixture.worker.submit("plan", "r", &plan(json!([act(&json!(1))])), &RunOptions::default()).unwrap();
    let before = fixture.store.commits();
    assert!(matches!(fixture.worker.drive("r").unwrap(), Driven::Ended(_)));
    let commits = fixture.store.commits() - before;
    // Dispatch, a renewal every 30 ms for 300 ms, the receipt, and the end.
    assert!((7..=16).contains(&commits), "{commits} commits");
}

#[test]
fn a_timer_exactly_one_inline_wait_away_fires_inside_the_drive_at_its_time() {
    let clock = ManualClock::at(1_000);
    let mut setup = Setup::new(memory_store());
    setup.options = WorkerOptions { inline_wait_ms: 200, poll_ms: 7, ..common::fast_options() };
    let fixture = setup.build_with_clock(clock.clone());
    let steps = plan(json!([{ "kind": "sleep", "name": "edge", "due_ms": 1_200 }]));
    fixture.worker.submit("plan", "t", &steps, &RunOptions::default()).unwrap();
    assert_eq!(fixture.worker.drive("t").unwrap(), Driven::Ended(Outcome::Done { result: json!(["slept"]) }));
    assert!(holdpoint::Clock::now_ms(&clock) >= 1_200 && holdpoint::Clock::now_ms(&clock) < 1_207);
}

#[test]
fn a_reservation_reaching_the_ceiling_exactly_is_admitted() {
    let mut setup = Setup::new(memory_store());
    setup.model = ScriptedModel::new([json!("fits")], 5).reserving(5);
    let fixture = setup.build();
    let options = RunOptions { deadline_ms: None, spend_ceiling: Some(5) };
    fixture.worker.submit("plan", "c", &plan(json!([{ "kind": "model", "name": "model" }])), &options).unwrap();
    assert_eq!(fixture.worker.drive("c").unwrap(), Driven::Ended(Outcome::Done { result: json!(["fits"]) }));
}

#[test]
fn a_resolution_fitting_the_history_exactly_is_recorded_and_one_more_is_refused() {
    let limits = Limits { max_events_per_run: 4, ..Limits::default() };
    let fixture = Setup::new(Arc::new(Store::open_in_memory(limits).unwrap())).build();
    let steps = plan(json!([{ "kind": "deferred", "name": "ask", "requests": [1, 2] }]));
    fixture.worker.submit("plan", "h", &steps, &RunOptions::default()).unwrap();
    fixture.worker.drive("h").unwrap();
    assert!(fixture.worker.resolve("h", 0, &json!("a")).is_ok());
    assert!(matches!(fixture.worker.resolve("h", 1, &json!("b")), Err(Error::Limit(LimitKind::History))));
    let bytes = Limits { max_run_bytes: 10_000, ..Limits::default() };
    let fixture = Setup::new(Arc::new(Store::open_in_memory(bytes).unwrap())).build();
    fixture.worker.submit("plan", "b", &steps, &RunOptions::default()).unwrap();
    fixture.worker.drive("b").unwrap();
    let used: usize =
        fixture.worker.history("b").unwrap().iter().map(|event| event.encode(1 << 20).unwrap().len()).sum();
    let filler = 10_000 - used - r#"{"deferred_resolved":{"result":"","step":0}}"#.len();
    assert!(fixture.worker.resolve("b", 0, &json!("x".repeat(filler))).is_ok(), "exactly at the byte bound");
    assert!(matches!(fixture.worker.resolve("b", 1, &json!("y")), Err(Error::Limit(LimitKind::History))));
}

#[test]
fn a_lease_expiring_exactly_now_can_be_claimed() {
    let store = memory_store();
    let first = Setup::new(Arc::clone(&store)).build_with_clock(ManualClock::at(10_000));
    let steps = plan(json!([act(&json!(1))]));
    first.worker.submit("plan", "e", &steps, &RunOptions::default()).unwrap();
    // A failing release leaves the lease held by a session that then disappears.
    let failing = TestHost::default();
    failing.set_authority(holdpoint::testing::HostAuthority::Wait);
    let holder = Worker::builder(Arc::clone(&store), "holder", failing)
        .definition(Plan { version: 1 })
        .clock(ManualClock::at(10_000))
        .options(WorkerOptions { lease_ms: 1_000, ..common::fast_options() })
        .build();
    let _ = holder.drive("e");
    let later = Setup::new(Arc::clone(&store)).build_with_clock(ManualClock::at(11_000));
    assert!(!matches!(later.worker.drive("e"), Err(Error::Busy(_))));
}

#[test]
fn a_cancellation_from_another_process_reaches_a_running_attempt() {
    let dir = tempfile::tempdir().unwrap();
    let mut setup = Setup::new(file_store(&dir));
    setup.activity = RecordingActivity::new([Behavior::Sleep { ms: 5_000, progress: None }]);
    setup.activity_options = ActivityOptions::idempotent();
    let fixture = setup.build();
    fixture.worker.submit("plan", "x", &plan(json!([act(&json!(1)), act(&json!(2))])), &RunOptions::default()).unwrap();
    let other = Store::open(&dir.path().join("ledger.sqlite3"), Limits::default()).unwrap();
    let store = Arc::clone(&fixture.store);
    std::thread::spawn(move || {
        wait_for(|| store.history("x").unwrap().iter().any(|event| matches!(event, Event::StepStarted { .. })));
        other.cancel("x", holdpoint::Clock::now_ms(&holdpoint::SystemClock)).unwrap();
    });
    let started = Instant::now();
    assert!(matches!(fixture.worker.drive("x").unwrap(), Driven::Ended(Outcome::Cancelled { .. })));
    assert!(started.elapsed() < Duration::from_secs(2));
}

#[test]
fn a_worker_at_its_abandoned_capacity_does_not_retry_a_model() {
    let hang = |_call: holdpoint::ModelCall<'_>| {
        std::thread::sleep(Duration::from_millis(400));
        Ok(ModelReply { response: json!("late"), cost: 0 })
    };
    let options = ModelOptions {
        retry: RetryPolicy { initial_ms: 1, max_attempts: Some(5), ..RetryPolicy::default() },
        timeouts: Timeouts { start_to_close_ms: Some(20), ..Timeouts::default() },
    };
    let worker = Worker::builder(memory_store(), "cap", TestHost::default())
        .definition(Plan { version: 1 })
        .model("slow", options, hang)
        .options(WorkerOptions { max_abandoned: 1, abandon_grace_ms: 10, ..common::fast_options() })
        .build();
    worker.submit("plan", "m", &plan(json!([{ "kind": "model", "name": "slow" }])), &RunOptions::default()).unwrap();
    assert!(matches!(worker.drive("m").unwrap(), Driven::Suspended(Wait::Executor(_))));
}

#[test]
fn an_unfinished_model_step_never_runs_under_changed_model_options() {
    let store = memory_store();
    let clock = ManualClock::at(1_000);
    let failing = ScriptedModel::default();
    failing.push(Err(holdpoint::Failure::retryable("http-503", "down").with_ended()));
    let slow_retry =
        ModelOptions { retry: RetryPolicy { initial_ms: 60_000, ..RetryPolicy::default() }, ..ModelOptions::default() };
    let first = Worker::builder(Arc::clone(&store), "a", TestHost::default())
        .definition(Plan { version: 1 })
        .model("model", slow_retry, failing)
        .clock(clock.clone())
        .build();
    first.submit("plan", "p", &plan(json!([{ "kind": "model", "name": "model" }])), &RunOptions::default()).unwrap();
    assert!(matches!(first.drive("p").unwrap(), Driven::Suspended(Wait::Until(_))));
    let second = Worker::builder(store, "b", TestHost::default())
        .definition(Plan { version: 1 })
        .model("model", ModelOptions::default(), ScriptedModel::new([json!("x")], 0))
        .clock(clock)
        .build();
    let outcome = Outcome::Unrecoverable { reason: "definition-changed".into() };
    assert_eq!(second.drive("p").unwrap(), Driven::Ended(outcome));
}

#[test]
fn the_scheduler_drives_a_new_or_woken_run_without_waiting_for_its_idle_tick() {
    let fixture = common::fixture();
    fixture.worker.start().unwrap();
    std::thread::sleep(Duration::from_millis(30));
    let started = Instant::now();
    fixture.worker.submit("plan", "n", &plan(json!([act(&json!(1))])), &RunOptions::default()).unwrap();
    wait_for(|| fixture.worker.result("n").unwrap().is_some());
    assert!(started.elapsed() < Duration::from_millis(300), "{:?}", started.elapsed());
    fixture.host.set_authority(holdpoint::testing::HostAuthority::Wait);
    fixture.worker.submit("plan", "w", &plan(json!([act(&json!(2))])), &RunOptions::default()).unwrap();
    std::thread::sleep(Duration::from_millis(100));
    fixture.host.set_authority(holdpoint::testing::HostAuthority::Granted);
    fixture.worker.wake("w").unwrap();
    wait_for(|| fixture.worker.result("w").unwrap().is_some());
    fixture.worker.stop();
}

#[test]
fn round4_01_heartbeats_never_crowd_out_a_receipt_or_the_end() {
    let limits = Limits { max_events_per_run: 3, ..Limits::default() };
    let beating = |call: holdpoint::ActivityCall<'_>| {
        for beat in 0..200 {
            call.attempt.heartbeat(json!(beat)).ok();
            std::thread::sleep(Duration::from_micros(200));
        }
        Ok(holdpoint::ActivityReply::of(json!("done")))
    };
    let worker = Worker::builder(Arc::new(Store::open_in_memory(limits).unwrap()), "beats", TestHost::default())
        .definition(Plan { version: 1 })
        .activity("act", ActivityOptions::external(), beating)
        .options(WorkerOptions { poll_ms: 1, lease_ms: 60_000, ..common::fast_options() })
        .build();
    worker.submit("plan", "hb", &plan(json!([act(&json!(1)), act(&json!(2))])), &RunOptions::default()).unwrap();
    assert!(matches!(worker.drive("hb").unwrap(), Driven::Ended(Outcome::Limited { kind: LimitKind::History, .. })));
    let history = worker.history("hb").unwrap();
    assert!(history.iter().any(|event| matches!(event, Event::StepCompleted { step: 0, .. })), "the receipt landed");
    let beats = history.iter().filter(|event| matches!(event, Event::StepHeartbeat { .. })).count();
    assert!(beats <= 2, "{beats} heartbeats");
}

#[test]
fn round4_04_a_model_that_declares_no_cost_estimate_is_refused_under_a_ceiling() {
    let closure = |_call: holdpoint::ModelCall<'_>| Ok(ModelReply { response: json!("paid"), cost: 100 });
    let worker = Worker::builder(memory_store(), "c", TestHost::default())
        .definition(Plan { version: 1 })
        .model("model", ModelOptions::default(), closure)
        .build();
    let options = RunOptions { deadline_ms: None, spend_ceiling: Some(0) };
    worker.submit("plan", "z", &plan(json!([{ "kind": "model", "name": "model" }])), &options).unwrap();
    assert!(matches!(worker.drive("z").unwrap(), Driven::Ended(Outcome::Limited { kind: LimitKind::Spend, .. })));
}

#[test]
fn results_nested_up_to_the_envelope_margin_are_kept() {
    let mut deep = json!(1);
    for _ in 0..20 {
        deep = json!([deep]);
    }
    let mut setup = Setup::new(memory_store());
    setup.activity = RecordingActivity::new([Behavior::Value(deep.clone())]);
    let fixture = setup.build();
    fixture.worker.submit("plan", "d", &plan(json!([act(&json!(1))])), &RunOptions::default()).unwrap();
    assert_eq!(fixture.worker.drive("d").unwrap(), Driven::Ended(Outcome::Done { result: json!([deep]) }));
}

#[test]
fn a_model_call_cancelled_without_an_answer_ends_the_run_cancelled() {
    let slow = |_call: holdpoint::ModelCall<'_>| {
        std::thread::sleep(Duration::from_millis(800));
        Ok(ModelReply { response: json!("late"), cost: 0 })
    };
    let worker = Arc::new(
        Worker::builder(memory_store(), "c", TestHost::default())
            .definition(Plan { version: 1 })
            .model("slow", ModelOptions::default(), slow)
            .options(WorkerOptions { abandon_grace_ms: 10, ..common::fast_options() })
            .build(),
    );
    worker.submit("plan", "mc", &plan(json!([{ "kind": "model", "name": "slow" }])), &RunOptions::default()).unwrap();
    let canceller = Arc::clone(&worker);
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(50));
        canceller.cancel("mc").unwrap();
    });
    assert!(matches!(worker.drive("mc").unwrap(), Driven::Ended(Outcome::Cancelled { .. })));
}

#[test]
fn round5_01_heartbeats_that_do_not_fit_never_cost_the_receipt() {
    let limits = Limits { max_events_per_run: 3, max_value_bytes: 2_000, ..Limits::default() };
    let beating = |call: holdpoint::ActivityCall<'_>| {
        call.attempt.heartbeat(json!("h".repeat(9_000))).ok();
        std::thread::sleep(Duration::from_millis(5));
        for beat in 0..60 {
            call.attempt.heartbeat(json!(beat)).ok();
            std::thread::sleep(Duration::from_millis(2));
        }
        Ok(holdpoint::ActivityReply::of(json!("done")))
    };
    let worker = Worker::builder(Arc::new(Store::open_in_memory(limits).unwrap()), "beats", TestHost::default())
        .definition(Plan { version: 1 })
        .activity("act", ActivityOptions::external(), beating)
        .options(WorkerOptions { poll_ms: 1, lease_ms: 15, inline_wait_ms: 1, ..common::fast_options() })
        .build();
    worker.submit("plan", "hb", &plan(json!([act(&json!(1)), act(&json!(2))])), &RunOptions::default()).unwrap();
    assert!(matches!(worker.drive("hb").unwrap(), Driven::Ended(Outcome::Limited { kind: LimitKind::History, .. })));
    let history = worker.history("hb").unwrap();
    assert!(history.iter().any(|event| matches!(event, Event::StepCompleted { step: 0, .. })), "the receipt landed");
}

#[test]
fn round5_02_a_model_failing_during_the_expiry_grace_ends_the_run_expired() {
    let failing = |_call: holdpoint::ModelCall<'_>| {
        std::thread::sleep(Duration::from_millis(200));
        Err(holdpoint::Failure::retryable("http-503", "down"))
    };
    let worker = Worker::builder(memory_store(), "x", TestHost::default())
        .definition(Plan { version: 1 })
        .model("m", ModelOptions::default(), failing)
        .options(WorkerOptions { abandon_grace_ms: 1_000, ..common::fast_options() })
        .build();
    let now = holdpoint::Clock::now_ms(&holdpoint::SystemClock);
    let options = RunOptions { deadline_ms: Some(now + 80), spend_ceiling: None };
    worker.submit("plan", "e", &plan(json!([{ "kind": "model", "name": "m" }])), &options).unwrap();
    assert!(matches!(worker.drive("e").unwrap(), Driven::Ended(Outcome::Expired { .. })));
}

#[test]
fn round6_a_failure_arriving_after_the_deadline_between_polls_ends_the_run_expired() {
    let failing = |_call: holdpoint::ModelCall<'_>| {
        std::thread::sleep(Duration::from_millis(500));
        Err(holdpoint::Failure::retryable("http-503", "down"))
    };
    let worker = Worker::builder(memory_store(), "x", TestHost::default())
        .definition(Plan { version: 1 })
        .model("m", ModelOptions::default(), failing)
        .options(WorkerOptions { poll_ms: 2_000, ..common::fast_options() })
        .build();
    let now = holdpoint::Clock::now_ms(&holdpoint::SystemClock);
    let options = RunOptions { deadline_ms: Some(now + 200), spend_ceiling: None };
    worker.submit("plan", "p", &plan(json!([{ "kind": "model", "name": "m" }])), &options).unwrap();
    assert!(matches!(worker.drive("p").unwrap(), Driven::Ended(Outcome::Expired { .. })));
}

#[test]
fn round6_an_answer_arriving_after_a_cancellation_is_recorded_and_the_run_cancelled() {
    let store = memory_store();
    let slot: Arc<std::sync::OnceLock<Arc<Worker>>> = Arc::new(std::sync::OnceLock::new());
    let canceller = Arc::clone(&slot);
    let answering = move |_call: holdpoint::ModelCall<'_>| {
        // Cancelled through the driving worker itself, which signals the session in memory.
        canceller.get().map(|worker| worker.cancel("q"));
        Ok(ModelReply { response: json!("kept"), cost: 1 })
    };
    let worker = Arc::new(
        Worker::builder(Arc::clone(&store), "x", TestHost::default())
            .definition(Plan { version: 1 })
            .model("m", ModelOptions::default(), answering)
            .options(WorkerOptions { poll_ms: 2_000, ..common::fast_options() })
            .build(),
    );
    let _ = slot.set(Arc::clone(&worker));
    let steps = plan(json!([{ "kind": "model", "name": "m" }, { "kind": "model", "name": "m" }]));
    worker.submit("plan", "q", &steps, &RunOptions::default()).unwrap();
    let driven = worker.drive("q").unwrap();
    assert!(matches!(driven, Driven::Ended(Outcome::Cancelled { .. })), "{driven:?}");
    assert!(store.history("q").unwrap().iter().any(|event| matches!(event, Event::StepCompleted { step: 0, .. })));
}
