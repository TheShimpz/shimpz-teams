//! The crash-safe, bounded idempotency journal of Assistant Action side effects (`action/journal.py`).
//!
//! It stores only fingerprints, logical operation ids, and bounded Action results, never a raw Action input. Each
//! operation receives its random logical `operation_id` in the same durable commit that prepares it, before any RPC,
//! and keeps it across human-request replay and restart (ADR-0092). An operation durably enters `executing` before its
//! side effect starts; finding it there again is an uncertain outcome and fails closed instead of risking a duplicate
//! side effect. Only a decoded human-request suspension, or Team's own refusal before dispatch, returns it to
//! `prepared`. It ends `completed` with a result, or `no_effect` when a reviewed read-only Action's handled failure
//! proved it had no business effect.
//!
//! A batch whose turn ended without delivery, and with no uncertain operation, becomes `ended`: it keeps its receipts
//! and frees its generation. Only the exact same batch reopens it to replay them; a fresh batch that repeats none of
//! its interrupts replaces it. A Routine batch is prepared `archivable`, reserving one archive marker before any of its
//! Actions runs; once its incident is durable it is `archived`, its operation rows go, and the marker refuses every
//! later prepare of that generation until it is released. Markers never count against the active generations.
//!
//! The database is `action-journal/journal.sqlite3`: WAL, synchronous FULL, `application_id` 1397246513,
//! `user_version` 3, CHECK-constrained state machines, `trusted_schema` off, a `0600` file in a `0700` directory.

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use rusqlite::{Connection, OpenFlags, OptionalExtension, params};
use sha2::{Digest, Sha256};
use strict_json::Value;
use team_core::entropy::Entropy;
use team_protocol::python::json;

use crate::result::{self, MAX_RESULT_BYTES};

mod file;
mod handle;

pub use handle::{Batch, Operation, new_operation_id, valid_operation_id};

/// The schema version.
pub const SCHEMA_VERSION: i64 = 3;
/// `SHJ1`.
pub const APPLICATION_ID: i64 = 0x5348_4A31;
/// The most active generations.
pub const MAX_GENERATIONS: usize = 1024;
/// The most archive markers per Local profile (ADR-0092).
pub const MAX_ARCHIVED: usize = 4096;
/// The most operations one batch holds.
pub const MAX_OPERATIONS: usize = 64;
/// The WAL is checkpointed every this many pages.
pub const WAL_AUTOCHECKPOINT_PAGES: i64 = 32;

const SCHEMA: &str = "
BEGIN IMMEDIATE;
CREATE TABLE batches (
    generation TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    operation_count INTEGER NOT NULL CHECK (operation_count > 0),
    state TEXT NOT NULL CHECK (state IN ('open', 'ended', 'archived')),
    archivable INTEGER NOT NULL CHECK (archivable IN (0, 1)),
    CHECK (state != 'archived' OR archivable = 1)
) WITHOUT ROWID;
CREATE TABLE operations (
    generation TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    interrupt_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    state TEXT NOT NULL CHECK (state IN ('prepared', 'executing', 'completed', 'no_effect')),
    origin TEXT CHECK (origin IN ('execution')),
    result BLOB,
    PRIMARY KEY (generation, interrupt_id),
    UNIQUE (generation, ordinal),
    UNIQUE (generation, operation_id),
    FOREIGN KEY (generation) REFERENCES batches(generation) ON DELETE CASCADE,
    CHECK ((state IN ('prepared', 'executing') AND origin IS NULL AND result IS NULL) OR
           (state = 'completed' AND origin IS NOT NULL AND result IS NOT NULL) OR
           (state = 'no_effect' AND origin IS NOT NULL AND result IS NULL))
) WITHOUT ROWID;
PRAGMA application_id = 1397246513;
PRAGMA user_version = 3;
COMMIT;
";
const STATES: [&str; 4] = ["prepared", "executing", "completed", "no_effect"];

