use std::sync::Arc;
use std::time::{Duration, UNIX_EPOCH};

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use strict_json::Value;
use team_assistant::spec::ActionSpec;
use team_core::stop::Stop;
use team_core::testing::{CountingEntropy, FakeClock};
use team_storage::files::FileStore;

use super::{
    FileSlot, FileSource, Stored, action_file, commitments, deliver, delivered, disclosure, grants,
    selected,
};
use crate::executor::{ActionFailure, ActionFile};
use crate::human::ActionTranscript;

const DATA: &[u8] = b"%PDF-1.4 quarterly report";

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

pub(crate) fn upload() -> ActionSpec {
    ActionSpec::of(&json(
        r#"{"id":"upload","input_schema":{"type":"object","properties":{"document":{"type":"string","minLength":32,"maxLength":32,"pattern":"^[0-9a-f]{32}$"}},"required":["document"],"additionalProperties":false},"output_schema":{"type":"object"},"human_requests":["approval"],"input_files":["document"],"effect":"mutating"}"#,
    ))
}

pub(crate) fn wire(id: &str) -> Value {
    json(&format!(
        r#"{{"id":"{id}","name":"Relatório de março.pdf","media_type":"application/pdf","size":{},"sha256":"{}","content":{{"type":"opaque","reason":"unsupported"}}}}"#,
        DATA.len(),
        super::hex(&<sha2::Sha256 as sha2::Digest>::digest(DATA)),
    ))
}

pub(crate) fn approved(interrupt: &str) -> ActionTranscript {
    ActionTranscript::new(interrupt)
        .append(&crate::challenges::tests::approval(), &Value::Bool(true))
        .expect("approved")
}

pub(crate) fn store(root: &std::path::Path) -> (FileStore, String) {
    let clock = Arc::new(FakeClock::at(UNIX_EPOCH + Duration::from_secs(1_000_000)));
    let store = FileStore::new(
        root.join("teams"),
        64 * 1024 * 1024,
        None,
        clock,
        Arc::new(CountingEntropy::default()),
    )
    .expect("store");
    let upload = store
        .put(
            "team_1",
            "Relatório de março.pdf",
            DATA,
            Some("application/pdf"),
            false,
        )
        .expect("stored");
    let id = upload.file.metadata.id;
    (store, id)
}

