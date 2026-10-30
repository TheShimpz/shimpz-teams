//! The SQLite ledger: runs, their histories, deferred requests, transient blobs, and executor obligations.
//!
//! This is the only module with SQL. Every write is one `BEGIN IMMEDIATE` transaction; the database runs in WAL mode
//! with `synchronous=FULL`, so a committed step survives a power loss. Tables are `STRICT`.

mod host;
mod write;

use std::fs::OpenOptions;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::sync::{Mutex, MutexGuard};

use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde_json::Value;

use crate::error::Error;
use crate::event::{Event, Outcome};
use crate::json;
use crate::limits::Limits;

pub(crate) use host::{NewRun, check_id};
pub use host::{Resolution, Submitted};
pub(crate) use write::{Dispatch, End, Lease, NewObligation, Write, Written};

const SCHEMA_VERSION: i64 = 1;

const SCHEMA: &str = "
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    definition TEXT NOT NULL,
    version INTEGER NOT NULL,
    input_digest TEXT NOT NULL,
    deadline_ms INTEGER,
    spend_ceiling INTEGER,
    state TEXT NOT NULL CHECK (state IN ('open', 'closed', 'acknowledged')),
    outcome TEXT,
    outcome_digest TEXT,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    bytes INTEGER NOT NULL,
    fence INTEGER NOT NULL DEFAULT 0,
    lease_session TEXT,
    lease_expires_ms INTEGER,
    wake_ms INTEGER,
    cancel INTEGER NOT NULL DEFAULT 0 CHECK (cancel IN (0, 1)),
    created_ms INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL,
    CHECK ((lease_session IS NULL) = (lease_expires_ms IS NULL)),
    CHECK ((state = 'open') = (outcome_digest IS NULL))
) STRICT;
CREATE INDEX runs_due ON runs (wake_ms) WHERE state = 'open';
CREATE TABLE events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
) STRICT, WITHOUT ROWID;
CREATE TABLE deferred (
    run_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    name TEXT NOT NULL,
    request TEXT NOT NULL,
    resolved TEXT,
    PRIMARY KEY (run_id, step)
) STRICT, WITHOUT ROWID;
CREATE TABLE blobs (
    run_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    data BLOB NOT NULL,
    PRIMARY KEY (run_id, id)
) STRICT;
CREATE TABLE obligations (
    run_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('session', 'attempt')),
    fence INTEGER NOT NULL,
    step INTEGER,
    attempt INTEGER,
    incarnation TEXT NOT NULL,
    created_ms INTEGER NOT NULL,
    PRIMARY KEY (run_id, id),
    CHECK ((kind = 'session') = (step IS NULL AND attempt IS NULL))
) STRICT;
";

/// A deferred request still waiting for the host.
#[derive(Debug, Clone, PartialEq)]
pub struct PendingRequest {
    /// The step to resolve.
    pub step: u32,
    /// The definition's name for the request.
    pub name: String,
    /// What the run asks for.
    pub request: Value,
}

/// An executor the host must prove ended before the run dispatches anything.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Obligation {
    /// The obligation's identity within the run.
    pub id: i64,
    /// The fencing token of the session that left it.
    pub fence: u64,
    /// The attempt it concerns, or `None` for everything a session owned.
    pub attempt: Option<(u32, u32)>,
    /// The worker incarnation that recorded it.
    pub incarnation: String,
}

/// A run as loaded for driving.
#[derive(Debug)]
pub(crate) struct Loaded {
    /// Whether the run has not ended (an ended run's outcome is read with [`Store::result`]).
    pub(crate) open: bool,
    pub(crate) events: Vec<Event>,
    pub(crate) seq: u64,
}

/// The ledger. Cheap to share behind an `Arc`; every call takes the connection for its own duration only.
#[derive(Debug)]
pub struct Store {
    connection: Mutex<Connection>,
    limits: Limits,
    commits: std::sync::atomic::AtomicU64,
}

impl Store {
    /// Opens (creating it with mode `0600` if needed) the ledger at `path`.
    ///
    /// # Errors
    ///
    /// [`Error::Storage`] when the file cannot be opened or holds another schema version.
    pub fn open(path: &Path, limits: Limits) -> Result<Self, Error> {
        OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .open(path)
            .map_err(|error| Error::Storage(format!("cannot open the ledger: {error}")))?;
        Self::prepare(Connection::open(path)?, limits)
    }

