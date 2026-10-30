//! A held run's one automatic recovery episode: verification with no model, one retry of the same operation only
//! after proven absence, and the pauses a policy fault or spent evidence publishes.

use super::now;
use crate::chat::fakes::json;
use crate::routine::executor::FaultClass;
use crate::routine::executor::fakes::Outcome;
use crate::routine::service::harness::{
    Harness, TEAM, VERIFIER, action, harness, routine_of, running,
};

fn read_only() -> Harness {
    let active = running(&[action("list-zones", "read_only", None)]);
    harness(active.clone(), &routine_of(&active, &["list-zones"], now()))
}

fn verified() -> Harness {
    let actions = [
        action("apply", "mutating", Some(VERIFIER)),
        action("check", "read_only", None),
    ];
    let active = running(&actions);
    harness(active.clone(), &routine_of(&active, &["apply"], now()))
}

fn handled() -> Outcome {
    Outcome::Fail(FaultClass::Handled, None, None)
}

fn transport() -> Outcome {
    Outcome::Fail(
        FaultClass::Transport,
        None,
        Some("exit-status:1".to_owned()),
    )
}

#[tokio::test]
async fn a_read_only_handled_failure_is_proven_absent_and_retried_once_under_the_same_operation() {
    let harness = read_only();
    harness.actions.answer(handled());
    let (_run, status) = harness.run_due().await;
    assert_eq!(status, "recovered");
    let operations = harness.actions.operations();
    assert_eq!(operations.len(), 2);
    assert_eq!(operations[0], operations[1]);
    assert_eq!(
        harness.notices().last().map(String::as_str),
        Some("recovered")
    );
}

#[tokio::test]
async fn the_one_retry_is_never_repeated_and_the_run_stays_held_for_its_card() {
    let harness = read_only();
    harness.actions.answer(handled());
    harness.actions.answer(handled());
    let (run_id, status) = harness.run_due().await;
    assert_eq!(status, "held");
    assert_eq!(harness.actions.operations().len(), 2);
    assert!(harness.service.open_routine_card(TEAM, &run_id).is_ok());
}

#[tokio::test]
async fn a_policy_fault_is_never_verified_away_and_pauses_its_routine() {
    let harness = read_only();
    harness
        .actions
        .answer(Outcome::Fail(FaultClass::Policy, None, None));
    let (_run, status) = harness.run_due().await;
    assert_eq!(status, "held");
    assert_eq!(harness.actions.operations().len(), 1);
    assert!(harness.routine().paused);
}

#[tokio::test]
async fn an_attempt_whose_workload_was_never_proven_stopped_is_neither_verified_nor_retried() {
    let harness = verified();
    harness
        .actions
        .answer(Outcome::Fail(FaultClass::Unquiesced, None, None));
    let (_run, status) = harness.run_due().await;
    assert_eq!(status, "held");
    assert_eq!(harness.actions.interrupts(), ["routine-step-0"]);
    assert!(!harness.routine().paused);
}

#[tokio::test]
async fn a_verified_occurrence_completes_the_held_step_with_its_recovered_result() {
    let harness = verified();
    harness.actions.answer(transport());
    let occurred = json(r#"{"outcome":"occurred","result":{"zones":["a"]}}"#);
    harness.actions.answer(Outcome::Complete(occurred));
    let (_run, status) = harness.run_due().await;
    assert_eq!(status, "recovered");
    assert_eq!(
        harness.actions.interrupts(),
        ["routine-step-0", "routine-verify"]
    );
}

#[tokio::test]
async fn a_verified_absence_retries_the_same_operation_once() {
    let harness = verified();
    harness.actions.answer(transport());
    harness
        .actions
        .answer(Outcome::Complete(json(r#"{"outcome":"not_occurred"}"#)));
    let (_run, status) = harness.run_due().await;
    assert_eq!(status, "recovered");
    let operations = harness.actions.operations();
    assert_eq!(
        harness.actions.interrupts(),
        ["routine-step-0", "routine-verify", "routine-step-0"]
    );
    assert_eq!(operations[0], operations[2]);
}

#[tokio::test]
async fn inconclusive_evidence_holds_the_run_without_dispatching_its_step_again() {
    let harness = verified();
    harness.actions.answer(transport());
    harness
        .actions
        .answer(Outcome::Complete(json(r#"{"outcome":"unknown"}"#)));
    let (_run, status) = harness.run_due().await;
    assert_eq!(status, "held");
    assert_eq!(
        harness.actions.interrupts(),
        ["routine-step-0", "routine-verify"]
    );
}

#[tokio::test]
async fn a_mutating_step_without_a_verifier_is_held_for_a_person() {
    let active = running(&[action("apply", "mutating", None)]);
    let harness = harness(active.clone(), &routine_of(&active, &["apply"], now()));
    harness.actions.answer(transport());
    let (run_id, status) = harness.run_due().await;
    assert_eq!(status, "held");
    let list = crate::routine::manage::list(&harness.store, TEAM).expect("listed");
    let incidents = list.get("incidents").cloned();
    assert!(matches!(incidents, Some(strict_json::Value::Array(items)) if items.len() == 1));
    assert!(harness.service.unresolved(TEAM, &run_id).is_ok());
}
