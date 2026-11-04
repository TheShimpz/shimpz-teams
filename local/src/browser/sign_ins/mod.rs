//! A Team's Sign-in: the easi.run profile a person saved their logins into (ADR-0117, ADR-0126).
//!
//! Each Team has at most one Sign-in. easi.run keeps its logins, encrypted, as one profile; this store keeps which
//! profile it is and its life. Its record (state, profile, times, open checkout, deletion fence) is sealed, bound to the
//! Team and the Sign-in, so its metadata cannot be altered without the key.
//!
//! The Sign-in has one exclusive checkout at a time, recorded before its Browser starts. Only a Save commits: it moves
//! to the next generation with compare-and-swap on the checkout and the generation it restored, and leaves the Sign-in
//! `ready`. Any other end of a checkout that restored a generation marks it `needs-person`; the end of a Team's first
//! checkout leaves no Sign-in. A stale end changes nothing. A Sign-in expires 30 days from its last Save and 7 days
//! unused; an expired one is deleted when read.
//!
//! A Supervisor's deletion records a durable fence first, holding the deletion's activity metadata (ADR-0120); the
//! fenced record stays, as a tombstone no checkout passes, until easi.run's deletion is proven and its completion
//! recorded. A profile no record names is residue the sessions remove. A Team process starting holds no checkout and no
//! Save, so it ends every open checkout as failed.

mod record;

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::UNIX_EPOCH;

use strict_json::{Number, Value};
use team_core::clock::Clock;
use team_core::entropy::{Entropy, token_hex};
use team_protocol::python::value::{exact_object, field};
use team_protocol::sign_in::{ABSENT, BUSY, CHANGED, ID_RE, NOT_RUNNING};
use team_storage::envelope::{Bounds, Envelope, Key};
use team_storage::private::{self, Keyring, StateError};

pub use record::Deleting;
use record::{Checkout as Held, Record};

/// The state file's schema.
pub const SCHEMA: i64 = 2;
/// The largest state file.
pub const MAX_STATE_BYTES: usize = 1024 * 1024;
/// The largest record plaintext.
pub const MAX_RECORD_BYTES: usize = 8192;
/// A Sign-in's life from its last Save.
pub const LIFETIME_SECONDS: f64 = 30.0 * 24.0 * 3600.0;
/// A Sign-in's life unused.
pub const IDLE_SECONDS: f64 = 7.0 * 24.0 * 3600.0;
/// A ready Sign-in.
pub const READY: &str = "ready";
/// A Sign-in a person must save again before an agent uses it.
pub const NEEDS_PERSON: &str = "needs-person";
const BOUNDS: Bounds = Bounds {
    maximum_encoded_part: MAX_RECORD_BYTES * 2 + 128,
    maximum_plaintext: MAX_RECORD_BYTES,
};

/// Who checks a Sign-in out.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// A person signing in.
    Person,
    /// An agent's task restored from it.
    Agent,
}

impl Kind {
    fn name(self) -> &'static str {
        match self {
            Self::Person => "person",
            Self::Agent => "agent",
        }
    }

    fn parse(name: &str) -> Option<Self> {
        match name {
            "person" => Some(Self::Person),
            "agent" => Some(Self::Agent),
            _ => None,
        }
    }
}

/// The Sign-in could not be read or kept as its contract requires; `code` names a closed refusal when one applies.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SignInError {
    /// The closed refusal (`sign_in`'s codes), or `None` when the store itself failed.
    pub code: Option<&'static str>,
}

impl SignInError {
    const FAILED: Self = Self { code: None };

    const fn refused(code: &'static str) -> Self {
        Self { code: Some(code) }
    }
}

impl From<StateError> for SignInError {
    fn from(_: StateError) -> Self {
        Self::FAILED
    }
}

impl std::fmt::Display for SignInError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.code.unwrap_or("the Sign-ins are unavailable"))
    }
}

impl std::error::Error for SignInError {}

/// One exclusive checkout of a Team's Sign-in: whose it is, and the generation it restored (0 for a first one).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Checkout {
    /// Its id.
    pub id: String,
    /// The Team.
    pub team_id: String,
    /// The Sign-in.
    pub sign_in_id: String,
    /// The Team incarnation it belongs to.
    pub incarnation: String,
    /// Who holds it.
    pub kind: Kind,
    /// The generation it restored.
    pub generation: i64,
}

