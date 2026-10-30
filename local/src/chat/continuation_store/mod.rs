//! Encrypted, short-lived paused-turn continuations (`local/chat/continuation_store.py`): at most one per Team, in
//! one owner-only state file beside a separate keyring.
//!
//! The state file is `{"schema":1,"records":{team:{team_id,kind,challenge_id,expires_at,generation,bindings,envelope}}}`
//! in canonical ASCII JSON; each record's plaintext is sealed in a plain AES-256-GCM envelope whose associated data is
//! the compact ASCII JSON `["shimpz-local-chat-continuation-v1",team,kind,challenge,expires_at,generation,[bindings]]`.

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use strict_json::{Number, Value};
use team_core::clock::Clock;
use team_core::entropy::Entropy;
use team_protocol::identifiers::TEAM_ID_RE;
use team_protocol::python::json::{Options, dumps};
use team_protocol::python::text;
use team_protocol::python::value::{int, object_of, string};
use team_storage::envelope::{Bounds, Envelope};
use team_storage::private::{
    Keyring, StateError, epoch_seconds, read_private, separate_paths, write_private_json,
};

/// The state's schema.
pub const SCHEMA_VERSION: i64 = 1;
/// The most continuations, one per Team.
pub const MAX_CONTINUATIONS: usize = 32;
/// The largest plaintext.
pub const MAX_PLAINTEXT_BYTES: usize = 256 * 1024;
/// The largest state file.
pub const MAX_STATE_BYTES: usize = 12 * 1024 * 1024;
/// The most release bindings of one record.
pub const MAX_BINDINGS: usize = 64;
/// The longest release binding, in UTF-8 bytes.
pub const MAX_BINDING_BYTES: usize = 640;
/// The longest a continuation lives.
pub const MAX_TTL_SECONDS: i64 = 900;
const KINDS: [&str; 2] = ["human", "integrations"];
const AAD: Options = Options {
    ensure_ascii: true,
    sort_keys: false,
    compact: true,
};
const BOUNDS: Bounds = Bounds {
    maximum_encoded_part: MAX_PLAINTEXT_BYTES * 2 + 128,
    maximum_plaintext: MAX_PLAINTEXT_BYTES,
};

/// Why the continuation state refused.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StoreError {
    /// The state or its keyring is unavailable, unsafe, malformed, or unauthentic.
    Unavailable,
    /// An argument is outside the store's contract.
    Invalid,
    /// The store holds as many continuations as it may.
    Capacity,
    /// The Team's continuation belongs to another challenge.
    NotFound,
}

impl From<StateError> for StoreError {
    fn from(_: StateError) -> Self {
        Self::Unavailable
    }
}

/// One decrypted continuation.
#[derive(Clone, PartialEq, Eq)]
pub struct StoredContinuation {
    /// Its Team.
    pub team_id: String,
    /// `human` or `integrations`.
    pub kind: String,
    /// The challenge it answers.
    pub challenge_id: String,
    /// When it expires, in Unix seconds.
    pub expires_at: i64,
    /// Its Team's record generation.
    pub generation: i64,
    /// Its sorted release bindings.
    pub bindings: Vec<String>,
    /// Its plaintext.
    pub payload: Vec<u8>,
}

impl std::fmt::Debug for StoredContinuation {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("StoredContinuation")
            .field("team_id", &self.team_id)
            .field("kind", &self.kind)
            .field("challenge_id", &self.challenge_id)
            .finish_non_exhaustive()
    }
}