/// Why the journal could not prove a transition; the text is a fixed description, never stored data.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum JournalError {
    /// The journal could not read or write (a transaction failed, or it is closed).
    Failure(&'static str),
    /// Durable state does not match the caller's immutable batch contract.
    Conflict(&'static str),
    /// A side effect may already have happened and must not be executed again.
    Uncertain,
    /// The journal or a persisted record violated its closed schema.
    Corrupt(&'static str),
    /// The generation holds an archived batch, which nothing may prepare, reopen, or replace.
    Archived,
}

impl std::fmt::Display for JournalError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Failure(text) | Self::Conflict(text) | Self::Corrupt(text) => {
                formatter.write_str(text)
            }
            Self::Uncertain => formatter.write_str(
                "Action execution outcome is uncertain; refusing a duplicate side effect",
            ),
            Self::Archived => formatter.write_str("Action generation is archived"),
        }
    }
}

impl std::error::Error for JournalError {}

/// The decision before an invocation, or a completed result to replay, with the logical operation id.
#[derive(Clone, Debug, PartialEq)]
pub struct Execution {
    /// Whether to run the Action now.
    pub execute: bool,
    /// The completed result to replay instead.
    pub result: Option<Value>,
    /// The logical operation id the attempt carries.
    pub operation_id: String,
}

/// The compact, result-free state of one operation, as an incident keeps it before its batch is archived.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OperationRecord {
    /// The Brain interrupt.
    pub interrupt_id: String,
    /// The logical operation id.
    pub operation_id: String,
    /// `prepared`, `executing`, `completed`, or `no_effect`.
    pub state: String,
    /// How many attempts began.
    pub attempts: i64,
    /// `execution` once the execution itself settled it.
    pub origin: Option<String>,
}

/// The journal's bounds.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Limits {
    /// The most active generations.
    pub max_generations: usize,
    /// The most operations per batch.
    pub max_operations: usize,
    /// The largest stored result.
    pub max_result_bytes: usize,
    /// The most archive markers.
    pub max_archived: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_generations: MAX_GENERATIONS,
            max_operations: MAX_OPERATIONS,
            max_result_bytes: MAX_RESULT_BYTES,
            max_archived: MAX_ARCHIVED,
        }
    }
}

/// One persisted operation row of a batch.
#[derive(Debug)]
struct Row {
    ordinal: i64,
    interrupt_id: String,
    fingerprint: String,
    state: String,
    result: Option<Vec<u8>>,
    operation_id: String,
    origin: Option<String>,
}

/// The Action journal over one private SQLite database.
pub struct Journal {
    path: PathBuf,
    limits: Limits,
    entropy: Arc<dyn Entropy>,
    connection: Mutex<Option<Connection>>,
}

impl std::fmt::Debug for Journal {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Journal")
            .field("path", &self.path)
            .field("limits", &self.limits)
            .finish_non_exhaustive()
    }
}

fn read<T>(result: rusqlite::Result<T>, failure: &'static str) -> Result<T, JournalError> {
    result.map_err(|_| JournalError::Corrupt(failure))
}

fn write<T>(result: rusqlite::Result<T>, failure: &'static str) -> Result<T, JournalError> {
    result.map_err(|_| JournalError::Failure(failure))
}

impl Journal {
    /// Opens (creating when absent) the journal at `path`, proving its custody, durability policy, and schema.
    ///
    /// # Errors
    /// [`JournalError::Corrupt`] for any unsafe path, unusable database, or schema drift; limits must be positive.
    pub fn open(
        path: &Path,
        limits: Limits,
        entropy: Arc<dyn Entropy>,
    ) -> Result<Self, JournalError> {
        if [
            limits.max_generations,
            limits.max_operations,
            limits.max_result_bytes,
            limits.max_archived,
        ]
        .contains(&0)
        {
            return Err(JournalError::Conflict(
                "Action journal limits must be positive",
            ));
        }
        let initialize = file::prepare(path)?;
        let flags = OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX
            | OpenFlags::SQLITE_OPEN_NOFOLLOW;
        let connection = Connection::open_with_flags(path, flags)
            .map_err(|_| JournalError::Corrupt("Action journal could not be opened safely"))?;
        configure(&connection)?;
        if initialize {
            read(
                connection.execute_batch(SCHEMA),
                "Action journal could not be opened safely",
            )?;
        }
        validate_schema(&connection)?;
        file::private_mode(path)?;
        Ok(Self {
            path: path.to_path_buf(),
            limits,
            entropy,
            connection: Mutex::new(Some(connection)),
        })
    }

