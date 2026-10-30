//! A due Routine claimed and replayed with no model: each dispatch sealed before its RPC, its end by its outcome.

use super::now;
use crate::chat::tests::fakes::json;
use crate::routine::executor::FaultClass;
use crate::routine::executor::fakes::Outcome;
use crate::routine::service::harness::{TEAM, action, harness, routine_of, running};

#[tokio::test]
async fn a_due_routine_is_claimed_and_replayed_with_no_model_and_ends_done() {
    let active = running(&[action("list-zones", "read_only", None)]);
    let harness = harness(
        active.clone(),
        &routine_of(&active, &["list-zones", "list-zones"], now()),
    );
    harness
        .actions
        .answer(Outcome::Complete(json(r#"{"zones":["a"]}"#)));
    let (run_id, status) = harness.run_due().await;
    assert_eq!(status, "done");
    assert_eq!(
        harness.actions.interrupts(),
        ["routine-step-0", "routine-step-1"]
    );
    assert_eq!(harness.run(&run_id), None);
    assert_eq!(harness.notices().last().map(String::as_str), Some("done"));
    // The ended run's generation and files were removed with it.
    assert_eq!(harness.state().discards.len(), 0);
}

#[tokio::test]
async fn a_later_step_that_fails_after_dispatch_holds_the_run_with_its_completed_prefix() {
    let active = running(&[
        action("list-zones", "read_only", None),
        action("apply", "mutating", None),
    ]);
    let harness = harness(
        active.clone(),
        &routine_of(&active, &["list-zones", "apply"], now()),
    );
    harness.actions.answer(Outcome::Complete(json("{}")));
    harness.actions.answer(Outcome::Fail(
        FaultClass::Transport,
        None,
        Some("exit-status:1".to_owned()),
    ));
    let (run_id, status) = harness.run_due().await;
    assert_eq!(status, "held");
    let opened = harness
        .service
        .open_recovery(TEAM, &run_id)
        .expect("an incident");
    assert_eq!(opened.cursor.step, 1);
    assert_eq!(opened.cursor.fault, "transport");
    assert_eq!(
        harness.actions.interrupts(),
        ["routine-step-0", "routine-step-1"]
    );
}

#[tokio::test]
async fn nothing_is_claimed_without_a_routine_key_or_while_chat_holds_or_awaits_the_team() {
    let active = running(&[action("list-zones", "read_only", None)]);
    let harness = harness(active.clone(), &routine_of(&active, &["list-zones"], now()));
    *harness.key.0.lock().expect("key") = None;
    assert!(harness.claim().await.is_none());
    *harness.key.0.lock().expect("key") = Some("e".repeat(64));
    let kind = crate::coordination::executions::ExecutionKind::Chat;
    let chat = harness
        .coordination
        .executions
        .admit(TEAM, kind, "c".repeat(32));
    assert!(harness.claim().await.is_none());
    drop(chat);
    assert!(harness.claim().await.is_some());
}

#[tokio::test]
async fn a_person_refused_while_a_routine_held_the_slot_gets_the_next_boundary() {
    let active = running(&[action("list-zones", "read_only", None)]);
    let harness = harness(active.clone(), &routine_of(&active, &["list-zones"], now()));
    let routine = crate::coordination::executions::ExecutionKind::Routine {
        routine_id: "a".repeat(32),
    };
    let held = harness
        .coordination
        .executions
        .admit(TEAM, routine, "r".repeat(32));
    let kind = crate::coordination::executions::ExecutionKind::Chat;
    assert!(
        harness
            .coordination
            .executions
            .admit(TEAM, kind, "c".repeat(32))
            .is_err()
    );
    drop(held);
    // The slot is free again, but the waiting person goes first for a bounded time.
    assert!(harness.claim().await.is_none());
}

#[tokio::test]
async fn a_changed_assistant_contract_marks_the_routine_for_reconfirmation_and_runs_nothing() {
    let active = running(&[action("list-zones", "read_only", None)]);
    let harness = harness(active.clone(), &routine_of(&active, &["list-zones"], now()));
    harness.team.change(TEAM, |team| {
        let mut pinned = (*team.assistants[0].pinned).clone();
        pinned.version = "2.0.0".to_owned();
        team.assistants[0].pinned = std::sync::Arc::new(pinned);
    });
    assert!(harness.claim().await.is_none());
    assert!(harness.routine().needs_reconfirm);
    assert_eq!(
        harness.notices().last().map(String::as_str),
        Some("scope-changed")
    );
    assert_eq!(harness.actions.operations().len(), 0);
}

#[tokio::test]
async fn a_segment_names_its_claimed_revision_and_lease_or_nothing_runs() {
    let active = running(&[action("list-zones", "read_only", None)]);
    let harness = harness(active.clone(), &routine_of(&active, &["list-zones"], now()));
    let mut claimed = harness.claim().await.expect("a due run");
    claimed.revision += 1;
    let stale = harness.segment(&claimed).await;
    assert_eq!(
        stale.map_err(|problem| problem.code),
        Err(Some("routine-revision-stale"))
    );
    claimed.revision -= 1;
    let lease = claimed.lease.clone();
    claimed.lease.sha256 = "0".repeat(64);
    let forged = harness.segment(&claimed).await;
    assert_eq!(
        forged.map_err(|problem| problem.code),
        Err(Some("routine-lease-invalid"))
    );
    claimed.lease = lease;
    assert_eq!(harness.segment(&claimed).await.as_deref(), Ok("done"));
    assert_eq!(harness.actions.operations().len(), 1);
}

#[tokio::test]
async fn halting_a_run_not_started_ends_it_stopped_and_fences_its_worker_out() {
    let active = running(&[action("list-zones", "read_only", None)]);
    let harness = harness(active.clone(), &routine_of(&active, &["list-zones"], now()));
    let claimed = harness.claim().await.expect("a due run");
    assert_eq!(
        harness.service.halt_routine_run(TEAM, &claimed.run_id),
        Ok(true)
    );
    assert_eq!(
        harness.notices().last().map(String::as_str),
        Some("stopped")
    );
    let late = harness.segment(&claimed).await;
    assert_eq!(
        late.map_err(|problem| problem.code),
        Err(Some("routine-lease-invalid"))
    );
    assert_eq!(harness.actions.operations().len(), 0);
}

#[tokio::test]
async fn a_stop_during_an_action_holds_the_run_and_publishes_nothing() {
    let active = running(&[action("apply", "mutating", None)]);
    let harness = harness(active.clone(), &routine_of(&active, &["apply"], now()));
    harness.actions.answer(Outcome::UntilStopped);
    let claimed = harness.claim().await.expect("a due run");
    let service = harness.service.clone();
    let run_id = claimed.run_id.clone();
    let stopper = async {
        while harness.actions.operations().is_empty() {
            tokio::task::yield_now().await;
        }
        assert_eq!(service.halt_routine_run(TEAM, &run_id), Ok(true));
    };
    let (status, ()) = tokio::join!(harness.segment(&claimed), stopper);
    assert_eq!(status.as_deref(), Ok("held"));
    assert!(!harness.routine().paused);
    assert_eq!(harness.actions.operations().len(), 1);
}

#[tokio::test]
async fn an_input_that_cannot_be_resolved_after_read_only_steps_fails_the_run() {
    let active = running(&[action("list-zones", "read_only", None)]);
    let mut routine = routine_of(&active, &["list-zones", "list-zones"], now());
    // The second step names a value the first never returns.
    let text = team_protocol::python::json::canonical(&routine.plan);
    let text = String::from_utf8(text).expect("utf-8");
    let at = text
        .rfind(r#""input":{}"#)
        .expect("the second step's input");
    let text = format!(
        r#"{}"input":{{"zone":{{"kind":"step_output","step":"s0","pointer":"/zone"}}}}{}"#,
        &text[..at],
        &text[at + r#""input":{}"#.len()..]
    );
    routine.plan = json(&text);
    let harness = harness(active.clone(), &routine);
    harness.actions.answer(Outcome::Complete(json("{}")));
    let (_run, status) = harness.run_due().await;
    assert_eq!(status, "failed");
    assert_eq!(harness.actions.operations().len(), 1);
}
