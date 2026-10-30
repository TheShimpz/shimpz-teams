//! A paused turn across a restart and expiry, the Integration and Stored Input pauses, Stop racing an Action, and the
//! Team's teardown.

use std::sync::Arc;
use std::time::Duration;

use strict_json::Value;
use team_action::executor::InvokeOutcome;
use team_core::secret::Secret;
use team_inference::testing::ScriptedBrain;
use team_integrations::token::TokenSet;

use super::harness::{TEAM, chat_body, harness, harness_with, reply, suspension, turn};
use crate::chat::fakes::{connected, human_request, json, password_request, searching};
use crate::chat::teardown::ContinuationsTeardown;
use crate::coordination::residue::{TeamRef, TeamTeardown};
use crate::http::route::Method;
use crate::http::service::Service;

async fn human_pause(harness: &super::harness::Harness) -> String {
    harness.brain.answer(Ok(suspension("interrupt-1")));
    harness.executor.answer(Box::new(|_, _| {
        Ok(InvokeOutcome::HumanRequest(human_request("approval", 0)))
    }));
    let paused = harness
        .chat(chat_body("List", &["shimpz-cloudflare"], None))
        .await;
    assert_eq!(paused.status, 428, "{:?}", paused.body);
    paused.text("challenge_id").expect("challenge").to_owned()
}

#[tokio::test]
async fn a_restart_restores_a_paused_turn_that_is_then_answered() {
    let harness = harness();
    let challenge = human_pause(&harness).await;
    let restarted = harness.restarted();
    restarted.restore().await.expect("restored");
    let pending = restarted
        .human_challenges()
        .current(TEAM)
        .expect("read")
        .expect("pending");
    assert_eq!(pending.id, challenge);
    harness.brain.answer(Ok(reply("Done.")));
    let body = json(&format!(
        r#"{{"challenge_id":"{challenge}","decision":"submit","value":true}}"#
    ));
    let route =
        crate::http::route::resolve(Method::Post, "/v1/teams/team_1/chat/human").expect("route");
    let call = crate::http::service::Call {
        route: &route,
        principal: crate::http::service::Principal::Supervisor {
            supervisor_id: "c".repeat(32),
            assurance: None,
        },
        body: crate::http::service::Body::Json(body),
        model: Some(team_inference::brain::ModelCredential {
            provider: "openai".to_owned(),
            model: String::new(),
            api_key: Secret::new("test-key-not-real-0000".to_owned()),
        }),
        decision_key: None,
        trace_id: "b".repeat(32),
        upgrade_key: None,
    };
    let Ok(crate::http::service::Answer::Stream(mut records)) = restarted.call(call).await else {
        panic!("not a stream");
    };
    let mut terminal = None;
    while let Some(record) = records.recv().await {
        if let crate::http::service::StreamRecord::Terminal { status, body } = record {
            terminal = Some((status, body));
        }
    }
    let (status, body) = terminal.expect("terminal");
    assert_eq!((status, body.get("reply")), (200, Some(&json("\"Done.\""))));
}

#[tokio::test]
async fn an_expired_pause_ends_its_batch_at_restart_and_in_a_running_controller() {
    let harness = harness();
    human_pause(&harness).await;
    harness.clock.advance(Duration::from_secs(400));
    let restarted = harness.restarted();
    restarted.restore().await.expect("restored");
    assert_eq!(
        restarted
            .human_challenges()
            .current(TEAM)
            .expect("read")
            .map(|item| item.id.clone()),
        None
    );
    assert_eq!(harness.continuations.current(TEAM).expect("read"), None);
    assert_eq!(
        harness.executor.events().last().map(String::as_str),
        Some("terminate")
    );
}

