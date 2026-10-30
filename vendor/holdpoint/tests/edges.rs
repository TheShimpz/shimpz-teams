//! Edge behaviors of steps: oversized and over-deep results, blobs, panics, capacity, deadlines during attempts,
//! policies of every containment, and the context's own accessors.

#![cfg(all(test, feature = "testing"))]

#[cfg(test)]
mod common;

use std::sync::Arc;

use common::{Fixture, Setup, memory_store, plan};
use holdpoint::testing::{Behavior, RecordingActivity};
use holdpoint::{
    ActivityOptions, Containment, Driven, Failure, Limits, ModelOptions, ModelReply, Outcome, Proof, RetryPolicy,
    RunOptions, Store, Timeouts, Wait, WorkerOptions,
};
use serde_json::{Value, json};

#[cfg(test)]
fn with(behaviors: Vec<Behavior>, options: ActivityOptions, limits: Limits) -> Fixture {
    let mut setup = Setup::new(Arc::new(Store::open_in_memory(limits).unwrap()));
    setup.activity = RecordingActivity::new(behaviors);
    setup.activity_options = options;
    setup.build()
}

#[cfg(test)]
fn run(fixture: &Fixture, id: &str, steps: Value) -> Driven {
    fixture.worker.submit("plan", id, &plan(steps), &RunOptions::default()).unwrap();
    fixture.worker.drive(id).unwrap()
}

#[cfg(test)]
fn done(result: Value) -> Driven {
    Driven::Ended(Outcome::Done { result })
}

#[cfg(test)]
fn act(input: &Value) -> Value {
    json!({ "kind": "activity", "name": "act", "input": input })
}

#[test]
fn oversized_and_over_deep_activity_results_fail_their_step() {
    let mut deep = json!(1);
    for _ in 0..62 {
        deep = json!([deep]);
    }
    let behaviors = vec![Behavior::Value(json!("x".repeat(600))), Behavior::Value(deep), Behavior::Echo];
    let fixture = with(behaviors, ActivityOptions::idempotent(), Limits { max_value_bytes: 512, ..Limits::default() });
    let driven = run(&fixture, "big", json!([act(&json!(1)), act(&json!(2)), act(&json!(3))]));
    assert_eq!(driven, done(json!([{ "failed": "result-too-large" }, { "failed": "result-too-deep" }, 3])));
}

#[test]
fn blobs_past_their_bounds_are_left_out_and_the_results_kept() {
    let behaviors = vec![
        Behavior::Blobs { count: 257, size: 1 },
        Behavior::Blobs { count: 1, size: 64 },
        Behavior::Blobs { count: 3, size: 1 },
    ];
    let limits = Limits { max_blob_bytes: 32, max_blobs_per_run: 2, ..Limits::default() };
    let fixture = with(behaviors, ActivityOptions::idempotent(), limits);
    let driven = run(&fixture, "blobs", json!([act(&json!(1)), act(&json!(2)), act(&json!(3))]));
    assert_eq!(driven, done(json!([{ "value": 1, "blobs": [0, 1] }, 2, 3])));
}

#[test]
fn a_panicking_local_step_fails_and_the_run_goes_on() {
    let fixture = common::fixture();
    let steps =
        json!([{ "kind": "panic", "name": "bug", "input": 1 }, { "kind": "local", "name": "double", "input": 2 }]);
    assert_eq!(run(&fixture, "panic", steps), done(json!([{ "failed": "panic" }, 4])));
}

#[test]
fn the_context_reports_spend_and_reads_blobs() {
    let behaviors = vec![Behavior::EchoWithBlob(vec![1, 2, 3, 4])];
    let mut setup = Setup::new(memory_store());
    setup.activity = RecordingActivity::new(behaviors);
    setup.model = holdpoint::testing::ScriptedModel::new([json!("a")], 7);
    let fixture = setup.build();
    let steps = json!([
        { "kind": "model", "name": "model" },
        { "kind": "spent" },
        act(&json!("shot")),
        { "kind": "blob", "id": 256 },
        { "kind": "blob", "id": 9 }
    ]);
    let driven = run(&fixture, "ctx", steps);
    assert_eq!(driven, done(json!(["a", 7, { "value": "shot", "blobs": [256] }, 4, null])));
}

