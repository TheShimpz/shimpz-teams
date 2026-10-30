use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::PathBuf;
use std::sync::Arc;

use strict_json::{Number, Value};
use team_core::testing::CountingEntropy;

use super::{Outcome, RecordPolicy, RecordStore, State};
use crate::private::StateError;

struct Policy {
    records_per_assistant: usize,
    maximum_records: usize,
}

impl RecordPolicy for Policy {
    fn maximum_state_bytes(&self) -> usize {
        4096
    }

    fn records_per_assistant(&self) -> usize {
        self.records_per_assistant
    }

    fn maximum_records(&self) -> usize {
        self.maximum_records
    }

    fn team_id(&self, id: &str) -> bool {
        id.starts_with("team_")
    }

    fn assistant_id(&self, id: &str) -> bool {
        id.starts_with("assistant")
    }

    fn record_id(&self, id: &str) -> bool {
        id.starts_with("record")
    }

    fn record_generation(&self, record: &Value) -> Option<u64> {
        match record.get("generation") {
            Some(Value::Number(Number::Int(generation))) => u64::try_from(*generation).ok(),
            _ => None,
        }
    }
}

const POLICY: Policy = Policy {
    records_per_assistant: 2,
    maximum_records: 3,
};

struct Fixture {
    _root: tempfile::TempDir,
    state_path: PathBuf,
    store: RecordStore<Policy>,
}

fn fixture() -> Fixture {
    let root = tempfile::tempdir().expect("temporary directory");
    let state_path = root.path().join("state/state.json");
    let store = RecordStore::new(
        state_path.clone(),
        root.path().join("key/aes256.key"),
        POLICY,
        Arc::new(CountingEntropy::default()),
    )
    .expect("separate paths");
    Fixture {
        _root: root,
        state_path,
        store,
    }
}

fn record(generation: i64) -> Value {
    Value::Object(vec![(
        "generation".to_owned(),
        Value::Number(Number::Int(generation)),
    )])
}

fn put(
    store: &RecordStore<Policy>,
    team: &str,
    assistant: &str,
    id: &str,
) -> Result<u64, StateError> {
    store.update(|state| {
        let generation = state.advance_generation();
        let records = state.records_mut(team, assistant);
        records.retain(|(existing, _)| existing != id);
        records.push((
            id.to_owned(),
            record(i64::try_from(generation).expect("small")),
        ));
        Ok(Outcome::Changed(generation))
    })
}

fn ids(store: &RecordStore<Policy>, team: &str, assistant: &str) -> Vec<String> {
    store
        .read(|state| {
            Ok::<_, StateError>(
                state
                    .records(team, assistant)
                    .map(|records| records.iter().map(|(id, _)| id.clone()).collect())
                    .unwrap_or_default(),
            )
        })
        .expect("read")
}

#[test]
fn records_round_trip_as_canonical_owner_only_state() {
    let fixture = fixture();
    assert_eq!(
        ids(&fixture.store, "team_a", "assistant"),
        Vec::<String>::new()
    );
    assert!(!fixture.state_path.exists());
    assert_eq!(
        put(&fixture.store, "team_a", "assistant", "record-1"),
        Ok(1)
    );
    assert_eq!(
        put(&fixture.store, "team_a", "assistant", "record-2"),
        Ok(2)
    );
    assert_eq!(
        fs::read_to_string(&fixture.state_path).expect("read"),
        r#"{"last_generation":2,"schema":1,"teams":{"team_a":{"assistant":{"record-1":{"generation":1},"record-2":{"generation":2}}}}}"#
    );
    assert_eq!(
        fs::metadata(&fixture.state_path).expect("stat").mode() & 0o7777,
        0o600
    );
    assert_eq!(
        ids(&fixture.store, "team_a", "assistant"),
        ["record-1", "record-2"]
    );
}