fn challenge_id(text: &str) -> bool {
    text.len() == 32
        && text
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

/// The canonical sorted bindings, or `None`.
fn canonical_bindings(values: &[String]) -> Option<Vec<String>> {
    let admitted = (1..=MAX_BINDINGS).contains(&values.len())
        && values.iter().all(|item| {
            !item.is_empty()
                && text::strip(item) == item
                && text::isprintable(item)
                && item.len() <= MAX_BINDING_BYTES
        });
    let mut sorted = values.to_vec();
    sorted.sort();
    sorted.dedup();
    (admitted && sorted.len() == values.len()).then_some(sorted)
}

fn aad(record: &StoredContinuation) -> Vec<u8> {
    let list = Value::Array(vec![
        Value::String("shimpz-local-chat-continuation-v1".to_owned()),
        Value::String(record.team_id.clone()),
        Value::String(record.kind.clone()),
        Value::String(record.challenge_id.clone()),
        int(record.expires_at),
        int(record.generation),
        Value::Array(record.bindings.iter().cloned().map(Value::String).collect()),
    ]);
    dumps(&list, AAD).into_bytes()
}

fn exact_int(value: Option<&Value>) -> Option<i64> {
    match value {
        Some(Value::Number(Number::Int(number))) => Some(*number),
        _ => None,
    }
}

const RECORD_FIELDS: [&str; 7] = [
    "team_id",
    "kind",
    "challenge_id",
    "expires_at",
    "generation",
    "bindings",
    "envelope",
];

/// The record's text members and canonical bindings, when present.
fn record_texts(value: &Value) -> Option<(String, String, String, Vec<String>)> {
    let text = |name: &str| value.get(name).and_then(string).map(str::to_owned);
    let bindings: Option<Vec<String>> = match value.get("bindings") {
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| string(item).map(str::to_owned))
            .collect(),
        _ => None,
    };
    Some((
        text("team_id")?,
        text("kind")?,
        text("challenge_id")?,
        canonical_bindings(&bindings?)?,
    ))
}

/// One admitted record's metadata (its payload empty) and envelope.
fn record(value: &Value, team: &str) -> Result<(StoredContinuation, Envelope), StoreError> {
    let Value::Object(members) = value else {
        return Err(StoreError::Unavailable);
    };
    let shaped = members.len() == RECORD_FIELDS.len()
        && members
            .iter()
            .all(|(name, _)| RECORD_FIELDS.contains(&name.as_str()));
    let (team_id, kind, challenge, bindings) =
        record_texts(value).ok_or(StoreError::Unavailable)?;
    let expires_at = exact_int(value.get("expires_at")).filter(|at| *at >= 1);
    let generations = 1..=i64::from(i32::MAX);
    let generation =
        exact_int(value.get("generation")).filter(|generation| generations.contains(generation));
    let envelope = value
        .get("envelope")
        .and_then(|envelope| Envelope::admit(envelope, BOUNDS).ok());
    let identified =
        shaped && team_id == team && KINDS.contains(&kind.as_str()) && challenge_id(&challenge);
    let (true, Some(expires_at), Some(generation), Some(envelope)) =
        (identified, expires_at, generation, envelope)
    else {
        return Err(StoreError::Unavailable);
    };
    let stored = StoredContinuation {
        team_id,
        kind,
        challenge_id: challenge,
        expires_at,
        generation,
        bindings,
        payload: Vec::new(),
    };
    Ok((stored, envelope))
}

fn record_value(record: &StoredContinuation, envelope: &Envelope) -> Value {
    object_of(vec![
        ("team_id", Value::String(record.team_id.clone())),
        ("kind", Value::String(record.kind.clone())),
        ("challenge_id", Value::String(record.challenge_id.clone())),
        ("expires_at", int(record.expires_at)),
        ("generation", int(record.generation)),
        (
            "bindings",
            Value::Array(record.bindings.iter().cloned().map(Value::String).collect()),
        ),
        ("envelope", envelope.to_value()),
    ])
}

/// The admitted records by Team, in stored order.
type Records = Vec<(String, Value)>;

fn admitted_state(value: &Value) -> Result<Records, StoreError> {
    let Value::Object(members) = value else {
        return Err(StoreError::Unavailable);
    };
    let shaped = members.len() == 2 && exact_int(value.get("schema")) == Some(SCHEMA_VERSION);
    let Some(Value::Object(records)) = value.get("records") else {
        return Err(StoreError::Unavailable);
    };
    if !shaped || records.len() > MAX_CONTINUATIONS {
        return Err(StoreError::Unavailable);
    }
    for (team, item) in records {
        if !TEAM_ID_RE.is_full(team) {
            return Err(StoreError::Unavailable);
        }
        record(item, team)?;
    }
    Ok(records.clone())
}

