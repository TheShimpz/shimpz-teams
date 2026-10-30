//! Batch and operation handles: their identities, their fingerprint, and the proof that a stored batch is the handle's.

use rusqlite::{Connection, OptionalExtension};
use strict_json::Value;
use team_core::entropy::{Entropy, EntropyUnavailable};
use team_protocol::content::SHA256_RE;
use team_protocol::turn::SAFE_ID_RE;

use super::{JournalError, Row, STATES, canonical, read, sha256_hex};

/// One Action's identity in a batch: its interrupt, the fingerprint of its validated request, and, for a permitted
/// retry after proven absence, the logical operation id it repeats.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Operation {
    /// The Brain interrupt id.
    pub interrupt_id: String,
    /// The SHA-256 of the canonical validated request.
    pub fingerprint: String,
    /// The logical operation id a permitted retry repeats; the journal mints one otherwise.
    pub operation_id: Option<String>,
}

/// One Brain suspension's immutable batch handle in one Team generation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Batch {
    /// The generation (the Team's network id, or a Routine run's attempt).
    pub generation: String,
    /// The batch fingerprint.
    pub fingerprint: String,
    /// Its operations in order.
    pub operations: Vec<Operation>,
}

impl Batch {
    /// Refuses an operation that is not this batch's.
    pub(super) fn require(&self, operation: &Operation) -> Result<(), JournalError> {
        if self.operations.contains(operation) {
            Ok(())
        } else {
            Err(JournalError::Conflict(
                "operation does not belong to this Action batch",
            ))
        }
    }
}

/// One random RFC 9562 version 4 UUID in canonical lowercase text.
///
/// # Errors
/// [`EntropyUnavailable`] when no random bytes could be drawn.
pub fn new_operation_id(entropy: &dyn Entropy) -> Result<String, EntropyUnavailable> {
    let mut bytes = [0_u8; 16];
    entropy.fill(&mut bytes)?;
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex = team_core::entropy::token_hex(&Fixed(bytes), 16)?;
    Ok(format!(
        "{}-{}-{}-{}-{}",
        &hex[..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..]
    ))
}

struct Fixed([u8; 16]);

impl Entropy for Fixed {
    fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyUnavailable> {
        bytes.copy_from_slice(&self.0[..bytes.len()]);
        Ok(())
    }
}

/// Whether `text` is a canonical lowercase version 4 UUID.
#[must_use]
pub fn valid_operation_id(text: &str) -> bool {
    let bytes = text.as_bytes();
    bytes.len() == 36
        && bytes.iter().enumerate().all(|(index, byte)| match index {
            8 | 13 | 18 | 23 => *byte == b'-',
            14 => *byte == b'4',
            19 => matches!(byte, b'8' | b'9' | b'a' | b'b'),
            _ => matches!(byte, b'0'..=b'9' | b'a'..=b'f'),
        })
}

pub(super) fn safe_id<'a>(text: &'a str, _label: &str) -> Result<&'a str, JournalError> {
    if SAFE_ID_RE.is_full(text) {
        Ok(text)
    } else {
        Err(JournalError::Conflict(
            "an Action journal identifier is invalid",
        ))
    }
}

fn operation(operation: &Operation) -> Result<(), JournalError> {
    safe_id(&operation.interrupt_id, "operation interrupt id")?;
    if !SHA256_RE.is_full(&operation.fingerprint) {
        return Err(JournalError::Conflict("operation fingerprint is invalid"));
    }
    if operation
        .operation_id
        .as_deref()
        .is_some_and(|id| !valid_operation_id(id))
    {
        return Err(JournalError::Conflict("operation id is invalid"));
    }
    Ok(())
}