#[test]
fn the_generation_survives_every_deletion() {
    let fixture = fixture();
    put(&fixture.store, "team_a", "assistant", "record-1").expect("written");
    assert_eq!(fixture.store.delete_team("team_a"), Ok(true));
    assert_eq!(fixture.store.delete_team("team_a"), Ok(false));
    assert_eq!(
        put(&fixture.store, "team_a", "assistant", "record-1"),
        Ok(2)
    );
    assert_eq!(fixture.store.delete_all(), Ok(true));
    assert_eq!(fixture.store.delete_all(), Ok(false));
    assert_eq!(
        fixture
            .store
            .read(|state| Ok::<_, StateError>(state.last_generation())),
        Ok(2)
    );
}

#[test]
fn deletions_prune_and_report_what_they_removed() {
    let fixture = fixture();
    put(&fixture.store, "team_a", "assistant", "record-1").expect("written");
    put(&fixture.store, "team_a", "assistant", "record-2").expect("written");
    put(&fixture.store, "team_a", "assistant-b", "record-1").expect("written");
    assert_eq!(
        fixture
            .store
            .retain_declared("team_a", "assistant", &["record-2", "record-9"]),
        Ok(true)
    );
    assert_eq!(
        fixture
            .store
            .retain_declared("team_a", "assistant", &["record-2"]),
        Ok(false)
    );
    assert_eq!(
        fixture.store.retain_declared("team_x", "assistant", &[]),
        Ok(false)
    );
    assert_eq!(ids(&fixture.store, "team_a", "assistant"), ["record-2"]);
    // Discarding an Assistant's last record removes its entry, and its Team's when that empties it.
    assert_eq!(
        fixture.store.retain_declared("team_a", "assistant", &[]),
        Ok(true)
    );
    assert_eq!(
        fixture.store.delete_assistant("team_a", "assistant"),
        Ok(false)
    );
    assert_eq!(
        fixture.store.delete_assistant("team_a", "assistant-b"),
        Ok(true)
    );
    assert_eq!(
        fs::read_to_string(&fixture.state_path).expect("read"),
        r#"{"last_generation":3,"schema":1,"teams":{}}"#
    );
}

#[test]
fn a_failed_or_unchanged_update_writes_nothing() {
    let fixture = fixture();
    let refused: Result<(), StateError> = fixture.store.update(|state| {
        state.records_mut("team_a", "assistant");
        Err(StateError::Unauthentic)
    });
    assert_eq!(refused, Err(StateError::Unauthentic));
    let unchanged: Result<(), StateError> = fixture.store.update(|state| {
        state.advance_generation();
        Ok(Outcome::Unchanged(()))
    });
    assert_eq!(unchanged, Ok(()));
    assert!(!fixture.state_path.exists());
    // A change the policy would refuse is never written.
    let invalid = fixture.store.update(|state| {
        state
            .records_mut("not-a-team", "assistant")
            .push(("record".to_owned(), record(0)));
        Ok::<_, StateError>(Outcome::Changed(()))
    });
    assert_eq!(invalid, Err(StateError::Malformed));
    assert!(!fixture.state_path.exists());
}

