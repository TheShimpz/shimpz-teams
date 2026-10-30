use strict_json::Value;

use super::super::{JournalError, Limits};
use super::{completed, first, fixture, json, operation, prepare, second};

fn one_generation() -> Limits {
    Limits {
        max_generations: 1,
        ..Limits::default()
    }
}

#[test]
fn a_purge_removes_even_an_uncertain_generation_and_frees_capacity() {
    let fixture = fixture();
    let journal = fixture.limited(one_generation());
    let batch = prepare(&journal, "generation-1", &[first()]);
    journal.begin(&batch, &first()).expect("began");
    journal.purge("generation-1").expect("purged");
    journal.purge("generation-1").expect("already purged");
    let replacement = prepare(&journal, "generation-2", &[second()]);
    assert!(
        journal
            .begin(&replacement, &second())
            .expect("began")
            .execute
    );
    assert!(matches!(
        journal.begin(&batch, &first()),
        Err(JournalError::Conflict(_))
    ));
}

#[test]
fn an_exact_batch_purge_keeps_a_newer_batch_of_the_same_generation() {
    let fixture = fixture();
    let journal = fixture.limited(one_generation());
    let paused = prepare(&journal, "generation-1", &[first()]);
    // A new turn ends the paused batch, replaces it, and starts its own Action before the old cleanup purges.
    journal.end_settled("generation-1").expect("ended");
    let newer = prepare(&journal, "generation-1", &[second()]);
    journal.begin(&newer, &second()).expect("began");
    journal
        .purge_batch("generation-1", &paused.fingerprint)
        .expect("purged");
    assert_eq!(
        journal.current_batch("generation-1"),
        Ok(Some((newer.fingerprint.clone(), "open".to_owned())))
    );
    assert_eq!(
        journal.uncertain_fingerprint("generation-1"),
        Ok(Some(newer.fingerprint.clone()))
    );
    journal
        .purge_batch("generation-1", &newer.fingerprint)
        .expect("purged");
    journal
        .purge_batch("generation-1", &newer.fingerprint)
        .expect("already purged");
    assert_eq!(journal.current_batch("generation-1"), Ok(None));
}

#[test]
fn an_exact_batch_purge_refuses_an_invalid_identity() {
    let fixture = fixture();
    let journal = fixture.open();
    let batch = prepare(&journal, "generation-1", &[first()]);
    let short = batch.fingerprint[..63].to_owned();
    let invalid = [
        ("generation-1", batch.fingerprint.to_uppercase()),
        ("generation-1", short),
        ("bad generation", batch.fingerprint.clone()),
    ];
    for (generation, fingerprint) in invalid {
        assert!(matches!(
            journal.purge_batch(generation, &fingerprint),
            Err(JournalError::Conflict(_))
        ));
    }
    assert_eq!(
        journal.current_batch("generation-1"),
        Ok(Some((batch.fingerprint, "open".to_owned())))
    );
}

#[test]
fn terminal_abandonment_removes_only_the_exact_uncertain_batch() {
    let fixture = fixture();
    let journal = fixture.limited(one_generation());
    let uncertain = prepare(&journal, "generation-1", &[first()]);
    journal.begin(&uncertain, &first()).expect("began");
    assert_eq!(journal.abandon_uncertain(&uncertain), Ok(true));
    assert_eq!(journal.abandon_uncertain(&uncertain), Ok(false));
    let replacement = journal
        .prepare_batch("generation-1", "thread-2", vec![second()], false)
        .expect("prepared");
    assert!(matches!(
        journal.abandon_uncertain(&uncertain),
        Err(JournalError::Conflict(_))
    ));
    assert!(
        journal
            .begin(&replacement, &second())
            .expect("began")
            .execute
    );
}