    /// Opens a private in-memory ledger (for tests and benchmarks; nothing survives the process).
    ///
    /// # Errors
    ///
    /// [`Error::Storage`] when SQLite cannot allocate it.
    pub fn open_in_memory(limits: Limits) -> Result<Self, Error> {
        Self::prepare(Connection::open_in_memory()?, limits)
    }

    fn prepare(connection: Connection, limits: Limits) -> Result<Self, Error> {
        connection.busy_timeout(std::time::Duration::from_secs(10))?;
        connection.pragma_update(None, "journal_mode", "WAL")?;
        connection.pragma_update(None, "synchronous", "FULL")?;
        // Deleted content is overwritten, not left in free pages, and the schema never runs code from the file.
        connection.pragma_update(None, "secure_delete", "ON")?;
        connection.pragma_update(None, "trusted_schema", "OFF")?;
        let version: i64 = connection.pragma_query_value(None, "user_version", |row| row.get(0))?;
        match version {
            0 => {
                connection.execute_batch(&format!(
                    "BEGIN IMMEDIATE; {SCHEMA} PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;"
                ))?;
            }
            SCHEMA_VERSION => {}
            other => return Err(Error::Storage(format!("unsupported ledger version {other}"))),
        }
        Ok(Self { connection: Mutex::new(connection), limits, commits: std::sync::atomic::AtomicU64::new(0) })
    }