/// At most one encrypted continuation per Team, kept atomically.
pub struct ContinuationStore {
    state_path: PathBuf,
    keyring: Keyring,
    clock: Arc<dyn Clock>,
    entropy: Arc<dyn Entropy>,
    capacity: usize,
    lock: Mutex<()>,
}

impl std::fmt::Debug for ContinuationStore {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("ContinuationStore")
    }
}

impl ContinuationStore {
    /// The store of `state` (`continuations.json`) and the keyring `key` (`aes256.key`) beside it.
    ///
    /// # Errors
    /// [`StoreError::Invalid`] when the keyring is not separate from the state, or the capacity is out of range.
    pub fn new(
        state: &Path,
        key: &Path,
        clock: Arc<dyn Clock>,
        entropy: Arc<dyn Entropy>,
        capacity: usize,
    ) -> Result<Self, StoreError> {
        separate_paths(state, key).map_err(|_| StoreError::Invalid)?;
        if !(1..=MAX_CONTINUATIONS).contains(&capacity) {
            return Err(StoreError::Invalid);
        }
        Ok(Self {
            state_path: state.to_path_buf(),
            keyring: Keyring::new(key.to_path_buf()),
            clock,
            entropy,
            capacity,
            lock: Mutex::new(()),
        })
    }

    fn guard(&self) -> MutexGuard<'_, ()> {
        self.lock.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn read(&self) -> Result<Records, StoreError> {
        let Some(payload) = read_private(&self.state_path, MAX_STATE_BYTES as u64)? else {
            return Ok(Vec::new());
        };
        let value = strict_json::parse(&payload).map_err(|_| StoreError::Unavailable)?;
        admitted_state(&value)
    }

    fn write(&self, records: Records) -> Result<(), StoreError> {
        let state = object_of(vec![
            ("schema", int(SCHEMA_VERSION)),
            ("records", Value::Object(records)),
        ]);
        admitted_state(&state)?;
        write_private_json(&self.state_path, &state, MAX_STATE_BYTES)?;
        Ok(())
    }

    fn now(&self) -> i64 {
        epoch_seconds(self.clock.as_ref())
    }

    /// Seals `payload` as the Team's one continuation, replacing an earlier one under the next generation.
    ///
    /// # Errors
    /// [`StoreError::Invalid`] for an argument outside the contract, [`StoreError::Capacity`] for a full store, and
    /// [`StoreError::Unavailable`] when the state cannot be read, sealed, or written.
    pub fn put(
        &self,
        team_id: &str,
        kind: &str,
        challenge: &str,
        expires_at: i64,
        bindings: &[String],
        payload: &[u8],
    ) -> Result<StoredContinuation, StoreError> {
        let now = self.now();
        let bindings = canonical_bindings(bindings).ok_or(StoreError::Invalid)?;
        let valid = TEAM_ID_RE.is_full(team_id)
            && KINDS.contains(&kind)
            && challenge_id(challenge)
            && now < expires_at
            && expires_at <= now + MAX_TTL_SECONDS
            && (1..=MAX_PLAINTEXT_BYTES).contains(&payload.len());
        if !valid {
            return Err(StoreError::Invalid);
        }
        let _guard = self.guard();
        let mut records = self.read()?;
        let previous = records.iter().position(|(team, _)| team == team_id);
        if previous.is_none() && records.len() >= self.capacity {
            return Err(StoreError::Capacity);
        }
        let generation = match previous {
            Some(index) => record(&records[index].1, team_id)?.0.generation + 1,
            None => 1,
        };
        let stored = StoredContinuation {
            team_id: team_id.to_owned(),
            kind: kind.to_owned(),
            challenge_id: challenge.to_owned(),
            expires_at,
            generation,
            bindings,
            payload: payload.to_vec(),
        };
        let key = self
            .keyring
            .load(records.is_empty(), self.entropy.as_ref())?;
        let envelope = Envelope::seal(&key, payload, &aad(&stored), self.entropy.as_ref())
            .map_err(|_| StoreError::Unavailable)?;
        let value = record_value(&stored, &envelope);
        match previous {
            Some(index) => records[index].1 = value,
            None => records.push((team_id.to_owned(), value)),
        }
        self.write(records)?;
        Ok(stored)
    }

