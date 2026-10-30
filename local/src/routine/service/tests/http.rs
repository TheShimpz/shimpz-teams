//! The Routine routes: the scheduler's claim and notices under the bearer, one leased run under its own routine
//! assertion, and a Supervisor's management, each answered in its canonical protocol view.

use strict_json::Value;
use team_protocol::routine_run::{canonical_claim, canonical_segment_answer};

use super::now;
use crate::chat::tests::fakes::json;
use crate::http::route::{Method, resolve};
use crate::http::service::{Answer, Body, Call, Principal, Problem, Service, StreamRecord};
use crate::routine::executor::FaultClass;
use crate::routine::executor::fakes::Outcome;
use crate::routine::service::harness::{Harness, TEAM, action, harness, routine_of, running};

fn routines() -> Harness {
    let active = running(&[action("apply", "mutating", None)]);
    harness(active.clone(), &routine_of(&active, &["apply"], now()))
}

fn supervisor() -> Principal {
    Principal::Supervisor {
        supervisor_id: "c".repeat(32),
        assurance: None,
        authority_sha256: "a".repeat(64),
    }
}

async fn machine(harness: &Harness, path: &str, body: Option<Value>) -> Result<Value, Problem> {
    let method = if body.is_some() {
        Method::Post
    } else {
        Method::Get
    };
    call(harness, (method, path, body), Principal::Machine).await
}

async fn call(
    harness: &Harness,
    request: (Method, &str, Option<Value>),
    principal: Principal,
) -> Result<Value, Problem> {
    let (method, path, body) = request;
    let route = resolve(method, path).expect("a route");
    let call = Call {
        route: &route,
        principal,
        body: body.map_or(Body::None, Body::Json),
        model: None,
        decision_key: None,
        trace_id: "b".repeat(32),
        upgrade_key: None,
    };
    match harness.service.call(call).await? {
        Answer::Json { body, .. } => Ok(body),
        Answer::Stream(mut records) => {
            let mut terminal = None;
            while let Some(record) = records.recv().await {
                if let StreamRecord::Terminal { body, .. } = record {
                    terminal = Some(body);
                }
            }
            Ok(terminal.expect("a terminal record"))
        }
        _ => panic!("an unexpected answer"),
    }
}

fn text(value: &Value, name: &str) -> String {
    match value.get(name) {
        Some(Value::String(text)) => text.clone(),
        _ => panic!("{name}"),
    }
}

