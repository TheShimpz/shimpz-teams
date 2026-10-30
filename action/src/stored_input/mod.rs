//! Encrypted Team custody of persistent Assistant-declared Action inputs (`action/stored_input.py`, ADR-0059).
//!
//! A Stored Input value never enters a workload: Team seals it once a person supplies it, injects it only into the
//! provider calls its declaration names (ADR-0106), and projects only its declared identifier and status. Each record
//! seals `{"origin","value"}` in an identified envelope whose associated data binds the Team, Assistant, Stored Input,
//! kind, and generation: `["shimpz-stored-input-v1", team, assistant, id, kind, generation]` in canonical JSON.

use std::path::PathBuf;
use std::sync::Arc;

use strict_json::{Number, Value};
use team_core::clock::Clock;
use team_core::entropy::Entropy;
use team_core::secret::Secret;
use team_integrations::challenge::team_id;
use team_integrations::providers::identifier;
use team_integrations::text::public_chars;
use team_protocol::content::SHA256_RE;
use team_protocol::python::json::{self, Options};
use team_storage::envelope::{Bounds, Envelope};
use team_storage::private::{self, StateError};
use team_storage::record_store::{Outcome, RecordPolicy, RecordStore, State};

/// The largest state file.
pub const MAX_STATE_BYTES: usize = 4 * 1024 * 1024;
/// The longest value, in characters.
pub const MAX_VALUE_CHARACTERS: usize = 1024;
/// The longest value, in UTF-8 bytes.
pub const MAX_VALUE_BYTES: usize = 16 * 1024;
/// The largest sealed plaintext.
pub const MAX_PLAINTEXT_BYTES: usize = MAX_VALUE_BYTES + 160;
/// The most Stored Inputs one Assistant of one Team holds.
pub const MAX_STORED_INPUTS_PER_ASSISTANT: usize = 8;
/// The most records the whole store holds.
pub const MAX_TOTAL_RECORDS: usize = 4096;
/// The one kind a Stored Input has.
pub const KIND: &str = "password";
const AAD_LABEL: &str = "shimpz-stored-input-v1";
const RECORD_FIELDS: [&str; 4] = ["envelope", "generation", "kind", "updated_at"];
const BOUNDS: Bounds = Bounds {
    maximum_encoded_part: MAX_PLAINTEXT_BYTES * 2 + 128,
    maximum_plaintext: MAX_PLAINTEXT_BYTES,
};
const PLAINTEXT_JSON: Options = Options {
    ensure_ascii: false,
    sort_keys: false,
    compact: true,
};

/// Why a Stored Input operation was refused; never carries a value, a path, or file content.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StoredInputError {
    /// The caller supplied an invalid id, kind, value, origin, or declaration.
    Invalid,
    /// The Stored Input has no value.
    Missing,
    /// The Assistant already holds its most Stored Inputs.
    Capacity,
    /// The state, keyring, or an envelope is unavailable, unsafe, malformed, or unauthentic.
    State(StateError),
}

impl From<StateError> for StoredInputError {
    fn from(error: StateError) -> Self {
        Self::State(error)
    }
}

impl std::fmt::Display for StoredInputError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Invalid => formatter.write_str("the Stored Input data is invalid"),
            Self::Missing => formatter.write_str("the Stored Input is not configured"),
            Self::Capacity => formatter.write_str("the Stored Input capacity is reached"),
            Self::State(error) => error.fmt(formatter),
        }
    }
}

impl std::error::Error for StoredInputError {}

/// One decrypted value; its `Debug` never shows it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoredValue {
    /// The value.
    pub value: Secret,
    /// Its store-wide generation.
    pub generation: u64,
    /// The SHA-256 origin of the Action interrupt that consumed it.
    pub origin: String,
}

/// One declared Stored Input's public fields.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Declaration {
    /// Its kind (`password`).
    pub kind: String,
    /// Its public label (at most 80 characters).
    pub label: String,
    /// Its public description (at most 400 characters).
    pub description: String,
}

/// Whether a declared Stored Input holds a value.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    /// No value.
    Missing,
    /// A sealed value.
    Stored,
}

impl Status {
    /// Its wire text.
    #[must_use]
    pub fn text(self) -> &'static str {
        match self {
            Self::Missing => "missing",
            Self::Stored => "stored",
        }
    }
}

/// One declared Stored Input's secret-free inventory row.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Metadata {
    /// The Stored Input id.
    pub id: String,
    /// Its kind.
    pub kind: String,
    /// Its label.
    pub label: String,
    /// Its description.
    pub description: String,
    /// Whether it holds a value.
    pub status: Status,
    /// Its generation, 0 when missing.
    pub generation: u64,
}

/// The record policy of the Stored Input store.
#[derive(Clone, Copy, Debug, Default)]
pub struct Policy;

