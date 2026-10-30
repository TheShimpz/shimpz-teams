//! A turn that pauses for a person or an Integration, and every way the pause ends.

use strict_json::Value;
use team_action::executor::InvokeOutcome;

use super::harness::{Harness, TEAM, chat_body, harness, reply, suspension};
use crate::chat::tests::fakes::{human_request, json};
use crate::http::route::Method;

/// Starts a turn whose one Action asks a person for an approval, and returns the paused challenge.
async fn paused(harness: &Harness, locale: Option<&str>) -> super::harness::Streamed {
    harness.brain.answer(Ok(suspension("interrupt-1")));
    harness.executor.answer(Box::new(|_, _| {
        Ok(InvokeOutcome::HumanRequest(human_request("approval", 0)))
    }));
    let paused = harness
        .chat(chat_body("List my zones", &["shimpz-cloudflare"], locale))
        .await;
    assert_eq!(paused.status, 428, "{:?}", paused.body);
    paused
}

#[tokio::test]
async fn a_human_pause_is_kept_encrypted_and_answers_the_pending_read() {
    let harness = harness();
    let pause = paused(&harness, None).await;
    assert_eq!(pause.text("status"), Some("human-required"));
    let shown =
        r#"{"fields":[{"name":"zone","value":"\"example.com\"","truncated":false}],"omitted":0}"#;
    assert_eq!(pause.body.get("input"), Some(&json(shown)));
    let challenge = pause.text("challenge_id").expect("challenge").to_owned();
    assert_eq!(harness.brain.received().len(), 1);
    let kept = harness.continuations.current(TEAM).expect("read");
    assert_eq!(kept.map(|item| item.kind), Some("human".to_owned()));
    let pending = harness
        .json(Method::Get, "/v1/teams/team_1/chat/human", None)
        .await;
    let pending = pending.expect("pending");
    assert_eq!(pending.get("challenge_id"), Some(&Value::String(challenge)));
}