/// The batch of `operations`, its fingerprint the SHA-256 of canonical
/// `{"generation", "operations": [[interrupt, fingerprint(, operation_id)]...], "thread": sha256(thread)}`.
pub(super) fn build(
    generation: &str,
    thread_id: &str,
    operations: Vec<Operation>,
    maximum: usize,
) -> Result<Batch, JournalError> {
    let generation = safe_id(generation, "generation")?;
    let thread = safe_id(thread_id, "thread id")?;
    operations.iter().try_for_each(operation)?;
    if operations.is_empty() || operations.len() > maximum {
        return Err(JournalError::Conflict(
            "Action batch exceeds the operation count limit",
        ));
    }
    let repeated = |ids: Vec<&str>| {
        ids.iter()
            .enumerate()
            .any(|(index, id)| ids[..index].contains(id))
    };
    if repeated(
        operations
            .iter()
            .map(|item| item.interrupt_id.as_str())
            .collect(),
    ) {
        return Err(JournalError::Conflict(
            "Action batch repeats an interrupt id",
        ));
    }
    if repeated(
        operations
            .iter()
            .filter_map(|item| item.operation_id.as_deref())
            .collect(),
    ) {
        return Err(JournalError::Conflict(
            "Action batch repeats an operation id",
        ));
    }
    let identity = identity(generation, thread, &operations);
    Ok(Batch {
        generation: generation.to_owned(),
        fingerprint: sha256_hex(&canonical(&identity)),
        operations,
    })
}

/// The canonical identity a batch fingerprint hashes.
fn identity(generation: &str, thread: &str, operations: &[Operation]) -> Value {
    let operations = operations
        .iter()
        .map(|item| {
            let mut parts = vec![
                Value::String(item.interrupt_id.clone()),
                Value::String(item.fingerprint.clone()),
            ];
            parts.extend(item.operation_id.clone().map(Value::String));
            Value::Array(parts)
        })
        .collect();
    Value::Object(vec![
        (
            "generation".to_owned(),
            Value::String(generation.to_owned()),
        ),
        ("operations".to_owned(), Value::Array(operations)),
        (
            "thread".to_owned(),
            Value::String(sha256_hex(thread.as_bytes())),
        ),
    ])
}

/// The stored rows of the handle's batch, proven to be exactly the handle's open batch.
pub(super) fn load(connection: &Connection, batch: &Batch) -> Result<Vec<Row>, JournalError> {
    let failure = "Action journal batch could not be read";
    let header: Option<(String, i64, String)> = read(
        connection
            .query_row(
                "SELECT fingerprint, operation_count, state FROM batches WHERE generation = ?",
                [&batch.generation],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional(),
        failure,
    )?;
    let (fingerprint, count, state) =
        header.ok_or(JournalError::Conflict("Action batch is no longer current"))?;
    if fingerprint == batch.fingerprint && state != "open" {
        return Err(JournalError::Conflict("Action batch has ended"));
    }
    let rows = read(rows(connection, &batch.generation), failure)?;
    let matches = fingerprint == batch.fingerprint
        && usize::try_from(count).is_ok_and(|count| count == batch.operations.len())
        && rows.len() == batch.operations.len()
        && rows
            .iter()
            .zip(&batch.operations)
            .enumerate()
            .all(|(index, (row, expected))| {
                usize::try_from(row.ordinal).is_ok_and(|ordinal| ordinal == index)
                    && row.interrupt_id == expected.interrupt_id
                    && row.fingerprint == expected.fingerprint
                    && STATES.contains(&row.state.as_str())
                    && valid_operation_id(&row.operation_id)
                    && expected
                        .operation_id
                        .as_ref()
                        .is_none_or(|id| *id == row.operation_id)
            });
    if !matches {
        return Err(JournalError::Conflict("Action batch changed or is corrupt"));
    }
    Ok(rows)
}

fn rows(connection: &Connection, generation: &str) -> rusqlite::Result<Vec<Row>> {
    let mut statement = connection.prepare(
        "SELECT ordinal, interrupt_id, fingerprint, state, result, operation_id, origin
         FROM operations WHERE generation = ? ORDER BY ordinal",
    )?;
    statement
        .query_map([generation], |row| {
            Ok(Row {
                ordinal: row.get(0)?,
                interrupt_id: row.get(1)?,
                fingerprint: row.get(2)?,
                state: row.get(3)?,
                result: row.get(4)?,
                operation_id: row.get(5)?,
                origin: row.get(6)?,
            })
        })?
        .collect()
}
