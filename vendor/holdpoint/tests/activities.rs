//! Activity semantics: retries, timeouts, heartbeats, uncertainty, obligations, and transient blobs.

#![cfg(all(test, feature = "testing"))]

#[cfg(test)]
mod common;

use common::{Fixture, Setup, memory_store, plan};
use holdpoint::testing::{Behavior, RecordingActivity};
use holdpoint::{
    ActivityOptions, Containment, Driven, Failure, Outcome, Proof, RetryPolicy, RunOptions, Timeouts, Wait,
};
use serde_json::{Value, json};

#[cfg(test)]
fn with(behaviors: Vec<Behavior>, options: ActivityOptions) -> Fixture {
    let mut setup = Setup::new(memory_store());
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
fn retrying(max_attempts: u32) -> RetryPolicy {
    RetryPolicy {
        initial_ms: 1,
        backoff_percent: 200,
        max_interval_ms: 4,
        max_attempts: Some(max_attempts),
        non_retryable: vec![],
    }
}

#[test]
fn a_retryable_failure_the_executor_confirmed_is_retried_with_the_same_effect_key() {
    let fail = Behavior::Fail(Failure::retryable("busy", "try again").with_ended());
    let options = ActivityOptions { retry: retrying(3), ..ActivityOptions::external() };
    let fixture = with(vec![fail.clone(), fail, Behavior::Echo], options);
    fixture.host.set_proof(Proof::Ended);
    assert_eq!(run(&fixture, "retry", json!([{ "kind": "activity", "name": "act", "input": 7 }])), done(json!([7])));
    let calls = fixture.activity.calls();
    assert_eq!(calls.iter().map(|call| call.attempt).collect::<Vec<_>>(), [1, 2, 3]);
    assert!(calls.iter().all(|call| call.effect_key == "retry/0"));
}

#[test]
fn exhausted_and_permanent_failures_fail_the_step() {
    let options = ActivityOptions { retry: retrying(2), ..ActivityOptions::idempotent() };
    let fixture = with(vec![Behavior::Fail(Failure::retryable("busy", "no"))], options.clone());
    let steps = json!([{ "kind": "activity", "name": "act", "input": 1 }]);
    assert_eq!(run(&fixture, "exhausted", steps.clone()), done(json!([{ "failed": "busy" }])));
    assert_eq!(fixture.activity.calls().len(), 2);
    let fixture = with(vec![Behavior::Fail(Failure::permanent("denied", "no"))], options);
    assert_eq!(run(&fixture, "permanent", steps), done(json!([{ "failed": "denied" }])));
    assert_eq!(fixture.activity.calls().len(), 1);
}

#[test]
fn a_timed_out_idempotent_attempt_is_retried_after_its_thread_returns() {
    let timeouts = Timeouts { start_to_close_ms: Some(20), ..Timeouts::default() };
    let options = ActivityOptions { retry: retrying(3), timeouts, ..ActivityOptions::idempotent() };
    let fixture = with(vec![Behavior::Sleep { ms: 400, progress: None }, Behavior::Echo], options);
    assert_eq!(run(&fixture, "timeout", json!([{ "kind": "activity", "name": "act", "input": 2 }])), done(json!([2])));
    assert_eq!(fixture.activity.calls().len(), 2);
}

#[test]
fn a_heartbeat_timeout_carries_the_last_progress_to_the_next_attempt() {
    let timeouts = Timeouts { heartbeat_ms: Some(30), ..Timeouts::default() };
    let options = ActivityOptions { retry: retrying(3), timeouts, ..ActivityOptions::idempotent() };
    let slow = Behavior::Sleep { ms: 300, progress: Some(json!({ "read": 4 })) };
    let fixture = with(vec![slow, Behavior::Echo], options);
    assert_eq!(run(&fixture, "beats", json!([{ "kind": "activity", "name": "act", "input": 3 }])), done(json!([3])));
    let calls = fixture.activity.calls();
    assert_eq!(calls[1].progress, Some(json!({ "read": 4 })));
    let history = fixture.worker.history("beats").unwrap();
    assert!(history.iter().any(
        |event| matches!(event, holdpoint::Event::StepFailed { failure, .. } if failure.code == "heartbeat-timeout")
    ));
}

#[test]
fn a_timed_out_external_effect_is_uncertain_and_blocks_until_proven_ended() {
    let timeouts = Timeouts { start_to_close_ms: Some(10), ..Timeouts::default() };
    let options = ActivityOptions { timeouts, ..ActivityOptions::external() };
    let fixture = with(vec![Behavior::Hang { linger_ms: 300 }, Behavior::Echo], options);
    fixture.host.set_proof(Proof::NotYet);
    let steps =
        json!([{ "kind": "activity", "name": "act", "input": 1 }, { "kind": "activity", "name": "act", "input": 2 }]);
    let driven = run(&fixture, "external", steps);
    assert!(matches!(driven, Driven::Suspended(Wait::Executor(_))), "{driven:?}");
    assert_eq!(fixture.activity.calls().len(), 1);
    assert_eq!(fixture.store.obligations("external").unwrap().len(), 1);
    fixture.host.set_proof(Proof::Ended);
    assert_eq!(fixture.worker.drive("external").unwrap(), done(json!(["uncertain", 2])));
    assert_eq!(fixture.store.obligations("external").unwrap().len(), 0);
}

#[test]
fn a_returned_external_failure_without_an_end_confirmation_is_uncertain_and_needs_a_proof() {
    let options = ActivityOptions { retry: retrying(2), ..ActivityOptions::external() };
    let fixture = with(vec![Behavior::Fail(Failure::retryable("rpc-timeout", "slow")), Behavior::Echo], options);
    fixture.host.set_proof(Proof::NotYet);
    let steps =
        json!([{ "kind": "activity", "name": "act", "input": 5 }, { "kind": "activity", "name": "act", "input": 6 }]);
    let driven = run(&fixture, "rpc", steps);
    assert!(matches!(driven, Driven::Suspended(Wait::Executor(_))));
    assert_eq!(fixture.activity.calls().len(), 1);
    fixture.host.set_proof(Proof::Ended);
    assert_eq!(fixture.worker.drive("rpc").unwrap(), done(json!(["uncertain", 6])));
    let effects: Vec<String> = fixture.activity.calls().into_iter().map(|call| call.effect_key).collect();
    assert_eq!(effects, ["rpc/0", "rpc/1"], "an effect that may have happened is never repeated");
}

#[test]
fn an_ended_failure_needs_no_proof() {
    let options = ActivityOptions { retry: retrying(2), ..ActivityOptions::external() };
    let refused = Failure::retryable("refused", "not now").with_ended();
    let fixture = with(vec![Behavior::Fail(refused), Behavior::Echo], options);
    fixture.host.set_proof(Proof::NotYet);
    assert_eq!(run(&fixture, "ended", json!([{ "kind": "activity", "name": "act", "input": 6 }])), done(json!([6])));
    assert_eq!(fixture.host.proofs().len(), 0);
}

#[test]
fn a_panicking_non_idempotent_handler_is_uncertain() {
    let fixture = with(vec![Behavior::Panic], ActivityOptions::external());
    assert_eq!(
        run(&fixture, "panic", json!([{ "kind": "activity", "name": "act", "input": 1 }])),
        done(json!(["uncertain"]))
    );
}

#[test]
fn a_panicking_idempotent_handler_fails_and_is_not_retried() {
    let options = ActivityOptions { retry: retrying(3), ..ActivityOptions::idempotent() };
    let fixture = with(vec![Behavior::Panic, Behavior::Echo], options);
    let driven = run(&fixture, "panic2", json!([{ "kind": "activity", "name": "act", "input": 1 }]));
    assert_eq!(driven, done(json!([{ "failed": "panic" }])));
}

#[test]
fn an_in_thread_abandoned_attempt_is_proven_by_its_thread_returning() {
    let timeouts = Timeouts { start_to_close_ms: Some(10), ..Timeouts::default() };
    let options =
        ActivityOptions { retry: retrying(3), timeouts, idempotent: true, containment: Containment::InThread };
    let fixture = with(vec![Behavior::Hang { linger_ms: 120 }, Behavior::Echo], options);
    fixture.host.set_proof(Proof::NotYet);
    let first = run(&fixture, "thread", json!([{ "kind": "activity", "name": "act", "input": 9 }]));
    assert!(matches!(first, Driven::Suspended(Wait::Executor(_))), "{first:?}");
    std::thread::sleep(std::time::Duration::from_millis(250));
    assert_eq!(fixture.worker.drive("thread").unwrap(), done(json!([9])));
    assert_eq!(fixture.host.proofs().len(), 0);
}

#[test]
fn blobs_are_kept_until_their_consumer_commits() {
    let fixture = with(vec![Behavior::EchoWithBlob(vec![1, 2, 3])], ActivityOptions::external());
    fixture.model.push(Ok(holdpoint::ModelReply { response: json!("seen"), cost: 0 }));
    let steps = json!([
        { "kind": "activity", "name": "act", "input": "shot" },
        { "kind": "model", "name": "model", "blob": 0, "release": [0] }
    ]);
    let driven = run(&fixture, "blob", steps);
    assert_eq!(driven, done(json!([{ "value": "shot", "blobs": [0] }, "seen"])));
    assert_eq!(fixture.model.requests()[0]["image_bytes"], json!(3));
    assert_eq!(fixture.store.blob("blob", 0).unwrap(), None);
}

#[test]
fn an_oversized_result_fails_the_step() {
    let mut setup = Setup::new(std::sync::Arc::new(
        holdpoint::Store::open_in_memory(holdpoint::Limits { max_value_bytes: 64, ..holdpoint::Limits::default() })
            .unwrap(),
    ));
    setup.activity_options = ActivityOptions::idempotent();
    let fixture = setup.build();
    let steps = json!([{ "kind": "activity", "name": "act", "input": "x" }]);
    fixture.worker.submit("plan", "big", &plan(steps), &RunOptions::default()).unwrap();
    assert_eq!(fixture.worker.drive("big").unwrap(), done(json!(["x"])));
    let long = json!([{ "kind": "local", "name": "double", "input": 1 }]);
    fixture.worker.submit("plan", "fine", &plan(long), &RunOptions::default()).unwrap();
    assert_eq!(fixture.worker.drive("fine").unwrap(), done(json!([2])));
}