fn assistant_id(text: &str) -> bool {
    team_protocol::identifiers::canonical_assistant_id(&Value::String(text.to_owned())).is_some()
}

impl RecordPolicy for Policy {
    fn maximum_state_bytes(&self) -> usize {
        MAX_STATE_BYTES
    }

    fn records_per_assistant(&self) -> usize {
        MAX_STORED_INPUTS_PER_ASSISTANT
    }

    fn maximum_records(&self) -> usize {
        MAX_TOTAL_RECORDS
    }

    fn team_id(&self, id: &str) -> bool {
        team_id(id)
    }

    fn assistant_id(&self, id: &str) -> bool {
        assistant_id(id)
    }

    fn record_id(&self, id: &str) -> bool {
        identifier(id)
    }

    fn record_generation(&self, record: &Value) -> Option<u64> {
        admit_record(record)
    }
}

/// Admits one stored record: exactly its four fields, the kind, a positive generation, a record time, and an
/// identified envelope within bounds; its generation.
fn admit_record(record: &Value) -> Option<u64> {
    let Value::Object(members) = record else {
        return None;
    };
    let mut names: Vec<&str> = members.iter().map(|(name, _)| name.as_str()).collect();
    names.sort_unstable();
    let generation = match record.get("generation") {
        Some(Value::Number(Number::Int(generation))) => u64::try_from(*generation)
            .ok()
            .filter(|value| *value >= 1)?,
        _ => return None,
    };
    let valid = names == RECORD_FIELDS
        && record.get("kind") == Some(&Value::String(KIND.to_owned()))
        && matches!(record.get("updated_at"), Some(Value::String(text)) if private::is_timestamp(text))
        && record
            .get("envelope")
            .is_some_and(|envelope| Envelope::admit_identified(envelope, BOUNDS).is_ok());
    valid.then_some(generation)
}

fn int(value: u64) -> Value {
    Value::Number(Number::Int(i64::try_from(value).unwrap_or(i64::MAX)))
}

fn aad(reference: (&str, &str, &str), generation: u64) -> Vec<u8> {
    json::canonical(&Value::Array(vec![
        Value::String(AAD_LABEL.to_owned()),
        Value::String(reference.0.to_owned()),
        Value::String(reference.1.to_owned()),
        Value::String(reference.2.to_owned()),
        Value::String(KIND.to_owned()),
        int(generation),
    ]))
}

fn admit_reference<'a>(
    team: &'a str,
    assistant: &'a str,
    id: &'a str,
) -> Result<(&'a str, &'a str, &'a str), StoredInputError> {
    if team_id(team) && assistant_id(assistant) && identifier(id) {
        Ok((team, assistant, id))
    } else {
        Err(StoredInputError::Invalid)
    }
}

fn admit_kind(kind: &str) -> Result<(), StoredInputError> {
    if kind == KIND {
        Ok(())
    } else {
        Err(StoredInputError::Invalid)
    }
}

/// Whether `value` is a non-empty value of at most 1,024 characters and 16 KiB.
fn valid_value(value: &str) -> bool {
    !value.is_empty()
        && value.chars().count() <= MAX_VALUE_CHARACTERS
        && value.len() <= MAX_VALUE_BYTES
}

fn stored<'a>(state: &'a State, reference: (&str, &str, &str)) -> Option<&'a Value> {
    state
        .records(reference.0, reference.1)?
        .iter()
        .find(|(id, _)| id == reference.2)
        .map(|(_, record)| record)
}

/// The encrypted Stored Input store.
pub struct StoredInputStore {
    records: RecordStore<Policy>,
    clock: Arc<dyn Clock>,
}

impl std::fmt::Debug for StoredInputStore {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("StoredInputStore")
            .field("records", &self.records)
            .finish_non_exhaustive()
    }
}

impl StoredInputStore {
    /// The store whose state lives at `state_path` and keyring at `key_path`.
    ///
    /// # Errors
    /// [`StoredInputError::State`] unless both paths are absolute and their directories differ.
    pub fn new(
        state_path: PathBuf,
        key_path: PathBuf,
        clock: Arc<dyn Clock>,
        entropy: Arc<dyn Entropy>,
    ) -> Result<Self, StoredInputError> {
        Ok(Self {
            records: RecordStore::new(state_path, key_path, Policy, entropy)?,
            clock,
        })
    }

