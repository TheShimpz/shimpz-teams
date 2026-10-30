//! Encrypted records per (Team, Assistant) in one identity-cached state file beside a separate keyring.
//!
//! The state file is `{"schema":1,"teams":{team:{assistant:{record_id:record}}},"last_generation":n}` in canonical
//! ASCII JSON. Each store (Stored Inputs, Integrations) states its record schema, id grammar, and limits in its
//! [`RecordPolicy`], and seals each record's secret into an identified envelope with its own associated data.
//!
//! `last_generation` is store-wide and survives every deletion, so a replaced secret never reuses the generation a
//! prepared Action batch bound before its predecessor was removed. Every state access holds the store's lock, every
//! mutation edits a private copy of the cached state, and every write drops the cache whether or not it succeeds.

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use strict_json::{Number, Value};
use team_core::entropy::Entropy;

use crate::custody::{FileIdentity, Snapshot};
use crate::envelope::Key;
use crate::private::{self, Keyring, StateError};

/// What one encrypted record store states about its records.
pub trait RecordPolicy: Send + Sync {
    /// The largest state file, in bytes.
    fn maximum_state_bytes(&self) -> usize;
    /// The most records one Assistant of one Team may have.
    fn records_per_assistant(&self) -> usize;
    /// The most records the whole state may have.
    fn maximum_records(&self) -> usize;
    /// Whether `id` is a canonical Team id.
    fn team_id(&self, id: &str) -> bool;
    /// Whether `id` is a canonical Assistant id.
    fn assistant_id(&self, id: &str) -> bool;
    /// Whether `id` is a canonical record id.
    fn record_id(&self, id: &str) -> bool;
    /// Admits one stored record and returns its generation; `None` refuses it.
    fn record_generation(&self, record: &Value) -> Option<u64>;
}

/// One admitted state document.
#[derive(Clone, Debug, PartialEq)]
pub struct State(Value);

/// One Assistant's records, in stored order.
pub type Records = Vec<(String, Value)>;

impl State {
    /// The state of a store that holds nothing yet.
    #[must_use]
    pub fn empty() -> Self {
        Self(Value::Object(vec![
            ("schema".to_owned(), int(1)),
            ("teams".to_owned(), Value::Object(Vec::new())),
            ("last_generation".to_owned(), int(0)),
        ]))
    }

    /// Admits a whole state document under `policy`: its shape, every id, every record, its generations, and the
    /// record limit, which is checked at each record so an oversized document is refused as soon as it passes it.
    ///
    /// # Errors
    /// [`StateError::Malformed`] or [`StateError::RecordLimit`].
    pub fn admit(value: Value, policy: &dyn RecordPolicy) -> Result<Self, StateError> {
        let Value::Object(members) = &value else {
            return Err(StateError::Malformed);
        };
        let mut names: Vec<&str> = members.iter().map(|(name, _)| name.as_str()).collect();
        names.sort_unstable();
        if names != ["last_generation", "schema", "teams"] || value.get("schema") != Some(&int(1)) {
            return Err(StateError::Malformed);
        }
        let last_generation =
            generation(value.get("last_generation")).ok_or(StateError::Malformed)?;
        let Some(Value::Object(teams)) = value.get("teams") else {
            return Err(StateError::Malformed);
        };
        let mut total = 0_usize;
        for (team, assistants) in teams {
            let Value::Object(assistants) = assistants else {
                return Err(StateError::Malformed);
            };
            if !policy.team_id(team) {
                return Err(StateError::Malformed);
            }
            for (assistant, records) in assistants {
                let Value::Object(records) = records else {
                    return Err(StateError::Malformed);
                };
                if !policy.assistant_id(assistant) || records.len() > policy.records_per_assistant()
                {
                    return Err(StateError::Malformed);
                }
                for (id, record) in records {
                    let admitted = policy.record_id(id)
                        && policy
                            .record_generation(record)
                            .is_some_and(|stored| stored <= last_generation);
                    if !admitted {
                        return Err(StateError::Malformed);
                    }
                    total += 1;
                    if total > policy.maximum_records() {
                        return Err(StateError::RecordLimit);
                    }
                }
            }
        }
        Ok(Self(value))
    }