/// An expired Sign-in deleted as it was read: its Team, generation, and incarnation, for Team to record.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Expired {
    /// The Team.
    pub team_id: String,
    /// Its generation.
    pub generation: i64,
    /// Its incarnation.
    pub incarnation: String,
}

/// A Supervisor's deletion not yet recorded as completed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Pending {
    /// The Team.
    pub team_id: String,
    /// The deletion's activity metadata.
    pub deleting: Deleting,
    /// The fenced generation.
    pub generation: i64,
    /// The fenced incarnation.
    pub incarnation: String,
}

#[derive(Debug, Default)]
struct Fences {
    // Each Team's in-process fence: a Save writes and commits only if no deletion or reset came since it began.
    saves: HashMap<String, u64>,
    // Expired Sign-ins deleted as they were read, for Team to record.
    expired: Vec<Expired>,
}

/// Failures a test injects where Python's tests patched the store.
#[cfg(test)]
#[derive(Debug, Default)]
pub(crate) struct Faults {
    /// Every state write fails, before (`Some(false)`) or after (`Some(true)`) it is written.
    pub write: Option<bool>,
    /// Every record read fails.
    pub records: bool,
}

/// Every Team's Sign-in on this Local.
pub struct SignInStore {
    index: PathBuf,
    keyring: Keyring,
    clock: Arc<dyn Clock>,
    entropy: Arc<dyn Entropy>,
    fences: Mutex<Fences>,
    #[cfg(test)]
    pub(crate) faults: Mutex<Faults>,
}

impl std::fmt::Debug for SignInStore {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("SignInStore")
    }
}

/// The Teams' records as stored: each Team's Sign-in id and sealed record.
type Teams = Vec<(String, Value)>;

fn record_aad(team_id: &str, sign_in_id: &str) -> Vec<u8> {
    format!("shimpz-sign-in-record\0{team_id}\0{sign_in_id}").into_bytes()
}

fn team_admitted(team_id: &str) -> Result<(), SignInError> {
    if team_protocol::identifiers::canonical_team_id(&Value::String(team_id.to_owned())).is_some() {
        Ok(())
    } else {
        Err(SignInError::FAILED)
    }
}

impl SignInStore {
    /// The store with its state under `state` and its keyring at `key`.
    ///
    /// # Errors
    /// [`SignInError`] when the paths are not absolute or share a directory.
    pub fn new(
        state: &Path,
        key: &Path,
        clock: Arc<dyn Clock>,
        entropy: Arc<dyn Entropy>,
    ) -> Result<Self, SignInError> {
        let index = state.join("index.json");
        private::separate_paths(&index, key)?;
        Ok(Self {
            index,
            keyring: Keyring::new(key.to_path_buf()),
            clock,
            entropy,
            fences: Mutex::new(Fences::default()),
            #[cfg(test)]
            faults: Mutex::new(Faults::default()),
        })
    }

    #[cfg(test)]
    fn faults(&self) -> MutexGuard<'_, Faults> {
        self.faults.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn lock(&self) -> MutexGuard<'_, Fences> {
        self.fences.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn now(&self) -> f64 {
        self.clock
            .now()
            .duration_since(UNIX_EPOCH)
            .map_or(0.0, |elapsed| elapsed.as_secs_f64())
    }

    fn key(&self, create: bool) -> Result<Key, SignInError> {
        Ok(self.keyring.load(create, self.entropy.as_ref())?)
    }

    fn state(&self) -> Result<Teams, SignInError> {
        let Some(payload) = private::read_private(&self.index, MAX_STATE_BYTES as u64)? else {
            return Ok(Vec::new());
        };
        let value = strict_json::parse(&payload).map_err(|_| SignInError::FAILED)?;
        let members = exact_object(&value, &["schema", "teams"]).ok_or(SignInError::FAILED)?;
        if *field(members, "schema") != Value::Number(Number::Int(SCHEMA)) {
            return Err(SignInError::FAILED);
        }
        match field(members, "teams") {
            Value::Object(teams) => Ok(teams.clone()),
            _ => Err(SignInError::FAILED),
        }
    }

    fn write(&self, teams: Teams) -> Result<(), SignInError> {
        #[cfg(test)]
        let after = match self.faults().write {
            Some(false) => return Err(SignInError::FAILED),
            Some(true) => true,
            None => false,
        };
        let value = Value::Object(vec![
            ("schema".to_owned(), Value::Number(Number::Int(SCHEMA))),
            ("teams".to_owned(), Value::Object(teams)),
        ]);
        private::write_private_json(&self.index, &value, MAX_STATE_BYTES)?;
        #[cfg(test)]
        if after {
            return Err(SignInError::FAILED);
        }
        Ok(())
    }

