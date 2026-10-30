//! Whole-generation operations: inspection, archive markers, discard, and purge.

use rusqlite::{OptionalExtension, params};
use team_protocol::content::SHA256_RE;

use super::handle::safe_id;
use super::{Journal, JournalError, OperationRecord, read, write};

impl Journal {
    fn header(&self, generation: &str) -> Result<Option<(String, String, i64)>, JournalError> {
        let generation = safe_id(generation, "generation")?;
        self.with(|connection| {
            read(
                connection
                    .query_row(
                        "SELECT fingerprint, state, archivable FROM batches WHERE generation = ?",
                        [generation],
                        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                    )
                    .optional(),
                "Action journal state could not be read",
            )
            .map_err(|_| JournalError::Failure("Action journal state could not be read"))
        })
    }

    /// The compact state of one live batch's operations, which its incident records before it is archived.
    ///
    /// # Errors
    /// [`JournalError::Conflict`] when the batch is no longer current, [`JournalError::Archived`] when it is archived,
    /// [`JournalError::Failure`] when the journal cannot be read.
    pub fn snapshot(
        &self,
        generation: &str,
        fingerprint: &str,
    ) -> Result<Vec<OperationRecord>, JournalError> {
        let header = self.header(generation)?;
        match header {
            Some((current, state, _)) if current == fingerprint => {
                if state == "archived" {
                    return Err(JournalError::Archived);
                }
            }
            _ => return Err(JournalError::Conflict("Action batch is no longer current")),
        }
        self.with(|connection| {
            let failure = |_| JournalError::Failure("Action journal state could not be read");
            let mut statement = connection
                .prepare(
                    "SELECT interrupt_id, operation_id, state, attempts, origin
                     FROM operations WHERE generation = ? ORDER BY ordinal",
                )
                .map_err(failure)?;
            statement
                .query_map([generation], |row| {
                    Ok(OperationRecord {
                        interrupt_id: row.get(0)?,
                        operation_id: row.get(1)?,
                        state: row.get(2)?,
                        attempts: row.get(3)?,
                        origin: row.get(4)?,
                    })
                })
                .and_then(Iterator::collect)
                .map_err(failure)
        })
    }

    /// Marks a held batch archived and removes its operation rows in one commit; an archived one stays as it is. Its
    /// incident must already be durable, since afterwards only the marker remains.
    ///
    /// # Errors
    /// [`JournalError::Conflict`] when the batch is no longer current or holds no archive reservation.
    pub fn archive(&self, generation: &str, fingerprint: &str) -> Result<(), JournalError> {
        let generation = safe_id(generation, "generation")?;
        let failure = "Action batch could not be archived";
        self.writing(failure, |connection| {
            let row: Option<(String, String, i64)> = write(
                connection
                    .query_row(
                        "SELECT fingerprint, state, archivable FROM batches WHERE generation = ?",
                        [generation],
                        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                    )
                    .optional(),
                failure,
            )?;
            let Some((current, state, archivable)) = row.filter(|(current, _, _)| current == fingerprint) else {
                return Err(JournalError::Conflict("Action batch is no longer current"));
            };
            if state == "archived" {
                return Ok(());
            }
            if archivable != 1 {
                return Err(JournalError::Conflict("Action batch holds no archive reservation"));
            }
            let changed = write(
                connection.execute(
                    "UPDATE batches SET state = 'archived' WHERE generation = ? AND fingerprint = ?",
                    params![generation, current],
                ),
                failure,
            )?;
            super::require_changed(changed, "Action batch changed before it was archived")?;
            write(connection.execute("DELETE FROM operations WHERE generation = ?", [generation]), failure)?;
            Ok(())
        })
    }

