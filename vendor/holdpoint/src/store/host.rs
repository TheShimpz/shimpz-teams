//! Writes that need no lease: submitting a run, a host's deferred result, a cancellation, and retention.
//!
//! Each checks the run's current state inside its own transaction, so it can never land on a run that ended or on a
//! request that was already answered differently.

use rusqlite::{OptionalExtension, Transaction, params};
use serde_json::Value;

use super::Store;
use crate::error::{Error, LimitKind};
use crate::event::Event;
use crate::json;
use crate::limits::Limits;

/// A run to submit.
#[derive(Debug, Clone, Copy)]
pub(crate) struct NewRun<'a> {
    pub(crate) id: &'a str,
    pub(crate) definition: &'a str,
    pub(crate) version: u32,
    pub(crate) input: &'a Value,
    pub(crate) deadline_ms: Option<i64>,
    pub(crate) spend_ceiling: Option<u64>,
    pub(crate) now_ms: i64,
}

/// Whether a submission created the run.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Submitted {
    /// The run is new.
    Created,
    /// The same run (same definition, version, and input) already existed; nothing changed.
    Existing,
}

/// Whether a resolution changed the run.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Resolution {
    /// The result was recorded and the run woken.
    Recorded,
    /// The same result was already recorded.
    Duplicate,
}

/// Refuses a run identifier outside `[A-Za-z0-9._:-]{1,128}`.
pub(crate) fn check_id(id: &str) -> Result<(), Error> {
    let valid = (1..=128).contains(&id.len())
        && id.bytes().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'));
    if valid { Ok(()) } else { Err(Error::Contract("a run id is 1-128 of [A-Za-z0-9._:-]".into())) }
}

/// A run's submitted identity: definition, version, input digest, deadline, and spend ceiling.
struct Identity {
    definition: String,
    version: i64,
    input_digest: String,
    deadline_ms: Option<i64>,
    spend_ceiling: Option<i64>,
}

/// `Existing` when the same run was already submitted; a conflict when the id names another run.
fn existing(transaction: &Transaction<'_>, run: &NewRun<'_>, digest: &str) -> Result<Option<Submitted>, Error> {
    let found = transaction
        .query_row(
            "SELECT definition, version, input_digest, deadline_ms, spend_ceiling FROM runs WHERE id = ?1",
            [run.id],
            |row| {
                Ok(Identity {
                    definition: row.get(0)?,
                    version: row.get(1)?,
                    input_digest: row.get(2)?,
                    deadline_ms: row.get(3)?,
                    spend_ceiling: row.get(4)?,
                })
            },
        )
        .optional()?;
    let Some(found) = found else {
        return Ok(None);
    };
    let ceiling = run.spend_ceiling.map(|ceiling| i64::try_from(ceiling).unwrap_or(i64::MAX));
    let same = found.definition == run.definition
        && found.version == i64::from(run.version)
        && found.input_digest == digest
        && found.deadline_ms == run.deadline_ms
        && found.spend_ceiling == ceiling;
    if same {
        Ok(Some(Submitted::Existing))
    } else {
        Err(Error::Conflict(format!("run {} exists with another definition, input, or options", run.id)))
    }
}

fn state_of(transaction: &Transaction<'_>, run_id: &str) -> Result<Option<String>, Error> {
    Ok(transaction.query_row("SELECT state FROM runs WHERE id = ?1", [run_id], |row| row.get(0)).optional()?)
}

fn append_one(
    transaction: &Transaction<'_>,
    run_id: &str,
    event: &Event,
    limits: &Limits,
    now_ms: i64,
) -> Result<(), Error> {
    let body = event.encode(limits.max_value_bytes + 4096)?;
    let length = i64::try_from(body.len()).unwrap_or(i64::MAX);
    let (seq, bytes): (i64, i64) =
        transaction
            .query_row("SELECT seq, bytes FROM runs WHERE id = ?1", [run_id], |row| Ok((row.get(0)?, row.get(1)?)))?;
    let full = u64::try_from(seq + 1).unwrap_or(u64::MAX) > limits.max_events_per_run
        || u64::try_from(bytes.saturating_add(length)).unwrap_or(u64::MAX) > limits.max_run_bytes;
    // A cancellation is tiny and must always land; a resolution must fit the run's history.
    if full && !matches!(event, Event::CancelRequested {}) {
        return Err(Error::Limit(LimitKind::History));
    }
    transaction.execute(
        "INSERT INTO events (run_id, seq, body) SELECT ?1, seq + 1, ?2 FROM runs WHERE id = ?1",
        params![run_id, body],
    )?;
    transaction.execute(
        "UPDATE runs SET seq = seq + 1, bytes = bytes + ?2, wake_ms = ?3, updated_ms = ?3 WHERE id = ?1",
        params![run_id, length, now_ms],
    )?;
    Ok(())
}