    /// Moves every committed page into the database file and empties the write-ahead log, so content a deletion
    /// overwrote survives in neither.
    ///
    /// # Errors
    ///
    /// [`Error::Storage`] when another connection keeps the log from being emptied.
    pub fn truncate_log(&self) -> Result<(), Error> {
        let connection = self.lock()?;
        let busy: i64 = connection.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |row| row.get(0))?;
        if busy == 0 { Ok(()) } else { Err(Error::Storage("the ledger log could not be emptied".into())) }
    }

    /// How many write transactions this handle committed (each one is a durable sync of the ledger).
    #[must_use]
    pub fn commits(&self) -> u64 {
        self.commits.load(std::sync::atomic::Ordering::Relaxed)
    }

    /// The bounds this store enforces.
    #[must_use]
    pub fn limits(&self) -> &Limits {
        &self.limits
    }

    fn lock(&self) -> Result<MutexGuard<'_, Connection>, Error> {
        self.connection.lock().map_err(|_| Error::Storage("the ledger connection is poisoned".into()))
    }

    pub(crate) fn event_bytes(&self) -> usize {
        self.limits.max_value_bytes + 4096
    }

    /// A run's outcome, or `None` while it is open.
    ///
    /// # Errors
    ///
    /// [`Error::State`] for an unknown or acknowledged run.
    pub fn result(&self, run_id: &str) -> Result<Option<Outcome>, Error> {
        let connection = self.lock()?;
        let row: Option<(String, Option<String>)> = connection
            .query_row("SELECT state, outcome FROM runs WHERE id = ?1", [run_id], |row| Ok((row.get(0)?, row.get(1)?)))
            .optional()?;
        match row {
            None => Err(Error::State(format!("unknown run {run_id}"))),
            Some((state, _)) if state == "acknowledged" => Err(Error::State(format!("run {run_id} was acknowledged"))),
            Some((_, None)) => Ok(None),
            Some((_, Some(outcome))) => {
                let value = json::parse(outcome.as_bytes(), self.event_bytes())?;
                serde_json::from_value(value).map(Some).map_err(|error| Error::Storage(error.to_string()))
            }
        }
    }

    /// A run's complete history, validated.
    ///
    /// # Errors
    ///
    /// [`Error::State`] for an unknown run; [`Error::Storage`] for a corrupt one.
    pub fn history(&self, run_id: &str) -> Result<Vec<Event>, Error> {
        Ok(self.load(run_id)?.events)
    }

    pub(crate) fn load(&self, run_id: &str) -> Result<Loaded, Error> {
        let connection = self.lock()?;
        let state: Option<(String, i64)> = connection
            .query_row("SELECT state, seq FROM runs WHERE id = ?1", [run_id], |row| Ok((row.get(0)?, row.get(1)?)))
            .optional()?;
        let (state, seq) = state.ok_or_else(|| Error::State(format!("unknown run {run_id}")))?;
        let events = read_events(&connection, run_id, 0, self.event_bytes())?;
        Ok(Loaded { open: state == "open", events, seq: u64::try_from(seq).unwrap_or(0) })
    }

    /// The deferred requests of a run still waiting for the host.
    ///
    /// # Errors
    ///
    /// [`Error::Storage`] when the ledger cannot be read.
    pub fn pending(&self, run_id: &str) -> Result<Vec<PendingRequest>, Error> {
        let connection = self.lock()?;
        let mut statement = connection.prepare_cached(
            "SELECT step, name, request FROM deferred WHERE run_id = ?1 AND resolved IS NULL ORDER BY step",
        )?;
        let rows =
            statement.query_map([run_id], |row| Ok((row.get::<_, i64>(0)?, row.get(1)?, row.get::<_, String>(2)?)))?;
        let mut pending = Vec::new();
        for row in rows {
            let (step, name, request) = row?;
            pending.push(PendingRequest {
                step: u32::try_from(step).map_err(|_| Error::Storage("corrupt deferred step".into()))?,
                name,
                request: json::parse(request.as_bytes(), self.event_bytes())?,
            });
        }
        Ok(pending)
    }

    /// The executor obligations a run still carries.
    ///
    /// # Errors
    ///
    /// [`Error::Storage`] when the ledger cannot be read.
    pub fn obligations(&self, run_id: &str) -> Result<Vec<Obligation>, Error> {
        let connection = self.lock()?;
        write::obligations(&connection, run_id)
    }

    /// A transient blob, or `None` once its last consumer committed.
    ///
    /// # Errors
    ///
    /// [`Error::Storage`] when the ledger cannot be read.
    pub fn blob(&self, run_id: &str, id: u64) -> Result<Option<Vec<u8>>, Error> {
        let connection = self.lock()?;
        let id = i64::try_from(id).map_err(|_| Error::Contract("blob id out of range".into()))?;
        Ok(connection
            .query_row("SELECT data FROM blobs WHERE run_id = ?1 AND id = ?2", params![run_id, id], |row| row.get(0))
            .optional()?)
    }

    /// The identifiers of open runs a scheduler should drive now: due, and not held by a live lease.
    ///
    /// # Errors
    ///
    /// [`Error::Storage`] when the ledger cannot be read.
    pub fn due(&self, now_ms: i64, limit: usize) -> Result<Vec<String>, Error> {
        let connection = self.lock()?;
        let mut statement = connection.prepare_cached(
            "SELECT id FROM runs WHERE state = 'open' AND wake_ms <= ?1 \
             AND (lease_session IS NULL OR lease_expires_ms <= ?1) ORDER BY wake_ms LIMIT ?2",
        )?;
        let limit = i64::try_from(limit).unwrap_or(i64::MAX);
        let ids = statement.query_map(params![now_ms, limit], |row| row.get(0))?;
        ids.collect::<Result<_, _>>().map_err(Error::from)
    }

    /// The earliest wake time of any open run, if one has a wake time.
    ///
    /// # Errors
    ///
    /// [`Error::Storage`] when the ledger cannot be read.
    pub fn next_wake(&self) -> Result<Option<i64>, Error> {
        let connection = self.lock()?;
        Ok(connection.query_row(
            "SELECT min(max(wake_ms, coalesce(lease_expires_ms, wake_ms))) FROM runs WHERE state = 'open'",
            [],
            |row| row.get(0),
        )?)
    }

    /// Whether another process asked the run to cancel, read without a write transaction.
    pub(crate) fn cancel_requested(&self, run_id: &str) -> Result<bool, Error> {
        let connection = self.lock()?;
        let cancel: i64 = connection.query_row("SELECT cancel FROM runs WHERE id = ?1", [run_id], |row| row.get(0))?;
        Ok(cancel == 1)
    }

    pub(crate) fn transaction<T>(
        &self,
        work: impl FnOnce(&rusqlite::Transaction<'_>) -> Result<T, Error>,
    ) -> Result<T, Error> {
        let mut connection = self.lock()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let value = work(&transaction)?;
        crate::failpoint::hit("store.commit");
        transaction.commit()?;
        self.commits.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        Ok(value)
    }
}

pub(crate) fn read_events(
    connection: &Connection,
    run_id: &str,
    after: u64,
    max_bytes: usize,
) -> Result<Vec<Event>, Error> {
    let mut statement =
        connection.prepare_cached("SELECT seq, body FROM events WHERE run_id = ?1 AND seq > ?2 ORDER BY seq")?;
    let after = i64::try_from(after).unwrap_or(i64::MAX);
    let rows =
        statement.query_map(params![run_id, after], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)))?;
    let mut events = Vec::new();
    for (expected, row) in (after + 1..).zip(rows) {
        let (seq, body) = row?;
        if seq != expected {
            return Err(Error::Storage("corrupt history: a gap in the event sequence".into()));
        }
        events.push(Event::decode(body.as_bytes(), max_bytes)?);
    }
    Ok(events)
}