    /// Closes the database; every later call fails.
    pub fn close(&self) {
        if let Ok(mut connection) = self.connection.lock() {
            connection.take();
        }
    }

    fn with<T>(
        &self,
        work: impl FnOnce(&Connection) -> Result<T, JournalError>,
    ) -> Result<T, JournalError> {
        let guard = self
            .connection
            .lock()
            .map_err(|_| JournalError::Failure("Action journal is closed"))?;
        let connection = guard
            .as_ref()
            .ok_or(JournalError::Failure("Action journal is closed"))?;
        work(connection)
    }

    /// One serialized write transaction, committed when `work` succeeds and rolled back on any refusal.
    fn writing<T>(
        &self,
        failure: &'static str,
        work: impl FnOnce(&Connection) -> Result<T, JournalError>,
    ) -> Result<T, JournalError> {
        self.with(|connection| {
            write(
                connection.execute_batch("BEGIN IMMEDIATE"),
                "Action journal transaction could not start",
            )?;
            match work(connection) {
                Ok(answer) => {
                    if connection.execute_batch("COMMIT").is_err() {
                        let _ = connection.execute_batch("ROLLBACK");
                        return Err(JournalError::Failure(
                            "Action journal transaction could not commit",
                        ));
                    }
                    Ok(answer)
                }
                Err(error) => {
                    let _ = connection.execute_batch("ROLLBACK");
                    Err(match error {
                        JournalError::Failure(_) => JournalError::Failure(failure),
                        other => other,
                    })
                }
            }
        })
    }

    /// The batch handle of `operations` for one generation and Brain thread, its fingerprint computed.
    ///
    /// # Errors
    /// [`JournalError::Conflict`] for an invalid id, an empty or oversized batch, or a repeated interrupt or
    /// operation id.
    pub fn batch(
        &self,
        generation: &str,
        thread_id: &str,
        operations: Vec<Operation>,
    ) -> Result<Batch, JournalError> {
        handle::build(
            generation,
            thread_id,
            operations,
            self.limits.max_operations,
        )
    }

    /// Durably prepares a batch and mints each new operation's id; an `archivable` batch reserves its marker. The
    /// exact same batch reopens (an ended one returns to replay its receipts); a fresh batch replaces an ended one
    /// that it shares no interrupt with; anything else is refused.
    ///
    /// # Errors
    /// [`JournalError::Archived`] for an archived generation, [`JournalError::Conflict`] for another pending batch or
    /// exhausted capacity, or a storage failure.
    pub fn prepare_batch(
        &self,
        generation: &str,
        thread_id: &str,
        operations: Vec<Operation>,
        archivable: bool,
    ) -> Result<Batch, JournalError> {
        let batch = self.batch(generation, thread_id, operations)?;
        self.writing("Action batch could not be prepared", |connection| {
            let row: Option<(String, String, i64)> = write(
                connection
                    .query_row(
                        "SELECT fingerprint, state, archivable FROM batches WHERE generation = ?",
                        [&batch.generation],
                        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                    )
                    .optional(),
                "Action batch could not be prepared",
            )?;
            match row {
                Some((_, state, _)) if state == "archived" => Err(JournalError::Archived),
                None => {
                    self.reserve(connection, archivable)?;
                    self.insert(connection, &batch, archivable)
                }
                Some((fingerprint, state, reserved)) if fingerprint == batch.fingerprint => {
                    if reserved != i64::from(archivable) {
                        return Err(JournalError::Conflict(
                            "Action batch changed its archive reservation",
                        ));
                    }
                    reopen(connection, &batch, &state)
                }
                Some((_, state, _)) if state == "open" => Err(JournalError::Conflict(
                    "another Action batch is pending for this generation",
                )),
                Some(_) => {
                    retire_ended(connection, &batch)?;
                    self.reserve(connection, archivable)?;
                    self.insert(connection, &batch, archivable)
                }
            }
        })?;
        Ok(batch)
    }

