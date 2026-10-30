use rusqlite::Connection;
use strict_json::{Number, Value};

use super::super::{
    Batch, Journal, JournalError, Limits, Operation, OperationRecord, valid_operation_id,
};
use super::{completed, first, fixture, operation, second};

const RETRY_ID: &str = "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6";

fn routine(
    journal: &Journal,
    generation: &str,
    operations: &[Operation],
) -> Result<Batch, JournalError> {
    journal.prepare_batch(generation, "thread", operations.to_vec(), true)
}

fn ids(fixture: &super::Fixture, generation: &str) -> Vec<Vec<Value>> {
    fixture.rows(&format!(
        "SELECT interrupt_id, operation_id, attempts FROM operations WHERE generation = '{generation}' ORDER BY ordinal"
    ))
}

fn text(value: &Value) -> &str {
    match value {
        Value::String(text) => text,
        _ => panic!("text"),
    }
}

#[test]
fn each_operation_id_is_durable_before_its_first_dispatch_and_survives_restart() {
    let fixture = fixture();
    let journal = fixture.open();
    let batch = super::prepare(&journal, "generation", &[first(), second()]);
    let prepared = ids(&fixture, "generation");
    let minted: Vec<&str> = prepared.iter().map(|row| text(&row[1])).collect();
    assert!(minted[0] != minted[1] && minted.iter().all(|id| valid_operation_id(id)));
    assert!(
        prepared
            .iter()
            .all(|row| row[2] == Value::Number(Number::Int(0)))
    );
    journal.close();
    let reopened = fixture.open();
    assert_eq!(
        super::prepare(&reopened, "generation", &[first(), second()]),
        batch
    );
    assert_eq!(
        reopened
            .begin(&batch, &first())
            .expect("began")
            .operation_id,
        minted[0]
    );
    reopened
        .complete(&batch, &first(), &super::json("{}"))
        .expect("completed");
    assert_eq!(
        reopened
            .begin(&batch, &first())
            .expect("replayed")
            .operation_id,
        minted[0]
    );
}

#[test]
fn a_permitted_retry_keeps_its_logical_operation_id_in_a_fresh_generation() {
    let fixture = fixture();
    let journal = fixture.open();
    let retry = Operation {
        operation_id: Some(RETRY_ID.to_owned()),
        ..first()
    };
    let batch = super::prepare(&journal, "attempt-2", &[retry.clone(), second()]);
    assert_eq!(
        journal.begin(&batch, &retry).expect("began").operation_id,
        RETRY_ID
    );
    assert_ne!(
        journal
            .begin(&batch, &second())
            .expect("began")
            .operation_id,
        RETRY_ID
    );
    let unbound = journal
        .batch("attempt-2", "thread-1", vec![first(), second()])
        .expect("handle");
    assert_ne!(unbound, batch);
    let upper = Operation {
        operation_id: Some(RETRY_ID.to_uppercase()),
        ..first()
    };
    let repeated = [
        retry.clone(),
        Operation {
            operation_id: Some(RETRY_ID.to_owned()),
            ..second()
        },
    ];
    for invalid in [vec![upper], repeated.to_vec()] {
        assert!(matches!(
            journal.prepare_batch("attempt-3", "thread", invalid, false),
            Err(JournalError::Conflict(_))
        ));
    }
}

#[test]
fn the_schema_refuses_an_outcome_without_the_execution_origin() {
    let fixture = fixture();
    super::prepare(&fixture.open(), "generation", &[first()]);
    let connection = Connection::open(&fixture.path).expect("opened");
    for statement in [
        "UPDATE operations SET state = 'completed', result = x'7b7d'",
        "UPDATE operations SET state = 'prepared', origin = 'execution'",
        "UPDATE operations SET attempts = -1",
        "UPDATE batches SET state = 'archived'",
    ] {
        assert!(connection.execute(statement, []).is_err(), "{statement}");
    }
}