    /// The state, and the Team's record opened and checked, or `None` when it has none.
    fn record(&self, team_id: &str) -> Result<(Teams, Option<Record>), SignInError> {
        #[cfg(test)]
        if self.faults().records {
            return Err(SignInError::FAILED);
        }
        let teams = self.state()?;
        let Some((_, envelope)) = teams.iter().find(|(team, _)| team == team_id) else {
            return Ok((teams, None));
        };
        let members =
            exact_object(envelope, &["sign_in_id", "record"]).ok_or(SignInError::FAILED)?;
        let Value::String(sign_in_id) = field(members, "sign_in_id") else {
            return Err(SignInError::FAILED);
        };
        if !ID_RE.is_full(sign_in_id) {
            return Err(SignInError::FAILED);
        }
        let sealed = Envelope::admit_identified(field(members, "record"), BOUNDS)
            .map_err(|_| SignInError::FAILED)?;
        let plaintext = sealed
            .open(&self.key(false)?, &record_aad(team_id, sign_in_id))
            .map_err(|_| SignInError::FAILED)?;
        let record = Record::parse(&plaintext, team_id, sign_in_id).ok_or(SignInError::FAILED)?;
        Ok((teams, Some(record)))
    }

    fn put(&self, mut teams: Teams, record: &Record) -> Result<(), SignInError> {
        let key = self.key(true)?;
        let sealed = Envelope::seal_identified(
            &key,
            record.plaintext().as_bytes(),
            &record_aad(&record.team_id, &record.sign_in_id),
            self.entropy.as_ref(),
        )
        .map_err(|_| SignInError::FAILED)?;
        let entry = Value::Object(vec![
            (
                "sign_in_id".to_owned(),
                Value::String(record.sign_in_id.clone()),
            ),
            ("record".to_owned(), sealed.to_value()),
        ]);
        match teams.iter_mut().find(|(team, _)| *team == record.team_id) {
            Some((_, existing)) => *existing = entry,
            None => teams.push((record.team_id.clone(), entry)),
        }
        self.write(teams)
    }

    fn without(teams: Teams, team_id: &str) -> Teams {
        teams
            .into_iter()
            .filter(|(team, _)| team != team_id)
            .collect()
    }

    fn expired_now(&self, record: &Record) -> bool {
        record.generation != 0 && self.now() >= record.expires()
    }

    /// Deletes an expired Sign-in and queues its record for Team once it is gone; whether it is.
    fn expire(
        &self,
        fences: &mut Fences,
        team_id: &str,
        record: &Record,
    ) -> Result<bool, SignInError> {
        let gone = self.delete(fences, team_id)?;
        if gone {
            fences.expired.push(Expired {
                team_id: team_id.to_owned(),
                generation: record.generation,
                incarnation: record.incarnation.clone(),
            });
        }
        Ok(gone)
    }

    /// Every expired Sign-in deleted as it was read since the last call.
    pub fn take_expired(&self) -> Vec<Expired> {
        std::mem::take(&mut self.lock().expired)
    }

    /// The Team's Sign-in as Admin shows it, or `None`; an expired one is deleted as it is read. One the `reader` image
    /// cannot restore shows as needing a person.
    ///
    /// # Errors
    /// [`SignInError`] when the store cannot be read.
    pub fn current(&self, team_id: &str) -> Result<Option<Value>, SignInError> {
        team_admitted(team_id)?;
        let mut fences = self.lock();
        let (_teams, record) = self.record(team_id)?;
        let Some(record) =
            record.filter(|record| record.deleting.is_none() && record.generation != 0)
        else {
            return Ok(None);
        };
        if self.expired_now(&record) {
            self.expire(&mut fences, team_id, &record)?;
            return Ok(None);
        }
        Ok(Some(record.public(false)))
    }