    /// Admits one more active generation; an archivable one also needs a free archive marker. Markers are not active,
    /// and every archivable batch, live or archived, holds one, so archiving never fails for capacity.
    fn reserve(&self, connection: &Connection, archivable: bool) -> Result<(), JournalError> {
        let count = |statement: &str| -> Result<usize, JournalError> {
            let value: i64 = connection
                .query_row(statement, [], |row| row.get(0))
                .map_err(|_| JournalError::Corrupt("Action journal capacity is invalid"))?;
            usize::try_from(value)
                .map_err(|_| JournalError::Corrupt("Action journal capacity is invalid"))
        };
        if count("SELECT COUNT(*) FROM batches WHERE state != 'archived'")?
            >= self.limits.max_generations
        {
            return Err(JournalError::Conflict(
                "Action journal generation capacity is exhausted",
            ));
        }
        if archivable
            && count("SELECT COUNT(*) FROM batches WHERE archivable = 1")?
                >= self.limits.max_archived
        {
            return Err(JournalError::Conflict(
                "Action journal archive capacity is exhausted",
            ));
        }
        Ok(())
    }

    fn insert(
        &self,
        connection: &Connection,
        batch: &Batch,
        archivable: bool,
    ) -> Result<(), JournalError> {
        let failure = "Action batch could not be prepared";
        let count = i64::try_from(batch.operations.len()).unwrap_or(i64::MAX);
        write(
            connection.execute(
                "INSERT INTO batches VALUES (?, ?, ?, 'open', ?)",
                params![
                    batch.generation,
                    batch.fingerprint,
                    count,
                    i64::from(archivable)
                ],
            ),
            failure,
        )?;
        for (ordinal, operation) in batch.operations.iter().enumerate() {
            let operation_id = match &operation.operation_id {
                Some(id) => id.clone(),
                None => new_operation_id(self.entropy.as_ref())
                    .map_err(|_| JournalError::Failure(failure))?,
            };
            write(
                connection.execute(
                    "INSERT INTO operations VALUES (?, ?, ?, ?, ?, 0, 'prepared', NULL, NULL)",
                    params![
                        batch.generation,
                        i64::try_from(ordinal).unwrap_or(i64::MAX),
                        operation.interrupt_id,
                        operation.fingerprint,
                        operation_id
                    ],
                ),
                failure,
            )?;
        }
        Ok(())
    }

    /// Begins one operation: a prepared one becomes `executing` (one more attempt); a completed one replays its
    /// result; an executing one is uncertain.
    ///
    /// # Errors
    /// [`JournalError::Uncertain`] for an executing operation, [`JournalError::Conflict`] for one that ended without
    /// effect or a batch that changed, [`JournalError::Corrupt`] for an invalid stored state.
    pub fn begin(&self, batch: &Batch, operation: &Operation) -> Result<Execution, JournalError> {
        batch.require(operation)?;
        self.writing("Action execution could not begin", |connection| {
            let row = Self::operation(connection, batch, operation)?;
            match row.state.as_str() {
                "executing" => Err(JournalError::Uncertain),
                "no_effect" => Err(JournalError::Conflict(
                    "Action operation already ended without effect",
                )),
                "completed" => Ok(Execution {
                    execute: false,
                    result: Some(self.decode(row.result.as_deref())?),
                    operation_id: row.operation_id,
                }),
                _ if row.result.is_some() => Err(JournalError::Corrupt(
                    "Action operation has an invalid durable state",
                )),
                _ => {
                    let changed = write(
                        connection.execute(
                            "UPDATE operations SET state = 'executing', attempts = attempts + 1
                             WHERE generation = ? AND interrupt_id = ? AND state = 'prepared'",
                            params![batch.generation, operation.interrupt_id],
                        ),
                        "Action execution could not begin",
                    )?;
                    require_changed(changed, "Action operation changed before execution")?;
                    Ok(Execution {
                        execute: true,
                        result: None,
                        operation_id: row.operation_id,
                    })
                }
            }
        })
    }