/// Seals a Cloudflare token for the Team's Assistant, as a completed authorization does.
fn connect(harness: &super::harness::Harness) {
    let tokens = TokenSet {
        access_token: Secret::new("oauth-access-test-token-123456789".to_owned()),
        refresh_token: None,
        scopes: vec!["dns.read".to_owned(), "zone.read".to_owned()],
        expires_in: 3600,
        broker_lease: None,
    };
    let scopes = vec!["dns.read".to_owned(), "zone.read".to_owned()];
    let reference = (TEAM, "shimpz-cloudflare", "cloudflare");
    let sealed = harness
        .integrations
        .put(reference, ("cloudflare", &scopes), &tokens, None);
    sealed.expect("connected");
}

#[tokio::test]
async fn an_integration_pause_waits_until_its_integration_is_connected() {
    let harness = harness_with(Arc::new(ScriptedBrain::default()), vec![connected()]);
    harness.brain.answer(Ok(suspension("interrupt-1")));
    let paused = harness
        .chat(chat_body("List", &["shimpz-cloudflare"], None))
        .await;
    assert_eq!(
        (paused.status, paused.text("status")),
        (428, Some("integrations-required")),
        "{:?}",
        paused.body
    );
    let challenge = paused.text("challenge_id").expect("challenge").to_owned();
    assert_eq!(
        harness.executor.events().len(),
        0,
        "nothing ran before the pause"
    );
    let pending = harness
        .json(Method::Get, "/v1/teams/team_1/chat/integrations", None)
        .await
        .expect("pending");
    assert_eq!(
        pending.get("challenge_id"),
        Some(&Value::String(challenge.clone()))
    );
    let resume = json(&format!(r#"{{"challenge_id":"{challenge}"}}"#));
    let still = harness
        .stream(
            Method::Post,
            "/v1/teams/team_1/chat/integrations",
            resume.clone(),
        )
        .await;
    assert_eq!(
        still.status, 428,
        "a missing Integration keeps the turn paused"
    );
    connect(&harness);
    harness.brain.answer(Ok(reply("Zones listed.")));
    let done = harness
        .stream(Method::Post, "/v1/teams/team_1/chat/integrations", resume)
        .await;
    assert_eq!(
        (done.status, done.text("reply")),
        (200, Some("Zones listed.")),
        "{:?}",
        done.body
    );
    assert_eq!(harness.executor.invoked(), vec!["interrupt-1"]);
    assert_eq!(harness.continuations.current(TEAM).expect("read"), None);
}

#[tokio::test]
async fn a_stored_input_answer_is_sealed_for_its_paused_action_and_never_replayed_as_text() {
    let harness = harness_with(Arc::new(ScriptedBrain::default()), vec![searching()]);
    harness.brain.answer(Ok(turn(
        r#""status":"action-required","reply":"","actions":[{"interrupt_id":"interrupt-1",
        "assistant_id":"shimpz-exa","action":"search","input":{}}]"#,
    )));
    harness.executor.answer(Box::new(|_, _| {
        Ok(InvokeOutcome::HumanRequest(password_request()))
    }));
    let paused = harness
        .chat(chat_body("Search", &["shimpz-exa"], None))
        .await;
    assert_eq!(paused.status, 428, "{:?}", paused.body);
    assert_eq!(
        paused.text("help_url"),
        Some("https://dashboard.exa.ai/api-keys")
    );
    let challenge = paused.text("challenge_id").expect("challenge").to_owned();
    harness.brain.answer(Ok(reply("Found it.")));
    let body = json(&format!(
        r#"{{"challenge_id":"{challenge}","decision":"submit","value":"value-not-real-0"}}"#
    ));
    let done = harness
        .stream(Method::Post, "/v1/teams/team_1/chat/human", body)
        .await;
    assert_eq!(
        (done.status, done.text("reply")),
        (200, Some("Found it.")),
        "{:?}",
        done.body
    );
    let held = harness
        .parts
        .stored_inputs
        .resolve((TEAM, "shimpz-exa", "exa-key"), "password");
    assert_eq!(
        held.map(|stored| stored.value.expose().to_owned()).ok(),
        Some("value-not-real-0".to_owned())
    );
    let kept = harness.transcripts.kept.lock().expect("kept").clone();
    assert!(
        kept.iter()
            .all(|(_, transcript)| transcript.responses.is_empty())
    );
}

