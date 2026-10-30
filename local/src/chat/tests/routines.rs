//! A chat turn that records a Routine: its card, question, or refusal, and the card's confirmation
//! (`tests/test_local_routine_proposal.py`).

use std::sync::Arc;

use strict_json::Value;
use team_routine::pin::PinnedAssistant;
use team_storage::private::epoch_seconds;

use super::harness::{Harness, Streamed, TEAM, harness, reply, suspension, turn};
use crate::audit::Principal;
use crate::chat::tests::fakes::json;
use crate::http::service::Problem;
use crate::routine::proposal::confirm::Answering;
use crate::routine::state::load;

const SUPERVISOR: &str = "cccccccccccccccccccccccccccccccc";
const MESSAGE: &str = "Liste as zonas de example.com a cada hora e mostre sempre";
const DESCRIPTION: &str = "Lista as zonas de example.com.";

/// A chat body under a request identity issued now, which may record a Routine.
fn fresh(harness: &Harness, message: &str) -> Value {
    let now = epoch_seconds(harness.clock.as_ref());
    json(&format!(
        r#"{{"message":"{message}","files":[],"assistant_ids":["shimpz-cloudflare"],"conversation":[],
        "request":{{"issued_at":{now},"nonce":"{}"}},"timezone":"UTC","locale":"pt","refine":null}}"#,
        "1".repeat(32)
    ))
}

fn recorded(record: &str) -> Value {
    turn(&format!(
        r#""status":"completed","reply":"Pronto.","actions":[],"routine":{record}"#
    ))
}

