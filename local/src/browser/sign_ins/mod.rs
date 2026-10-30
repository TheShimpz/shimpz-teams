//! A Team's Sign-in: the sealed browser session a person saved their logins into (ADR-0117, 2026-10-20 amendment).
//!
//! Each Team has at most one Sign-in. Its capsule is kept as numbered chunk files, each sealed (AES-256-GCM) under a
//! keyring apart from the state and bound to the Team, its incarnation (the Team network's id), the Sign-in, its
//! generation, and the chunk's place, so a chunk never moves between capsules. Its record (state, sites found in its
//! cookies, times, chunk count, writer image, open checkout, deletion fence) is sealed too, bound to the Team and the
//! Sign-in, so neither the capsule nor its metadata can be altered without the key.
//!
//! The Sign-in has one exclusive checkout at a time, recorded before its Browser starts. Only a Save commits: it writes
//! the next generation with compare-and-swap on the checkout and the generation it restored, and leaves the Sign-in
//! `ready`. Any other end of a checkout that restored a generation keeps that generation's bytes but marks it
//! `needs-person`; the end of a Team's first checkout leaves no Sign-in. A stale end changes nothing. A Sign-in lives 30
//! days from its last Save and 7 days unused; an expired one is deleted when read.
//!
//! A capsule restores only into the exact Browser image that wrote it; a person's checkout of a capsule another image
//! wrote starts with no capsule, and such a Sign-in shows as needing a person.
//!
//! A Supervisor's deletion records a durable fence first, holding the deletion's activity metadata (ADR-0120), then
//! removes every capsule and draft and proves them gone; the fenced record stays, as a tombstone no checkout passes,
//! until its completion is recorded. A Team process starting holds no checkout and no Save, so it ends every open
//! checkout as failed, removes what every fenced deletion left, and deletes every draft and capsule no record names.

mod draft;
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

pub use draft::Draft;
pub use record::Deleting;
use record::{Checkout as Held, Record};

/// The state file's schema.
pub const SCHEMA: i64 = 2;
/// The largest state file.
pub const MAX_STATE_BYTES: usize = 1024 * 1024;
/// The largest record plaintext.
pub const MAX_RECORD_BYTES: usize = 8192;
/// One capsule chunk's plaintext bound.
pub const CHUNK_BYTES: usize = 1024 * 1024;
/// The most chunks a capsule has.
pub const MAX_CHUNKS: usize = 160;
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
    // Each Team's generation whose publication is unknown: it needs a person until that is durably recorded.
    quarantine: HashMap<String, i64>,
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
    /// Every directory removal leaves the directory in place.
    pub remove: bool,
    /// The state directory cannot be synchronized.
    pub index_unsynced: bool,
}