    /// Removes a settled archive marker once no resumable state needs it; an already-absent one is done.
    ///
    /// # Errors
    /// [`JournalError::Conflict`] when the generation holds anything else.
    pub fn release_archive(&self, generation: &str, fingerprint: &str) -> Result<(), JournalError> {
        let generation = safe_id(generation, "generation")?;
        let failure = "Action archive could not be released";
        self.writing(failure, |connection| {
            let row: Option<(String, String)> = write(
                connection
                    .query_row(
                        "SELECT fingerprint, state FROM batches WHERE generation = ?",
                        [generation],
                        |row| Ok((row.get(0)?, row.get(1)?)),
                    )
                    .optional(),
                failure,
            )?;
            match row {
                None => Ok(()),
                Some((current, state)) if current == fingerprint && state == "archived" => {
                    write(
                        connection
                            .execute("DELETE FROM batches WHERE generation = ?", [generation]),
                        failure,
                    )?;
                    Ok(())
                }
                Some(_) => Err(JournalError::Conflict(
                    "Action generation holds no such archived batch",
                )),
            }
        })
    }

    fn remove(
        &self,
        failure: &'static str,
        statement: &str,
        generation: &str,
        fingerprint: Option<&str>,
    ) -> Result<(), JournalError> {
        let generation = safe_id(generation, "generation")?;
        self.writing(failure, |connection| {
            let deleted = match fingerprint {
                Some(fingerprint) => {
                    connection.execute(statement, params![generation, fingerprint])
                }
                None => connection.execute(statement, [generation]),
            };
            write(deleted, failure).map(|_| ())
        })
    }

    /// Removes an ended run's live batch, keeping an archive marker that unresolved evidence still needs.
    ///
    /// # Errors
    /// A storage failure.
    pub fn discard(&self, generation: &str) -> Result<(), JournalError> {
        let statement = "DELETE FROM batches WHERE generation = ? AND state != 'archived'";
        self.remove(
            "Action generation could not be discarded",
            statement,
            generation,
            None,
        )
    }

    /// Removes everything a generation holds, archive marker included, as deleting its Team does.
    ///
    /// # Errors
    /// A storage failure.
    pub fn purge(&self, generation: &str) -> Result<(), JournalError> {
        self.remove(
            "Action generation could not be purged",
            "DELETE FROM batches WHERE generation = ?",
            generation,
            None,
        )
    }

    /// Removes exactly one batch, whatever its state, as ending its paused turn does; an absent one is done. A newer
    /// batch of the same generation belongs to a later turn and stays.
    ///
    /// # Errors
    /// [`JournalError::Conflict`] for an invalid fingerprint, or a storage failure.
    pub fn purge_batch(&self, generation: &str, fingerprint: &str) -> Result<(), JournalError> {
        if !SHA256_RE.is_full(fingerprint) {
            return Err(JournalError::Conflict(
                "Action batch fingerprint is invalid",
            ));
        }
        let statement = "DELETE FROM batches WHERE generation = ? AND fingerprint = ?";
        self.remove(
            "Action batch could not be purged",
            statement,
            generation,
            Some(fingerprint),
        )
    }

    /// The fingerprint and state (`open`, `ended`, or `archived`) of a generation's batch, if it holds one.
    ///
    /// # Errors
    /// [`JournalError::Failure`] when the journal cannot be read.
    pub fn current_batch(
        &self,
        generation: &str,
    ) -> Result<Option<(String, String)>, JournalError> {
        Ok(self
            .header(generation)?
            .map(|(fingerprint, state, _)| (fingerprint, state)))
    }

    /// The fingerprint of a generation's batch when one of its operations may have acted.
    ///
    /// # Errors
    /// [`JournalError::Failure`] when the journal cannot be read.
    pub fn uncertain_fingerprint(&self, generation: &str) -> Result<Option<String>, JournalError> {
        let generation = safe_id(generation, "generation")?;
        self.with(|connection| {
            connection
                .query_row(
                    "SELECT b.fingerprint FROM batches AS b JOIN operations AS o ON o.generation = b.generation
                     WHERE b.generation = ? AND o.state = 'executing' LIMIT 1",
                    [generation],
                    |row| row.get(0),
                )
                .optional()
                .map_err(|_| JournalError::Failure("Action journal state could not be read"))
        })
    }
}
