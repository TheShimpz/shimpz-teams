//! End-to-end behavior of runs through the worker.

#![cfg(all(test, feature = "testing"))]

#[cfg(test)]
mod common;

use common::{Setup, fixture, memory_store, plan};
use holdpoint::{Driven, Error, Event, Outcome, Resolution, RunOptions, Submitted, Wait};
use serde_json::json;

#[test]
fn a_model_step_runs_once_and_its_outcome_is_kept() {
    let fixture = fixture();
    fixture.model.push(Ok(holdpoint::ModelReply { response: json!("hello"), cost: 3 }));
    let input = plan(json!([{ "kind": "model", "name": "model", "prompt": "hi" }]));
    assert_eq!(fixture.worker.submit("plan", "r1", &input, &RunOptions::default()), Ok(Submitted::Created));
    let driven = fixture.worker.drive("r1").unwrap();
    assert_eq!(driven, Driven::Ended(Outcome::Done { result: json!(["hello"]) }));
    assert_eq!(fixture.worker.drive("r1").unwrap(), driven);
    assert_eq!(fixture.model.requests(), [json!({ "after": 0, "prompt": "hi", "image_bytes": null })]);
    let kinds: Vec<_> =
        fixture.worker.history("r1").unwrap().iter().map(|event| format!("{event:?}")[..12].to_owned()).collect();
    assert_eq!(kinds.len(), 5, "{kinds:?}");
}

#[cfg(test)]
fn batch_of_two(fixture: &common::Fixture, run: &str) {
    let input = plan(json!([{ "kind": "deferred", "name": "ask", "requests": [{ "q": 1 }, { "q": 2 }] }]));
    fixture.worker.submit("plan", run, &input, &RunOptions::default()).unwrap();
    assert_eq!(fixture.worker.drive(run).unwrap(), Driven::Suspended(Wait::Deferred));
}

#[test]
fn a_deferred_batch_is_registered_whole_and_waits_for_every_answer() {
    let fixture = fixture();
    batch_of_two(&fixture, "r2");
    let pending = fixture.worker.pending("r2").unwrap();
    assert_eq!(pending.iter().map(|request| request.step).collect::<Vec<_>>(), [0, 1]);
    assert_eq!(fixture.worker.resolve("r2", 1, &json!("b")), Ok(Resolution::Recorded));
    assert_eq!(fixture.worker.drive("r2").unwrap(), Driven::Suspended(Wait::Deferred));
    fixture.worker.resolve("r2", 0, &json!("a")).unwrap();
    assert_eq!(fixture.worker.drive("r2").unwrap(), Driven::Ended(Outcome::Done { result: json!([["a", "b"]]) }));
}

#[test]
fn resolutions_are_idempotent_and_refuse_conflicts() {
    let fixture = fixture();
    batch_of_two(&fixture, "r2b");
    assert_eq!(fixture.worker.resolve("r2b", 1, &json!("b")), Ok(Resolution::Recorded));
    assert_eq!(fixture.worker.resolve("r2b", 1, &json!("b")), Ok(Resolution::Duplicate));
    assert!(matches!(fixture.worker.resolve("r2b", 1, &json!("c")), Err(Error::Conflict(_))));
    assert!(matches!(fixture.worker.resolve("r2b", 7, &json!("c")), Err(Error::State(_))));
    fixture.worker.resolve("r2b", 0, &json!("a")).unwrap();
    fixture.worker.drive("r2b").unwrap();
    assert!(matches!(fixture.worker.resolve("r2b", 0, &json!("a")), Err(Error::State(_))));
}

#[test]
fn completed_steps_never_run_again_after_a_restart() {
    let store = memory_store();
    let first = Setup::new(std::sync::Arc::clone(&store)).build();
    first.model.push(Ok(holdpoint::ModelReply { response: json!("m"), cost: 0 }));
    let input = plan(json!([
        { "kind": "model", "name": "model" },
        { "kind": "activity", "name": "act", "input": 5 },
        { "kind": "deferred", "name": "ask", "requests": [1] },
        { "kind": "local", "name": "double", "input": 4 }
    ]));
    first.worker.submit("plan", "r3", &input, &RunOptions::default()).unwrap();
    assert_eq!(first.worker.drive("r3").unwrap(), Driven::Suspended(Wait::Deferred));
    drop(first);
    let second = Setup::new(std::sync::Arc::clone(&store)).build();
    second.worker.resolve("r3", 2, &json!("yes")).unwrap();
    let outcome = second.worker.drive("r3").unwrap();
    assert_eq!(outcome, Driven::Ended(Outcome::Done { result: json!(["m", 5, ["yes"], 8]) }));
    assert_eq!(second.model.requests().len(), 0);
    assert_eq!(second.activity.calls().len(), 0);
}

#[test]
fn submission_is_idempotent_by_id() {
    let fixture = fixture();
    let input = plan(json!([]));
    let options = RunOptions::default();
    assert_eq!(fixture.worker.submit("plan", "same", &input, &options), Ok(Submitted::Created));
    assert_eq!(fixture.worker.submit("plan", "same", &input, &options), Ok(Submitted::Existing));
    assert_eq!(fixture.worker.drive("same").unwrap(), Driven::Ended(Outcome::Done { result: json!([]) }));
    assert!(matches!(fixture.worker.history("same").unwrap().last(), Some(Event::RunCompleted { .. })));
    assert_eq!(fixture.worker.submit("plan", "same", &input, &options), Ok(Submitted::Existing));
}

#[test]
fn submission_refuses_conflicts_unknown_definitions_and_bad_ids() {
    let fixture = fixture();
    let options = RunOptions::default();
    fixture.worker.submit("plan", "same", &plan(json!([])), &options).unwrap();
    assert!(matches!(fixture.worker.submit("plan", "same", &plan(json!([1])), &options), Err(Error::Conflict(_))));
    assert!(matches!(fixture.worker.submit("missing", "other", &plan(json!([])), &options), Err(Error::Contract(_))));
    assert!(matches!(fixture.worker.submit("plan", "bad id!", &plan(json!([])), &options), Err(Error::Contract(_))));
}
