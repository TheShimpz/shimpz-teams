//! A turn's selected files (ADR-0093): the references it holds, its prepared attachments, and their outcomes.

use strict_json::Value;
use team_inference::brain::BrainError;

use super::harness::{Harness, TEAM, chat_body, harness, reply};
use crate::chat::fakes::json;
use crate::prepare::PrepareError;

fn put(harness: &Harness, name: &str) -> String {
    let upload = harness
        .parts
        .files
        .put(TEAM, name, name.as_bytes(), Some("text/plain"), false);
    upload.expect("stored").file.metadata.id
}

fn with_files(mut body: Value, files: &[&str]) -> Value {
    if let Value::Object(members) = &mut body {
        for member in members.iter_mut() {
            if member.0 == "files" {
                member.1 = Value::Array(
                    files
                        .iter()
                        .map(|id| Value::String((*id).to_owned()))
                        .collect(),
                );
            }
        }
    }
    body
}

fn referenced(harness: &Harness) -> Vec<String> {
    let path = harness.parts.files.root().join(TEAM).join("files.sqlite3");
    let connection = rusqlite::Connection::open(path).expect("opened");
    let mut statement = connection
        .prepare("SELECT id FROM files WHERE referenced=1 ORDER BY id")
        .expect("prepared");
    statement
        .query_map([], |row| row.get(0))
        .expect("queried")
        .collect::<rusqlite::Result<Vec<String>>>()
        .expect("read")
}

#[tokio::test]
async fn a_completed_turn_leaves_its_brain_thread_referencing_only_its_own_files() {
    let harness = harness();
    let (first, second) = (put(&harness, "a.txt"), put(&harness, "b.txt"));
    harness.brain.answer(Ok(reply("Read.")));
    let done = harness
        .chat(with_files(chat_body("Read it", &[], None), &[&first]))
        .await;
    assert_eq!(done.status, 200, "{:?}", done.body);
    assert_eq!(referenced(&harness), vec![first.clone()]);
    // The Brain received the file as its prepared attachment, and no Browser beside attachment content.
    let start = &harness.brain.received()[0].body;
    let attachments = start.get("attachments").cloned();
    assert!(matches!(&attachments, Some(Value::Array(items)) if items.len() == 1));
    assert_eq!(start.get("browser_available"), Some(&Value::Bool(false)));
    harness.brain.answer(Ok(reply("Read too.")));
    harness
        .chat(with_files(chat_body("And this", &[], None), &[&second]))
        .await;
    assert_eq!(referenced(&harness), vec![second]);
}

#[tokio::test]
async fn a_failed_turn_releases_only_the_files_it_newly_referenced() {
    let harness = harness();
    let (earlier, failing) = (put(&harness, "earlier.txt"), put(&harness, "failing.txt"));
    harness
        .parts
        .files
        .reference(TEAM, &[&earlier])
        .expect("referenced");
    harness.brain.answer(Err(BrainError::Unavailable));
    let failed = harness
        .chat(with_files(
            chat_body("Read", &[], None),
            &[&earlier, &failing],
        ))
        .await;
    assert_eq!(failed.code(), Some("brain-runtime-failed"));
    assert_eq!(referenced(&harness), vec![earlier]);
}

#[tokio::test]
async fn a_turn_naming_an_absent_file_fails_before_its_brain_start() {
    let harness = harness();
    let missing = "0123456789abcdef0123456789abcdef";
    let refused = harness
        .chat(with_files(chat_body("Read", &[], None), &[missing]))
        .await;
    assert_eq!(
        (refused.status, refused.code()),
        (404, Some("file-not-found"))
    );
    assert_eq!(harness.brain.received().len(), 0);
}

#[tokio::test]
async fn every_preparation_failure_has_an_explicit_public_outcome() {
    let cases = [
        (PrepareError::Unavailable, 503, "attachments-unavailable"),
        (PrepareError::Changed, 409, "team-context-changed"),
        (PrepareError::NotFound, 404, "file-not-found"),
        (PrepareError::Stopped, 409, "chat-stopped"),
    ];
    for (failure, status, code) in cases {
        let harness = harness();
        let file = put(&harness, "notes.txt");
        *harness.preparer.failure.lock().expect("failure") = Some(failure);
        let refused = harness
            .chat(with_files(chat_body("Read", &[], None), &[&file]))
            .await;
        assert_eq!(
            (refused.status, refused.code()),
            (status, Some(code)),
            "{failure:?}"
        );
        assert_eq!(harness.brain.received().len(), 0);
        // The failed turn kept no reference of its own.
        assert_eq!(referenced(&harness), Vec::<String>::new());
    }
}

#[tokio::test]
async fn while_attachment_content_is_in_the_turn_only_an_authorizing_action_is_declared() {
    let harness = harness();
    let file = put(&harness, "notes.txt");
    harness.brain.answer(Ok(reply("Ok.")));
    harness
        .chat(with_files(
            chat_body("Read", &["shimpz-cloudflare"], None),
            &[&file],
        ))
        .await;
    let assistants = harness.brain.received()[0].body.get("assistants").cloned();
    let declared = assistants.and_then(|list| match list {
        Value::Array(items) => items
            .first()
            .and_then(|assistant| assistant.get("actions"))
            .cloned(),
        _ => None,
    });
    // The Brain sees the Action with its authorization capability; the engine admits only authorizing ones.
    let authorization = declared.and_then(|actions| match actions {
        Value::Array(items) => items
            .first()
            .and_then(|action| action.get("authorization"))
            .cloned(),
        _ => None,
    });
    assert_eq!(authorization, Some(json("true")));
}
