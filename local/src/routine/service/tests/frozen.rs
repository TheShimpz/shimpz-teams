//! A run frozen for a person: its challenge opened in the Admin language, answered, denied, or ended by a changed Team.

use strict_json::Value;
use team_chat::progress::Reporter;

use super::now;
use crate::chat::tests::fakes::{human_request, json};
use crate::http::operation::Operation;
use crate::http::server::ChallengeBook;
use crate::http::service::Body;
use crate::routine::executor::fakes::Outcome;
use crate::routine::service::harness::{Harness, TEAM, action, harness, routine_of, running};
use crate::routine::service::http::Challenges;

async fn frozen() -> (Harness, String) {
    let active = running(&[action("list-zones", "read_only", None)]);
    let harness = harness(active.clone(), &routine_of(&active, &["list-zones"], now()));
    harness
        .actions
        .answer(Outcome::Human(human_request("approval", 0)));
    let (run_id, status) = harness.run_due().await;
    assert_eq!(status, "frozen");
    (harness, run_id)
}

async fn opened(harness: &Harness, run_id: &str) -> String {
    let opened = harness
        .service
        .open_routine_challenge(TEAM, run_id, "pt")
        .await
        .expect("opened");
    assert_eq!(
        opened.get("run_id"),
        Some(&Value::String(run_id.to_owned()))
    );
    match opened.get("challenge_id") {
        Some(Value::String(challenge)) => challenge.clone(),
        _ => panic!("a challenge"),
    }
}

fn answer(challenge: &str, decision: &str) -> Body {
    let value = if decision == "submit" {
        r#","value":true"#
    } else {
        ""
    };
    Body::Json(json(&format!(
        r#"{{"challenge_id":"{challenge}","decision":"{decision}"{value}}}"#
    )))
}

fn status(answer: &Value) -> Option<&str> {
    match answer.get("status") {
        Some(Value::String(status)) => Some(status),
        _ => None,
    }
}

#[tokio::test]
async fn an_approval_freezes_the_run_and_a_person_resumes_it_under_the_same_operation() {
    let (harness, run_id) = frozen().await;
    let challenge = opened(&harness, &run_id).await;
    // A Routine challenge lives in its own store, never beside chat's one pending challenge.
    let chat = harness.chat.human_challenges().current(TEAM).expect("read");
    assert!(chat.is_none());
    let books = Challenges {
        chat: harness.chat.clone(),
        routines: harness.service.clone(),
    };
    let pending = books.pending(Operation::RoutineHumanSubmit, TEAM).await;
    let pending = pending.expect("read").expect("pending");
    let found = (pending.kind.as_str(), pending.challenge_id.as_str());
    assert_eq!(found, ("approval", challenge.as_str()));
    let resumed = harness
        .service
        .resume_routine_human(
            TEAM,
            &run_id,
            &answer(&challenge, "submit"),
            &Reporter::silent(),
        )
        .await
        .expect("resumed");
    assert_eq!(status(&resumed), Some("done"));
    let operations = harness.actions.operations();
    assert_eq!(operations.len(), 2);
    assert_eq!(operations[0], operations[1]);
    // The answered challenge is consumed: answering it again is refused.
    let again = harness
        .service
        .resume_routine_human(
            TEAM,
            &run_id,
            &answer(&challenge, "submit"),
            &Reporter::silent(),
        )
        .await;
    assert!(again.is_err());
}

/// The responses each opened batch carried, by interrupt.
fn carried(harness: &Harness) -> Vec<Vec<(String, usize)>> {
    let scopes = harness.actions.scopes.lock().expect("scopes").clone();
    scopes
        .iter()
        .map(|scope| {
            scope
                .transcripts
                .iter()
                .map(|kept| (kept.interrupt_id.clone(), kept.responses.len()))
                .collect()
        })
        .collect()
}

#[tokio::test]
async fn an_approval_reaches_only_the_run_it_was_given_to() {
    let (harness, run_id) = frozen().await;
    let challenge = opened(&harness, &run_id).await;
    let resumed = harness
        .service
        .resume_routine_human(
            TEAM,
            &run_id,
            &answer(&challenge, "submit"),
            &Reporter::silent(),
        )
        .await
        .expect("resumed");
    assert_eq!(status(&resumed), Some("done"));
    harness
        .store
        .update(TEAM, |mut state| {
            state.routines[0].next_run_at = now() - 1;
            Ok::<_, crate::routine::store::StoreError>((state, ()))
        })
        .expect("due again");
    harness
        .actions
        .answer(Outcome::Human(human_request("approval", 0)));
    let (next, status) = harness.run_due().await;
    assert_ne!(next, run_id);
    assert_eq!(status, "frozen");
    // Every run reuses the step's interrupt id; only the resumed batch of the first run carried its approval.
    let carried = carried(&harness);
    assert_eq!(
        carried,
        vec![vec![], vec![("routine-step-0".to_owned(), 1)], vec![]]
    );
}