fn input(id: &str) -> Value {
    json(&format!(r#"{{"document":"{id}"}}"#))
}

fn file(id: &str) -> ActionFile {
    selected(&[wire(id)]).remove(0)
}

#[test]
fn only_a_selected_deliverable_file_reaches_a_file_taking_action() {
    let id = "0123456789abcdef0123456789abcdef";
    let names = upload().input_files;
    let chosen = selected(&[wire(id)]);
    assert_eq!(
        action_file(&names, &input(id), &chosen),
        Ok(Some(&chosen[0]))
    );
    assert_eq!(action_file(&[], &input(id), &chosen), Ok(None));
    let other = "f".repeat(32);
    assert!(action_file(&names, &input(&other), &chosen).is_err());
    assert!(action_file(&names, &json("{}"), &chosen).is_err());
    let empty = ActionFile {
        size: 0,
        ..file(id)
    };
    let dotted = ActionFile {
        name: "../x".to_owned(),
        ..file(id)
    };
    let upper = ActionFile {
        media_type: "PDF".to_owned(),
        ..file(id)
    };
    for changed in [empty, dotted, upper] {
        assert!(action_file(&names, &input(id), &[changed]).is_err());
    }
}

#[test]
fn file_commitments_are_always_in_the_operation_fingerprint() {
    let id = "0123456789abcdef0123456789abcdef";
    assert_eq!(commitments(None), json("[]"));
    let committed = commitments(Some(&file(id)));
    assert_eq!(
        committed,
        Value::Array(vec![file(id).metadata()]),
        "the closed metadata, never the content"
    );
}

#[test]
fn only_an_authorization_request_discloses_the_file() {
    let id = "0123456789abcdef0123456789abcdef";
    let (names, chosen) = (upload().input_files, selected(&[wire(id)]));
    let disclosed = disclosure(&names, &input(id), &chosen, "approval").expect("disclosed");
    assert_eq!(disclosed, Some(file(id).metadata()));
    assert_eq!(
        disclosure(&names, &input(id), &chosen, "input:text"),
        Ok(None)
    );
    assert_eq!(disclosure(&[], &input(id), &chosen, "approval"), Ok(None));
}

#[test]
fn the_first_invocation_receives_metadata_only_and_never_reads_the_bytes() {
    let id = "0123456789abcdef0123456789abcdef";
    let (action, chosen) = (upload(), file(id));
    let transcript = ActionTranscript::new("interrupt-1");
    assert!(!grants(Some(&chosen), &action, &transcript));
    let files = deliver(&action, Some(&chosen), &transcript, &input(id), None).expect("files");
    let record = files.get(id).expect("record");
    assert_eq!(record.get("content"), Some(&json(r#"{"type":"withheld"}"#)));
    assert_eq!(record.get("id"), None);
    assert_eq!(delivered(&files), None);
}

#[test]
fn only_the_approved_replay_receives_the_verified_original_bytes() {
    let root = tempfile::tempdir().expect("temporary directory");
    let (source, id) = store(root.path());
    let (action, chosen) = (upload(), file(&id));
    let transcript = approved("interrupt-1");
    assert!(grants(Some(&chosen), &action, &transcript));
    let stored = source.read("team_1", &id).expect("read");
    let files = deliver(
        &action,
        Some(&chosen),
        &transcript,
        &input(&id),
        Some(&stored),
    )
    .expect("delivered");
    let content = files.get(&id).and_then(|record| record.get("content"));
    let text = content.and_then(|content| content.get("base64"));
    let Some(Value::String(text)) = text else {
        panic!("delivered content")
    };
    assert_eq!(STANDARD.decode(text).expect("base64"), DATA);
    let size = i64::try_from(DATA.len()).expect("size");
    assert_eq!(delivered(&files), Some((id, size)));
}

#[test]
fn changed_collected_or_unselected_bytes_are_never_delivered() {
    let root = tempfile::tempdir().expect("temporary directory");
    let (source, id) = store(root.path());
    let (action, chosen, transcript) = (upload(), file(&id), approved("interrupt-1"));
    let exact = source.read("team_1", &id).expect("read");
    let changed = Stored {
        data: vec![b'x'; DATA.len()],
        ..exact.clone()
    };
    let resized = Stored {
        size: exact.size + 1,
        ..exact.clone()
    };
    for stored in [changed, resized] {
        let refused = deliver(
            &action,
            Some(&chosen),
            &transcript,
            &input(&id),
            Some(&stored),
        );
        assert!(refused.is_err());
    }
    assert!(source.read("team_1", &"e".repeat(32)).is_err());
    // A file-taking Action outside a turn that selected its file never runs, even before authorization.
    let unbound = ActionTranscript::new("interrupt-1");
    assert!(deliver(&action, None, &unbound, &input(&id), None).is_err());
    // Withheld content after the Action's own authorization is refused too: bytes or nothing.
    assert!(deliver(&action, Some(&chosen), &transcript, &input(&id), None).is_err());
    let ordinary = ActionSpec::of(&json(r#"{"id":"list","input_schema":{"type":"object"}}"#));
    let none = deliver(&ordinary, None, &transcript, &json("{}"), None);
    assert_eq!(none, Ok(json("{}")));
}

#[test]
fn only_the_actions_own_declared_authorization_grants_the_bytes() {
    let id = "0123456789abcdef0123456789abcdef";
    let chosen = file(id);
    let declaring = |kinds: &str| {
        ActionSpec::of(&json(&format!(
            r#"{{"id":"a","input_schema":{{"type":"object"}},"human_requests":{kinds}}}"#
        )))
    };
    let approved = approved("i");
    assert!(grants(
        Some(&chosen),
        &declaring(r#"["approval"]"#),
        &approved
    ));
    assert!(!grants(
        Some(&chosen),
        &declaring(r#"["auth:password"]"#),
        &approved
    ));
    let fresh = ActionTranscript::new("i");
    assert!(!grants(
        Some(&chosen),
        &declaring(r#"["approval"]"#),
        &fresh
    ));
    let two = declaring(r#"["approval","auth:totp"]"#);
    assert!(!grants(Some(&chosen), &two, &approved));
    assert!(!grants(None, &declaring(r#"["approval"]"#), &approved));
}

#[tokio::test(start_paused = true)]
async fn only_a_delivery_holds_the_slot_and_one_shared_deadline() {
    let slot = FileSlot::default();
    let stop = Stop::new();
    assert!(slot.admit(false, &stop).await.expect("admitted").is_none());
    assert!(slot.free());
    let started = tokio::time::Instant::now();
    let held = slot
        .admit(true, &stop)
        .await
        .expect("admitted")
        .expect("held");
    assert!(!slot.free());
    assert_eq!(held.deadline, started + super::FILE_RPC_TIMEOUT);
    drop(held);
    assert!(slot.free());
}

#[tokio::test(start_paused = true)]
async fn a_waiting_delivery_is_refused_at_its_deadline_or_at_stop() {
    let slot = Arc::new(FileSlot::default());
    let stop = Stop::new();
    let held = slot.admit(true, &stop).await.expect("admitted");
    let started = tokio::time::Instant::now();
    assert_eq!(
        slot.admit(true, &stop).await.map(|held| held.is_some()),
        Err(ActionFailure::FileBusy)
    );
    assert_eq!(started.elapsed(), super::FILE_RPC_TIMEOUT);
    let stopped = Stop::new();
    let waiting = {
        let (slot, stopped) = (Arc::clone(&slot), stopped.clone());
        tokio::spawn(async move { slot.admit(true, &stopped).await.map(|held| held.is_some()) })
    };
    tokio::time::sleep(Duration::from_secs(1)).await;
    stopped.stop();
    assert_eq!(waiting.await.expect("joined"), Err(ActionFailure::Stopped));
    drop(held);
    assert!(slot.free());
}

#[tokio::test]
async fn a_stop_that_wins_while_the_slot_frees_is_refused_and_releases_the_slot() {
    let slot = FileSlot::default();
    let stop = Stop::new();
    stop.stop();
    // The slot is free and Stop already won: nothing is held.
    assert_eq!(
        slot.admit(true, &stop).await.map(|held| held.is_some()),
        Err(ActionFailure::Stopped)
    );
    assert!(slot.free());
}