    /// The stored document.
    #[must_use]
    pub fn value(&self) -> &Value {
        &self.0
    }

    /// The store-wide last generation.
    #[must_use]
    pub fn last_generation(&self) -> u64 {
        generation(self.0.get("last_generation")).unwrap_or(0)
    }

    /// Advances and returns the store-wide generation, for one record about to be sealed.
    pub fn advance_generation(&mut self) -> u64 {
        let next = self.last_generation() + 1;
        *member_mut(&mut self.0, "last_generation") =
            Value::Number(Number::Int(i64::try_from(next).unwrap_or(i64::MAX)));
        next
    }

    /// One Assistant's records, when it has any entry.
    #[must_use]
    pub fn records(&self, team: &str, assistant: &str) -> Option<&Records> {
        match self.0.get("teams")?.get(team)?.get(assistant)? {
            Value::Object(records) => Some(records),
            _ => None,
        }
    }

    /// One Assistant's records, its Team and Assistant entries created when absent.
    pub fn records_mut(&mut self, team: &str, assistant: &str) -> &mut Records {
        let teams = member_mut(&mut self.0, "teams");
        let assistants = member_mut(teams, team);
        let Value::Object(records) = member_mut(assistants, assistant) else {
            unreachable!("member_mut answers an object");
        };
        records
    }

    /// Whether any Assistant holds a record.
    #[must_use]
    pub fn has_records(&self) -> bool {
        objects(self.0.get("teams")).iter().any(|(_, assistants)| {
            objects(Some(assistants))
                .iter()
                .any(|(_, records)| !objects(Some(records)).is_empty())
        })
    }

    /// Removes an Assistant entry left without records, and its Team entry when that leaves it empty.
    pub fn prune_empty(&mut self, team: &str, assistant: &str) {
        let Some(Value::Object(teams)) = member(&mut self.0, "teams") else {
            return;
        };
        let Some(Value::Object(assistants)) = find(teams, team) else {
            return;
        };
        if matches!(find(assistants, assistant), Some(Value::Object(records)) if records.is_empty())
        {
            assistants.retain(|(name, _)| name != assistant);
        }
        if assistants.is_empty() {
            teams.retain(|(name, _)| name != team);
        }
    }

    /// Removes one Assistant's records; whether it had an entry.
    pub fn delete_assistant(&mut self, team: &str, assistant: &str) -> bool {
        let Some(Value::Object(teams)) = member(&mut self.0, "teams") else {
            return false;
        };
        let Some(Value::Object(assistants)) = find(teams, team) else {
            return false;
        };
        let before = assistants.len();
        assistants.retain(|(name, _)| name != assistant);
        let removed = assistants.len() != before;
        if removed && assistants.is_empty() {
            teams.retain(|(name, _)| name != team);
        }
        removed
    }

    /// Removes one Team's records; whether it had an entry.
    pub fn delete_team(&mut self, team: &str) -> bool {
        let Some(Value::Object(teams)) = member(&mut self.0, "teams") else {
            return false;
        };
        let before = teams.len();
        teams.retain(|(name, _)| name != team);
        teams.len() != before
    }

    /// Removes every record; the generation stays.
    pub fn clear(&mut self) {
        *member_mut(&mut self.0, "teams") = Value::Object(Vec::new());
    }
}

fn int(value: i64) -> Value {
    Value::Number(Number::Int(value))
}

fn generation(value: Option<&Value>) -> Option<u64> {
    match value {
        Some(Value::Number(Number::Int(number))) => u64::try_from(*number).ok(),
        _ => None,
    }
}

fn objects(value: Option<&Value>) -> &[(String, Value)] {
    match value {
        Some(Value::Object(members)) => members,
        _ => &[],
    }
}

fn find<'a>(members: &'a mut [(String, Value)], name: &str) -> Option<&'a mut Value> {
    members
        .iter_mut()
        .find(|(member, _)| member == name)
        .map(|(_, value)| value)
}