impl Store {
    /// Submits a run, idempotently by its identifier.
    pub(crate) fn create(&self, run: &NewRun<'_>) -> Result<Submitted, Error> {
        check_id(run.id)?;
        let digest = json::digest(run.input);
        self.limits.check_value(&json::to_canonical(run.input))?;
        let started = Event::RunStarted {
            definition: run.definition.to_owned(),
            version: run.version,
            input: run.input.clone(),
            deadline_ms: run.deadline_ms,
            spend_ceiling: run.spend_ceiling,
        };
        let body = started.encode(self.event_bytes())?;
        let max_runs = self.limits.max_runs;
        self.transaction(|transaction| {
            if let Some(submitted) = existing(transaction, run, &digest)? {
                return Ok(submitted);
            }
            let count: i64 = transaction.query_row("SELECT count(*) FROM runs", [], |row| row.get(0))?;
            if u64::try_from(count).unwrap_or(u64::MAX) >= max_runs {
                return Err(Error::Limit(LimitKind::Store));
            }
            let ceiling = run.spend_ceiling.map(|ceiling| i64::try_from(ceiling).unwrap_or(i64::MAX));
            let length = i64::try_from(body.len()).unwrap_or(i64::MAX);
            transaction
                .execute(
                    "INSERT INTO runs (id, definition, version, input_digest, deadline_ms, spend_ceiling, state, seq, \
                     bytes, wake_ms, created_ms, updated_ms) VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'open', 1, ?7, ?8, ?8, ?8)",
                    params![run.id, run.definition, run.version, digest, run.deadline_ms, ceiling, length, run.now_ms],
                )
                .and_then(|_| {
                    transaction.execute("INSERT INTO events (run_id, seq, body) VALUES (?1, 1, ?2)", params![run.id, body])
                })?;
            Ok(Submitted::Created)
        })
    }

    /// Records the host's result for a deferred request and wakes the run.
    ///
    /// # Errors
    ///
    /// [`Error::State`] when the run ended or has no such request; [`Error::Conflict`] when the request was already
    /// resolved with another result.
    pub fn resolve(&self, run_id: &str, step: u32, result: &Value, now_ms: i64) -> Result<Resolution, Error> {
        let canonical = json::to_canonical(result);
        self.limits.check_value(&canonical)?;
        let limits = self.limits.clone();
        self.transaction(|transaction| {
            match state_of(transaction, run_id)?.as_deref() {
                Some("open") => {}
                Some(_) => return Err(Error::State(format!("run {run_id} has ended"))),
                None => return Err(Error::State(format!("unknown run {run_id}"))),
            }
            let recorded: Option<Option<String>> = transaction
                .query_row(
                    "SELECT resolved FROM deferred WHERE run_id = ?1 AND step = ?2",
                    params![run_id, step],
                    |row| row.get(0),
                )
                .optional()?;
            match recorded {
                None => Err(Error::State(format!("run {run_id} has no deferred request at step {step}"))),
                Some(Some(previous)) if previous == canonical => Ok(Resolution::Duplicate),
                Some(Some(_)) => Err(Error::Conflict(format!("step {step} of run {run_id} was resolved differently"))),
                Some(None) => {
                    transaction.execute(
                        "UPDATE deferred SET resolved = ?3 WHERE run_id = ?1 AND step = ?2",
                        params![run_id, step, canonical],
                    )?;
                    append_one(
                        transaction,
                        run_id,
                        &Event::DeferredResolved { step, result: result.clone() },
                        &limits,
                        now_ms,
                    )?;
                    Ok(Resolution::Recorded)
                }
            }
        })
    }

    /// Asks an open run to cancel and wakes it; `false` when the run had already ended or was already cancelled.
    ///
    /// # Errors
    ///
    /// [`Error::State`] for an unknown run.
    pub fn cancel(&self, run_id: &str, now_ms: i64) -> Result<bool, Error> {
        let limits = self.limits.clone();
        self.transaction(|transaction| {
            let row: Option<(String, i64)> = transaction
                .query_row("SELECT state, cancel FROM runs WHERE id = ?1", [run_id], |row| {
                    Ok((row.get(0)?, row.get(1)?))
                })
                .optional()?;
            match row {
                None => Err(Error::State(format!("unknown run {run_id}"))),
                Some((state, cancel)) if state != "open" || cancel == 1 => Ok(false),
                Some(_) => {
                    transaction.execute("UPDATE runs SET cancel = 1 WHERE id = ?1", [run_id])?;
                    append_one(transaction, run_id, &Event::CancelRequested {}, &limits, now_ms)?;
                    Ok(true)
                }
            }
        })
    }