#[tokio::test]
async fn a_stop_while_the_paused_action_runs_leaves_no_pause_behind() {
    let harness = harness();
    harness.brain.answer(Ok(suspension("interrupt-1")));
    let executions = harness.coordination.executions.clone();
    harness.executor.answer(Box::new(move |_, _| {
        let _ = executions.cancel(TEAM, true);
        Ok(InvokeOutcome::HumanRequest(human_request("approval", 0)))
    }));
    let stopped = harness
        .chat(chat_body("List", &["shimpz-cloudflare"], None))
        .await;
    assert_eq!(
        (stopped.status, stopped.code()),
        (409, Some("chat-stopped"))
    );
    assert_eq!(harness.continuations.current(TEAM).expect("read"), None);
    let pending = harness
        .json(Method::Get, "/v1/teams/team_1/chat/human", None)
        .await
        .expect("pending");
    assert_eq!(pending.get("status"), Some(&json("\"none\"")));
    assert_eq!(
        harness.executor.events().last().map(String::as_str),
        Some("terminate")
    );
}

#[tokio::test]
async fn a_destroyed_team_s_paused_turns_end_with_their_continuations() {
    let harness = harness();
    let challenge = human_pause(&harness).await;
    let teardown = ContinuationsTeardown(harness.service.clone());
    let team = TeamRef {
        team_id: TEAM.to_owned(),
        generation: Some("f".repeat(64)),
    };
    teardown.remove_team(&team).await.expect("removed");
    assert_eq!(harness.continuations.current(TEAM).expect("read"), None);
    let late = harness.answer(&challenge, "submit").await;
    assert_eq!(late.code(), Some("human-request-expired"));
    teardown.remove_space().await.expect("removed");
}

const USAGE: &str = r#""usage":{"model_calls":1,"provider_requests":1,"failed_calls":0,"unreported_calls":0,
    "input_tokens":1,"output_tokens":1,"cache_read_tokens":0,"cache_write_tokens":0}"#;

#[tokio::test]
async fn capability_planning_names_only_public_candidates_and_never_grants_authority() {
    let harness = harness();
    let answer =
        format!(r#"{{"status":"install-required","assistant_ids":["shimpz-cloudflare"],{USAGE}}}"#);
    harness.brain.answer(Ok(json(&answer)));
    let body = json(
        r#"{"objective":"Configure example.com","candidates":[{"id":"shimpz-cloudflare","name":"Shimpz Cloudflare",
        "summary":"Manage reviewed DNS records.","actions":["list-zones"],"integrations":[]}]}"#,
    );
    let path = "/v1/teams/team_1/chat/capability-plan";
    let plan = harness.json(Method::Post, path, Some(body)).await;
    let expected =
        r#"{"team_id":"team_1","status":"install-required","assistant_ids":["shimpz-cloudflare"]}"#;
    assert_eq!(plan, Ok(json(expected)));
    let refused = harness
        .json(Method::Post, path, Some(json(r#"{"objective":"x"}"#)))
        .await;
    assert_eq!(
        refused.map_err(|problem| problem.code),
        Err(Some("invalid-body"))
    );
    assert_eq!(harness.executor.events().len(), 0);
}

#[tokio::test]
async fn intent_routing_classifies_without_describing_installable_assistants_unasked() {
    let harness = harness();
    let answer = format!(
        r#"{{"intent":"ordinary-task","query":"","assistant_ids":[],"reply":"","task_follows":false,{USAGE}}}"#
    );
    harness.brain.answer(Ok(json(&answer)));
    let body = json(
        r#"{"objective":"Hello","expected_intent":null,"candidates":[],"lifecycle_reference":null,
        "conversation":[],"locale":"en"}"#,
    );
    let routed = harness
        .json(
            Method::Post,
            "/v1/teams/team_1/chat/intent-route",
            Some(body),
        )
        .await;
    let routed = routed.expect("routed");
    assert_eq!(routed.get("intent"), Some(&json("\"ordinary-task\"")));
    assert_eq!(routed.get("capability"), None);
}