    /// The execution itself returned `result`.
    ///
    /// # Errors
    /// [`JournalError::Conflict`] for a result that is not bounded canonical JSON, an operation that was not
    /// executing, or a different result than the one already completed.
    pub fn complete(
        &self,
        batch: &Batch,
        operation: &Operation,
        result: &Value,
    ) -> Result<(), JournalError> {
        let encoded = result::canonical(result, self.limits.max_result_bytes)
            .map_err(|_| JournalError::Conflict("Action result is not bounded canonical JSON"))?;
        self.settle(batch, operation, "completed", Some(&encoded))
    }

    /// A handled failure of an Action whose pinned reviewed declaration is `read_only` had no business effect.
    ///
    /// # Errors
    /// As [`Journal::complete`].
    pub fn fail_without_effect(
        &self,
        batch: &Batch,
        operation: &Operation,
    ) -> Result<(), JournalError> {
        self.settle(batch, operation, "no_effect", None)
    }

    fn settle(
        &self,
        batch: &Batch,
        operation: &Operation,
        state: &'static str,
        encoded: Option<&[u8]>,
    ) -> Result<(), JournalError> {
        batch.require(operation)?;
        self.writing("Action result could not be committed", |connection| {
            let row = Self::operation(connection, batch, operation)?;
            if row.state == state && row.origin.as_deref() == Some("execution") {
                return if row.result.as_deref() == encoded {
                    Ok(())
                } else {
                    Err(JournalError::Conflict(
                        "Action result changed after completion",
                    ))
                };
            }
            if row.state != "executing" || row.result.is_some() {
                return Err(JournalError::Conflict("Action operation was not executing"));
            }
            let changed = write(
                connection.execute(
                    "UPDATE operations SET state = ?, origin = 'execution', result = ?
                     WHERE generation = ? AND interrupt_id = ? AND state = 'executing'",
                    params![state, encoded, batch.generation, operation.interrupt_id],
                ),
                "Action result could not be committed",
            )?;
            require_changed(changed, "Action operation changed before completion")
        })
    }

    /// Returns one proven human-request suspension to deterministic replay; its operation id is kept.
    ///
    /// # Errors
    /// [`JournalError::Conflict`] unless the operation is executing without a result.
    pub fn suspend(&self, batch: &Batch, operation: &Operation) -> Result<(), JournalError> {
        self.reprepare(batch, operation, "Action suspension could not be committed")
    }

    /// Returns an attempt Team refused before its workload process started to `prepared`; nothing ran.
    ///
    /// # Errors
    /// As [`Journal::suspend`].
    pub fn not_dispatched(&self, batch: &Batch, operation: &Operation) -> Result<(), JournalError> {
        self.reprepare(
            batch,
            operation,
            "Action dispatch refusal could not be committed",
        )
    }

    fn reprepare(
        &self,
        batch: &Batch,
        operation: &Operation,
        failure: &'static str,
    ) -> Result<(), JournalError> {
        batch.require(operation)?;
        self.writing(failure, |connection| {
            let row = Self::operation(connection, batch, operation)?;
            if row.state != "executing" || row.result.is_some() {
                return Err(JournalError::Conflict("Action operation was not executing"));
            }
            let changed = write(
                connection.execute(
                    "UPDATE operations SET state = 'prepared'
                     WHERE generation = ? AND interrupt_id = ? AND state = 'executing' AND result IS NULL",
                    params![batch.generation, operation.interrupt_id],
                ),
                failure,
            )?;
            require_changed(changed, "Action operation changed before it returned to prepared")
        })
    }

    fn decode(&self, raw: Option<&[u8]>) -> Result<Value, JournalError> {
        let corrupt = JournalError::Corrupt("cached Action result is invalid");
        let raw = raw
            .filter(|raw| raw.len() <= self.limits.max_result_bytes)
            .ok_or(corrupt)?;
        let value = strict_json::parse(raw).map_err(|_| corrupt)?;
        if result::canonical(&value, self.limits.max_result_bytes).map_err(|_| corrupt)? != raw {
            return Err(corrupt);
        }
        Ok(value)
    }