fn member<'a>(value: &'a mut Value, name: &str) -> Option<&'a mut Value> {
    match value {
        Value::Object(members) => find(members, name),
        _ => None,
    }
}

/// The object member `name` of `value`, created as an empty object when absent; `value` itself becomes an object if
/// it is not one (never the case for admitted state).
fn member_mut<'a>(value: &'a mut Value, name: &str) -> &'a mut Value {
    if !matches!(value, Value::Object(_)) {
        *value = Value::Object(Vec::new());
    }
    let Value::Object(members) = value else {
        unreachable!("made an object above");
    };
    let index = members
        .iter()
        .position(|(member, _)| member == name)
        .unwrap_or_else(|| {
            members.push((name.to_owned(), Value::Object(Vec::new())));
            members.len() - 1
        });
    &mut members[index].1
}

/// Whether an update changed the state, and so must be written.
#[derive(Debug, PartialEq, Eq)]
pub enum Outcome<T> {
    /// Nothing changed; nothing is written.
    Unchanged(T),
    /// The state changed; it is admitted again and written before the update returns.
    Changed(T),
}

struct Cache {
    initialized: bool,
    identity: Option<FileIdentity>,
    state: Arc<State>,
}

/// One encrypted record store: its state file, its keyring, and its policy.
pub struct RecordStore<P: RecordPolicy> {
    policy: P,
    state_path: PathBuf,
    keyring: Keyring,
    entropy: Arc<dyn Entropy>,
    cache: Mutex<Cache>,
}

impl<P: RecordPolicy> std::fmt::Debug for RecordStore<P> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RecordStore")
            .field("state_path", &self.state_path)
            .field("keyring", &self.keyring)
            .finish_non_exhaustive()
    }
}

impl<P: RecordPolicy> RecordStore<P> {
    /// The store whose state lives at `state_path` and keyring at `key_path`.
    ///
    /// # Errors
    /// [`StateError::Layout`] unless both are absolute and their directories differ.
    pub fn new(
        state_path: PathBuf,
        key_path: PathBuf,
        policy: P,
        entropy: Arc<dyn Entropy>,
    ) -> Result<Self, StateError> {
        private::separate_paths(&state_path, &key_path)?;
        Ok(Self {
            policy,
            state_path,
            keyring: Keyring::new(key_path),
            entropy,
            cache: Mutex::new(Cache {
                initialized: false,
                identity: None,
                state: Arc::new(State::empty()),
            }),
        })
    }

    /// The store's policy.
    pub fn policy(&self) -> &P {
        &self.policy
    }

    /// The state file's path.
    #[must_use]
    pub fn state_path(&self) -> &Path {
        &self.state_path
    }

    /// The entropy every seal and new key of this store draws from.
    #[must_use]
    pub fn entropy(&self) -> &dyn Entropy {
        self.entropy.as_ref()
    }

    /// The keyring's key; with `create`, a fresh key is stored first when none exists.
    ///
    /// # Errors
    /// As [`Keyring::load`].
    pub fn key(&self, create: bool) -> Result<Key, StateError> {
        self.keyring.load(create, self.entropy.as_ref())
    }

    /// Reads the current state under the store's lock and answers `read` from it.
    ///
    /// # Errors
    /// The state's [`StateError`], or `read`'s own error.
    pub fn read<T, E: From<StateError>>(
        &self,
        read: impl FnOnce(&State) -> Result<T, E>,
    ) -> Result<T, E> {
        let mut cache = self.cache.lock().map_err(|_| StateError::Unavailable)?;
        let state = self.current(&mut cache)?;
        read(&state)
    }

