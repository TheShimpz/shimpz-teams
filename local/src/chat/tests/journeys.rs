//! A turn from its admission to its terminal: replies, Actions, Stop, the chat limit, and drift.

use std::sync::Arc;
use std::time::Duration;

use strict_json::Value;
use team_core::future::BoxFuture;
use team_inference::brain::{BrainClient, BrainError, BrainOperation, BrainRequest};
use team_inference::testing::ScriptedBrain;

use super::harness::{
    BrainClientScripted, TEAM, chat_body, cloudflare, harness, harness_on, harness_with, reply,
    suspension, turn,
};
use crate::chat::tests::fakes::json;
use crate::coordination::executions::ExecutionKind;
use crate::http::route::Method;

#[tokio::test]
async fn a_brain_only_turn_completes_with_its_usage_under_the_teams_current_name() {
    let harness = harness();
    harness
        .team
        .change(TEAM, |team| team.display_name = Some("Growth".to_owned()));
    harness.brain.answer(Ok(reply("Hello.")));
    let done = harness.chat(chat_body("Hello", &[], None)).await;
    assert_eq!(done.status, 200, "{:?}", done.body);
    assert_eq!(
        (done.text("reply"), done.text("team_name")),
        (Some("Hello."), Some("Growth"))
    );
    let usage = done
        .body
        .get("usage")
        .and_then(|usage| usage.get("models"))
        .cloned();
    assert_eq!(
        usage,
        Some(json(
            r#"[{"provider":"openai","model":"gpt-6-luna","input_tokens":1331,"output_tokens":36}]"#
        ))
    );
    let received = harness.brain.received();
    assert_eq!(received[0].operation, BrainOperation::Turn);
    assert_eq!(received[0].body.get("team_name"), Some(&json("\"Growth\"")));
    assert_eq!(received[0].body.get("assistants"), Some(&json("[]")));
    let thread = format!("\"local:local-space:team_1:{}:default\"", "f".repeat(64));
    assert_eq!(received[0].body.get("thread_id"), Some(&json(&thread)));
    // The slot is free again: the next turn of the Team is admitted.
    harness.brain.answer(Ok(reply("Again.")));
    assert_eq!(
        harness.chat(chat_body("Hello", &[], None)).await.status,
        200
    );
}