    /// Whether the exact batch is still its generation's: absent is `false`, another batch is refused.
    fn current(
        connection: &Connection,
        batch: &Batch,
        replaced: &'static str,
    ) -> Result<bool, JournalError> {
        let fingerprint: Option<String> = write(
            connection
                .query_row(
                    "SELECT fingerprint FROM batches WHERE generation = ?",
                    [&batch.generation],
                    |row| row.get(0),
                )
                .optional(),
            "Action journal state could not be read",
        )?;
        match fingerprint {
            None => Ok(false),
            Some(fingerprint) if fingerprint == batch.fingerprint => Ok(true),
            Some(_) => Err(JournalError::Conflict(replaced)),
        }
    }

    fn delete(
        connection: &Connection,
        batch: &Batch,
        failure: &'static str,
        message: &'static str,
    ) -> Result<(), JournalError> {
        let changed = write(
            connection.execute(
                "DELETE FROM batches WHERE generation = ? AND fingerprint = ?",
                params![batch.generation, batch.fingerprint],
            ),
            failure,
        )?;
        require_changed(changed, message)
    }

    /// The model accepted every result of the batch: its rows go. An already-delivered batch is done.
    ///
    /// # Errors
    /// [`JournalError::Conflict`] before every result exists or when a newer batch replaced it.
    pub fn delivered(&self, batch: &Batch) -> Result<(), JournalError> {
        let failure = "Action batch delivery could not be committed";
        self.writing(failure, |connection| {
            if !Self::current(
                connection,
                batch,
                "a newer Action batch replaced this delivery handle",
            )? {
                return Ok(());
            }
            let rows = handle::load(connection, batch)?;
            if rows.iter().any(|row| row.state != "completed") {
                return Err(JournalError::Conflict(
                    "Action batch cannot be delivered before every result exists",
                ));
            }
            for row in &rows {
                self.decode(row.result.as_deref())?;
            }
            Self::delete(
                connection,
                batch,
                failure,
                "Action batch changed before delivery",
            )
        })
    }

    /// Ends one handled terminal attempt only when its exact batch is uncertain; whether it did.
    ///
    /// # Errors
    /// [`JournalError::Conflict`] when a newer batch replaced it.
    pub fn abandon_uncertain(&self, batch: &Batch) -> Result<bool, JournalError> {
        let failure = "uncertain Action batch could not be abandoned";
        self.writing(failure, |connection| {
            if !Self::current(
                connection,
                batch,
                "a newer Action batch replaced this abandonment handle",
            )? || handle::load(connection, batch)?
                .iter()
                .all(|row| row.state != "executing")
            {
                return Ok(false);
            }
            Self::delete(
                connection,
                batch,
                failure,
                "Action batch changed before terminal abandonment",
            )?;
            Ok(true)
        })
    }

    /// Ends one exact undelivered batch at a terminal turn unless one of its operations may have acted; whether it
    /// did. An absent or already-ended batch is not ended again.
    ///
    /// # Errors
    /// [`JournalError::Conflict`] when a newer batch replaced it.
    pub fn end(&self, batch: &Batch) -> Result<bool, JournalError> {
        self.writing("Action batch could not be ended", |connection| {
            let row: Option<(String, String)> = write(
                connection
                    .query_row(
                        "SELECT fingerprint, state FROM batches WHERE generation = ?",
                        [&batch.generation],
                        |row| Ok((row.get(0)?, row.get(1)?)),
                    )
                    .optional(),
                "Action batch could not be ended",
            )?;
            match row {
                None => Ok(false),
                Some((fingerprint, state))
                    if fingerprint == batch.fingerprint && state == "ended" =>
                {
                    Ok(false)
                }
                Some((fingerprint, _)) if fingerprint != batch.fingerprint => Err(
                    JournalError::Conflict("a newer Action batch replaced this ending handle"),
                ),
                Some(_) => {
                    let states: Vec<String> = handle::load(connection, batch)?
                        .into_iter()
                        .map(|row| row.state)
                        .collect();
                    end_settled(connection, &batch.generation, &states)
                }
            }
        })
    }

