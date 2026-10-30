//! The durable handoff of a chat turn (ADR-0125; plan B0 and B3): a send attaches to its running turn or receives
//! its kept terminal, a Team process that died at each handoff point leaves a turn the next send resumes from its last
//! checkpoint with no Action run twice, and only the turn's own session settles it.

use std::sync::Arc;
use std::sync::atomic::Ordering;

use strict_json::Value;
use team_core::future::BoxFuture;
use team_inference::brain::{BrainClient, BrainError, BrainOperation, BrainRequest};
use team_inference::testing::ScriptedBrain;

use super::harness::{
    BrainClientScripted, Harness, SESSION, call_on, chat_body, cloudflare, drained, harness,
    harness_with, reply, suspension,
};
use crate::chat::service::ChatService;
use crate::chat::tests::fakes::json;
use crate::http::route::Method;

const CHAT: &str = "/v1/teams/team_1/chat";

/// A Brain that never answers its `dies_on` operation: the Team process dies during that call.
struct Dying {
    scripted: Arc<ScriptedBrain>,
    dies_on: BrainOperation,
    died: Arc<std::sync::atomic::AtomicBool>,
}

impl BrainClient for Dying {
    fn call<'a>(&'a self, request: BrainRequest<'a>) -> BoxFuture<'a, Result<Value, BrainError>> {
        Box::pin(async move {
            if request.operation == self.dies_on {
                self.died.store(true, Ordering::SeqCst);
                return std::future::pending().await;
            }
            self.scripted.call(request).await
        })
    }
}

impl BrainClientScripted for Dying {
    fn client(self: Arc<Self>) -> Arc<dyn BrainClient> {
        self
    }

    fn scripted(self: Arc<Self>) -> Arc<ScriptedBrain> {
        Arc::clone(&self.scripted)
    }
}

fn dying(dies_on: BrainOperation) -> (Harness, Arc<std::sync::atomic::AtomicBool>) {
    let died = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let harness = harness_with(
        Arc::new(Dying {
            scripted: Arc::new(ScriptedBrain::default()),
            dies_on,
            died: Arc::clone(&died),
        }),
        vec![cloudflare()],
    );
    (harness, died)
}

/// Sends `body` once, leaving its turn running in this process until `reached` holds, where the process dies.
async fn sent(service: &ChatService, body: &Value, reached: impl Fn() -> bool) {
    let answer = call_on(service, Method::Post, CHAT, Some(body.clone()), SESSION).await;
    assert!(answer.is_ok(), "{answer:?}");
    for _ in 0..500 {
        if reached() {
            // The turn's last durable write before the death point completes.
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            return;
        }
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    }
    panic!("the turn never reached its death point");
}

async fn chat_on(service: &ChatService, body: &Value) -> super::harness::Streamed {
    drained(call_on(service, Method::Post, CHAT, Some(body.clone()), SESSION).await).await
}

fn settle(body: &Value) -> Value {
    let request = body.get("request").cloned().expect("request");
    Value::Object(vec![("request".to_owned(), request)])
}

#[tokio::test]
async fn a_repeated_send_receives_the_same_terminal_until_admin_settles_it_and_never_runs_again() {
    let harness = harness();
    harness.brain.answer(Ok(reply("Hello.")));
    let body = chat_body("Hello", &[], None);
    let first = harness.chat(body.clone()).await;
    assert_eq!((first.status, first.text("reply")), (200, Some("Hello.")));
    // Admin lost the answer: the same send receives the same terminal, and the Brain is not asked again.
    let again = harness.chat(body.clone()).await;
    assert_eq!(
        (again.status, again.body.clone()),
        (200, first.body.clone())
    );
    assert_eq!(harness.brain.received().len(), 1);
    // Another session neither receives it nor settles it.
    let other = "b".repeat(64);
    let refused = call_on(
        &harness.service,
        Method::Post,
        CHAT,
        Some(body.clone()),
        &other,
    )
    .await;
    assert_eq!(refused.expect_err("another session").status, 403);
    let delivered = "/v1/teams/team_1/chat/delivered";
    let refused = call_on(
        &harness.service,
        Method::Post,
        delivered,
        Some(settle(&body)),
        &other,
    )
    .await;
    assert_eq!(refused.expect_err("another session").status, 403);
    harness
        .json(Method::Post, delivered, Some(settle(&body)))
        .await
        .expect("delivered");
    let late = harness.call(Method::Post, CHAT, Some(body)).await;
    assert_eq!(late.expect_err("delivered").code, Some("turn-delivered"));
}

#[tokio::test]
async fn a_send_of_a_turn_still_running_attaches_to_it() {
    let (harness, _) = dying(BrainOperation::Resume);
    harness.brain.answer(Ok(suspension("interrupt-1")));
    let body = chat_body("List my zones", &["shimpz-cloudflare"], None);
    sent(&harness.service, &body, || {
        !harness.executor.invoked().is_empty()
    })
    .await;
    let attached = call_on(
        &harness.service,
        Method::Post,
        CHAT,
        Some(body.clone()),
        SESSION,
    )
    .await;
    assert!(matches!(
        attached,
        Ok(crate::http::service::Answer::Stream(_))
    ));
    // The same body under the same identity is one turn: another body is refused.
    let changed = chat_body("List my zones", &[], None);
    let Value::Object(mut members) = changed else {
        unreachable!()
    };
    for (name, value) in &mut members {
        if name == "request" {
            *value = body.get("request").cloned().expect("request");
        }
    }
    let refused = harness
        .call(Method::Post, CHAT, Some(Value::Object(members)))
        .await;
    assert_eq!(refused.expect_err("changed").code, Some("turn-changed"));
    assert_eq!(harness.executor.invoked(), ["interrupt-1"]);
}