    /// Seals a consumed value for exactly the Action interrupt `origin` names, advancing its generation atomically.
    ///
    /// # Errors
    /// [`StoredInputError::Invalid`] for invalid input, [`StoredInputError::Capacity`] for a full Assistant, or a
    /// state failure.
    pub fn seal(
        &self,
        reference: (&str, &str, &str),
        kind: &str,
        value: &Secret,
        origin: &str,
    ) -> Result<u64, StoredInputError> {
        let reference = admit_reference(reference.0, reference.1, reference.2)?;
        admit_kind(kind)?;
        if !valid_value(value.expose()) || !SHA256_RE.is_full(origin) {
            return Err(StoredInputError::Invalid);
        }
        let plaintext = plaintext(value, origin)?;
        self.records.update(|state| {
            let key = self.records.key(!state.has_records())?;
            let full = state
                .records(reference.0, reference.1)
                .is_some_and(|records| {
                    records.len() >= MAX_STORED_INPUTS_PER_ASSISTANT
                        && !records.iter().any(|(id, _)| id == reference.2)
                });
            if full {
                return Err(StoredInputError::Capacity);
            }
            let generation = state.advance_generation();
            let envelope = Envelope::seal_identified(
                &key,
                &plaintext,
                &aad(reference, generation),
                self.records.entropy(),
            )
            .map_err(|_| StoredInputError::State(StateError::Unavailable))?;
            let record = Value::Object(vec![
                ("kind".to_owned(), Value::String(KIND.to_owned())),
                ("generation".to_owned(), int(generation)),
                (
                    "updated_at".to_owned(),
                    Value::String(private::timestamp(self.clock.as_ref())),
                ),
                ("envelope".to_owned(), envelope.to_value()),
            ]);
            let records = state.records_mut(reference.0, reference.1);
            match records.iter_mut().find(|(id, _)| id == reference.2) {
                Some((_, existing)) => *existing = record,
                None => records.push((reference.2.to_owned(), record)),
            }
            Ok(Outcome::Changed(generation))
        })
    }

    /// Decrypts one exact declared value without reading any peer.
    ///
    /// # Errors
    /// [`StoredInputError::Missing`] when it holds no value; [`StoredInputError::State`] when it does not open.
    pub fn resolve(
        &self,
        reference: (&str, &str, &str),
        kind: &str,
    ) -> Result<StoredValue, StoredInputError> {
        let reference = admit_reference(reference.0, reference.1, reference.2)?;
        admit_kind(kind)?;
        self.records.read(|state| {
            let record = stored(state, reference).ok_or(StoredInputError::Missing)?;
            let generation =
                admit_record(record).ok_or(StoredInputError::State(StateError::Malformed))?;
            let envelope = record
                .get("envelope")
                .and_then(|value| Envelope::admit_identified(value, BOUNDS).ok())
                .ok_or(StoredInputError::State(StateError::Malformed))?;
            let key = self.records.key(false)?;
            let plaintext = envelope
                .open(&key, &aad(reference, generation))
                .map_err(|_| StoredInputError::State(StateError::Unauthentic))?;
            let (value, origin) =
                decrypted(&plaintext).ok_or(StoredInputError::State(StateError::Malformed))?;
            Ok(StoredValue {
                value,
                generation,
                origin,
            })
        })
    }

    /// Every declared Stored Input's status, without decrypting or returning any value.
    ///
    /// # Errors
    /// [`StoredInputError::Invalid`] for invalid declarations, or a state failure.
    pub fn metadata(
        &self,
        team: &str,
        assistant: &str,
        declarations: &[(String, Declaration)],
    ) -> Result<Vec<Metadata>, StoredInputError> {
        if !team_id(team) || !assistant_id(assistant) {
            return Err(StoredInputError::Invalid);
        }
        admit_declarations(declarations)?;
        self.records.read(|state| {
            declarations
                .iter()
                .map(|(id, declaration)| {
                    let generation = match stored(state, (team, assistant, id)) {
                        None => None,
                        Some(record) => Some(
                            admit_record(record)
                                .ok_or(StoredInputError::State(StateError::Malformed))?,
                        ),
                    };
                    Ok(Metadata {
                        id: id.clone(),
                        kind: declaration.kind.clone(),
                        label: declaration.label.clone(),
                        description: declaration.description.clone(),
                        status: if generation.is_some() {
                            Status::Stored
                        } else {
                            Status::Missing
                        },
                        generation: generation.unwrap_or(0),
                    })
                })
                .collect()
        })
    }