    /// Ends a generation's undelivered open batch before a fresh turn unless one of its operations may have acted.
    ///
    /// # Errors
    /// [`JournalError::Conflict`] for an invalid generation, or a storage failure.
    pub fn end_settled(&self, generation: &str) -> Result<(), JournalError> {
        let generation = handle::safe_id(generation, "generation")?;
        let failure = "settled Action generation could not be ended";
        self.writing(failure, |connection| {
            let mut statement = write(
                connection.prepare(
                    "SELECT o.state FROM batches AS b JOIN operations AS o ON o.generation = b.generation
                     WHERE b.generation = ? AND b.state = 'open'",
                ),
                failure,
            )?;
            let states: Vec<String> =
                write(statement.query_map([generation], |row| row.get(0)).and_then(Iterator::collect), failure)?;
            if !states.is_empty() {
                end_settled(connection, generation, &states)?;
            }
            Ok(())
        })
    }

    fn operation(
        connection: &Connection,
        batch: &Batch,
        operation: &Operation,
    ) -> Result<Row, JournalError> {
        handle::load(connection, batch)?
            .into_iter()
            .find(|row| row.interrupt_id == operation.interrupt_id)
            .ok_or(JournalError::Conflict(
                "Action operation changed, ended, or is corrupt",
            ))
    }
}

mod lifecycle;

/// Marks an open batch `ended` unless one of `states` is `executing`; whether it did.
fn end_settled(
    connection: &Connection,
    generation: &str,
    states: &[String],
) -> Result<bool, JournalError> {
    if states.iter().any(|state| state == "executing") {
        return Ok(false);
    }
    let changed = write(
        connection.execute(
            "UPDATE batches SET state = 'ended' WHERE generation = ? AND state = 'open'",
            [generation],
        ),
        "Action batch could not be ended",
    )?;
    require_changed(changed, "Action batch changed before it ended")?;
    Ok(true)
}

/// Resumes the exact same batch; an ended one returns to replay its kept receipts.
fn reopen(connection: &Connection, batch: &Batch, state: &str) -> Result<(), JournalError> {
    if state == "ended" {
        let changed = write(
            connection.execute(
                "UPDATE batches SET state = 'open' WHERE generation = ? AND fingerprint = ? AND state = 'ended'",
                params![batch.generation, batch.fingerprint],
            ),
            "Action batch could not be prepared",
        )?;
        require_changed(changed, "ended Action batch changed before replay")?;
    }
    handle::load(connection, batch).map(|_| ())
}

/// Frees an ended generation for a fresh batch; repeating one of its interrupts is a refused replay.
fn retire_ended(connection: &Connection, batch: &Batch) -> Result<(), JournalError> {
    let failure = "Action batch could not be prepared";
    let mut statement = write(
        connection.prepare("SELECT interrupt_id FROM operations WHERE generation = ?"),
        failure,
    )?;
    let ended: Vec<String> = write(
        statement
            .query_map([&batch.generation], |row| row.get(0))
            .and_then(Iterator::collect),
        failure,
    )?;
    if batch
        .operations
        .iter()
        .any(|operation| ended.contains(&operation.interrupt_id))
    {
        return Err(JournalError::Conflict(
            "Action batch repeats an ended Action interrupt",
        ));
    }
    let changed = write(
        connection.execute(
            "DELETE FROM batches WHERE generation = ? AND state = 'ended'",
            [&batch.generation],
        ),
        failure,
    )?;
    require_changed(changed, "ended Action batch changed before replacement")
}

fn require_changed(changed: usize, message: &'static str) -> Result<(), JournalError> {
    if changed == 1 {
        Ok(())
    } else {
        Err(JournalError::Conflict(message))
    }
}