#[tokio::test]
async fn a_denial_ends_the_frozen_run_denied_and_runs_nothing() {
    let (harness, run_id) = frozen().await;
    let challenge = opened(&harness, &run_id).await;
    let denied = harness
        .service
        .resume_routine_human(
            TEAM,
            &run_id,
            &answer(&challenge, "deny"),
            &Reporter::silent(),
        )
        .await
        .expect("denied");
    assert_eq!(status(&denied), Some("denied"));
    assert_eq!(harness.run(&run_id), None);
    assert_eq!(harness.actions.operations().len(), 1);
    assert_eq!(harness.notices().last().map(String::as_str), Some("denied"));
}

#[tokio::test]
async fn an_answer_refused_while_a_chat_holds_the_team_leaves_the_challenge_answerable() {
    let (harness, run_id) = frozen().await;
    let challenge = opened(&harness, &run_id).await;
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
        .resume_routine_human(
            TEAM,
            &run_id,
            &answer(&challenge, "submit"),
            &Reporter::silent(),
        )
        .await;
    assert_eq!(
        busy.map_err(|problem| problem.code),
        Err(Some("chat-active"))
    );
    drop(chat);
    let resumed = harness
        .service
        .resume_routine_human(
            TEAM,
            &run_id,
            &answer(&challenge, "submit"),
            &Reporter::silent(),
        )
        .await
        .expect("resumed");
    assert_eq!(status(&resumed), Some("done"));
}

#[tokio::test]
async fn a_changed_team_ends_the_frozen_run_instead_of_replaying() {
    let (harness, run_id) = frozen().await;
    harness.team.change(TEAM, |team| {
        team.assistants[0].binding.container = "d".repeat(64);
    });
    let changed = harness
        .service
        .open_routine_challenge(TEAM, &run_id, "en")
        .await;
    assert_eq!(
        changed.map_err(|problem| problem.code),
        Err(Some("team-context-changed"))
    );
    assert_eq!(harness.run(&run_id), None);
    assert_eq!(harness.actions.operations().len(), 1);
}

#[tokio::test]
async fn a_renamed_team_never_ends_a_frozen_run() {
    let (harness, run_id) = frozen().await;
    harness
        .team
        .change(TEAM, |team| team.team_name = "Renamed".to_owned());
    let challenge = opened(&harness, &run_id).await;
    assert_ne!(challenge.len(), 0);
}

#[tokio::test]
async fn an_answer_to_another_run_s_or_an_unknown_challenge_is_refused() {
    let (harness, run_id) = frozen().await;
    let unknown = harness
        .service
        .resume_routine_human(
            TEAM,
            &run_id,
            &answer(&"0".repeat(32), "submit"),
            &Reporter::silent(),
        )
        .await;
    assert_eq!(
        unknown.map_err(|problem| problem.code),
        Err(Some("human-request-expired"))
    );
    let malformed = Body::Json(json(r#"{"decision":"maybe"}"#));
    let refused = harness
        .service
        .resume_routine_human(TEAM, &run_id, &malformed, &Reporter::silent())
        .await;
    assert_eq!(
        refused.map_err(|problem| problem.code),
        Err(Some("invalid-body"))
    );
}

#[tokio::test]
async fn a_frozen_run_whose_protection_is_no_longer_held_still_shows_its_request_and_says_it_lost_it()
 {
    let (harness, run_id) = frozen().await;
    // As after a restart: this process no longer holds the values the run protected.
    harness.service.parts().protections.clear();
    let challenge = opened(&harness, &run_id).await;
    let denied = harness
        .service
        .resume_routine_human(
            TEAM,
            &run_id,
            &answer(&challenge, "deny"),
            &Reporter::silent(),
        )
        .await
        .expect("denied");
    assert_eq!(status(&denied), Some("denied"));
    let last = harness.state().notices.last().cloned().expect("a notice");
    assert!(last.protection_lost);
}