#[tokio::test]
async fn a_team_that_died_during_the_brain_start_runs_the_turn_from_its_start() {
    let (harness, died) = dying(BrainOperation::Turn);
    let body = chat_body("Hello", &[], None);
    sent(&harness.service, &body, || died.load(Ordering::SeqCst)).await;
    let restarted = Arc::new(ScriptedBrain::default());
    restarted.answer(Ok(reply("Hello.")));
    let service = harness.after_death(Arc::clone(&restarted));
    let done = chat_on(&service, &body).await;
    assert_eq!((done.status, done.text("reply")), (200, Some("Hello.")));
    assert_eq!(restarted.received()[0].operation, BrainOperation::Turn);
}

#[tokio::test]
async fn a_team_that_died_resuming_the_brain_resumes_from_its_checkpoint_without_starting_again() {
    let (harness, _) = dying(BrainOperation::Resume);
    harness.brain.answer(Ok(suspension("interrupt-1")));
    harness.executor.complete(json(r#"{"zones":["a"]}"#));
    let body = chat_body("List my zones", &["shimpz-cloudflare"], None);
    sent(&harness.service, &body, || {
        !harness.executor.invoked().is_empty()
    })
    .await;
    assert_eq!(harness.executor.invoked(), ["interrupt-1"]);
    let restarted = Arc::new(ScriptedBrain::default());
    restarted.answer(Ok(reply("One zone.")));
    let service = harness.after_death(Arc::clone(&restarted));
    let done = chat_on(&service, &body).await;
    assert_eq!((done.status, done.text("reply")), (200, Some("One zone.")));
    // The turn resumed from its checkpoint: the Brain was only asked to resume, never to start the turn again. The
    // batch is replayed through the Action journal, whose receipts are still there (here, a fake that runs it).
    let operations: Vec<_> = restarted
        .received()
        .iter()
        .map(|call| call.operation)
        .collect();
    assert_eq!(operations, [BrainOperation::Resume]);
    let delivered = harness
        .executor
        .events()
        .iter()
        .filter(|event| event.starts_with("delivered"))
        .count();
    assert_eq!(delivered, 1);
}

#[tokio::test]
async fn a_team_that_died_after_the_brain_answered_delivers_the_reply_without_running_anything_again()
 {
    let harness = harness();
    harness.brain.answer(Ok(suspension("interrupt-1")));
    harness.brain.answer(Ok(reply("One zone.")));
    harness.executor.complete(json(r#"{"zones":["a"]}"#));
    harness
        .executor
        .dies_delivering
        .store(true, Ordering::SeqCst);
    let body = chat_body("List my zones", &["shimpz-cloudflare"], None);
    sent(&harness.service, &body, || {
        harness.brain.received().len() == 2
    })
    .await;
    harness
        .executor
        .dies_delivering
        .store(false, Ordering::SeqCst);
    let restarted = Arc::new(ScriptedBrain::default());
    let service = harness.after_death(Arc::clone(&restarted));
    let done = chat_on(&service, &body).await;
    assert_eq!(
        (done.status, done.text("reply")),
        (200, Some("One zone.")),
        "{:?}",
        done.body
    );
    // The checkpoint held the Brain's completed answer: no Brain call and no Action ran again.
    assert_eq!(restarted.received().len(), 0);
    assert_eq!(harness.executor.invoked(), ["interrupt-1"]);
}

#[tokio::test]
async fn a_revoked_turn_is_never_resumed_and_an_expired_one_is_refused() {
    let (harness, _) = dying(BrainOperation::Resume);
    harness.brain.answer(Ok(suspension("interrupt-1")));
    let body = chat_body("List my zones", &["shimpz-cloudflare"], None);
    sent(&harness.service, &body, || {
        !harness.executor.invoked().is_empty()
    })
    .await;
    let restarted = Arc::new(ScriptedBrain::default());
    let service = harness.after_death(Arc::clone(&restarted));
    // The session logged out while the Team was down: Admin revokes the turn, and a later send of it runs nothing.
    let revoked = "/v1/teams/team_1/chat/revoked";
    let settled = call_on(
        &service,
        Method::Post,
        revoked,
        Some(settle(&body)),
        SESSION,
    )
    .await;
    assert!(settled.is_ok(), "{settled:?}");
    let late = call_on(&service, Method::Post, CHAT, Some(body), SESSION).await;
    assert_eq!(late.expect_err("revoked").code, Some("turn-delivered"));
    assert_eq!(restarted.received().len(), 0);
    // A send past its grant is refused before anything runs.
    let stale = json(&format!(
        r#"{{"message":"Hello","files":[],"assistant_ids":[],"conversation":[],
        "request":{{"issued_at":1700000000,"nonce":"{}"}},"timezone":null,"locale":null,"refine":null}}"#,
        super::harness::nonce()
    ));
    let expired = call_on(&service, Method::Post, CHAT, Some(stale), SESSION).await;
    assert_eq!(
        expired.expect_err("expired").code,
        Some("authority-expired")
    );
}