    /// Records that the receiver durably committed an ended run's outcome: its history, requests, and blobs are
    /// deleted and a tombstone (identity, input digest, outcome digest) remains, so a repeated submission is still
    /// recognized. Idempotent.
    ///
    /// # Errors
    ///
    /// [`Error::State`] when the run is unknown or still open.
    pub fn acknowledge(&self, run_id: &str, now_ms: i64) -> Result<(), Error> {
        self.transaction(|transaction| {
            match state_of(transaction, run_id)?.as_deref() {
                Some("closed") => {}
                Some("acknowledged") => return Ok(()),
                Some(_) => return Err(Error::State(format!("run {run_id} is still open"))),
                None => return Err(Error::State(format!("unknown run {run_id}"))),
            }
            delete_content(transaction, run_id)?;
            transaction.execute(
                "UPDATE runs SET state = 'acknowledged', outcome = NULL, updated_ms = ?2 WHERE id = ?1",
                params![run_id, now_ms],
            )?;
            Ok(())
        })
    }
}

/// What deleting a run must prove first: every recorded obligation, and the session of a lease that expired
/// without a clean release (its executors may still act).
#[derive(Debug, Clone)]
pub(crate) struct Deletion {
    pub(crate) fence: u64,
    pub(crate) lease_expires_ms: Option<i64>,
    pub(crate) obligations: Vec<super::Obligation>,
}

impl Store {
    /// What deleting `run_id` must prove, or `None` for an unknown run.
    pub(crate) fn deletion(&self, run_id: &str) -> Result<Option<Deletion>, Error> {
        self.transaction(|transaction| {
            let row: Option<(i64, Option<i64>)> = transaction
                .query_row("SELECT fence, lease_expires_ms FROM runs WHERE id = ?1", [run_id], |row| {
                    Ok((row.get(0)?, row.get(1)?))
                })
                .optional()?;
            let Some((fence, lease_expires_ms)) = row else {
                return Ok(None);
            };
            let obligations = super::write::obligations(transaction, run_id)?;
            Ok(Some(Deletion { fence: u64::try_from(fence).unwrap_or(0), lease_expires_ms, obligations }))
        })
    }

    /// Deletes a run once `proven` covers every obligation it carries and no session claimed it since `fence`.
    pub(crate) fn delete_proven(&self, run_id: &str, fence: u64, proven: &[i64], now_ms: i64) -> Result<(), Error> {
        self.transaction(|transaction| {
            let row: Option<(i64, Option<i64>)> = transaction
                .query_row("SELECT fence, lease_expires_ms FROM runs WHERE id = ?1", [run_id], |row| {
                    Ok((row.get(0)?, row.get(1)?))
                })
                .optional()?;
            let Some((current, expires)) = row else {
                return Ok(());
            };
            if expires.is_some_and(|expires| expires > now_ms) {
                return Err(Error::Busy(format!("run {run_id} is held by a live session")));
            }
            if u64::try_from(current).unwrap_or(0) != fence {
                return Err(Error::State(format!("run {run_id} changed while its deletion was being proven")));
            }
            let remaining = super::write::obligations(transaction, run_id)?;
            if remaining.iter().any(|obligation| !proven.contains(&obligation.id)) {
                return Err(Error::State(format!("run {run_id} has executors not yet proven ended")));
            }
            for table in ["obligations", "events", "deferred", "blobs"] {
                transaction.execute(&format!("DELETE FROM {table} WHERE run_id = ?1"), [run_id])?;
            }
            transaction.execute("DELETE FROM runs WHERE id = ?1", [run_id])?;
            Ok(())
        })
    }

    /// Makes an open run due now.
    pub(crate) fn wake(&self, run_id: &str, now_ms: i64) -> Result<(), Error> {
        self.transaction(|transaction| {
            transaction
                .execute("UPDATE runs SET wake_ms = ?2 WHERE id = ?1 AND state = 'open'", params![run_id, now_ms])?;
            Ok(())
        })
    }
}

fn delete_content(transaction: &Transaction<'_>, run_id: &str) -> Result<(), Error> {
    for table in ["events", "deferred", "blobs"] {
        transaction.execute(&format!("DELETE FROM {table} WHERE run_id = ?1"), [run_id])?;
    }
    Ok(())
}
