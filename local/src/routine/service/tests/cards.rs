//! The recovery card of a held run: its call, its entity tag, and Rodar, which never starts while the held attempt's
//! workload could still be running.

use strict_json::Value;

use super::now;
use crate::chat::tests::fakes::json;
use crate::http::service::Body;
use crate::routine::executor::fakes::Outcome;
use crate::routine::executor::{FaultClass, WorkloadState};
use crate::routine::service::harness::{Harness, TEAM, action, harness, routine_of, running};

async fn held(fault: FaultClass) -> (Harness, String) {
    let active = running(&[action("apply", "mutating", None)]);
    let harness = harness(active.clone(), &routine_of(&active, &["apply"], now()));
    harness.actions.answer(Outcome::Fail(fault, None, None));
    let (run_id, status) = harness.run_due().await;
    assert_eq!(status, "held");
    (harness, run_id)
}

fn tag(card: &Value) -> String {
    match card.get("tag") {
        Some(Value::String(tag)) => tag.clone(),
        _ => panic!("a tag"),
    }
}

fn answer(tag: &str) -> Body {
    Body::Json(json(&format!(r#"{{"tag":"{tag}"}}"#)))
}

#[tokio::test]
async fn a_card_names_the_held_call_and_rodar_answers_only_its_own_tag() {
    let (harness, run_id) = held(FaultClass::Transport).await;
    let card = harness
        .service
        .open_routine_card(TEAM, &run_id)
        .expect("a card");
    assert_eq!(card.get("action"), Some(&json(r#""apply""#)));
    assert_eq!(card.get("position"), Some(&json("1")));
    let stale = harness
        .service
        .answer_routine_card(TEAM, &run_id, &answer(&"0".repeat(64)))
        .await;
    assert_eq!(
        stale.map_err(|problem| problem.code),
        Err(Some("routine-card-stale"))
    );
    harness
        .service
        .answer_routine_card(TEAM, &run_id, &answer(&tag(&card)))
        .await
        .expect("answered");
    // The held run is set aside and released; one fresh run starts under the normal claim.
    assert_eq!(harness.run(&run_id), None);
    let fresh = harness.claim().await.expect("a fresh run");
    assert_ne!(fresh.run_id, run_id);
    let again = harness
        .service
        .answer_routine_card(TEAM, &run_id, &answer(&tag(&card)))
        .await;
    assert_eq!(
        again.map_err(|problem| problem.code),
        Err(Some("routine-incident-unavailable"))
    );
}

#[tokio::test]
async fn rodar_never_restarts_while_the_held_attempt_s_workload_may_still_run() {
    let (harness, run_id) = held(FaultClass::Unquiesced).await;
    let card = harness
        .service
        .open_routine_card(TEAM, &run_id)
        .expect("a card");
    let refused = harness
        .service
        .answer_routine_card(TEAM, &run_id, &answer(&tag(&card)))
        .await;
    assert_eq!(
        refused.map_err(|problem| problem.code),
        Err(Some("routine-workload-unquiesced"))
    );
    assert!(harness.service.unresolved(TEAM, &run_id).is_ok());
    *harness.workloads.state.lock().expect("state") = Some(WorkloadState {
        running: false,
        started_at: None,
    });
    harness
        .service
        .answer_routine_card(TEAM, &run_id, &answer(&tag(&card)))
        .await
        .expect("answered");
}

#[tokio::test]
async fn an_answer_waits_for_the_execution_slot_and_its_tag_still_answers() {
    let (harness, run_id) = held(FaultClass::Transport).await;
    let card = harness
        .service
        .open_routine_card(TEAM, &run_id)
        .expect("a card");
    let chat = harness
        .coordination
        .executions
        .admit(
            TEAM,
            crate::coordination::executions::ExecutionKind::Chat,
            "c".repeat(32),
        )
        .expect("a chat turn");
    let busy = harness
        .service
        .answer_routine_card(TEAM, &run_id, &answer(&tag(&card)))
        .await;
    assert_eq!(
        busy.map_err(|problem| problem.code),
        Err(Some("chat-active"))
    );
    drop(chat);
    assert!(
        harness
            .service
            .answer_routine_card(TEAM, &run_id, &answer(&tag(&card)))
            .await
            .is_ok()
    );
}

#[tokio::test]
async fn pausing_holds_dispatch_and_resuming_never_bypasses_an_unresolved_incident() {
    let (harness, run_id) = held(FaultClass::Transport).await;
    let paused = harness
        .service
        .set_routine_paused(TEAM, &harness.routine().routine_id, true)
        .await;
    assert!(paused.is_ok());
    harness
        .service
        .set_routine_paused(TEAM, &harness.routine().routine_id, false)
        .await
        .expect("resumed");
    // The unresolved incident still holds the Routine.
    assert!(harness.claim().await.is_none());
    assert!(harness.service.unresolved(TEAM, &run_id).is_ok());
    let missing = harness
        .service
        .set_routine_paused(TEAM, "not-an-id", true)
        .await;
    assert_eq!(
        missing.map_err(|problem| problem.code),
        Err(Some("routine-not-found"))
    );
}
