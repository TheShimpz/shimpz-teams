//! Deleting a Routine, a Team, or the Space's Routines, and the watchdog's recovery of runs nothing is running.

use strict_json::Value;

use super::now;
use crate::chat::fakes::{human_request, json};
use crate::routine::executor::FaultClass;
use crate::routine::executor::fakes::Outcome;
use crate::routine::service::harness::{
    Harness, ROUTINE, TEAM, action, harness, routine_of, running,
};

fn one_step() -> Harness {
    let active = running(&[action("apply", "mutating", None)]);
    harness(active.clone(), &routine_of(&active, &["apply"], now()))
}

fn deleted(answer: &Value) -> Option<&Value> {
    answer.get("deleted")
}

#[tokio::test]
async fn deleting_a_routine_ends_its_frozen_run_stopped_and_removes_it() {
    let harness = one_step();
    harness
        .actions
        .answer(Outcome::Human(human_request("approval", 0)));
    let (run_id, status) = harness.run_due().await;
    assert_eq!(status, "frozen");
    let answer = harness
        .service
        .delete_routine(TEAM, ROUTINE)
        .await
        .expect("deleted");
    assert_eq!(deleted(&answer), Some(&Value::Bool(true)));
    assert_eq!(harness.run(&run_id), None);
    assert_eq!(harness.state().routines.len(), 0);
    let notices = harness.notices();
    assert!(notices.contains(&"stopped".to_owned()) && notices.contains(&"deleted".to_owned()));
}

#[tokio::test]
async fn deleting_a_routine_sets_its_held_run_aside_and_releases_what_it_kept() {
    let harness = one_step();
    harness
        .actions
        .answer(Outcome::Fail(FaultClass::Transport, None, None));
    let (run_id, status) = harness.run_due().await;
    assert_eq!(status, "held");
    let answer = harness
        .service
        .delete_routine(TEAM, ROUTINE)
        .await
        .expect("deleted");
    assert_eq!(deleted(&answer), Some(&Value::Bool(true)));
    assert_eq!(harness.run(&run_id), None);
    let lock = harness.store.lock(TEAM).expect("lock");
    assert_eq!(lock.cursors().expect("cursors"), Vec::<String>::new());
    assert!(
        harness
            .actions
            .journal
            .batches
            .lock()
            .expect("journal")
            .is_empty()
    );
}

#[tokio::test]
async fn deleting_an_absent_routine_succeeds_and_a_malformed_one_is_not_found() {
    let harness = one_step();
    let absent = harness
        .service
        .delete_routine(TEAM, &"b".repeat(32))
        .await
        .expect("deleted");
    assert_eq!(deleted(&absent), Some(&Value::Bool(true)));
    let malformed = harness.service.delete_routine(TEAM, "x").await;
    assert_eq!(
        malformed.map_err(|problem| problem.code),
        Err(Some("routine-not-found"))
    );
    assert_eq!(harness.state().routines.len(), 1);
}

#[tokio::test]
async fn a_team_s_routine_generations_state_and_challenges_are_deleted_with_it() {
    let harness = one_step();
    harness
        .actions
        .answer(Outcome::Fail(FaultClass::Transport, None, None));
    let (_run, status) = harness.run_due().await;
    assert_eq!(status, "held");
    harness.service.delete_team_routines(TEAM).expect("deleted");
    assert_eq!(harness.state().routines.len(), 0);
    assert!(
        harness
            .actions
            .journal
            .batches
            .lock()
            .expect("journal")
            .is_empty()
    );
    harness.service.delete_all_routines().expect("reset");
    assert_eq!(harness.store.teams().expect("teams"), Vec::<String>::new());
}

#[tokio::test]
async fn the_watchdog_ends_a_claimed_run_nothing_ran_as_interrupted_at_startup() {
    let harness = one_step();
    let claimed = harness.claim().await.expect("a due run");
    harness.service.check_routines(true).await.expect("checked");
    assert_eq!(harness.run(&claimed.run_id), None);
    let last = harness.state().notices.last().cloned().expect("a notice");
    assert_eq!(last.outcome, "failed");
    assert_eq!(last.detail.get("code"), Some(&json(r#""interrupted""#)));
}

#[tokio::test]
async fn the_watchdog_removes_a_continuation_no_run_owns() {
    let harness = one_step();
    {
        let lock = harness.store.lock(TEAM).expect("lock");
        lock.put_continuation(&"f".repeat(32), b"{}").expect("kept");
    }
    harness
        .service
        .check_routines(false)
        .await
        .expect("checked");
    let lock = harness.store.lock(TEAM).expect("lock");
    assert_eq!(lock.continuations().expect("listed"), Vec::<String>::new());
}

#[tokio::test]
async fn a_running_segment_out_of_active_time_is_stopped_and_ends_failed_not_stopped() {
    let harness = one_step();
    harness.actions.answer(Outcome::UntilStopped);
    let claimed = harness.claim().await.expect("a due run");
    let watchdog = async {
        while harness.actions.operations().is_empty() {
            tokio::task::yield_now().await;
        }
        // The run's whole active time has passed.
        tokio::time::pause();
        tokio::time::advance(std::time::Duration::from_hours(24)).await;
        harness
            .service
            .check_routines(false)
            .await
            .expect("checked");
    };
    let (status, ()) = tokio::join!(harness.segment(&claimed), watchdog);
    // An uncertain attempt cut by its deadline is held for recovery, never recorded as a person's Stop.
    assert_eq!(status.as_deref(), Ok("held"));
    assert!(!harness.notices().contains(&"stopped".to_owned()));
}