#[test]
fn a_run_past_its_deadline_abandons_the_running_attempt() {
    let behaviors = vec![Behavior::Sleep { ms: 2_000, progress: None }];
    let fixture = with(behaviors, ActivityOptions::idempotent(), Limits::default());
    let now = holdpoint::Clock::now_ms(&holdpoint::SystemClock);
    let options = RunOptions { deadline_ms: Some(now + 100), spend_ceiling: None };
    fixture.worker.submit("plan", "late", &plan(json!([act(&json!(1))])), &options).unwrap();
    let started = std::time::Instant::now();
    assert!(matches!(fixture.worker.drive("late").unwrap(), Driven::Ended(Outcome::Expired { .. })));
    assert!(started.elapsed() < std::time::Duration::from_millis(1_500));
    let history = fixture.worker.history("late").unwrap();
    assert!(
        history
            .iter()
            .any(|event| matches!(event, holdpoint::Event::StepFailed { failure, .. } if failure.code == "expired"))
    );
}

#[test]
fn a_worker_at_its_abandoned_capacity_dispatches_nothing_new() {
    let timeouts = Timeouts { start_to_close_ms: Some(10), ..Timeouts::default() };
    let options =
        ActivityOptions { timeouts, idempotent: true, containment: Containment::InThread, retry: RetryPolicy::never() };
    let mut setup = Setup::new(memory_store());
    setup.activity = RecordingActivity::new([Behavior::Hang { linger_ms: 400 }, Behavior::Echo]);
    setup.activity_options = options;
    setup.options = WorkerOptions { max_abandoned: 1, abandon_grace_ms: 10, ..common::fast_options() };
    let fixture = setup.build();
    let driven = run(&fixture, "cap", json!([act(&json!(1)), act(&json!(2))]));
    assert!(matches!(driven, Driven::Suspended(Wait::Executor(_))), "{driven:?}");
    std::thread::sleep(std::time::Duration::from_millis(500));
    assert_eq!(fixture.worker.drive("cap").unwrap(), done(json!([{ "failed": "timeout" }, 2])));
}

#[test]
fn a_non_idempotent_in_thread_failure_is_uncertain_without_an_obligation() {
    let options = ActivityOptions { containment: Containment::InThread, ..ActivityOptions::external() };
    let fixture = with(vec![Behavior::Fail(Failure::retryable("half", "half done"))], options, Limits::default());
    assert_eq!(run(&fixture, "inthread", json!([act(&json!(1))])), done(json!(["uncertain"])));
    assert_eq!(fixture.store.obligations("inthread").unwrap().len(), 0);
}

#[test]
fn an_idempotent_external_failure_without_confirmation_is_retried_after_a_proof() {
    let options = ActivityOptions {
        idempotent: true,
        retry: RetryPolicy { initial_ms: 1, max_attempts: Some(2), ..RetryPolicy::default() },
        ..ActivityOptions::external()
    };
    let fixture =
        with(vec![Behavior::Fail(Failure::retryable("rpc", "lost")), Behavior::Echo], options, Limits::default());
    fixture.host.set_proof(Proof::NotYet);
    assert!(matches!(run(&fixture, "idem", json!([act(&json!(5))])), Driven::Suspended(Wait::Executor(_))));
    fixture.host.set_proof(Proof::Ended);
    assert_eq!(fixture.worker.drive("idem").unwrap(), done(json!([5])));
    let calls = fixture.activity.calls();
    assert!(calls.iter().all(|call| call.effect_key == "idem/0") && calls.len() == 2);
}

#[test]
fn an_oversized_model_answer_fails_the_step() {
    let mut setup =
        Setup::new(Arc::new(Store::open_in_memory(Limits { max_value_bytes: 64, ..Limits::default() }).unwrap()));
    setup.model = holdpoint::testing::ScriptedModel::new([json!("y".repeat(100))], 1);
    let fixture = setup.build();
    assert_eq!(
        run(&fixture, "huge", json!([{ "kind": "model", "name": "model" }])),
        done(json!([{ "failed": "result-too-large" }]))
    );
}

#[test]
fn a_model_call_that_outlives_its_timeout_is_retried() {
    let mut setup = Setup::new(memory_store());
    let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let counted = Arc::clone(&calls);
    let fixture_model = move |_call: holdpoint::ModelCall<'_>| {
        if counted.fetch_add(1, std::sync::atomic::Ordering::SeqCst) == 0 {
            std::thread::sleep(std::time::Duration::from_millis(300));
        }
        Ok(ModelReply { response: json!("second"), cost: 0 })
    };
    setup.options = WorkerOptions { abandon_grace_ms: 10, ..common::fast_options() };
    let fixture = setup.build();
    let options = ModelOptions {
        retry: RetryPolicy { initial_ms: 1, max_attempts: Some(3), ..RetryPolicy::default() },
        timeouts: Timeouts { start_to_close_ms: Some(30), ..Timeouts::default() },
    };
    let worker =
        holdpoint::Worker::builder(Arc::clone(&fixture.store), "slow", holdpoint::testing::TestHost::default())
            .definition(common::Plan { version: 1 })
            .model("slow", options, fixture_model)
            .options(WorkerOptions { abandon_grace_ms: 10, ..common::fast_options() })
            .build();
    worker.submit("plan", "slow", &plan(json!([{ "kind": "model", "name": "slow" }])), &RunOptions::default()).unwrap();
    assert_eq!(worker.drive("slow").unwrap(), done(json!(["second"])));
    assert_eq!(calls.load(std::sync::atomic::Ordering::SeqCst), 2);
}