fn record() -> String {
    format!(r#"{{"name":"Zonas","description":"{DESCRIPTION}","replaces":null}}"#)
}

/// A turn that lists the zones and then records them as a Routine.
async fn recording_turn(harness: &Harness, message: &str, record: &str) -> Streamed {
    harness.brain.answer(Ok(suspension("interrupt-1")));
    harness
        .executor
        .complete(json(r#"{"zones":["example.com"]}"#));
    harness.brain.answer(Ok(recorded(record)));
    harness.chat(fresh(harness, message)).await
}

fn answering<'a>(
    harness: &'a Harness,
    actor: &'a Principal,
    supervisor: Option<&'a str>,
) -> Answering<'a> {
    Answering {
        team: harness.team.as_ref(),
        locks: &harness.coordination.locks,
        audit: harness.audit.as_ref(),
        actor,
        supervisor,
    }
}

fn actor() -> Principal {
    Principal {
        id: Some(SUPERVISOR.to_owned()),
        class: crate::audit::PrincipalClass::Human,
        credential_state: None,
        trace_id: None,
    }
}

fn proposal_id(done: &Streamed) -> String {
    let card = done
        .body
        .get("routine_proposal")
        .unwrap_or_else(|| panic!("{:?}", done.body));
    card.get("proposal_id")
        .and_then(team_protocol::python::value::string)
        .expect("id")
        .to_owned()
}

#[tokio::test]
async fn the_persons_turn_records_a_card_whose_confirmation_creates_the_routine() {
    let harness = harness();
    let done = recording_turn(&harness, MESSAGE, &record()).await;
    assert_eq!(
        (done.status, done.text("reply")),
        (200, Some("Pronto.")),
        "{:?}",
        done.body
    );
    let card = done.body.get("routine_proposal").expect("card");
    assert_eq!(
        card.get("description"),
        Some(&Value::String(DESCRIPTION.to_owned()))
    );
    assert_eq!(card.get("output"), Some(&json(r#""show""#)));
    // The card's step is the call the turn ran, read-only, with its literal zone complete.
    let step = card.get("steps").and_then(|steps| match steps {
        Value::Array(items) => items.first().cloned(),
        _ => None,
    });
    let step = step.expect("step");
    assert_eq!(
        (step.get("action"), step.get("read_only")),
        (Some(&json(r#""list-zones""#)), Some(&Value::Bool(true)))
    );
    let actor = actor();
    let routine = &harness.parts.routine;
    let id = proposal_id(&done);
    let created = routine
        .confirm(&answering(&harness, &actor, Some(SUPERVISOR)), TEAM, &id)
        .await
        .expect("confirmed");
    assert_eq!(created.get("status"), Some(&json(r#""created""#)));
    let state = load(&routine.routines, TEAM).expect("state");
    assert_eq!(state.routines.len(), 1);
    assert!(state.routines[0].confirmation.is_some());
    // A repeated confirmation, or a cancel after it, answers with the Routine the confirmation committed.
    let again = routine
        .confirm(&answering(&harness, &actor, Some(SUPERVISOR)), TEAM, &id)
        .await;
    assert_eq!(again.as_ref().ok(), Some(&created));
    let cancelled = routine
        .revoke(&answering(&harness, &actor, Some(SUPERVISOR)), TEAM, &id)
        .await;
    assert_eq!(cancelled.ok(), Some(created));
    assert_eq!(
        load(&routine.routines, TEAM).expect("state").routines.len(),
        1
    );
}

#[tokio::test]
async fn cancelling_revokes_the_card_and_it_never_confirms() {
    let harness = harness();
    let id = proposal_id(&recording_turn(&harness, MESSAGE, &record()).await);
    let actor = actor();
    let routine = &harness.parts.routine;
    let revoked = routine
        .revoke(&answering(&harness, &actor, Some(SUPERVISOR)), TEAM, &id)
        .await
        .expect("revoked");
    assert_eq!(revoked.get("status"), Some(&json(r#""revoked""#)));
    let late = routine
        .confirm(&answering(&harness, &actor, Some(SUPERVISOR)), TEAM, &id)
        .await;
    assert_eq!(
        late.map_err(|problem| problem.code),
        Err(Some("routine-proposal-expired"))
    );
    assert_eq!(
        load(&routine.routines, TEAM).expect("state").routines.len(),
        0
    );
}

#[tokio::test]
async fn only_its_person_answers_a_card_and_only_while_the_team_is_unchanged() {
    let harness = harness();
    let id = proposal_id(&recording_turn(&harness, MESSAGE, &record()).await);
    let actor = actor();
    let routine = &harness.parts.routine;
    let nobody = routine
        .confirm(&answering(&harness, &actor, None), TEAM, &id)
        .await;
    assert_eq!(
        nobody.map_err(|problem| problem.code),
        Err(Some("routine-card-person-required"))
    );
    let other = "d".repeat(32);
    let someone = routine
        .confirm(&answering(&harness, &actor, Some(&other)), TEAM, &id)
        .await;
    assert_eq!(
        someone.map_err(|problem| problem.code),
        Err(Some("routine-proposal-expired"))
    );
    // An Assistant updated since the card was made changes its pin, and the confirmation refuses.
    harness.team.change(TEAM, |team| {
        let pinned = &team.assistants[0].pinned;
        let updated = PinnedAssistant {
            version: "2.0.0".to_owned(),
            ..(**pinned).clone()
        };
        team.assistants[0].pinned = Arc::new(updated);
    });
    let changed: Result<Value, Problem> = routine
        .confirm(&answering(&harness, &actor, Some(SUPERVISOR)), TEAM, &id)
        .await;
    assert_eq!(
        changed.map_err(|problem| problem.code),
        Err(Some("team-context-changed"))
    );
    assert_eq!(
        load(&routine.routines, TEAM).expect("state").routines.len(),
        0
    );
}

#[tokio::test]
async fn a_recording_with_no_stated_schedule_asks_and_keeps_its_span() {
    let harness = harness();
    let done = recording_turn(
        &harness,
        "Liste as zonas de example.com e mostre sempre",
        &record(),
    )
    .await;
    assert_eq!(done.status, 200, "{:?}", done.body);
    let question = done.body.get("routine_question").expect("question");
    assert_eq!(
        question.get("code"),
        Some(&json(r#""routine-schedule-unstated""#))
    );
    assert_eq!(done.body.get("routine_proposal"), None);
    // The next send continues the span: its Brain sees the question it answers.
    harness.brain.answer(Ok(reply("Ok.")));
    harness.chat(fresh(&harness, "A cada hora")).await;
    let latest = harness.brain.received().pop().expect("start");
    assert_eq!(
        latest
            .body
            .get("routine_question")
            .and_then(|asked| asked.get("code")),
        question.get("code")
    );
}

#[tokio::test]
async fn a_malformed_record_fails_the_turn_and_a_stale_request_records_nothing() {
    let harness = harness();
    let malformed = recording_turn(&harness, MESSAGE, r#"{"name":"Zonas","op":"create"}"#).await;
    assert_eq!(
        (malformed.status, malformed.code()),
        (502, Some("brain-runtime-failed"))
    );
    // A request identity issued long ago opens no send: its record keeps the reply and carries a refusal.
    harness.brain.answer(Ok(suspension("interrupt-1")));
    harness
        .executor
        .complete(json(r#"{"zones":["example.com"]}"#));
    harness.brain.answer(Ok(recorded(&record())));
    let stale = harness
        .chat(super::harness::chat_body(
            MESSAGE,
            &["shimpz-cloudflare"],
            None,
        ))
        .await;
    assert_eq!((stale.status, stale.text("reply")), (200, Some("Pronto.")));
    let refusal = stale
        .body
        .get("routine_refusal")
        .and_then(|refusal| refusal.get("code"))
        .cloned();
    assert_eq!(refusal, Some(json(r#""routine-recording-unavailable""#)));
}

#[tokio::test]
async fn a_turn_about_a_routine_reasons_with_the_routine_model() {
    let harness = harness();
    let model = |done: &Streamed| {
        let usage = done
            .body
            .get("usage")
            .and_then(|usage| usage.get("models"))
            .cloned();
        let first = usage.and_then(|models| match models {
            Value::Array(items) => items.first().and_then(|item| item.get("model")).cloned(),
            _ => None,
        });
        first.expect("model")
    };
    harness.brain.answer(Ok(reply("Ok.")));
    let routine = harness
        .chat(fresh(
            &harness,
            "cria uma rotina pra mim, que lista as zonas",
        ))
        .await;
    assert_eq!(model(&routine), json(r#""gpt-6.1-sol""#));
    harness.brain.answer(Ok(reply("Ok.")));
    let ordinary = harness
        .chat(super::harness::chat_body(
            "Liste as zonas",
            &["shimpz-cloudflare"],
            None,
        ))
        .await;
    assert_eq!(model(&ordinary), json(r#""gpt-6-luna""#));
}

#[tokio::test]
async fn an_injected_integration_token_never_reaches_a_recording() {
    const TOKEN: &str = "integration-token-value-1";
    let harness = harness();
    harness
        .executor
        .injected
        .lock()
        .expect("injected")
        .push(TOKEN.to_owned());
    // The model holds the token (a result echoed it, or a person pasted it) and passes it on as an input.
    harness.brain.answer(Ok(turn(&format!(
        r#""status":"action-required","reply":"","actions":[{{"interrupt_id":"interrupt-1",
        "assistant_id":"shimpz-cloudflare","action":"list-zones","input":{{"zone":"{TOKEN}"}}}}]"#
    ))));
    harness
        .executor
        .complete(json(&format!(r#"{{"zones":["{TOKEN}"]}}"#)));
    harness.brain.answer(Ok(recorded(&record())));
    let message = format!("Liste as zonas de {TOKEN} a cada hora e mostre sempre");
    let done = harness.chat(fresh(&harness, &message)).await;
    assert!(
        !format!("{:?}", done.body).contains(TOKEN),
        "{:?}",
        done.body
    );
    let routine = &harness.parts.routine;
    if let Some(card) = done.body.get("routine_proposal") {
        let id = card
            .get("proposal_id")
            .and_then(team_protocol::python::value::string)
            .expect("id")
            .to_owned();
        let actor = actor();
        let _ = routine
            .confirm(&answering(&harness, &actor, Some(SUPERVISOR)), TEAM, &id)
            .await;
    }
    let state = load(&routine.routines, TEAM).expect("state");
    assert!(!format!("{:?}", state.routines).contains(TOKEN));
}