/// Every Team's Sign-in on this Local.
pub struct SignInStore {
    root: PathBuf,
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

fn chunk_aad(checkout: &Checkout, generation: i64, index: usize) -> Vec<u8> {
    format!(
        "shimpz-sign-in-capsule\0{}\0{}\0{}\0{generation}\0{index}",
        checkout.team_id, checkout.incarnation, checkout.sign_in_id
    )
    .into_bytes()
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
            root: state.to_path_buf(),
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

    /// Deletes one directory and proves it gone; one already absent is no failure.
    fn remove(&self, directory: &Path) -> Result<(), SignInError> {
        // Only the store's own state is ever removed.
        if !directory.starts_with(&self.root) {
            return Err(SignInError::FAILED);
        }
        #[cfg(test)]
        if self.faults().remove {
            return if directory.symlink_metadata().is_ok() {
                Err(SignInError::FAILED)
            } else {
                Ok(())
            };
        }
        match std::fs::remove_dir_all(directory) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return Err(SignInError::FAILED),
        }
        if directory.symlink_metadata().is_ok() {
            return Err(SignInError::FAILED);
        }
        Ok(())
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

    fn capsule_dir(&self, sign_in_id: &str, generation: i64) -> PathBuf {
        self.root
            .join("capsules")
            .join(format!("{sign_in_id}-{generation}"))
    }

    fn drafts(&self, team_id: &str) -> PathBuf {
        self.root.join("drafts").join(team_id)
    }

    fn expired_now(&self, record: &Record) -> bool {
        record.generation != 0 && self.now() >= record.expires()
    }

    /// Records a generation whose publication was unknown as needing a person, durably; until then it stays held.
    fn settle_quarantine(&self, fences: &mut Fences, team_id: &str) -> Result<(), SignInError> {
        let Some(generation) = fences.quarantine.get(team_id).copied() else {
            return Ok(());
        };
        let (teams, record) = self.record(team_id)?;
        if let Some(mut record) = record
            && record.generation == generation
            && record.state != NEEDS_PERSON
        {
            NEEDS_PERSON.clone_into(&mut record.state);
            if self.put(teams, &record).is_err() {
                return Ok(());
            }
        }
        fences.quarantine.remove(team_id);
        Ok(())
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
    pub fn current(
        &self,
        team_id: &str,
        reader: Option<&str>,
    ) -> Result<Option<Value>, SignInError> {
        team_admitted(team_id)?;
        let mut fences = self.lock();
        self.settle_quarantine(&mut fences, team_id)?;
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
        let held = fences.quarantine.contains_key(team_id);
        let needs_person = Some(record.writer.as_str()) != reader || held;
        Ok(Some(record.public(needs_person)))
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
        self.settle_quarantine(&mut fences, team_id)?;
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
        let needs_person = record.as_ref().is_some_and(|record| {
            record.state == NEEDS_PERSON || fences.quarantine.contains_key(team_id)
        });
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

    /// The checked-out generation's chunk count, for the `reader` image to restore; `None` for a first checkout and
    /// for a person's checkout of a capsule another image wrote: they sign in anew.
    ///
    /// # Errors
    /// [`SignInError`] `sign-in-not-running` once the checkout is stale, `sign-in-busy` for an agent's checkout of a
    /// capsule another image wrote.
    pub fn capsule(
        &self,
        checkout: &Checkout,
        reader: &str,
    ) -> Result<Option<Capsule>, SignInError> {
        let _fences = self.lock();
        let Some((_teams, record)) = self.held(checkout)? else {
            return Err(SignInError::refused(NOT_RUNNING));
        };
        if checkout.generation == 0 {
            return Ok(None);
        }
        if record.writer != reader {
            return if checkout.kind == Kind::Person {
                Ok(None)
            } else {
                Err(SignInError::refused(BUSY))
            };
        }
        Ok(Some(Capsule {
            key: self.key(false)?,
            directory: self.capsule_dir(&checkout.sign_in_id, checkout.generation),
            checkout: checkout.clone(),
            chunks: usize::try_from(record.chunks).unwrap_or_default(),
            next: 0,
        }))
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

    /// Removes what the deletion `event_id` fenced, its capsules and drafts; whether nothing of it is left. A Sign-in
    /// no longer fenced by it was already deleted, and one there now was started afterwards: it stays.
    ///
    /// # Errors
    /// [`SignInError`] when the store cannot be read.
    pub fn purge(&self, team_id: &str, event_id: &str) -> Result<bool, SignInError> {
        team_admitted(team_id)?;
        let _fences = self.lock();
        let (_teams, record) = self.record(team_id)?;
        match record {
            Some(record) if record.fenced_by(event_id) => {
                Ok(self.purge_record(team_id, Some(&record)))
            }
            _ => Ok(true),
        }
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

    fn purge_record(&self, team_id: &str, record: Option<&Record>) -> bool {
        let capsules = record.map_or_else(Vec::new, |record| self.capsules_of(&record.sign_in_id));
        capsules
            .iter()
            .chain(std::iter::once(&self.drafts(team_id)))
            .all(|directory| self.remove(directory).is_ok())
    }

    /// Deletes the Team's Sign-in for its own lifecycle: capsules, drafts, and record; whether nothing is left. A
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
        // From the durable fence on, nothing checks the held generation out; the hold goes only once it is gone.
        if !self.purge_record(team_id, record.as_ref()) {
            return Ok(false);
        }
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
        fences.quarantine.remove(team_id);
        Ok(supervisor || !self.state()?.iter().any(|(team, _)| team == team_id))
    }

    fn capsules_of(&self, sign_in_id: &str) -> Vec<PathBuf> {
        let Ok(entries) = std::fs::read_dir(self.root.join("capsules")) else {
            return Vec::new();
        };
        entries
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| {
                path.file_name()
                    .and_then(|name| name.to_str())
                    .and_then(|name| name.rsplit_once('-'))
                    .is_some_and(|(owner, _)| owner == sign_in_id)
            })
            .collect()
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

    /// A Space reset: every Sign-in and every capsule is deleted, and every Save under way fenced. Only the fenced
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
        self.remove(&self.root.join("capsules"))?;
        self.remove(&self.root.join("drafts"))?;
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
    /// Supervisor's deletion keeps its fenced record. Then every draft and every capsule no record names is deleted.
    ///
    /// # Errors
    /// [`SignInError`] when the store cannot be read or cleaned.
    pub fn reconcile(&self) -> Result<(), SignInError> {
        for (team_id, _) in self.state()? {
            let (_teams, record) = self.record(&team_id)?;
            let Some(record) = record else { continue };
            match (&record.deleting, &record.checkout) {
                (Some(deleting), _) if !deleting.event_id.is_empty() => {
                    let _fences = self.lock();
                    self.purge_record(&team_id, Some(&record));
                }
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
        self.sweep()
    }

    /// Deletes every draft, and every capsule no record names.
    fn sweep(&self) -> Result<(), SignInError> {
        let _fences = self.lock();
        let mut named = Vec::new();
        for (team_id, _) in self.state()? {
            if let (_, Some(record)) = self.record(&team_id)? {
                named.push(format!("{}-{}", record.sign_in_id, record.generation));
            }
        }
        self.remove(&self.root.join("drafts"))?;
        let Ok(entries) = std::fs::read_dir(self.root.join("capsules")) else {
            return Ok(());
        };
        for entry in entries.filter_map(Result::ok) {
            let name = entry.file_name().to_string_lossy().into_owned();
            if !named.contains(&name) {
                self.remove(&entry.path())?;
            }
        }
        Ok(())
    }

    /// The next generation's capsule, sealed chunk by chunk as it arrives; nothing is kept until it commits.
    ///
    /// # Errors
    /// [`SignInError`] `sign-in-not-running` once the checkout is stale.
    pub fn draft(self: &Arc<Self>, checkout: &Checkout) -> Result<Draft, SignInError> {
        let fences = self.lock();
        if self.held(checkout)?.is_none() {
            return Err(SignInError::refused(NOT_RUNNING));
        }
        let fence = fences.saves.get(&checkout.team_id).copied().unwrap_or(0);
        drop(fences);
        Ok(Draft::new(Arc::clone(self), checkout.clone(), fence))
    }
}

/// A checked-out generation's capsule, chunk by chunk, each authenticated as it is read.
pub struct Capsule {
    key: Key,
    directory: PathBuf,
    checkout: Checkout,
    chunks: usize,
    next: usize,
}

impl std::fmt::Debug for Capsule {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Capsule")
            .field("chunks", &self.chunks)
            .finish_non_exhaustive()
    }
}

impl Iterator for Capsule {
    type Item = Result<Vec<u8>, super::runtime::CapsuleUnreadable>;

    fn next(&mut self) -> Option<Self::Item> {
        if self.next >= self.chunks {
            return None;
        }
        let index = self.next;
        self.next += 1;
        let path = self.directory.join(format!("{index:05}.chunk"));
        let sealed = private::read_private(&path, (CHUNK_BYTES + 12 + 16) as u64)
            .ok()
            .flatten();
        Some(
            sealed
                .and_then(|sealed| {
                    draft::open_chunk(
                        &self.key,
                        &sealed,
                        &chunk_aad(&self.checkout, self.checkout.generation, index),
                    )
                })
                .ok_or(super::runtime::CapsuleUnreadable),
        )
    }
}

#[cfg(test)]
pub(crate) mod tests;