#[test]
fn a_held_batch_snapshot_keeps_each_operation_state_without_results() {
    let fixture = fixture();
    let journal = fixture.limited(Limits {
        max_generations: 1,
        ..Limits::default()
    });
    let batch = routine(&journal, "routine-gen", &[first(), second()]).expect("prepared");
    completed(&journal, &batch, &first(), r#"{"ok":1}"#);
    journal.begin(&batch, &second()).expect("began");
    let records = journal
        .snapshot("routine-gen", &batch.fingerprint)
        .expect("snapshot");
    let states: Vec<(&str, &str, i64, Option<&str>)> = records
        .iter()
        .map(|item: &OperationRecord| {
            (
                item.interrupt_id.as_str(),
                item.state.as_str(),
                item.attempts,
                item.origin.as_deref(),
            )
        })
        .collect();
    assert_eq!(
        states,
        [
            ("interrupt-1", "completed", 1, Some("execution")),
            ("interrupt-2", "executing", 1, None)
        ]
    );
    assert!(matches!(
        journal.snapshot("routine-gen", &"0".repeat(64)),
        Err(JournalError::Conflict(_))
    ));
}

#[test]
fn an_archived_generation_refuses_every_replay() {
    let fixture = fixture();
    let journal = fixture.limited(Limits {
        max_generations: 1,
        ..Limits::default()
    });
    let batch = routine(&journal, "routine-gen", &[first(), second()]).expect("prepared");
    completed(&journal, &batch, &first(), r#"{"ok":1}"#);
    journal.begin(&batch, &second()).expect("began");
    journal
        .archive("routine-gen", &batch.fingerprint)
        .expect("archived");
    journal
        .archive("routine-gen", &batch.fingerprint)
        .expect("already archived");
    assert_eq!(ids(&fixture, "routine-gen").len(), 0);
    assert_eq!(journal.uncertain_fingerprint("routine-gen"), Ok(None));
    assert_eq!(
        routine(&journal, "routine-gen", &[first(), second()]),
        Err(JournalError::Archived)
    );
    assert_eq!(
        journal.prepare_batch(
            "routine-gen",
            "thread",
            vec![operation("fresh", "input")],
            false
        ),
        Err(JournalError::Archived)
    );
    assert_eq!(
        journal.snapshot("routine-gen", &batch.fingerprint),
        Err(JournalError::Archived)
    );
    assert!(matches!(
        journal.begin(&batch, &second()),
        Err(JournalError::Conflict(_))
    ));
    assert!(matches!(
        journal.abandon_uncertain(&batch),
        Ok(false) | Err(JournalError::Conflict(_))
    ));
}

#[test]
fn an_archive_marker_is_inactive_until_released() {
    let fixture = fixture();
    let journal = fixture.limited(Limits {
        max_generations: 1,
        ..Limits::default()
    });
    let batch = routine(&journal, "routine-gen", &[first()]).expect("prepared");
    journal
        .archive("routine-gen", &batch.fingerprint)
        .expect("archived");
    journal.end_settled("routine-gen").expect("nothing open");
    assert_eq!(
        journal.current_batch("routine-gen"),
        Ok(Some((batch.fingerprint.clone(), "archived".to_owned())))
    );
    // The marker is not active: a chat generation is admitted even at a one-generation bound.
    let chat = super::prepare(&journal, "chat-gen", &[first()]);
    journal.discard("routine-gen").expect("discarded");
    assert_eq!(
        routine(&journal, "routine-gen", &[first()]),
        Err(JournalError::Archived)
    );
    completed(&journal, &chat, &first(), "{}");
    journal.delivered(&chat).expect("delivered");
    journal
        .release_archive("routine-gen", &batch.fingerprint)
        .expect("released");
    journal
        .release_archive("routine-gen", &batch.fingerprint)
        .expect("already released");
    assert_eq!(journal.current_batch("routine-gen"), Ok(None));
    super::prepare(&journal, "routine-gen", &[first()]);
}

#[test]
fn archive_markers_are_reserved_before_any_action_runs_and_never_evicted() {
    let fixture = fixture();
    let journal = fixture.limited(Limits {
        max_archived: 2,
        ..Limits::default()
    });
    let held = routine(&journal, "held", &[first()]).expect("prepared");
    let live = routine(&journal, "live", &[first()]).expect("prepared");
    let full = Err(JournalError::Conflict(
        "Action journal archive capacity is exhausted",
    ));
    assert_eq!(routine(&journal, "third", &[first()]), full);
    journal
        .prepare_batch("chat", "thread", vec![first()], false)
        .expect("prepared");
    journal
        .archive("held", &held.fingerprint)
        .expect("archived");
    assert_eq!(routine(&journal, "third", &[first()]), full);
    journal.discard("live").expect("discarded");
    routine(&journal, "third", &[first()]).expect("prepared");
    let reserved = routine(&journal, "chat", &[first()]);
    assert_eq!(
        reserved,
        Err(JournalError::Conflict(
            "Action batch changed its archive reservation"
        ))
    );
    let chat = journal
        .prepare_batch("chat", "thread", vec![first()], false)
        .expect("prepared");
    assert_eq!(
        journal.archive("chat", &chat.fingerprint),
        Err(JournalError::Conflict(
            "Action batch holds no archive reservation"
        ))
    );
    assert_eq!(
        journal.archive("chat", &live.fingerprint),
        Err(JournalError::Conflict("Action batch is no longer current"))
    );
    assert!(matches!(
        journal.release_archive("chat", &chat.fingerprint),
        Err(JournalError::Conflict(_))
    ));
    journal.purge("held").expect("purged");
    assert_eq!(journal.current_batch("held"), Ok(None));
}

#[test]
fn every_crash_window_recovers_without_dispatching() {
    for phase in ["prepared", "executing"] {
        let fixture = fixture();
        let journal = fixture.open();
        let batch = routine(&journal, "routine-gen", &[first()]).expect("prepared");
        if phase == "executing" {
            journal.begin(&batch, &first()).expect("began");
        }
        std::mem::forget(journal);
        let reopened = fixture.open();
        let minted = text(&ids(&fixture, "routine-gen")[0][1]).to_owned();
        let same = routine(&reopened, "routine-gen", &[first()]).expect("reopened");
        if phase == "prepared" {
            assert_eq!(
                reopened.begin(&same, &first()).expect("began").operation_id,
                minted
            );
            continue;
        }
        // The interrupted dispatch stays uncertain; reconciliation archives it without dispatching again.
        assert_eq!(
            reopened.begin(&same, &first()),
            Err(JournalError::Uncertain)
        );
        let fingerprint = reopened
            .uncertain_fingerprint("routine-gen")
            .expect("read")
            .expect("uncertain");
        reopened
            .archive("routine-gen", &fingerprint)
            .expect("archived");
        assert_eq!(reopened.uncertain_fingerprint("routine-gen"), Ok(None));
    }
}