    /// The Team's inventory: every declared Stored Input of each Assistant with its status, sorted, without values
    /// or generations: `{"team_id", "stored_inputs": [{"assistant_id", "stored_input_id", "status"}]}`.
    ///
    /// # Errors
    /// [`StoredInputError::Invalid`] for an invalid or repeated Assistant, or as [`StoredInputStore::metadata`].
    pub fn inventory(
        &self,
        team: &str,
        assistants: &[(String, Vec<(String, Declaration)>)],
    ) -> Result<Value, StoredInputError> {
        let mut rows: Vec<(String, String, Status)> = Vec::new();
        for (index, (assistant, declarations)) in assistants.iter().enumerate() {
            if assistants[..index]
                .iter()
                .any(|(seen, _)| seen == assistant)
            {
                return Err(StoredInputError::Invalid);
            }
            for row in self.metadata(team, assistant, declarations)? {
                rows.push((assistant.clone(), row.id, row.status));
            }
        }
        rows.sort_by(|left, right| (&left.0, &left.1).cmp(&(&right.0, &right.1)));
        let text = |value: &str| Value::String(value.to_owned());
        let listing = rows
            .into_iter()
            .map(|(assistant, id, status)| {
                Value::Object(vec![
                    ("assistant_id".to_owned(), text(&assistant)),
                    ("stored_input_id".to_owned(), text(&id)),
                    ("status".to_owned(), text(status.text())),
                ])
            })
            .collect();
        Ok(Value::Object(vec![
            ("team_id".to_owned(), text(team)),
            ("stored_inputs".to_owned(), Value::Array(listing)),
        ]))
    }

    /// Deletes one exact value; whether it held one.
    ///
    /// # Errors
    /// [`StoredInputError::Invalid`] for an invalid id, or a state failure.
    pub fn delete(&self, reference: (&str, &str, &str)) -> Result<bool, StoredInputError> {
        let (team, assistant, id) = admit_reference(reference.0, reference.1, reference.2)?;
        self.records.update(|state| {
            if stored(state, (team, assistant, id)).is_none() {
                return Ok(Outcome::Unchanged(false));
            }
            state
                .records_mut(team, assistant)
                .retain(|(stored, _)| stored != id);
            state.prune_empty(team, assistant);
            Ok(Outcome::Changed(true))
        })
    }

    /// Discards the values an Assistant's current release no longer declares; whether any were.
    ///
    /// # Errors
    /// A state failure.
    pub fn retain_declared(
        &self,
        team: &str,
        assistant: &str,
        declared: &[&str],
    ) -> Result<bool, StoredInputError> {
        Ok(self.records.retain_declared(team, assistant, declared)?)
    }

    /// Deletes one Assistant's values of one Team; whether it had any.
    ///
    /// # Errors
    /// A state failure.
    pub fn delete_assistant(&self, team: &str, assistant: &str) -> Result<bool, StoredInputError> {
        Ok(self.records.delete_assistant(team, assistant)?)
    }

    /// Deletes one Team's values; whether it had any.
    ///
    /// # Errors
    /// A state failure.
    pub fn delete_team(&self, team: &str) -> Result<bool, StoredInputError> {
        Ok(self.records.delete_team(team)?)
    }

    /// Deletes every value during an owned Space reset; whether any existed.
    ///
    /// # Errors
    /// A state failure.
    pub fn delete_all(&self) -> Result<bool, StoredInputError> {
        Ok(self.records.delete_all()?)
    }
}

/// The sealed plaintext `{"origin","value"}`: compact, unsorted, UTF-8.
fn plaintext(
    value: &Secret,
    origin: &str,
) -> Result<zeroize::Zeroizing<Vec<u8>>, StoredInputError> {
    let document = Value::Object(vec![
        ("origin".to_owned(), Value::String(origin.to_owned())),
        ("value".to_owned(), Value::String(value.expose().to_owned())),
    ]);
    let plaintext = zeroize::Zeroizing::new(json::dumps(&document, PLAINTEXT_JSON).into_bytes());
    if plaintext.len() > MAX_PLAINTEXT_BYTES {
        return Err(StoredInputError::Invalid);
    }
    Ok(plaintext)
}

fn admit_declarations(declarations: &[(String, Declaration)]) -> Result<(), StoredInputError> {
    if declarations.len() > MAX_STORED_INPUTS_PER_ASSISTANT {
        return Err(StoredInputError::Invalid);
    }
    for (index, (id, declaration)) in declarations.iter().enumerate() {
        let valid = identifier(id)
            && !declarations[..index].iter().any(|(seen, _)| seen == id)
            && declaration.kind == KIND
            && public_chars(&declaration.label, 80)
            && public_chars(&declaration.description, 400);
        if !valid {
            return Err(StoredInputError::Invalid);
        }
    }
    Ok(())
}

/// The value and origin a decrypted plaintext holds, or `None` for anything else.
fn decrypted(plaintext: &[u8]) -> Option<(Secret, String)> {
    if plaintext.len() > MAX_PLAINTEXT_BYTES {
        return None;
    }
    let value = strict_json::parse(plaintext).ok()?;
    let Value::Object(members) = &value else {
        return None;
    };
    let mut names: Vec<&str> = members.iter().map(|(name, _)| name.as_str()).collect();
    names.sort_unstable();
    match (names.as_slice(), value.get("value"), value.get("origin")) {
        (["origin", "value"], Some(Value::String(secret)), Some(Value::String(origin)))
            if valid_value(secret) && SHA256_RE.is_full(origin) =>
        {
            Some((Secret::new(secret.clone()), origin.clone()))
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests;