#[tokio::test]
async fn an_approval_replays_the_same_action_before_the_brain_resumes() {
    let harness = harness();
    let pause = paused(&harness, None).await;
    let challenge = pause.text("challenge_id").expect("challenge").to_owned();
    harness.brain.answer(Ok(reply("Two zones.")));
    harness.executor.complete(json(r#"{"zones":["a"]}"#));
    let done = harness.answer(&challenge, "submit").await;
    assert_eq!((done.status, done.text("reply")), (200, Some("Two zones.")));
    assert_eq!(
        harness.executor.invoked(),
        vec!["interrupt-1", "interrupt-1"]
    );
    let kept = harness.transcripts.kept.lock().expect("kept").len();
    assert_eq!(kept, 0, "a completed turn leaves no replay transcript");
    assert_eq!(harness.continuations.current(TEAM).expect("read"), None);
    // The answered challenge is consumed: answering it again is refused.
    let again = harness.answer(&challenge, "submit").await;
    assert_eq!(
        (again.status, again.code()),
        (409, Some("human-request-expired"))
    );
}

#[tokio::test]
async fn a_denial_ends_the_turn_and_its_batch_without_a_brain_resume() {
    let harness = harness();
    let pause = paused(&harness, None).await;
    let challenge = pause.text("challenge_id").expect("challenge").to_owned();
    let denied = harness.answer(&challenge, "deny").await;
    assert_eq!(
        (denied.status, denied.text("status"), denied.text("reason")),
        (200, Some("human-denied"), Some("denied"))
    );
    assert_eq!(harness.brain.received().len(), 1);
    assert_eq!(
        harness.executor.events().last().map(String::as_str),
        Some("terminate")
    );
    assert_eq!(harness.continuations.current(TEAM).expect("read"), None);
    let pending = harness
        .json(Method::Get, "/v1/teams/team_1/chat/human", None)
        .await
        .expect("pending");
    assert_eq!(pending.get("status"), Some(&json("\"none\"")));
}

#[tokio::test]
async fn an_invalid_answer_is_refused_and_leaves_the_challenge_answerable() {
    let harness = harness();
    let pause = paused(&harness, None).await;
    let challenge = pause.text("challenge_id").expect("challenge").to_owned();
    let body = json(&format!(
        r#"{{"challenge_id":"{challenge}","decision":"submit","value":false}}"#
    ));
    let refused = harness
        .stream(Method::Post, "/v1/teams/team_1/chat/human", body)
        .await;
    assert_eq!(
        (refused.status, refused.code()),
        (422, Some("invalid-human-response"))
    );
    for body in [
        r#"{"decision":"maybe"}"#,
        r#"{"challenge_id":"x","decision":"deny","value":1}"#,
    ] {
        let refused = harness
            .call(
                Method::Post,
                "/v1/teams/team_1/chat/human",
                Some(json(body)),
            )
            .await;
        assert_eq!(
            refused.map(|_| ()).map_err(|problem| problem.code),
            Err(Some("invalid-body"))
        );
    }
    harness.brain.answer(Ok(reply("Done.")));
    assert_eq!(harness.answer(&challenge, "submit").await.status, 200);
}

#[tokio::test]
async fn stop_withdraws_a_paused_turn_its_continuation_and_its_batch() {
    let harness = harness();
    let pause = paused(&harness, None).await;
    let challenge = pause.text("challenge_id").expect("challenge").to_owned();
    let stopped = harness
        .json(Method::Post, "/v1/teams/team_1/chat/stop", Some(json("{}")))
        .await
        .expect("stop");
    assert_eq!(stopped.get("accepted"), Some(&Value::Bool(true)));
    assert_eq!(harness.continuations.current(TEAM).expect("read"), None);
    assert_eq!(
        harness.executor.events().last().map(String::as_str),
        Some("terminate")
    );
    let late = harness.answer(&challenge, "submit").await;
    assert_eq!(
        (late.status, late.code()),
        (409, Some("human-request-expired"))
    );
}

#[tokio::test]
async fn a_new_message_meets_the_pending_challenge_in_its_interface_language() {
    let harness = harness();
    let pause = paused(&harness, Some("pt")).await;
    // The paused turn's language renders the copy; another language reissues a fresh challenge of the same request.
    assert_eq!(pause.text("locale"), Some("pt"));
    let rendered = pause
        .body
        .get("rendered")
        .and_then(|rendered| rendered.get("title"))
        .cloned();
    assert_eq!(rendered, Some(json("\"PT List zones\"")));
    let first = pause.text("challenge_id").expect("challenge").to_owned();
    let again = harness.chat(chat_body("And now?", &[], Some("en"))).await;
    assert_eq!((again.status, again.text("locale")), (428, Some("en")));
    let reissued = again.text("challenge_id").expect("challenge").to_owned();
    assert_ne!(reissued, first);
    let opened = harness
        .json(
            Method::Post,
            "/v1/teams/team_1/chat/human/challenge",
            Some(json(r#"{"locale":"en"}"#)),
        )
        .await
        .expect("opened");
    assert_eq!(
        opened.get("challenge_id"),
        Some(&Value::String(reissued.clone()))
    );
    let stale = harness.answer(&first, "submit").await;
    assert_eq!(stale.code(), Some("human-request-expired"));
    harness.brain.answer(Ok(reply("Ok.")));
    assert_eq!(harness.answer(&reissued, "submit").await.status, 200);
}

#[tokio::test]
async fn a_paused_turn_whose_assistant_changed_is_ended_when_it_is_answered() {
    let harness = harness();
    let pause = paused(&harness, None).await;
    let challenge = pause.text("challenge_id").expect("challenge").to_owned();
    harness.team.change(TEAM, |team| {
        team.assistants[0].binding.container = "d".repeat(64);
    });
    let changed = harness.answer(&challenge, "submit").await;
    assert_eq!(
        (changed.status, changed.code()),
        (409, Some("team-context-changed"))
    );
    assert_eq!(harness.continuations.current(TEAM).expect("read"), None);
    let pending = harness
        .json(Method::Get, "/v1/teams/team_1/chat/human", None)
        .await
        .expect("pending");
    assert_eq!(pending.get("status"), Some(&json("\"none\"")));
}