#[tokio::test]
async fn an_action_runs_through_the_durable_batch_before_the_brain_resumes_and_then_is_delivered() {
    let harness = harness();
    harness.brain.answer(Ok(suspension("interrupt-1")));
    harness.brain.answer(Ok(reply("Two zones.")));
    harness.executor.complete(json(r#"{"zones":["a","b"]}"#));
    let done = harness
        .chat(chat_body("List my zones", &["shimpz-cloudflare"], None))
        .await;
    assert_eq!((done.status, done.text("reply")), (200, Some("Two zones.")));
    assert_eq!(
        harness.executor.events(),
        vec![
            "prepare interrupt-1",
            "invoke interrupt-1",
            "delivered interrupt-1"
        ]
    );
    let resumed = &harness.brain.received()[1];
    assert_eq!(resumed.operation, BrainOperation::Resume);
    assert_eq!(
        resumed.body.get("results"),
        Some(&json(r#"{"interrupt-1":{"zones":["a","b"]}}"#))
    );
    // The batch is bound to the Team's generation and its Brain thread.
    let scope = harness.executor.scopes.lock().expect("scopes")[0].clone();
    assert_eq!((scope.generation.len(), scope.routine), (64, false));
    // Each Action's progress is reported as one measured pair.
    let actions: Vec<_> = done
        .progress
        .iter()
        .filter(|event| event.get("phase") == Some(&json("\"action\"")))
        .collect();
    assert_eq!(actions.len(), 2);
}

#[tokio::test]
async fn an_unselected_or_absent_assistant_is_refused_before_the_brain() {
    let harness = harness();
    let refused = harness
        .chat(chat_body("Hello", &["account-helper"], None))
        .await;
    assert_eq!(
        (refused.status, refused.code()),
        (409, Some("assistant-unavailable"))
    );
    assert_eq!(harness.brain.received().len(), 0);
    harness.brain.answer(Ok(turn(
        r#""status":"action-required","reply":"","actions":[{"interrupt_id":"i","assistant_id":"shimpz-cloudflare",
        "action":"list-zones","input":{}}]"#,
    )));
    // The Brain may never call an Assistant the turn did not select.
    let refused = harness.chat(chat_body("Hello", &[], None)).await;
    assert_eq!(
        (refused.status, refused.code()),
        (502, Some("brain-runtime-failed"))
    );
    assert_eq!(harness.executor.invoked().len(), 0);
}

#[tokio::test]
async fn a_failed_batch_ends_its_journal_state_and_frees_the_team() {
    let harness = harness();
    harness.brain.answer(Ok(suspension("interrupt-1")));
    harness.executor.answer(Box::new(|_, _| {
        Err(team_action::executor::ActionFailure::Uncertain)
    }));
    let failed = harness
        .chat(chat_body("List", &["shimpz-cloudflare"], None))
        .await;
    assert_eq!(
        (failed.status, failed.code()),
        (502, Some("assistant-rpc-failed"))
    );
    assert_eq!(
        harness.executor.events().last().map(String::as_str),
        Some("terminate")
    );
    harness.brain.answer(Ok(reply("Fine.")));
    assert_eq!(
        harness.chat(chat_body("Hello", &[], None)).await.status,
        200
    );
}

#[tokio::test]
async fn the_space_chat_limit_and_a_running_routine_refuse_a_turn_at_admission() {
    let harness = harness();
    let limit = harness
        .json(Method::Put, "/v1/chat/limit", Some(json(r#"{"limit":1}"#)))
        .await;
    assert_eq!(limit, Ok(json(r#"{"limit":1}"#)));
    let held = harness
        .coordination
        .executions
        .admit("team_2", ExecutionKind::Chat, "t".into())
        .expect("held");
    let busy = harness.chat(chat_body("Hello", &[], None)).await;
    assert_eq!((busy.status, busy.code()), (429, Some("space-busy")));
    drop(held);
    let routine = ExecutionKind::Routine {
        routine_id: "r".repeat(32),
    };
    let segment = harness
        .coordination
        .executions
        .admit(TEAM, routine, "s".into())
        .expect("segment");
    let waiting = harness.chat(chat_body("Hello", &[], None)).await;
    assert_eq!(
        (waiting.status, waiting.code()),
        (409, Some("routine-active"))
    );
    drop(segment);
    assert_eq!(
        harness.json(Method::Get, "/v1/chat/limit", None).await,
        Ok(json(r#"{"limit":1}"#))
    );
    let refused = harness
        .json(Method::Put, "/v1/chat/limit", Some(json(r#"{"limit":3}"#)))
        .await;
    assert_eq!(
        refused.map_err(|problem| problem.code),
        Err(Some("invalid-body"))
    );
}

#[tokio::test]
async fn a_provider_or_identity_change_ends_the_turn_before_its_action() {
    let harness = harness();
    harness
        .inference
        .save(
            TEAM,
            &team_inference::config::InferenceConfig {
                provider: "anthropic".to_owned(),
                model: "claude-sonnet-5-5".to_owned(),
            },
        )
        .expect("saved");
    let mismatch = harness.chat(chat_body("Hello", &[], None)).await;
    assert_eq!(
        (mismatch.status, mismatch.code()),
        (409, Some("inference-provider-mismatch"))
    );
    harness
        .inference
        .save(
            TEAM,
            &team_inference::config::InferenceConfig {
                provider: "openai".to_owned(),
                model: "gpt-6-luna".to_owned(),
            },
        )
        .expect("saved");
    // The Assistant's container is replaced while the Brain plans its Action.
    let team = Arc::new(crate::chat::tests::fakes::FakeTeam::with(
        TEAM,
        vec![cloudflare()],
    ));
    let scripted = Arc::new(ScriptedBrain::default());
    let drifting = Arc::new(Drifting {
        scripted,
        team: Arc::clone(&team),
    });
    let harness = harness_on(drifting, team);
    harness.brain.answer(Ok(suspension("interrupt-1")));
    let changed = harness
        .chat(chat_body("List", &["shimpz-cloudflare"], None))
        .await;
    assert_eq!(
        (changed.status, changed.code()),
        (409, Some("team-context-changed"))
    );
    assert_eq!(harness.executor.invoked().len(), 0);
}

/// A Brain that replaces the Team's Assistant container while it answers.
struct Drifting {
    scripted: Arc<ScriptedBrain>,
    team: Arc<crate::chat::tests::fakes::FakeTeam>,
}

impl BrainClient for Drifting {
    fn call<'a>(&'a self, request: BrainRequest<'a>) -> BoxFuture<'a, Result<Value, BrainError>> {
        Box::pin(async move {
            for team in self.team.teams.lock().expect("teams").values_mut() {
                for active in &mut team.assistants {
                    active.binding.container = "d".repeat(64);
                }
            }
            self.scripted.call(request).await
        })
    }
}

impl BrainClientScripted for Drifting {
    fn client(self: Arc<Self>) -> Arc<dyn BrainClient> {
        self
    }

    fn scripted(self: Arc<Self>) -> Arc<ScriptedBrain> {
        Arc::clone(&self.scripted)
    }
}

/// A Brain whose turn start waits for the turn's Stop.
struct Blocking(Arc<ScriptedBrain>);

impl BrainClient for Blocking {
    fn call<'a>(&'a self, request: BrainRequest<'a>) -> BoxFuture<'a, Result<Value, BrainError>> {
        Box::pin(async move {
            if request.operation == BrainOperation::Turn {
                request.stop.stopped().await;
                return Err(BrainError::Stopped);
            }
            self.0.call(request).await
        })
    }
}

impl BrainClientScripted for Blocking {
    fn client(self: Arc<Self>) -> Arc<dyn BrainClient> {
        self
    }

    fn scripted(self: Arc<Self>) -> Arc<ScriptedBrain> {
        Arc::clone(&self.0)
    }
}

#[tokio::test]
async fn stop_wakes_the_blocked_brain_request_ends_the_turn_stopped_and_frees_the_team() {
    let harness = harness_with(
        Arc::new(Blocking(Arc::new(ScriptedBrain::default()))),
        vec![cloudflare()],
    );
    let answer = harness
        .call(
            Method::Post,
            "/v1/teams/team_1/chat",
            Some(chat_body("Hello", &[], None)),
        )
        .await;
    let Ok(crate::http::service::Answer::Stream(mut records)) = answer else {
        panic!("not a stream")
    };
    // The turn holds its slot until Stop reaches it.
    for _ in 0..100 {
        if harness.coordination.executions.chat_executions() == 1 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    let stopped = harness
        .json(Method::Post, "/v1/teams/team_1/chat/stop", Some(json("{}")))
        .await
        .expect("stop");
    assert_eq!(stopped.get("accepted"), Some(&Value::Bool(true)));
    assert_eq!(stopped.get("confirmed"), Some(&Value::Bool(false)));
    let terminal = loop {
        match records.recv().await.expect("record") {
            crate::http::service::StreamRecord::Terminal { status, body } => break (status, body),
            crate::http::service::StreamRecord::Progress(_) => {}
        }
    };
    assert_eq!(
        (terminal.0, terminal.1.get("code")),
        (409, Some(&json("\"chat-stopped\"")))
    );
    assert_eq!(harness.coordination.executions.chat_executions(), 0);
    // Nothing was left to stop.
    let idle = harness
        .json(Method::Post, "/v1/teams/team_1/chat/stop", Some(json("{}")))
        .await
        .expect("stop");
    assert_eq!(idle.get("accepted"), Some(&Value::Bool(false)));
}

#[tokio::test]
async fn memory_a_turn_proposes_is_learned_only_as_its_reply_commits_and_each_change_is_recorded() {
    let harness = harness();
    harness.brain.answer(Ok(turn(
        r#""status":"completed","reply":"Noted.","actions":[],
        "memory":[{"op":"remember","topic":"tone","preference":"Short replies."}]"#,
    )));
    let done = harness
        .chat(chat_body("Keep replies short", &[], None))
        .await;
    assert_eq!(done.status, 200, "{:?}", done.body);
    let learned = harness.inference.load_knowledge(TEAM).expect("knowledge");
    assert_eq!(
        learned,
        vec![json(r#"{"topic":"tone","preference":"Short replies."}"#)]
    );
    let phases: Vec<_> = harness
        .audit
        .events()
        .iter()
        .filter_map(|event| {
            event
                .fields()
                .iter()
                .find(|(name, _)| name == "activity")
                .map(|(_, value)| value.get("phase").cloned())
        })
        .collect();
    assert_eq!(
        phases,
        vec![Some(json("\"requested\"")), Some(json("\"completed\""))]
    );
    // The next turn's Brain receives what was learned.
    harness.brain.answer(Ok(reply("Ok.")));
    harness.chat(chat_body("Hi", &[], None)).await;
    let memories = harness.brain.received()[1].body.get("memories").cloned();
    assert_eq!(
        memories,
        Some(json(r#"[{"topic":"tone","preference":"Short replies."}]"#))
    );
}