    /// Edits a private copy of the current state under the store's lock; a [`Outcome::Changed`] copy is admitted again
    /// and durably written before this returns, and the cache is dropped whether or not the write succeeds.
    ///
    /// # Errors
    /// The state's [`StateError`], `update`'s own error (nothing is then written), or a write failure.
    pub fn update<T, E: From<StateError>>(
        &self,
        update: impl FnOnce(&mut State) -> Result<Outcome<T>, E>,
    ) -> Result<T, E> {
        let mut cache = self.cache.lock().map_err(|_| StateError::Unavailable)?;
        let current = self.current(&mut cache)?;
        let mut state = State::clone(&current);
        match update(&mut state)? {
            Outcome::Unchanged(answer) => Ok(answer),
            Outcome::Changed(answer) => {
                cache.initialized = false;
                cache.identity = None;
                let admitted = State::admit(state.0, &self.policy)?;
                private::write_private_json(
                    &self.state_path,
                    &admitted.0,
                    self.policy.maximum_state_bytes(),
                )?;
                Ok(answer)
            }
        }
    }

    fn current(&self, cache: &mut Cache) -> Result<Arc<State>, StateError> {
        let maximum = self.policy.maximum_state_bytes() as u64;
        let previous = if cache.initialized {
            cache.identity
        } else {
            None
        };
        let snapshot = private::read_private_if_changed(&self.state_path, maximum, previous);
        let (identity, state) = match snapshot {
            Err(error) => {
                cache.initialized = false;
                return Err(error);
            }
            Ok(Snapshot::Unchanged(_)) => return Ok(Arc::clone(&cache.state)),
            Ok(Snapshot::Absent) if cache.initialized && cache.identity.is_none() => {
                return Ok(Arc::clone(&cache.state));
            }
            Ok(Snapshot::Absent) => (None, State::empty()),
            Ok(Snapshot::Read(identity, payload)) => {
                let value = strict_json::parse(&payload).map_err(|_| StateError::Malformed);
                let admitted = value.and_then(|value| State::admit(value, &self.policy));
                match admitted {
                    Ok(state) => (Some(identity), state),
                    Err(error) => {
                        cache.initialized = false;
                        return Err(error);
                    }
                }
            }
        };
        *cache = Cache {
            initialized: true,
            identity,
            state: Arc::new(state),
        };
        Ok(Arc::clone(&cache.state))
    }

    /// Discards the records an Assistant's current release no longer declares; `declared` holds admitted ids.
    ///
    /// # Errors
    /// The state's [`StateError`].
    pub fn retain_declared(
        &self,
        team: &str,
        assistant: &str,
        declared: &[&str],
    ) -> Result<bool, StateError> {
        self.update(|state| {
            let Some(records) = state.records(team, assistant) else {
                return Ok(Outcome::Unchanged(false));
            };
            if records
                .iter()
                .all(|(id, _)| declared.contains(&id.as_str()))
            {
                return Ok(Outcome::Unchanged(false));
            }
            state
                .records_mut(team, assistant)
                .retain(|(id, _)| declared.contains(&id.as_str()));
            state.prune_empty(team, assistant);
            Ok(Outcome::Changed(true))
        })
    }

    /// Removes one Assistant's records; whether any entry existed.
    ///
    /// # Errors
    /// The state's [`StateError`].
    pub fn delete_assistant(&self, team: &str, assistant: &str) -> Result<bool, StateError> {
        self.update(|state| {
            Ok(if state.delete_assistant(team, assistant) {
                Outcome::Changed(true)
            } else {
                Outcome::Unchanged(false)
            })
        })
    }

    /// Removes one Team's records; whether any entry existed.
    ///
    /// # Errors
    /// The state's [`StateError`].
    pub fn delete_team(&self, team: &str) -> Result<bool, StateError> {
        self.update(|state| {
            Ok(if state.delete_team(team) {
                Outcome::Changed(true)
            } else {
                Outcome::Unchanged(false)
            })
        })
    }

    /// Purges every record during an owned Space reset; whether any existed.
    ///
    /// # Errors
    /// The state's [`StateError`].
    pub fn delete_all(&self) -> Result<bool, StateError> {
        self.update(|state| {
            if !state.has_records() {
                return Ok(Outcome::Unchanged(false));
            }
            state.clear();
            Ok(Outcome::Changed(true))
        })
    }
}

#[cfg(test)]
mod tests;