    /// Checks the Team's Sign-in out, durably, before its Browser starts; a first one has no generation yet.
    ///
    /// # Errors
    /// [`SignInError`] `sign-in-busy` while it is checked out, being deleted, or needs a person for an agent.
    pub fn checkout(
        &self,
        team_id: &str,
        incarnation: &str,
        kind: Kind,
    ) -> Result<Checkout, SignInError> {
        team_admitted(team_id)?;
        let mut fences = self.lock();
        let (mut teams, mut record) = self.record(team_id)?;
        if record
            .as_ref()
            .is_some_and(|record| record.deleting.is_some())
        {
            return Err(SignInError::refused(BUSY));
        }
        if let Some(stale) = record
            .clone()
            .filter(|record| self.expired_now(record) || record.incarnation != incarnation)
        {
            // An expired or earlier incarnation's Sign-in is gone before anything new is checked out.
            let gone = if self.expired_now(&stale) {
                self.expire(&mut fences, team_id, &stale)?
            } else {
                self.delete(&mut fences, team_id)?
            };
            if !gone {
                return Err(SignInError::refused(BUSY));
            }
            (teams, record) = self.record(team_id)?;
        }
        self.check_out(&mut fences, teams, record, team_id, incarnation, kind)
    }

    fn check_out(
        &self,
        fences: &mut Fences,
        teams: Teams,
        record: Option<Record>,
        team_id: &str,
        incarnation: &str,
        kind: Kind,
    ) -> Result<Checkout, SignInError> {
        if record
            .as_ref()
            .is_some_and(|record| record.checkout.is_some())
        {
            return Err(SignInError::refused(BUSY));
        }
        let needs_person = record
            .as_ref()
            .is_some_and(|record| record.state == NEEDS_PERSON);
        if kind == Kind::Agent && needs_person {
            return Err(SignInError::refused(BUSY));
        }
        let now = self.now();
        let mut record = match record {
            Some(record) => record,
            None => Record::first(team_id, incarnation, now, &self.token()?),
        };
        let checkout = Checkout {
            id: self.token()?,
            team_id: team_id.to_owned(),
            sign_in_id: record.sign_in_id.clone(),
            incarnation: incarnation.to_owned(),
            kind,
            generation: record.generation,
        };
        record.checkout = Some(Held {
            id: checkout.id.clone(),
            kind: kind.name().to_owned(),
        });
        record.last_used_at = now.max(record.last_used_at);
        self.put(teams, &record)?;
        fences.saves.entry(team_id.to_owned()).or_insert(0);
        Ok(checkout)
    }

    fn token(&self) -> Result<String, SignInError> {
        token_hex(self.entropy.as_ref(), 16).map_err(|_| SignInError::FAILED)
    }

    /// The state and record while `checkout` is still the Sign-in's current one.
    fn held(&self, checkout: &Checkout) -> Result<Option<(Teams, Record)>, SignInError> {
        let (teams, record) = self.record(&checkout.team_id)?;
        Ok(record
            .filter(|record| {
                record.deleting.is_none()
                    && record
                        .checkout
                        .as_ref()
                        .is_some_and(|held| held.id == checkout.id)
                    && record.generation == checkout.generation
            })
            .map(|record| (teams, record)))
    }

    /// The easi.run profile of a checkout's Sign-in; `None` before its first one was made.
    ///
    /// # Errors
    /// [`SignInError`] `sign-in-not-running` once the checkout is stale.
    pub fn profile(&self, checkout: &Checkout) -> Result<Option<String>, SignInError> {
        let _fences = self.lock();
        let Some((_teams, record)) = self.held(checkout)? else {
            return Err(SignInError::refused(NOT_RUNNING));
        };
        Ok(Some(record.profile).filter(|profile| !profile.is_empty()))
    }

    /// Records the profile just made for a checkout's Sign-in, durably, before its Browser starts.
    ///
    /// # Errors
    /// [`SignInError`] `sign-in-not-running` once the checkout is stale, or a store failure.
    pub fn attach(&self, checkout: &Checkout, profile: &str) -> Result<(), SignInError> {
        let _fences = self.lock();
        let Some((teams, mut record)) = self.held(checkout)? else {
            return Err(SignInError::refused(NOT_RUNNING));
        };
        profile.clone_into(&mut record.profile);
        self.put(teams, &record)
    }

    /// Commits the next generation, only if the checkout is still current and `admitted`.
    ///
    /// # Errors
    /// [`SignInError`] when the Save was revoked or the store failed.
    pub fn commit(
        &self,
        checkout: &Checkout,
        fence: u64,
        admitted: impl FnOnce() -> bool,
    ) -> Result<Value, SignInError> {
        let fences = self.lock();
        let revoked = fences.saves.get(&checkout.team_id).copied().unwrap_or(0) != fence;
        let current = self.held(checkout)?;
        let Some((teams, mut record)) = current.filter(|_| !revoked && admitted()) else {
            return Err(SignInError::FAILED);
        };
        let now = self.now();
        record.generation += 1;
        READY.clone_into(&mut record.state);
        record.saved_at = now;
        record.last_used_at = now;
        record.checkout = None;
        self.put(teams, &record)?;
        Ok(record.public(false))
    }