#[test]
fn a_replaced_state_file_is_read_again() {
    let fixture = fixture();
    put(&fixture.store, "team_a", "assistant", "record-1").expect("written");
    assert_eq!(ids(&fixture.store, "team_a", "assistant"), ["record-1"]);
    let other = fixture.state_path.with_file_name("next");
    fs::write(&other, r#"{"last_generation":5,"schema":1,"teams":{"team_b":{"assistant":{"record-5":{"generation":5}}}}}"#)
        .expect("written");
    fs::set_permissions(&other, fs::Permissions::from_mode(0o600)).expect("mode set");
    fs::rename(&other, &fixture.state_path).expect("replaced");
    assert_eq!(ids(&fixture.store, "team_b", "assistant"), ["record-5"]);
    fs::remove_file(&fixture.state_path).expect("removed");
    assert_eq!(
        ids(&fixture.store, "team_b", "assistant"),
        Vec::<String>::new()
    );
}

fn admit(text: &str) -> Result<State, StateError> {
    State::admit(strict_json::parse(text.as_bytes()).expect("JSON"), &POLICY)
}

#[test]
fn stored_state_is_admitted_whole_or_refused() {
    assert!(admit(r#"{"schema":1,"teams":{},"last_generation":0}"#).is_ok());
    for malformed in [
        r"[]",
        r#"{"schema":2,"teams":{},"last_generation":0}"#,
        r#"{"schema":1,"teams":{},"last_generation":-1}"#,
        r#"{"schema":1,"teams":{},"last_generation":1.0}"#,
        r#"{"schema":1,"teams":[],"last_generation":0}"#,
        r#"{"schema":1,"teams":{},"last_generation":0,"extra":1}"#,
        r#"{"schema":1,"teams":{"team_a":[]},"last_generation":0}"#,
        r#"{"schema":1,"teams":{"other":{}},"last_generation":0}"#,
        r#"{"schema":1,"teams":{"team_a":{"other":{}}},"last_generation":0}"#,
        r#"{"schema":1,"teams":{"team_a":{"assistant":[]}},"last_generation":0}"#,
        r#"{"schema":1,"teams":{"team_a":{"assistant":{"other":{"generation":0}}}},"last_generation":0}"#,
        r#"{"schema":1,"teams":{"team_a":{"assistant":{"record":{}}}},"last_generation":0}"#,
        // A record newer than the store-wide generation.
        r#"{"schema":1,"teams":{"team_a":{"assistant":{"record":{"generation":2}}}},"last_generation":1}"#,
        r#"{"schema":1,"teams":{"team_a":{"assistant":{"record1":{"generation":0},"record2":{"generation":0},"record3":{"generation":0}}}},"last_generation":0}"#,
    ] {
        assert_eq!(
            admit(malformed).map(|_| ()),
            Err(StateError::Malformed),
            "{malformed}"
        );
    }
    let over = r#"{"schema":1,"teams":{"team_a":{"assistant":{"record1":{"generation":0},"record2":{"generation":0}},"assistant-b":{"record1":{"generation":0},"record2":{"generation":0}}}},"last_generation":0}"#;
    assert_eq!(admit(over).map(|_| ()), Err(StateError::RecordLimit));
}

#[test]
fn unsafe_or_malformed_state_files_are_refused() {
    let fixture = fixture();
    put(&fixture.store, "team_a", "assistant", "record-1").expect("written");
    fs::set_permissions(&fixture.state_path, fs::Permissions::from_mode(0o644)).expect("mode set");
    assert_eq!(
        put(&fixture.store, "team_a", "assistant", "record-2"),
        Err(StateError::Unsafe)
    );
    fs::write(&fixture.state_path, b"{").expect("written");
    fs::set_permissions(&fixture.state_path, fs::Permissions::from_mode(0o600)).expect("mode set");
    assert_eq!(fixture.store.delete_all(), Err(StateError::Malformed));
    fs::write(&fixture.state_path, vec![b' '; 4097]).expect("written");
    assert_eq!(fixture.store.delete_all(), Err(StateError::Unsafe));
}

#[test]
fn the_keyring_is_created_beside_never_inside_the_state() {
    let fixture = fixture();
    assert_eq!(
        fixture.store.key(false).map(|_| ()),
        Err(StateError::Unavailable)
    );
    let created = fixture.store.key(true).expect("created");
    assert_eq!(
        fixture.store.key(false).expect("read").expose(),
        created.expose()
    );
    let root = tempfile::tempdir().expect("temporary directory");
    let shared = RecordStore::new(
        root.path().join("state.json"),
        root.path().join("aes256.key"),
        POLICY,
        Arc::new(CountingEntropy::default()),
    );
    assert_eq!(shared.map(|_| ()), Err(StateError::Layout));
}