fn configure(connection: &Connection) -> Result<(), JournalError> {
    let corrupt = |_| JournalError::Corrupt("Action journal could not enable its durable mode");
    connection
        .execute_batch("PRAGMA trusted_schema = OFF; PRAGMA foreign_keys = ON;")
        .map_err(corrupt)?;
    connection
        .busy_timeout(std::time::Duration::from_secs(5))
        .map_err(corrupt)?;
    let mode: String = connection
        .query_row("PRAGMA journal_mode = WAL", [], |row| row.get(0))
        .map_err(corrupt)?;
    if mode != "wal" {
        return Err(JournalError::Corrupt(
            "Action journal could not enable its durable mode",
        ));
    }
    connection
        .execute_batch("PRAGMA synchronous = FULL")
        .map_err(corrupt)?;
    let checkpoint: i64 = connection
        .query_row(
            &format!("PRAGMA wal_autocheckpoint = {WAL_AUTOCHECKPOINT_PAGES}"),
            [],
            |row| row.get(0),
        )
        .map_err(corrupt)?;
    let synchronous: i64 = connection
        .query_row("PRAGMA synchronous", [], |row| row.get(0))
        .map_err(corrupt)?;
    if checkpoint != WAL_AUTOCHECKPOINT_PAGES || synchronous != 2 {
        return Err(JournalError::Corrupt(
            "Action journal durability policy could not be applied",
        ));
    }
    Ok(())
}

/// What schema validation reads.
struct Schema {
    check: Vec<String>,
    application: i64,
    version: i64,
    tables: Vec<String>,
    batches: Vec<String>,
    operations: Vec<String>,
    dangling: bool,
}

fn read_schema(connection: &Connection) -> rusqlite::Result<Schema> {
    let strings = |statement: &str, column: usize| -> rusqlite::Result<Vec<String>> {
        let mut prepared = connection.prepare(statement)?;
        prepared.query_map([], |row| row.get(column))?.collect()
    };
    let integer = |statement: &str| connection.query_row(statement, [], |row| row.get(0));
    let mut tables = strings("SELECT name FROM sqlite_master WHERE type = 'table'", 0)?;
    tables.sort_unstable();
    let mut foreign = connection.prepare("PRAGMA foreign_key_check")?;
    let dangling = foreign.query([])?.next()?.is_some();
    Ok(Schema {
        check: strings("PRAGMA quick_check", 0)?,
        application: integer("PRAGMA application_id")?,
        version: integer("PRAGMA user_version")?,
        tables,
        batches: strings("PRAGMA table_info(batches)", 1)?,
        operations: strings("PRAGMA table_info(operations)", 1)?,
        dangling,
    })
}

fn validate_schema(connection: &Connection) -> Result<(), JournalError> {
    let schema = read_schema(connection)
        .map_err(|_| JournalError::Corrupt("Action journal integrity could not be verified"))?;
    let operations = [
        "generation",
        "ordinal",
        "interrupt_id",
        "fingerprint",
        "operation_id",
        "attempts",
        "state",
        "origin",
        "result",
    ];
    let valid = schema.check == ["ok"]
        && schema.application == APPLICATION_ID
        && schema.version == SCHEMA_VERSION
        && schema.tables == ["batches", "operations"]
        && schema.batches
            == [
                "generation",
                "fingerprint",
                "operation_count",
                "state",
                "archivable",
            ]
        && schema.operations == operations
        && !schema.dangling;
    if valid {
        Ok(())
    } else {
        Err(JournalError::Corrupt(
            "Action journal schema or contents are invalid",
        ))
    }
}

/// `bytes` as lowercase hexadecimal.
pub(crate) fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    bytes
        .iter()
        .fold(String::with_capacity(bytes.len() * 2), |mut text, byte| {
            let _ = write!(text, "{byte:02x}");
            text
        })
}

/// SHA-256 of `bytes` as lowercase hexadecimal.
pub(crate) fn sha256_hex(bytes: &[u8]) -> String {
    hex(&Sha256::digest(bytes))
}

/// The canonical JSON bytes of `value`.
pub(crate) fn canonical(value: &Value) -> Vec<u8> {
    json::canonical(value)
}

#[cfg(test)]
mod tests;