    /// The fence a Save started under: a deletion or reset moves it, and that Save then keeps nothing.
    #[must_use]
    pub fn save_fence(&self, team_id: &str) -> u64 {
        self.lock().saves.get(team_id).copied().unwrap_or(0)
    }

    /// Every profile a Sign-in's record names, deleting or not.
    ///
    /// # Errors
    /// [`SignInError`] when the store cannot be read.
    pub fn named_profiles(&self) -> Result<Vec<String>, SignInError> {
        let _fences = self.lock();
        let mut named = Vec::new();
        for (team_id, _) in self.state()? {
            if let (_, Some(record)) = self.record(&team_id)?
                && !record.profile.is_empty()
            {
                named.push(record.profile);
            }
        }
        Ok(named)
    }

    /// The profile a fenced deletion must remove, once it is still fenced by `event_id`.
    ///
    /// # Errors
    /// [`SignInError`] when the store cannot be read.
    pub fn fenced_profile(
        &self,
        team_id: &str,
        event_id: &str,
    ) -> Result<Option<String>, SignInError> {
        team_admitted(team_id)?;
        let _fences = self.lock();
        let (_teams, record) = self.record(team_id)?;
        Ok(record
            .filter(|record| record.fenced_by(event_id) && !record.profile.is_empty())
            .map(|record| record.profile))
    }

    /// Ends a checkout without a Save: a restored generation now needs a person; a first one leaves nothing.
    ///
    /// # Errors
    /// [`SignInError`] when the store cannot be written.
    pub fn finish(&self, checkout: &Checkout) -> Result<(), SignInError> {
        let _fences = self.lock();
        let Some((teams, mut record)) = self.held(checkout)? else {
            return Ok(());
        };
        if record.generation == 0 {
            return self.write(Self::without(teams, &checkout.team_id));
        }
        record.checkout = None;
        NEEDS_PERSON.clone_into(&mut record.state);
        self.put(teams, &record)
    }

    /// Fences the exact Sign-in the Supervisor confirmed (its id, generation, and Team incarnation) for deletion,
    /// keeping the deletion's activity metadata until its completion is recorded.
    ///
    /// # Errors
    /// [`SignInError`] `sign-in-absent` or `sign-in-changed`, or a store failure.
    pub fn begin_delete(
        &self,
        team_id: &str,
        expected: (&str, i64, &str),
        deleting: Deleting,
    ) -> Result<(), SignInError> {
        team_admitted(team_id)?;
        if deleting.event_id.is_empty() || !deleting.admitted() {
            return Err(SignInError::FAILED);
        }
        let mut fences = self.lock();
        let (teams, record) = self.record(team_id)?;
        let Some(record) =
            record.filter(|record| record.generation != 0 && record.deleting.is_none())
        else {
            return Err(SignInError::refused(ABSENT));
        };
        if (
            record.sign_in_id.as_str(),
            record.generation,
            record.incarnation.as_str(),
        ) != expected
        {
            return Err(SignInError::refused(CHANGED));
        }
        self.fence(&mut fences, teams, record, deleting)
    }

    fn fence(
        &self,
        fences: &mut Fences,
        teams: Teams,
        mut record: Record,
        deleting: Deleting,
    ) -> Result<(), SignInError> {
        *fences.saves.entry(record.team_id.clone()).or_insert(0) += 1;
        if record.deleting.is_none() {
            record.deleting = Some(deleting);
            self.put(teams, &record)?;
        }
        Ok(())
    }

    /// Every Supervisor deletion not yet recorded as completed.
    ///
    /// # Errors
    /// [`SignInError`] when the store cannot be read.
    pub fn deletions(&self) -> Result<Vec<Pending>, SignInError> {
        let _fences = self.lock();
        let mut pending = Vec::new();
        for (team_id, _) in self.state()? {
            let (_teams, record) = self.record(&team_id)?;
            if let Some(record) = record
                && let Some(deleting) = record
                    .deleting
                    .filter(|deleting| !deleting.event_id.is_empty())
            {
                pending.push(Pending {
                    team_id,
                    deleting,
                    generation: record.generation,
                    incarnation: record.incarnation,
                });
            }
        }
        Ok(pending)
    }