#[test]
fn terminal_abandonment_keeps_a_completed_batch_for_replay() {
    let fixture = fixture();
    let journal = fixture.open();
    let batch = prepare(&journal, "generation-1", &[first()]);
    completed(&journal, &batch, &first(), r#"{"answer":1}"#);
    assert_eq!(journal.abandon_uncertain(&batch), Ok(false));
    let replay = journal.begin(&batch, &first()).expect("replayed");
    assert_eq!(
        (replay.execute, replay.result),
        (false, Some(json(r#"{"answer":1}"#)))
    );
}

#[test]
fn a_fresh_turn_ends_settled_work_but_keeps_uncertain_work() {
    let fixture = fixture();
    let journal = fixture.open();
    let prepared = prepare(&journal, "generation-0", &[first()]);
    journal.end_settled("generation-0").expect("ended");
    assert_eq!(
        journal.begin(&prepared, &first()),
        Err(JournalError::Conflict("Action batch has ended"))
    );
    let paused = prepare(&journal, "generation-1", &[first(), second()]);
    completed(&journal, &paused, &first(), r#"{"answer":1}"#);
    journal.begin(&paused, &second()).expect("began");
    journal.suspend(&paused, &second()).expect("suspended");
    journal.end_settled("generation-1").expect("ended");
    journal.end_settled("generation-1").expect("still ended");
    assert_eq!(
        journal.current_batch("generation-1"),
        Ok(Some((paused.fingerprint, "ended".to_owned())))
    );
    journal
        .end_settled("generation-absent")
        .expect("nothing to end");
    let uncertain = prepare(&journal, "generation-2", &[second()]);
    journal.begin(&uncertain, &second()).expect("began");
    journal.end_settled("generation-2").expect("kept");
    assert_eq!(
        journal.current_batch("generation-2"),
        Ok(Some((uncertain.fingerprint.clone(), "open".to_owned())))
    );
    assert_eq!(
        journal.begin(&uncertain, &second()),
        Err(JournalError::Uncertain)
    );
}

#[test]
fn settled_batch_ends_on_provider_failure_or_stop() {
    let fixture = fixture();
    let journal = fixture.limited(one_generation());
    let batch = prepare(&journal, "generation-1", &[first()]);
    completed(&journal, &batch, &first(), r#"{"answer":1}"#);
    assert_eq!(
        (journal.end(&batch), journal.end(&batch)),
        (Ok(true), Ok(false))
    );
    let receipts = fixture.rows("SELECT b.state, o.state, o.result FROM batches AS b JOIN operations AS o USING (generation)");
    let text = |value: &str| Value::String(value.to_owned());
    assert_eq!(
        receipts,
        [[text("ended"), text("completed"), text(r#"{"answer":1}"#)]]
    );
}

#[test]
fn an_ended_batch_refuses_every_transition_but_its_exact_replay() {
    let fixture = fixture();
    let journal = fixture.limited(one_generation());
    let batch = prepare(&journal, "generation-1", &[first()]);
    completed(&journal, &batch, &first(), r#"{"answer":1}"#);
    journal.end(&batch).expect("ended");
    assert!(matches!(
        journal.begin(&batch, &first()),
        Err(JournalError::Conflict(_))
    ));
    assert!(matches!(
        journal.delivered(&batch),
        Err(JournalError::Conflict(_))
    ));
    let repeat = journal.prepare_batch("generation-1", "thread-2", vec![second(), first()], false);
    assert!(matches!(repeat, Err(JournalError::Conflict(_))));
    let capacity = journal.prepare_batch("generation-2", "thread-2", vec![second()], false);
    assert_eq!(
        capacity,
        Err(JournalError::Conflict(
            "Action journal generation capacity is exhausted"
        ))
    );
    assert_eq!(journal.uncertain_fingerprint("generation-1"), Ok(None));
}

#[test]
fn an_ended_batch_reopens_for_its_exact_replay_and_yields_to_a_fresh_batch() {
    let fixture = fixture();
    let journal = fixture.limited(one_generation());
    let batch = prepare(&journal, "generation-1", &[first()]);
    completed(&journal, &batch, &first(), r#"{"answer":1}"#);
    journal.end(&batch).expect("ended");
    assert_eq!(prepare(&journal, "generation-1", &[first()]), batch);
    assert_eq!(
        journal.begin(&batch, &first()).expect("replayed").result,
        Some(json(r#"{"answer":1}"#))
    );
    assert_eq!(journal.end(&batch), Ok(true));
    let fresh = prepare(&journal, "generation-1", &[second()]);
    assert_eq!(
        journal.end(&batch),
        Err(JournalError::Conflict(
            "a newer Action batch replaced this ending handle"
        ))
    );
    completed(&journal, &fresh, &second(), r#"{"answer":2}"#);
    journal.delivered(&fresh).expect("delivered");
    assert_eq!(journal.end(&fresh), Ok(false));
}

#[test]
fn a_reopened_journal_ends_a_completed_batch_before_a_fresh_turn() {
    let fixture = fixture();
    let journal = fixture.open();
    let batch = prepare(&journal, "generation-1", &[first()]);
    completed(&journal, &batch, &first(), r#"{"answer":1}"#);
    journal.close();
    let reopened = fixture.open();
    let pending = reopened.prepare_batch("generation-1", "thread-1", vec![second()], false);
    assert_eq!(
        pending,
        Err(JournalError::Conflict(
            "another Action batch is pending for this generation"
        ))
    );
    reopened.end_settled("generation-1").expect("ended");
    let fresh = prepare(&reopened, "generation-1", &[second()]);
    assert!(reopened.begin(&fresh, &second()).expect("began").execute);
}

#[test]
fn a_read_only_failure_ends_without_effect_and_is_never_dispatched_again() {
    let fixture = fixture();
    let journal = fixture.open();
    let batch = prepare(&journal, "generation-1", &[first(), second()]);
    assert_eq!(
        journal.fail_without_effect(&batch, &first()),
        Err(JournalError::Conflict("Action operation was not executing"))
    );
    journal.begin(&batch, &first()).expect("began");
    journal
        .fail_without_effect(&batch, &first())
        .expect("settled");
    journal
        .fail_without_effect(&batch, &first())
        .expect("already settled");
    assert_eq!(
        journal.begin(&batch, &first()),
        Err(JournalError::Conflict(
            "Action operation already ended without effect"
        ))
    );
    assert!(matches!(
        journal.delivered(&batch),
        Err(JournalError::Conflict(_))
    ));
    assert_eq!(
        (
            journal.uncertain_fingerprint("generation-1"),
            journal.end(&batch)
        ),
        (Ok(None), Ok(true))
    );
    let row = fixture
        .rows("SELECT state, origin, result FROM operations WHERE interrupt_id = 'interrupt-1'");
    assert_eq!(
        row,
        [[
            Value::String("no_effect".to_owned()),
            Value::String("execution".to_owned()),
            Value::Null
        ]]
    );
}

#[test]
fn only_an_executing_attempt_returns_to_prepared() {
    let fixture = fixture();
    let journal = fixture.open();
    let batch = prepare(&journal, "generation-1", &[first()]);
    assert!(matches!(
        journal.not_dispatched(&batch, &first()),
        Err(JournalError::Conflict(_))
    ));
    journal.begin(&batch, &first()).expect("began");
    journal.not_dispatched(&batch, &first()).expect("returned");
    assert!(
        journal
            .begin(&batch, &first())
            .expect("began again")
            .execute
    );
    let outsider = operation("outsider", "value");
    assert_eq!(
        journal.begin(&batch, &outsider),
        Err(JournalError::Conflict(
            "operation does not belong to this Action batch"
        ))
    );
}
