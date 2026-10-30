//! The Space's chat execution limit (ADR-0119): how many chat turns the Space runs at once.
//!
//! Team owns it, as one owner-only record in the private inference volume. It is read once and then replaced only by a
//! committed save, so every admission compares against the same current value; an absent record is the default. A
//! record that is unreadable or not this store's own fails closed: no chat turn starts until a save replaces it or the
//! Space is reset.

use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard, PoisonError};

use strict_json::{Number, Value};
use team_protocol::payload::canonical_chat_limit;
use team_protocol::python::value::{int, object_of};
use team_storage::json_record;

use crate::coordination::executions::{ChatLimit, LimitUnavailable};

/// The record's schema.
pub const SCHEMA: i64 = 1;
/// The limit without a record.
pub const DEFAULT: usize = 2;
/// The record's bound.
pub const MAX_RECORD_BYTES: usize = 256;
/// The record's name in the inference directory.
pub const NAME: &str = "chat-limit.json";

/// The current limit, loaded once and replaced only by a committed save.
#[derive(Debug)]
pub struct ChatLimitStore {
    path: PathBuf,
    current: Mutex<Option<Result<usize, LimitUnavailable>>>,
}

fn load(path: &Path) -> Result<usize, LimitUnavailable> {
    let value = json_record::read(path, MAX_RECORD_BYTES).map_err(|_| LimitUnavailable)?;
    let Some(value) = value else {
        return Ok(DEFAULT);
    };
    let Value::Object(members) = &value else {
        return Err(LimitUnavailable);
    };
    let schema = value.get("schema");
    let limit = value.get("limit");
    let (Some(Value::Number(Number::Int(schema))), Some(limit), 2) = (schema, limit, members.len())
    else {
        return Err(LimitUnavailable);
    };
    let admitted = canonical_chat_limit(&object_of(vec![("limit", limit.clone())]));
    match (admitted, *schema == SCHEMA, limit) {
        (Some(_), true, Value::Number(Number::Int(limit))) => {
            usize::try_from(*limit).map_err(|_| LimitUnavailable)
        }
        _ => Err(LimitUnavailable),
    }
}

impl ChatLimitStore {
    /// The store of `inference/chat-limit.json` below `root`.
    #[must_use]
    pub fn new(root: &Path) -> Self {
        Self {
            path: root.join(NAME),
            current: Mutex::new(None),
        }
    }

    fn guard(&self) -> MutexGuard<'_, Option<Result<usize, LimitUnavailable>>> {
        self.current.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The current limit, or [`LimitUnavailable`] while its record is unreadable or not the store's own.
    ///
    /// # Errors
    /// [`LimitUnavailable`] for a record that fails closed.
    pub fn state(&self) -> Result<usize, LimitUnavailable> {
        let mut current = self.guard();
        *current.get_or_insert_with(|| load(&self.path))
    }

    /// Forgets the limit for a Space reset; the next admission reads the default.
    ///
    /// # Errors
    /// [`LimitUnavailable`] when the record could not be removed; the loaded limit stays.
    pub fn delete(&self) -> Result<(), LimitUnavailable> {
        let mut current = self.guard();
        match std::fs::remove_file(&self.path) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return Err(LimitUnavailable),
        }
        *current = None;
        Ok(())
    }
}

impl ChatLimit for ChatLimitStore {
    /// The current limit; an unreadable record admits no chat turn.
    fn current(&self) -> usize {
        self.state().unwrap_or(0)
    }

    /// Durably replaces the limit; admissions see it once the record is committed.
    fn save(&self, limit: u8) -> Result<u8, LimitUnavailable> {
        let mut current = self.guard();
        let record = object_of(vec![("schema", int(SCHEMA)), ("limit", int(limit.into()))]);
        json_record::write(&self.path, &record, MAX_RECORD_BYTES).map_err(|_| LimitUnavailable)?;
        *current = Some(Ok(usize::from(limit)));
        Ok(limit)
    }
}

#[cfg(test)]
mod tests;
