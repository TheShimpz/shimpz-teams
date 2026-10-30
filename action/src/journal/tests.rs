use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::PathBuf;
use std::sync::Arc;

use rusqlite::Connection;
use strict_json::Value;
use team_core::testing::CountingEntropy;

use super::{Batch, Journal, JournalError, Limits, Operation, sha256_hex, valid_operation_id};

mod archive;
mod lifecycle;

pub(crate) struct Fixture {
    pub root: tempfile::TempDir,
    pub path: PathBuf,
}

pub(crate) fn fixture() -> Fixture {
    let root = tempfile::tempdir().expect("temporary directory");
    let path = root.path().join("private/journal.sqlite3");
    Fixture { root, path }
}

impl Fixture {
    pub(crate) fn open(&self) -> Journal {
        self.limited(Limits::default())
    }

    pub(crate) fn limited(&self, limits: Limits) -> Journal {
        Journal::open(&self.path, limits, Arc::new(CountingEntropy::default())).expect("opened")
    }

    pub(crate) fn rows(&self, statement: &str) -> Vec<Vec<Value>> {
        let connection = Connection::open(&self.path).expect("opened");
        let mut statement = connection.prepare(statement).expect("prepared");
        let columns = statement.column_count();
        statement
            .query_map([], |row| {
                (0..columns)
                    .map(|index| {
                        Ok(match row.get_ref(index)? {
                            rusqlite::types::ValueRef::Null
                            | rusqlite::types::ValueRef::Real(_) => Value::Null,
                            rusqlite::types::ValueRef::Integer(value) => {
                                Value::Number(strict_json::Number::Int(value))
                            }
                            rusqlite::types::ValueRef::Text(text)
                            | rusqlite::types::ValueRef::Blob(text) => {
                                Value::String(String::from_utf8_lossy(text).into_owned())
                            }
                        })
                    })
                    .collect()
            })
            .and_then(Iterator::collect)
            .expect("rows")
    }
}

pub(crate) fn operation(interrupt: &str, input: &str) -> Operation {
    Operation {
        interrupt_id: interrupt.to_owned(),
        fingerprint: sha256_hex(input.as_bytes()),
        operation_id: None,
    }
}

pub(crate) fn first() -> Operation {
    operation("interrupt-1", "validated-input-1")
}

pub(crate) fn second() -> Operation {
    operation("interrupt-2", "validated-input-2")
}

pub(crate) fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

pub(crate) fn prepare(journal: &Journal, generation: &str, operations: &[Operation]) -> Batch {
    journal
        .prepare_batch(generation, "thread-1", operations.to_vec(), false)
        .expect("prepared")
}

pub(crate) fn completed(journal: &Journal, batch: &Batch, operation: &Operation, result: &str) {
    assert!(journal.begin(batch, operation).expect("began").execute);
    journal
        .complete(batch, operation, &json(result))
        .expect("completed");
}

#[test]
fn the_batch_fingerprint_hashes_its_canonical_identity() {
    let fixture = fixture();
    let batch = prepare(&fixture.open(), "generation-1", &[first()]);
    let thread = sha256_hex(b"thread-1");
    let identity = format!(
        r#"{{"generation":"generation-1","operations":[["interrupt-1","{}"]],"thread":"{thread}"}}"#,
        first().fingerprint
    );
    assert_eq!(batch.fingerprint, sha256_hex(identity.as_bytes()));
}