#[test]
fn a_changed_unfinished_model_request_is_nondeterminism() {
    let setup = Setup::new(memory_store());
    setup.model.push(Err(Failure::retryable("http-503", "down").with_ended()));
    let clock = holdpoint::ManualClock::at(1_000);
    let fixture = setup.build_with_clock(clock.clone());
    let first = plan(json!([{ "kind": "model", "name": "model", "prompt": "one" }]));
    fixture.worker.submit("plan", "drift", &first, &RunOptions::default()).unwrap();
    let options =
        ModelOptions { retry: RetryPolicy { initial_ms: 60_000, ..RetryPolicy::default() }, ..ModelOptions::default() };
    let worker =
        holdpoint::Worker::builder(Arc::clone(&fixture.store), "drift", holdpoint::testing::TestHost::default())
            .definition(Drifting)
            .model("model", options, fixture.model.clone())
            .clock(clock.clone())
            .build();
    assert!(matches!(worker.drive("drift").unwrap(), Driven::Suspended(Wait::Until(_))));
    PROMPT.store(true, std::sync::atomic::Ordering::SeqCst);
    clock.advance(61_000);
    assert_eq!(
        worker.drive("drift").unwrap(),
        Driven::Ended(Outcome::Unrecoverable { reason: "nondeterminism".into() })
    );
}

static PROMPT: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// The plan definition, except that its model request changes once `PROMPT` is set.
#[cfg(test)]
struct Drifting;

#[cfg(test)]
impl holdpoint::Definition for Drifting {
    fn id(&self) -> &'static str {
        "plan"
    }

    fn version(&self) -> u32 {
        1
    }

    fn run(&self, ctx: &mut holdpoint::Ctx<'_>) -> Result<Outcome, holdpoint::Interrupt> {
        let changed = PROMPT.load(std::sync::atomic::Ordering::SeqCst);
        let answer = ctx.model("model", &[], |_| Ok(json!({ "prompt": if changed { "two" } else { "one" } })))?;
        Ok(Outcome::Done { result: answer.map_or(Value::Null, |output| output.value) })
    }
}

#[test]
fn an_attempt_whose_deadline_passed_before_it_started_does_no_work() {
    let timeouts = Timeouts { start_to_close_ms: Some(0), ..Timeouts::default() };
    let options = ActivityOptions { timeouts, ..ActivityOptions::external() };
    let mut setup = Setup::new(memory_store());
    setup.activity = RecordingActivity::new([Behavior::Echo]);
    setup.activity_options = options;
    let fixture = setup.build_with_clock(holdpoint::ManualClock::at(1_000));
    let Driven::Ended(Outcome::Done { result }) = run(&fixture, "late-start", json!([act(&json!(1))])) else {
        panic!("the run did not end");
    };
    // The waiting session may declare the timeout first; either way the step failed without acting.
    assert!(result == json!([{ "failed": "not-started" }]) || result == json!([{ "failed": "timeout" }]), "{result}");
    assert_eq!(fixture.activity.calls().len(), 0, "the handler never ran");
}

/// Sets a progress far larger than the ledger holds, then reaches a bound.
#[cfg(test)]
struct Bloated;

#[cfg(test)]
impl holdpoint::Definition for Bloated {
    fn id(&self) -> &'static str {
        "bloated"
    }

    fn version(&self) -> u32 {
        1
    }

    fn run(&self, ctx: &mut holdpoint::Ctx<'_>) -> Result<Outcome, holdpoint::Interrupt> {
        ctx.set_progress(json!("p".repeat(8_000)));
        Err(holdpoint::Interrupt::Limited(holdpoint::LimitKind::Steps))
    }
}

#[test]
fn a_run_whose_progress_cannot_fit_ends_without_it() {
    let store = Arc::new(Store::open_in_memory(Limits { max_value_bytes: 1_200, ..Limits::default() }).unwrap());
    let worker = holdpoint::Worker::builder(store, "bloated", holdpoint::testing::TestHost::default())
        .definition(Bloated)
        .build();
    worker.submit("bloated", "fat", &Value::Null, &RunOptions::default()).unwrap();
    let outcome = Outcome::Limited { kind: holdpoint::LimitKind::Steps, progress: Value::Null };
    assert_eq!(worker.drive("fat").unwrap(), Driven::Ended(outcome));
}