    /// Drops the deletion `event_id`'s fenced record once its completion is recorded; whether it is gone.
    ///
    /// # Errors
    /// [`SignInError`] when the store cannot be written.
    pub fn settle(&self, team_id: &str, event_id: &str) -> Result<bool, SignInError> {
        team_admitted(team_id)?;
        let _fences = self.lock();
        let (teams, record) = self.record(team_id)?;
        if !record.is_some_and(|record| record.fenced_by(event_id)) {
            return Ok(true);
        }
        self.write(Self::without(teams, team_id))?;
        Ok(!self.state()?.iter().any(|(team, _)| team == team_id))
    }

    /// Deletes the Team's Sign-in for its own lifecycle, its record; its profile is then residue the sessions remove; whether nothing is left. A
    /// Supervisor's deletion under way keeps its fenced record until its completion is recorded.
    fn delete(&self, fences: &mut Fences, team_id: &str) -> Result<bool, SignInError> {
        let (teams, record) = self.record(team_id)?;
        *fences.saves.entry(team_id.to_owned()).or_insert(0) += 1;
        let record = match record {
            Some(record) => {
                self.fence(fences, teams, record.clone(), Deleting::internal())?;
                Some(record)
            }
            None => None,
        };
        let supervisor = record
            .as_ref()
            .and_then(|record| record.deleting.as_ref())
            .is_some_and(|deleting| !deleting.event_id.is_empty());
        if !supervisor {
            let (teams, _) = self.record(team_id)?;
            if teams.iter().any(|(team, _)| team == team_id) {
                self.write(Self::without(teams, team_id))?;
            }
        }
        Ok(supervisor || !self.state()?.iter().any(|(team, _)| team == team_id))
    }

    /// A deleted Team's Sign-in goes with it, fencing a Save under way; whether nothing is left.
    ///
    /// # Errors
    /// [`SignInError`] when the store cannot be read or written.
    pub fn delete_team(&self, team_id: &str) -> Result<bool, SignInError> {
        team_admitted(team_id)?;
        let mut fences = self.lock();
        self.delete(&mut fences, team_id)
    }

    /// A Space reset: every Sign-in is deleted, and every Save under way fenced. Only the fenced
    /// records of Supervisor deletions under way stay, holding nothing but their activity metadata.
    ///
    /// # Errors
    /// [`SignInError`] when anything may remain.
    pub fn delete_all(&self) -> Result<(), SignInError> {
        let mut fences = self.lock();
        let teams = self.state()?;
        let known: Vec<String> = fences
            .saves
            .keys()
            .cloned()
            .chain(teams.iter().map(|(team, _)| team.clone()))
            .collect();
        for team in known {
            *fences.saves.entry(team).or_insert(0) += 1;
        }
        drop(fences);
        let pending: Vec<String> = self
            .deletions()?
            .into_iter()
            .map(|pending| pending.team_id)
            .collect();
        if !pending.is_empty() {
            let kept = teams
                .into_iter()
                .filter(|(team, _)| pending.contains(team))
                .collect();
            return self.write(kept);
        }
        match std::fs::remove_file(&self.index) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(_) => Err(SignInError::FAILED),
        }
    }

    /// At Team startup: ends every open checkout as failed and removes what every fenced deletion left; a
    /// Supervisor's deletion keeps its fenced record.  names is deleted.
    ///
    /// # Errors
    /// [`SignInError`] when the store cannot be read or cleaned.
    pub fn reconcile(&self) -> Result<(), SignInError> {
        for (team_id, _) in self.state()? {
            let (_teams, record) = self.record(&team_id)?;
            let Some(record) = record else { continue };
            match (&record.deleting, &record.checkout) {
                (Some(deleting), _) if !deleting.event_id.is_empty() => {}
                (Some(_), _) => {
                    let mut fences = self.lock();
                    self.delete(&mut fences, &team_id)?;
                }
                (None, Some(held)) => self.finish(&Checkout {
                    id: held.id.clone(),
                    team_id: team_id.clone(),
                    sign_in_id: record.sign_in_id.clone(),
                    incarnation: record.incarnation.clone(),
                    kind: Kind::parse(&held.kind).unwrap_or(Kind::Person),
                    generation: record.generation,
                })?,
                (None, None) => {}
            }
        }
        Ok(())
    }
}

#[cfg(test)]
pub(crate) mod tests;