#[test]
fn completed_replays() {
    let fixture = fixture();
    let journal = fixture.open();
    let batch = prepare(&journal, "generation-1", &[first()]);
    let started = journal.begin(&batch, &first()).expect("began");
    assert!(
        started.execute && started.result.is_none() && valid_operation_id(&started.operation_id)
    );
    journal
        .complete(&batch, &first(), &json(r#"{"z":[2,1],"a":"ok"}"#))
        .expect("completed");
    journal.close();
    let reopened = fixture.open();
    assert_eq!(prepare(&reopened, "generation-1", &[first()]), batch);
    let replay = reopened.begin(&batch, &first()).expect("replayed");
    assert_eq!(
        (replay.execute, replay.result, replay.operation_id),
        (
            false,
            Some(json(r#"{"a":"ok","z":[2,1]}"#)),
            started.operation_id
        )
    );
    let modes = [fixture.path.parent().expect("parent"), &fixture.path]
        .map(|path| fs::metadata(path).expect("stat").mode() & 0o7777);
    assert_eq!(modes, [0o700, 0o600]);
}

#[test]
fn journal_executing_is_uncertain_never_reexecuted() {
    let fixture = fixture();
    let journal = fixture.open();
    let batch = prepare(&journal, "generation-1", &[first()]);
    assert!(journal.begin(&batch, &first()).expect("began").execute);
    journal.close();
    let reopened = fixture.open();
    let same = prepare(&reopened, "generation-1", &[first()]);
    assert_eq!(
        reopened.begin(&same, &first()),
        Err(JournalError::Uncertain)
    );
    assert_eq!(reopened.end(&same), Ok(false));
    assert_eq!(
        reopened.uncertain_fingerprint("generation-1"),
        Ok(Some(same.fingerprint))
    );
}

#[test]
fn a_proven_suspension_returns_only_the_executing_operation_to_prepared() {
    let fixture = fixture();
    let journal = fixture.open();
    let batch = prepare(&journal, "generation-1", &[first(), second()]);
    completed(&journal, &batch, &first(), r#"{"answer":1}"#);
    let attempt = journal.begin(&batch, &second()).expect("began");
    journal.suspend(&batch, &second()).expect("suspended");
    assert_eq!(
        journal.begin(&batch, &first()).expect("replayed").result,
        Some(json(r#"{"answer":1}"#))
    );
    let replay = journal.begin(&batch, &second()).expect("began again");
    assert_eq!(
        (replay.execute, replay.operation_id),
        (true, attempt.operation_id)
    );
    assert!(matches!(
        journal.suspend(&batch, &first()),
        Err(JournalError::Conflict(_))
    ));
    let attempts =
        fixture.rows("SELECT attempts FROM operations WHERE interrupt_id = 'interrupt-2'");
    assert_eq!(attempts, [[Value::Number(strict_json::Number::Int(2))]]);
}

#[test]
fn an_acknowledged_transition_survives_an_unclosed_process() {
    for (phase, expected) in [
        ("prepared", "prepared"),
        ("executing", "executing"),
        ("completed", "completed"),
    ] {
        let fixture = fixture();
        let journal = fixture.open();
        let batch = prepare(&journal, "generation-1", &[first()]);
        if phase != "prepared" {
            journal.begin(&batch, &first()).expect("began");
        }
        if phase == "completed" {
            journal
                .complete(&batch, &first(), &json(r#"{"answer":1}"#))
                .expect("completed");
        }
        // Dying without closing: the process ends with the connection and its WAL still open.
        std::mem::forget(journal);
        let state = fixture.rows("SELECT state FROM operations WHERE generation = 'generation-1'");
        assert_eq!(state, [[Value::String(expected.to_owned())]], "{phase}");
    }
}

#[test]
fn the_journal_uses_its_bounded_wal_full_durability_policy() {
    let fixture = fixture();
    let journal = fixture.open();
    let mode: String = journal
        .with(|connection| {
            Ok(connection
                .query_row("PRAGMA journal_mode", [], |row| row.get(0))
                .expect("mode"))
        })
        .expect("read");
    let synchronous: i64 = journal
        .with(|connection| {
            Ok(connection
                .query_row("PRAGMA synchronous", [], |row| row.get(0))
                .expect("sync"))
        })
        .expect("read");
    assert_eq!((mode.as_str(), synchronous), ("wal", 2));
    let header = fixture.rows("PRAGMA application_id");
    assert_eq!(
        header,
        [[Value::Number(strict_json::Number::Int(
            super::APPLICATION_ID
        ))]]
    );
}

#[test]
fn a_changed_pending_batch_or_completed_result_fails_closed() {
    let fixture = fixture();
    let journal = fixture.open();
    let batch = prepare(&journal, "generation-1", &[first()]);
    let other = journal.prepare_batch("generation-1", "thread-1", vec![second()], false);
    assert!(matches!(other, Err(JournalError::Conflict(_))));
    let thread = journal.prepare_batch("generation-1", "other-thread", vec![first()], false);
    assert!(matches!(thread, Err(JournalError::Conflict(_))));
    completed(&journal, &batch, &first(), r#"{"answer":1}"#);
    journal
        .complete(&batch, &first(), &json(r#"{"answer":1}"#))
        .expect("same result");
    let changed = journal.complete(&batch, &first(), &json(r#"{"answer":2}"#));
    assert_eq!(
        changed,
        Err(JournalError::Conflict(
            "Action result changed after completion"
        ))
    );
}

#[test]
fn a_point_transition_revalidates_the_persisted_batch_header() {
    let fixture = fixture();
    let journal = fixture.open();
    let batch = prepare(&journal, "generation-1", &[first()]);
    journal.begin(&batch, &first()).expect("began");
    journal
        .with(|connection| {
            connection
                .execute(
                    "UPDATE batches SET fingerprint = ? WHERE generation = ?",
                    [&"f".repeat(64), &batch.generation],
                )
                .expect("tampered");
            Ok(())
        })
        .expect("written");
    assert!(matches!(
        journal.complete(&batch, &first(), &json("{}")),
        Err(JournalError::Conflict(_))
    ));
}

#[test]
fn delivery_requires_every_result_then_admits_the_next_batch() {
    let fixture = fixture();
    let journal = fixture.limited(Limits {
        max_generations: 1,
        ..Limits::default()
    });
    let batch = prepare(&journal, "generation-1", &[first(), second()]);
    completed(&journal, &batch, &first(), r#"{"first":true}"#);
    assert!(matches!(
        journal.delivered(&batch),
        Err(JournalError::Conflict(_))
    ));
    completed(&journal, &batch, &second(), r#"{"second":true}"#);
    journal.delivered(&batch).expect("delivered");
    journal.delivered(&batch).expect("already delivered");
    let next = prepare(
        &journal,
        "generation-1",
        &[operation("interrupt-3", "input-3")],
    );
    assert_ne!(next.fingerprint, batch.fingerprint);
    assert!(matches!(
        journal.delivered(&batch),
        Err(JournalError::Conflict(_))
    ));
    assert!(matches!(
        journal.begin(&batch, &first()),
        Err(JournalError::Conflict(_))
    ));
    completed(&journal, &next, &next.operations[0], r#"{"third":true}"#);
    journal.delivered(&next).expect("delivered");
    prepare(&journal, "generation-2", &[first()]);
}

#[test]
fn a_noncanonical_stored_result_or_database_fails_closed() {
    let fixture = fixture();
    let journal = fixture.open();
    let batch = prepare(&journal, "generation-1", &[first()]);
    completed(&journal, &batch, &first(), r#"{"answer":1}"#);
    journal.close();
    let connection = Connection::open(&fixture.path).expect("opened");
    connection
        .execute(
            "UPDATE operations SET result = ? WHERE interrupt_id = 'interrupt-1'",
            [b"{\"answer\": 1}".to_vec()],
        )
        .expect("tampered");
    drop(connection);
    let reopened = fixture.open();
    assert_eq!(
        reopened.begin(&batch, &first()),
        Err(JournalError::Corrupt("cached Action result is invalid"))
    );
    assert!(matches!(
        reopened.delivered(&batch),
        Err(JournalError::Corrupt(_))
    ));
    reopened.close();
    for name in ["journal.sqlite3-wal", "journal.sqlite3-shm"] {
        let _ = fs::remove_file(fixture.path.with_file_name(name));
    }
    fs::write(&fixture.path, b"not-a-sqlite-database").expect("written");
    let opened = Journal::open(
        &fixture.path,
        Limits::default(),
        Arc::new(CountingEntropy::default()),
    );
    assert!(matches!(opened, Err(JournalError::Corrupt(_))));
}

#[test]
fn capacity_and_result_limits_hold_without_raw_inputs() {
    let fixture = fixture();
    let journal = fixture.limited(Limits {
        max_generations: 1,
        max_operations: 1,
        max_result_bytes: 16,
        ..Limits::default()
    });
    let batch = journal
        .prepare_batch("generation-1", "thread-secret", vec![first()], false)
        .expect("prepared");
    let capacity = journal.prepare_batch("generation-2", "thread-2", vec![second()], false);
    assert_eq!(
        capacity,
        Err(JournalError::Conflict(
            "Action journal generation capacity is exhausted"
        ))
    );
    let count = journal.prepare_batch(
        "generation-1",
        "thread-secret",
        vec![first(), second()],
        false,
    );
    assert!(matches!(count, Err(JournalError::Conflict(_))));
    journal.begin(&batch, &first()).expect("began");
    let large = journal.complete(
        &batch,
        &first(),
        &json(r#"{"secret":"raw-input-must-not-fit"}"#),
    );
    assert!(matches!(large, Err(JournalError::Conflict(_))));
    journal.close();
    let raw = fs::read(&fixture.path).expect("read");
    let wal = fs::read(fixture.path.with_file_name("journal.sqlite3-wal")).unwrap_or_default();
    let leaked = |needle: &[u8]| {
        [&raw, &wal]
            .iter()
            .any(|bytes| bytes.windows(needle.len()).any(|window| window == needle))
    };
    assert!(!leaked(b"thread-secret") && !leaked(b"validated-input-1"));
}

#[test]
fn unsafe_files_links_and_owners_are_refused() {
    let fixture = fixture();
    let open = || {
        Journal::open(
            &fixture.path,
            Limits::default(),
            Arc::new(CountingEntropy::default()),
        )
    };
    fs::create_dir_all(fixture.path.parent().expect("parent")).expect("created");
    fs::write(&fixture.path, b"").expect("written");
    fs::set_permissions(&fixture.path, fs::Permissions::from_mode(0o644)).expect("mode");
    assert!(matches!(open(), Err(JournalError::Corrupt(_))));
    fs::remove_file(&fixture.path).expect("removed");
    let victim = fixture.root.path().join("victim");
    fs::write(&victim, b"unchanged").expect("written");
    std::os::unix::fs::symlink(&victim, &fixture.path).expect("linked");
    assert!(matches!(open(), Err(JournalError::Corrupt(_))));
    assert_eq!(fs::read(&victim).expect("read"), b"unchanged");
    fs::remove_file(&fixture.path).expect("removed");
    fixture.open().close();
    fs::hard_link(&fixture.path, fixture.path.with_file_name("copy.sqlite3")).expect("linked");
    assert!(matches!(open(), Err(JournalError::Corrupt(_))));
}

#[test]
fn a_schema_version_drift_is_refused() {
    let fixture = fixture();
    let journal = fixture.open();
    journal
        .with(|connection| {
            connection
                .execute_batch("PRAGMA user_version = 99")
                .expect("set");
            Ok(())
        })
        .expect("written");
    journal.close();
    let opened = Journal::open(
        &fixture.path,
        Limits::default(),
        Arc::new(CountingEntropy::default()),
    );
    assert_eq!(
        opened.err(),
        Some(JournalError::Corrupt(
            "Action journal schema or contents are invalid"
        ))
    );
    let closed = super::tests::fixture().open();
    closed.close();
    assert_eq!(
        closed.current_batch("generation-1"),
        Err(JournalError::Failure("Action journal is closed"))
    );
}