    fn resolved(&self, team: &str, value: &Value) -> Result<StoredContinuation, StoreError> {
        let (mut stored, envelope) = record(value, team)?;
        let key = self.keyring.load(false, self.entropy.as_ref())?;
        let plaintext = envelope
            .open(&key, &aad(&stored))
            .map_err(|_| StoreError::Unavailable)?;
        if !(1..=MAX_PLAINTEXT_BYTES).contains(&plaintext.len()) {
            return Err(StoreError::Unavailable);
        }
        stored.payload = plaintext.to_vec();
        Ok(stored)
    }

    fn expiring(&self, drained: bool) -> Result<Vec<StoredContinuation>, StoreError> {
        let _guard = self.guard();
        let mut records = self.read()?;
        let now = self.now();
        let expired =
            |value: &Value| exact_int(value.get("expires_at")).is_some_and(|at| at <= now);
        let (mut gone, mut live): (Records, Records) =
            records.drain(..).partition(|(_, value)| expired(value));
        let resolve = |records: &mut Records| {
            records.sort_by(|left, right| left.0.cmp(&right.0));
            records
                .iter()
                .map(|(team, value)| self.resolved(team, value))
                .collect::<Result<Vec<_>, _>>()
        };
        // Expired records are opened before they are removed; live ones after the expired are removed.
        let resolved = if drained {
            Some(resolve(&mut gone)?)
        } else {
            None
        };
        if !gone.is_empty() {
            self.write(live.clone())?;
        }
        match resolved {
            Some(resolved) => Ok(resolved),
            None => resolve(&mut live),
        }
    }

    /// Every live continuation, sorted by Team, after removing the expired ones.
    ///
    /// # Errors
    /// [`StoreError::Unavailable`] when the state cannot be read, opened, or written.
    pub fn active(&self) -> Result<Vec<StoredContinuation>, StoreError> {
        self.expiring(false)
    }

    /// Atomically removes and returns the expired continuations, for their dependent cleanup.
    ///
    /// # Errors
    /// As [`ContinuationStore::active`].
    pub fn drain_expired(&self) -> Result<Vec<StoredContinuation>, StoreError> {
        self.expiring(true)
    }

    /// The Team's live continuation.
    ///
    /// # Errors
    /// As [`ContinuationStore::active`].
    pub fn current(&self, team_id: &str) -> Result<Option<StoredContinuation>, StoreError> {
        Ok(self
            .active()?
            .into_iter()
            .find(|stored| stored.team_id == team_id))
    }

    /// Deletes the Team's continuation; with `challenge`, only that challenge's. Whether one was deleted.
    ///
    /// # Errors
    /// [`StoreError::NotFound`] when the Team's continuation answers another challenge, and as
    /// [`ContinuationStore::active`].
    pub fn delete(&self, team_id: &str, challenge: Option<&str>) -> Result<bool, StoreError> {
        if !TEAM_ID_RE.is_full(team_id) || challenge.is_some_and(|id| !challenge_id(id)) {
            return Err(StoreError::Invalid);
        }
        let _guard = self.guard();
        let mut records = self.read()?;
        let Some(index) = records.iter().position(|(team, _)| team == team_id) else {
            return Ok(false);
        };
        let (stored, _) = record(&records[index].1, team_id)?;
        if challenge.is_some_and(|id| id != stored.challenge_id) {
            return Err(StoreError::NotFound);
        }
        records.remove(index);
        self.write(records)?;
        Ok(true)
    }

    /// Deletes every continuation; how many there were.
    ///
    /// # Errors
    /// As [`ContinuationStore::active`].
    pub fn clear(&self) -> Result<usize, StoreError> {
        let _guard = self.guard();
        let records = self.read()?;
        let removed = records.len();
        if removed > 0 {
            self.write(Vec::new())?;
        }
        Ok(removed)
    }
}

mod restore;
#[cfg(test)]
mod tests;