#[tokio::test]
async fn a_claimed_run_runs_end_to_end_under_its_own_routine_assertion_only() {
    let harness = routines();
    let claim = Some(json(r#"{"long":true}"#));
    let claimed = machine(&harness, "/v1/routines/claim", claim)
        .await
        .expect("claimed");
    assert!(canonical_claim(&claimed).is_some(), "{claimed:?}");
    let run = claimed.get("run").cloned().expect("a run");
    let path = format!(
        "/v1/teams/{TEAM}/routines/runs/{}/segment",
        text(&run, "run_id")
    );
    let body = json(&format!(
        r#"{{"revision":1,"plan_digest":"{}"}}"#,
        text(&run, "plan_digest")
    ));
    let denied = call(
        &harness,
        (Method::Post, &path, Some(body.clone())),
        supervisor(),
    )
    .await;
    assert_eq!(
        denied.map_err(|problem| problem.code),
        Err(Some("invalid-routine"))
    );
    let routine = Principal::Routine {
        key_fingerprint: "e".repeat(64),
        lease_sha256: team_routine::record::lease_sha256(&text(&run, "lease_token")),
    };
    let answer = call(&harness, (Method::Post, &path, Some(body)), routine)
        .await
        .expect("a segment");
    assert!(canonical_segment_answer(&answer).is_some(), "{answer:?}");
    assert_eq!(text(&answer, "status"), "done");
    let short = Some(json(r#"{"long":false}"#));
    let none = machine(&harness, "/v1/routines/claim", short)
        .await
        .expect("nothing due");
    assert!(canonical_claim(&none).is_some(), "{none:?}");
}

fn deliveries(batch: &Value) -> Value {
    let Some(Value::Array(notices)) = batch.get("notices") else {
        panic!("notices");
    };
    assert_ne!(notices.len(), 0);
    let field =
        |notice: &Value, name: &str| (name.to_owned(), notice.get(name).cloned().expect("a field"));
    let delivered = notices
        .iter()
        .map(|notice| {
            Value::Object(vec![
                field(notice, "team_id"),
                field(notice, "notice_id"),
                field(notice, "version"),
            ])
        })
        .collect();
    Value::Object(vec![("deliveries".to_owned(), Value::Array(delivered))])
}

#[tokio::test]
async fn notices_are_delivered_in_batches_and_acknowledged_by_version() {
    let harness = routines();
    let (_run, status) = harness.run_due().await;
    assert_eq!(status, "done");
    let batch = machine(&harness, "/v1/routines/notices", None)
        .await
        .expect("notices");
    let acknowledged = machine(
        &harness,
        "/v1/routines/notices/ack",
        Some(deliveries(&batch)),
    )
    .await;
    assert_eq!(acknowledged, Ok(json(r#"{"acknowledged":true}"#)));
    let invalid = machine(&harness, "/v1/routines/notices/ack", Some(json("{}"))).await;
    assert_eq!(
        invalid.map_err(|problem| problem.code),
        Err(Some("invalid-body"))
    );
}

#[tokio::test]
async fn a_supervisor_lists_routines_and_opens_and_answers_a_card_but_a_machine_cannot() {
    let harness = routines();
    harness
        .actions
        .answer(Outcome::Fail(FaultClass::Transport, None, None));
    let (run_id, status) = harness.run_due().await;
    assert_eq!(status, "held");
    let listed = call(
        &harness,
        (Method::Get, &format!("/v1/teams/{TEAM}/routines"), None),
        supervisor(),
    )
    .await
    .expect("listed");
    assert!(matches!(listed.get("incidents"), Some(Value::Array(items)) if items.len() == 1));
    let card_path = format!("/v1/teams/{TEAM}/routines/incidents/{run_id}/card");
    let machine = call(
        &harness,
        (Method::Post, &card_path, Some(json("{}"))),
        Principal::Machine,
    )
    .await;
    assert_eq!(
        machine.map_err(|problem| problem.code),
        Err(Some("routine-card-person-required"))
    );
    let card = call(
        &harness,
        (Method::Post, &card_path, Some(json("{}"))),
        supervisor(),
    )
    .await
    .expect("a card");
    let answer_path = format!("/v1/teams/{TEAM}/routines/incidents/{run_id}/answer");
    let tag = json(&format!(r#"{{"tag":"{}"}}"#, text(&card, "tag")));
    let answered = call(
        &harness,
        (Method::Post, &answer_path, Some(tag)),
        supervisor(),
    )
    .await;
    assert_eq!(
        answered,
        Ok(json(&format!(
            r#"{{"team_id":"{TEAM}","run_id":"{run_id}"}}"#
        )))
    );
}

#[tokio::test]
async fn a_routine_is_paused_and_resumed_with_an_empty_body_only() {
    let harness = routines();
    let path = format!("/v1/teams/{TEAM}/routines/{}/pause", "a".repeat(32));
    let refused = call(
        &harness,
        (Method::Post, &path, Some(json(r#"{"x":1}"#))),
        supervisor(),
    )
    .await;
    assert_eq!(
        refused.map_err(|problem| problem.code),
        Err(Some("invalid-body"))
    );
    let paused = call(
        &harness,
        (Method::Post, &path, Some(json("{}"))),
        supervisor(),
    )
    .await
    .expect("paused");
    assert_eq!(paused.get("paused"), Some(&Value::Bool(true)));
    assert!(harness.claim().await.is_none());
    let resume = format!("/v1/teams/{TEAM}/routines/{}/resume", "a".repeat(32));
    call(
        &harness,
        (Method::Post, &resume, Some(json("{}"))),
        supervisor(),
    )
    .await
    .expect("resumed");
    assert!(harness.claim().await.is_some());
}
