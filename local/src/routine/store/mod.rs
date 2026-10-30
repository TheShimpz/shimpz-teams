//! Local Routine persistence: one private state file per Team and its sealed run records (ADR-0086, ADR-0092).
//!
//! A Team's Routines, runs and their incidents, and notices live in one private JSON file that every transition
//! replaces atomically. A frozen run's secret-free continuation, a run's cursor, its recovery snapshot, and an
//! incident's compact evidence are each encrypted separately, bound by their AAD to exactly what they belong to; each
//! is written before the state that relies on it, so a crash leaves at worst an unreferenced one, which recovery
//! removes.
//!
//! **Locks.** Every Team has one lock, keyed by its directory name, which lives while a holder or waiter references
//! it. It is not re-entrant: [`RoutineStore::lock`] returns a [`TeamLock`], and everything done under it (reading the
//! state, a transition, a sealed record, the listing of sealed records) is a method of that guard, never a second
//! acquisition. The store's conveniences ([`RoutineStore::load`], [`RoutineStore::update`], [`RoutineStore::delete`])
//! hold the lock only for their own call. A Space reset takes [`RoutineStore::exclusive`]: writes are refused from
//! then on, every lock in use is awaited, and a write that began before the reset can never land after it.
//!
//! Everything here is blocking file I/O; async callers run it on the blocking pool.

mod codec;
mod remove;

use std::collections::HashMap;
use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError};

use ring::hmac;
use sha2::{Digest, Sha256};
use strict_json::Value;
use team_action::journal::MAX_OPERATIONS;
use team_core::entropy::Entropy;
use team_protocol::identifiers::canonical_team_id;
use team_protocol::python::json::{Options, canonical, dumps};
use team_protocol::routine::{MAX_OUTPUT_BYTES, ROUTINE_ID_RE};
use team_protocol::routine_notice::MAX_SUMMARY_BYTES;
use team_routine::cursor::{self, Binding, Cursor, MAX_CURSOR_BYTES};
use team_routine::plan::{
    MAX_PLAN_BYTES, MAX_RESOLVED_INPUT_BYTES, MAX_STEPS, TEAM_DEFINITION_BYTES,
};
use team_routine::record::{
    MAX_DISCARDS, MAX_ROUTINE_NOTICES, MAX_ROUTINES, MAX_RUNS, MAX_UNDELIVERED_NOTICES,
    TeamRoutines,
};
use team_storage::envelope::{Bounds, Envelope, Key};
use team_storage::private::{Keyring, StateError, read_private, write_private};

pub use self::codec::{SCHEMA, decode, encode, generation_of};

/// A notice at most: its shown output, its plan summary, its usage, and every other field.
pub const MAX_NOTICE_BYTES: usize = MAX_OUTPUT_BYTES + MAX_SUMMARY_BYTES + 8 * 1024;
const NOTICES_BYTES: usize = (MAX_UNDELIVERED_NOTICES + MAX_ROUTINE_NOTICES) * MAX_NOTICE_BYTES;
/// The most starts a Team's rolling window holds ([`team_routine::claim::starts::MAX_STARTS`]).
const STARTS: usize = 20_000;
const RECORDS_BYTES: usize = STARTS * 64 + MAX_DISCARDS * 256;
const ROUTINES_BYTES: usize = TEAM_DEFINITION_BYTES + MAX_ROUTINES * 18 * 1024;
/// The state file holds every definition at the Team's aggregate budget, each Routine's other fields, every
/// undelivered notice, and every run, start, and discard, each at its own bound.
pub const MAX_STATE_BYTES: usize =
    ROUTINES_BYTES + NOTICES_BYTES + RECORDS_BYTES + MAX_RUNS * 4 * 1024 + 64 * 1024;
/// The state bytes whose decoded Routine state is kept for reuse across every Team: room for one largest state.
pub const MAX_DECODED_STATE_BYTES: usize = MAX_STATE_BYTES;
/// Reads of one state file that may race its atomic replace before a failure is taken as real.
pub const UNLOCKED_READ_ATTEMPTS: usize = 3;
/// The recovery snapshot: the plan, its binding, and the Routine's name.
pub const MAX_RECOVERY_BYTES: usize = MAX_PLAN_BYTES + 8 * 1024;
/// The incident's compact evidence: its copy of the snapshot and its batch's operation rows.
pub const MAX_INCIDENT_BYTES: usize = MAX_RECOVERY_BYTES + MAX_OPERATIONS * 512 + 8 * 1024;
/// A frozen Routine run's continuation plaintext (`local/chat/continuation.py` `MAX_ROUTINE_PLAINTEXT_BYTES`): what a
/// chat continuation may hold (256 KiB), the pending request's resolved input, every step's interrupt id, and the
/// unfinished Action's earlier answers at their longest (7 of 16 000 characters, each escaped to six bytes, plus 512).
pub const MAX_ROUTINE_PLAINTEXT_BYTES: usize =
    256 * 1024 + MAX_RESOLVED_INPUT_BYTES + MAX_STEPS * 32 + 7 * (6 * 16_000 + 512);
/// A frozen Routine run's stored continuation (`MAX_ROUTINE_BYTES`): the base64 plaintext, its 64 release bindings of
/// at most 640 bytes each, and its kind.
pub const MAX_CONTINUATION_BYTES: usize =
    4 * MAX_ROUTINE_PLAINTEXT_BYTES.div_ceil(3) + 64 * (640 + 3) + 256;
/// The longest base64 part a sealed record may carry: the largest sealed record, encoded.
const MAX_ENCODED_PART: usize = max(MAX_CONTINUATION_BYTES, MAX_INCIDENT_BYTES) * 2 + 128;

const fn max(left: usize, right: usize) -> usize {
    if left > right { left } else { right }
}

/// Routine state is invalid or unavailable; callers fail closed. It never carries a path, key, or content.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StoreError {
    /// The Team id is not a canonical Team id.
    InvalidTeam,
    /// The run id is not a Routine run id.
    InvalidRun,
    /// A record handed to the store is outside its bounds.
    InvalidRecord,
    /// The state file is not one strict JSON document.
    NotJson,
    /// Stored state or a sealed record is malformed.
    Malformed,
    /// A file or directory failed its ownership contract.
    Unsafe,
    /// The state exceeds its fixed byte limit.
    TooLarge,
    /// A file, directory, or the keyring could not be read, listed, or created, or a required one is absent.
    Unavailable,
    /// A write or removal could not be made durable.
    NotPersisted,
    /// A sealed record did not authenticate.
    Unauthentic,
    /// A write-once record already holds other bytes.
    Immutable,
    /// The store is being reset; the write was refused.
    Resetting,
    /// The keyring lives inside the state root.
    Layout,
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::InvalidTeam => "Routine Team is invalid",
            Self::InvalidRun => "Routine run is invalid",
            Self::InvalidRecord => "Routine record is invalid",
            Self::NotJson => "Routine state is not valid JSON",
            Self::Malformed => "Routine state is malformed",
            Self::Unsafe => "Routine state failed its ownership contract",
            Self::TooLarge => "Routine state exceeds its fixed byte limit",
            Self::Unavailable => "Routine state is unavailable",
            Self::NotPersisted => "Routine state could not be persisted",
            Self::Unauthentic => "Routine record authentication failed",
            Self::Immutable => "Routine record is immutable",
            Self::Resetting => "Routine state is being reset",
            Self::Layout => "Routine keyring must be separate from Routine state",
        })
    }
}

impl std::error::Error for StoreError {}

impl From<StateError> for StoreError {
    fn from(error: StateError) -> Self {
        match error {
            StateError::Unsafe => Self::Unsafe,
            StateError::TooLarge => Self::TooLarge,
            StateError::NotPersisted => Self::NotPersisted,
            StateError::Malformed | StateError::RecordLimit | StateError::InvalidKey => {
                Self::Malformed
            }
            StateError::Unauthentic => Self::Unauthentic,
            StateError::Layout => Self::Layout,
            StateError::Unavailable => Self::Unavailable,
        }
    }
}

/// The canonical Team id, or [`StoreError::InvalidTeam`].
///
/// # Errors
/// [`StoreError::InvalidTeam`].
pub fn team_id(value: &str) -> Result<&str, StoreError> {
    canonical_team_id(&Value::String(value.to_owned()))
        .map(|_| value)
        .ok_or(StoreError::InvalidTeam)
}

/// A Routine run (or incident) id, or [`StoreError::InvalidRun`].
///
/// # Errors
/// [`StoreError::InvalidRun`].
pub fn run_id(value: &str) -> Result<&str, StoreError> {
    if ROUTINE_ID_RE.is_full(value) {
        Ok(value)
    } else {
        Err(StoreError::InvalidRun)
    }
}

/// The JSON of an AAD or digest header: compact, ASCII, insertion order.
const FRAMING: Options = Options {
    ensure_ascii: true,
    sort_keys: false,
    compact: true,
};

fn framed(parts: Vec<Value>) -> Vec<u8> {
    dumps(&Value::Array(parts), FRAMING).into_bytes()
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn continuation_aad(team: &str, run: &str) -> Vec<u8> {
    framed(vec![
        text("shimpz-local-routine-continuation-v1"),
        text(team),
        text(run),
    ])
}

/// A cursor opens only for its Team incarnation, Routine revision, and run.
fn cursor_aad(team: &str, binding: &Binding) -> Vec<u8> {
    framed(vec![
        text("shimpz-local-routine-cursor-v1"),
        text(team),
        text(&binding.incarnation),
        text(&binding.routine_id),
        Value::Number(strict_json::Number::Int(binding.revision)),
        text(&binding.run_id),
    ])
}

fn recovery_aad(team: &str, run: &str) -> Vec<u8> {
    framed(vec![
        text("shimpz-local-routine-recovery-v1"),
        text(team),
        text(run),
    ])
}

fn incident_aad(team: &str, incident: &str) -> Vec<u8> {
    framed(vec![
        text("shimpz-local-routine-incident-v1"),
        text(team),
        text(incident),
    ])
}

fn hex(bytes: &[u8]) -> String {
    let mut text = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        let _ = write!(text, "{byte:02x}");
    }
    text
}

/// One Team directory's lock: held or free, and the waiters for it.
#[derive(Debug, Default)]
struct Gate {
    held: Mutex<bool>,
    freed: Condvar,
}

/// An owned hold of one [`Gate`], released when dropped.
#[derive(Debug)]
struct Held(Arc<Gate>);

impl Held {
    fn acquire(gate: Arc<Gate>) -> Self {
        let mut held = gate.held.lock().unwrap_or_else(PoisonError::into_inner);
        while *held {
            held = gate
                .freed
                .wait(held)
                .unwrap_or_else(PoisonError::into_inner);
        }
        *held = true;
        drop(held);
        Self(gate)
    }
}

impl Drop for Held {
    fn drop(&mut self) {
        *self.0.held.lock().unwrap_or_else(PoisonError::into_inner) = false;
        self.0.freed.notify_one();
    }
}

/// What the store's guard protects: the reset flag and epoch, the Team locks, and the decoded states.
#[derive(Default)]
struct Guarded {
    closed: bool,
    epoch: u64,
    locks: HashMap<String, Arc<Gate>>,
    /// Each Team's state as its file last decoded, beside its exact bytes, least recently read first.
    decoded: Vec<(String, Vec<u8>, TeamRoutines)>,
}

/// Every Local Team's Routine state, changed only through serialized, atomic read-modify-write transitions.
pub struct RoutineStore {
    root: PathBuf,
    keyring: Keyring,
    entropy: Arc<dyn Entropy>,
    guard: Mutex<Guarded>,
    key_lock: Mutex<()>,
    max_state_bytes: usize,
    max_decoded_bytes: usize,
}

impl std::fmt::Debug for RoutineStore {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RoutineStore")
            .finish_non_exhaustive()
    }
}

impl RoutineStore {
    /// The store of `root` (`routines/state`) sealed under the keyring at `key_path` (`routines/key/aes256.key`).
    ///
    /// # Errors
    /// [`StoreError::Layout`] when the keyring lives inside the state root.
    pub fn new(
        root: &Path,
        key_path: &Path,
        entropy: Arc<dyn Entropy>,
    ) -> Result<Self, StoreError> {
        if key_path.parent() == Some(root) || key_path.starts_with(root) {
            return Err(StoreError::Layout);
        }
        Ok(Self {
            root: root.to_path_buf(),
            keyring: Keyring::new(key_path.to_path_buf()),
            entropy,
            guard: Mutex::new(Guarded::default()),
            key_lock: Mutex::new(()),
            max_state_bytes: MAX_STATE_BYTES,
            max_decoded_bytes: MAX_DECODED_STATE_BYTES,
        })
    }

    fn guarded(&self) -> MutexGuard<'_, Guarded> {
        self.guard.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The directory name of `team`: the SHA-256 of its id, so no id ever becomes a path.
    pub(crate) fn team_dir_name(team: &str) -> String {
        hex(&Sha256::digest(team.as_bytes()))
    }

    fn team_dir(&self, team: &str) -> PathBuf {
        self.root.join(Self::team_dir_name(team))
    }

    /// The gate of one Team directory, shared by every holder and waiter; gates nobody references are forgotten.
    fn gate(&self, name: &str) -> (Arc<Gate>, u64) {
        let mut guarded = self.guarded();
        guarded.locks.retain(|_, gate| Arc::strong_count(gate) > 1);
        let gate = Arc::clone(guarded.locks.entry(name.to_owned()).or_default());
        (gate, guarded.epoch)
    }

    /// Holds the Team's Routine lock; everything done under it is a method of the returned guard.
    ///
    /// # Errors
    /// [`StoreError::InvalidTeam`].
    pub fn lock(&self, team: &str) -> Result<TeamLock<'_>, StoreError> {
        let team = team_id(team)?;
        let (gate, epoch) = self.gate(&Self::team_dir_name(team));
        Ok(TeamLock {
            store: self,
            team: team.to_owned(),
            epoch,
            _held: Held::acquire(gate),
        })
    }

    /// The Team's current state, read under its lock.
    ///
    /// # Errors
    /// [`StoreError`] when it cannot be read or is malformed.
    pub fn load(&self, team: &str) -> Result<TeamRoutines, StoreError> {
        self.lock(team)?.load()
    }

    /// Applies one transition under the Team's lock and persists it before returning its result; an unchanged state
    /// is not written.
    ///
    /// # Errors
    /// The transition's own error, or a [`StoreError`] converted into it.
    pub fn update<T, E: From<StoreError>>(
        &self,
        team: &str,
        change: impl FnOnce(TeamRoutines) -> Result<(TeamRoutines, T), E>,
    ) -> Result<T, E> {
        self.lock(team)?.update(change)
    }

    /// Removes the Team's whole Routine directory and everything it holds; an absent Team is already clean.
    ///
    /// # Errors
    /// [`StoreError`] when it cannot be removed.
    pub fn delete(&self, team: &str) -> Result<(), StoreError> {
        self.lock(team)?.delete()
    }

    /// Holds every Team's Routine lock with writes refused, as a Space reset needs; the epoch then moves on.
    #[must_use]
    pub fn exclusive(&self) -> Exclusive<'_> {
        let gates: Vec<(String, Arc<Gate>)> = {
            let mut guarded = self.guarded();
            guarded.closed = true;
            guarded.epoch += 1;
            guarded
                .locks
                .iter()
                .map(|(name, gate)| (name.clone(), Arc::clone(gate)))
                .collect()
        };
        let mut exclusive = Exclusive {
            store: self,
            raw: HashMap::new(),
            teams: HashMap::new(),
        };
        for (name, gate) in gates {
            exclusive.raw.insert(name, Held::acquire(gate));
        }
        exclusive
    }

    fn keep(&self, team: &str, payload: Vec<u8>, state: TeamRoutines) {
        let mut guarded = self.guarded();
        guarded.decoded.retain(|(kept, _, _)| kept != team);
        guarded.decoded.push((team.to_owned(), payload, state));
        while guarded
            .decoded
            .iter()
            .map(|(_, bytes, _)| bytes.len())
            .sum::<usize>()
            > self.max_decoded_bytes
        {
            guarded.decoded.remove(0);
        }
    }

    fn forget(&self, team: &str) {
        self.guarded().decoded.retain(|(kept, _, _)| kept != team);
    }

    fn key(&self, create: bool) -> Result<Key, StoreError> {
        // One keyring for every Team: concurrent first writers must not each create a different key.
        let _held = self.key_lock.lock().unwrap_or_else(PoisonError::into_inner);
        Ok(self.keyring.load(create, self.entropy.as_ref())?)
    }

    /// Every Team with Routine state, read from the state files themselves.
    ///
    /// # Errors
    /// [`StoreError`] when the root cannot be listed or a state file is unreadable or filed under another Team.
    pub fn teams(&self) -> Result<Vec<String>, StoreError> {
        let mut teams = Vec::new();
        for name in remove::owned_directories(&self.root)? {
            let Some(payload) = self.read_unlocked(&self.root.join(&name).join("state.json"))?
            else {
                continue;
            };
            let value = strict_json::parse(&payload).map_err(|_| StoreError::Malformed)?;
            let team = match value.get("team_id") {
                Some(Value::String(team)) if canonical_team_id(&text(team)).is_some() => {
                    team.clone()
                }
                _ => return Err(StoreError::Malformed),
            };
            if Self::team_dir_name(&team) != name {
                return Err(StoreError::Malformed);
            }
            teams.push(team);
        }
        Ok(teams)
    }

    /// A state file read without its Team's lock, which the Team is only known from once it is read: a writer's
    /// atomic replace can unlink the very file a reader just opened, so a failing read is tried again.
    fn read_unlocked(&self, path: &Path) -> Result<Option<Vec<u8>>, StoreError> {
        let maximum = self.max_state_bytes as u64;
        for _attempt in 1..UNLOCKED_READ_ATTEMPTS {
            if let Ok(payload) = read_private(path, maximum) {
                return Ok(payload);
            }
        }
        Ok(read_private(path, maximum)?)
    }

    /// The keyed digest a `changes` Routine compares one run's safe result by (ADR-0092 amendment, 2026-10-05).
    ///
    /// It is bound to the Team, its incarnation, the Routine revision, and the shown step, under a key derived apart
    /// from the sealing key (HKDF-SHA256, info `shimpz-routine-output-v1`), so neither a known Routine id nor a
    /// guessed result can be checked against it offline.
    ///
    /// # Errors
    /// [`StoreError`] for an invalid Team or an unavailable keyring.
    pub fn output_digest(
        &self,
        team: &str,
        binding: &Binding,
        step: &str,
        material: &[u8],
    ) -> Result<String, StoreError> {
        let team = team_id(team)?;
        let key = self.key(true)?;
        let derived = hkdf_sha256(key.expose(), b"shimpz-routine-output-v1");
        // A JSON header holds no raw line break and names the material's length, so the framing is unambiguous.
        let length = i64::try_from(material.len()).unwrap_or(i64::MAX);
        let mut message = framed(vec![
            text(team),
            text(&binding.incarnation),
            text(&binding.routine_id),
            Value::Number(strict_json::Number::Int(binding.revision)),
            text(step),
            Value::Number(strict_json::Number::Int(length)),
        ]);
        message.push(b'\n');
        message.extend_from_slice(material);
        let signing = hmac::Key::new(hmac::HMAC_SHA256, &derived);
        Ok(hex(hmac::sign(&signing, &message).as_ref()))
    }

    /// Removes every Team's Routine state, including a Team no network names now, and the keyring.
    ///
    /// # Errors
    /// [`StoreError`] when anything owned cannot be listed or removed.
    pub fn delete_all(&self) -> Result<(), StoreError> {
        for name in remove::owned_directories(&self.root)? {
            remove::remove_directory(&self.root, &name)?;
        }
        self.guarded().decoded.clear();
        match std::fs::remove_file(self.keyring.path()) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(_) => Err(StoreError::NotPersisted),
        }
    }
}

/// HKDF-SHA256 with no salt (RFC 5869: a hash-length zero salt) and one 32-byte output block.
fn hkdf_sha256(input: &[u8], info: &[u8]) -> [u8; 32] {
    let salt = hmac::Key::new(hmac::HMAC_SHA256, &[0_u8; 32]);
    let pseudorandom = hmac::sign(&salt, input);
    let expand = hmac::Key::new(hmac::HMAC_SHA256, pseudorandom.as_ref());
    let mut block = info.to_vec();
    block.push(1);
    let mut output = [0_u8; 32];
    output.copy_from_slice(hmac::sign(&expand, &block).as_ref());
    output
}

/// The kind of sealed record, with its name suffix, AAD, and plaintext bound.
#[derive(Clone, Copy, Debug)]
enum Sealed {
    Continuation,
    Cursor,
    Recovery,
    Incident,
}

impl Sealed {
    const fn suffix(self) -> &'static str {
        match self {
            Self::Continuation => ".continuation",
            Self::Cursor => ".cursor",
            Self::Recovery => ".recovery",
            Self::Incident => ".incident",
        }
    }

    const fn maximum(self) -> usize {
        match self {
            Self::Continuation => MAX_CONTINUATION_BYTES,
            Self::Cursor => MAX_CURSOR_BYTES,
            Self::Recovery => MAX_RECOVERY_BYTES,
            Self::Incident => MAX_INCIDENT_BYTES,
        }
    }
}

/// One Team's Routine lock, held: every read and write of the Team's Routine state happens through it.
pub struct TeamLock<'s> {
    store: &'s RoutineStore,
    team: String,
    epoch: u64,
    _held: Held,
}

impl std::fmt::Debug for TeamLock<'_> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("TeamLock")
            .field("team", &self.team)
            .finish_non_exhaustive()
    }
}

impl TeamLock<'_> {
    /// The Team it holds.
    #[must_use]
    pub fn team(&self) -> &str {
        &self.team
    }

    fn dir(&self) -> PathBuf {
        self.store.team_dir(&self.team)
    }

    fn writable(&self) -> Result<(), StoreError> {
        let guarded = self.store.guarded();
        if guarded.closed || guarded.epoch != self.epoch {
            return Err(StoreError::Resetting);
        }
        Ok(())
    }

    /// The Team's current state; an absent file is the empty state.
    ///
    /// # Errors
    /// [`StoreError`] when it cannot be read or is malformed.
    pub fn load(&self) -> Result<TeamRoutines, StoreError> {
        let path = self.dir().join("state.json");
        let Some(payload) = read_private(&path, self.store.max_state_bytes as u64)? else {
            self.store.forget(&self.team);
            return Ok(TeamRoutines::default());
        };
        {
            let mut guarded = self.store.guarded();
            if let Some(index) = guarded
                .decoded
                .iter()
                .position(|(team, bytes, _)| *team == self.team && *bytes == payload)
            {
                let kept = guarded.decoded.remove(index);
                let state = kept.2.clone();
                guarded.decoded.push(kept);
                return Ok(state);
            }
        }
        let state = decode(&payload, &self.team)?;
        self.store.keep(&self.team, payload, state.clone());
        Ok(state)
    }

    /// Applies one transition and persists it before returning its result; an unchanged state is not written.
    ///
    /// # Errors
    /// The transition's own error, or a [`StoreError`] converted into it.
    pub fn update<T, E: From<StoreError>>(
        &self,
        change: impl FnOnce(TeamRoutines) -> Result<(TeamRoutines, T), E>,
    ) -> Result<T, E> {
        self.writable()?;
        let before = self.load()?;
        let (after, result) = change(before.clone())?;
        // Equal values, or the same canonical bytes (a value read back has its members in sorted order).
        if after != before && encode(&after, &self.team) != encode(&before, &self.team) {
            self.save(&after)?;
        }
        Ok(result)
    }

    fn save(&self, state: &TeamRoutines) -> Result<(), StoreError> {
        let payload = encode(state, &self.team);
        if payload.len() > self.store.max_state_bytes {
            return Err(StoreError::TooLarge);
        }
        // Decoding what was encoded checks it before it replaces the file, and is the next read's state.
        self.store.forget(&self.team);
        let decoded = decode(&payload, &self.team)?;
        write_private(&self.dir().join("state.json"), &payload)?;
        self.store.keep(&self.team, payload, decoded);
        Ok(())
    }

    /// Seals one record; `write_once` accepts only its exact first bytes again, idempotently, and refuses others.
    fn sealed_write(
        &self,
        name: &str,
        payload: &[u8],
        aad: &[u8],
        kind: Sealed,
        write_once: bool,
    ) -> Result<(), StoreError> {
        self.writable()?;
        if write_once && let Some(sealed) = self.sealed_read(name, aad, kind)? {
            return if sealed == payload {
                Ok(())
            } else {
                Err(StoreError::Immutable)
            };
        }
        let key = self.store.key(true)?;
        let envelope = Envelope::seal(&key, payload, aad, self.store.entropy.as_ref())
            .map_err(|_| StoreError::Unavailable)?;
        write_private(&self.dir().join(name), &canonical(&envelope.to_value()))?;
        Ok(())
    }

    /// One sealed record, or `None` when absent; one that cannot be read, opened, or authenticated fails closed.
    fn sealed_read(
        &self,
        name: &str,
        aad: &[u8],
        kind: Sealed,
    ) -> Result<Option<Vec<u8>>, StoreError> {
        let maximum = kind.maximum();
        let Some(raw) = read_private(&self.dir().join(name), (maximum * 2 + 256) as u64)? else {
            return Ok(None);
        };
        let value = strict_json::parse(&raw).map_err(|_| StoreError::Malformed)?;
        let bounds = Bounds {
            maximum_encoded_part: MAX_ENCODED_PART,
            maximum_plaintext: maximum,
        };
        let envelope = Envelope::admit(&value, bounds).map_err(|_| StoreError::Malformed)?;
        let key = self.store.key(false)?;
        let opened = envelope
            .open(&key, aad)
            .map_err(|_| StoreError::Unauthentic)?;
        Ok(Some(opened.to_vec()))
    }

    fn sealed_delete(&self, name: &str) -> Result<(), StoreError> {
        match std::fs::remove_file(self.dir().join(name)) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(_) => Err(StoreError::NotPersisted),
        }
    }

    /// The run ids with a stored record of `kind`, including any a crash left unreferenced, sorted.
    fn sealed_names(&self, kind: Sealed) -> Result<Vec<String>, StoreError> {
        let entries = match std::fs::read_dir(self.dir()) {
            Ok(entries) => entries,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(_) => return Err(StoreError::Unavailable),
        };
        let mut names = Vec::new();
        for entry in entries {
            let name = entry.map_err(|_| StoreError::Unavailable)?.file_name();
            if let Some(run) = name
                .to_str()
                .and_then(|name| name.strip_suffix(kind.suffix()))
                && ROUTINE_ID_RE.is_full(run)
            {
                names.push(run.to_owned());
            }
        }
        names.sort();
        Ok(names)
    }

    /// Seals a frozen run's secret-free continuation.
    ///
    /// # Errors
    /// [`StoreError`] for an invalid run or payload, a reset, or a failed write.
    pub fn put_continuation(&self, run: &str, payload: &[u8]) -> Result<(), StoreError> {
        let run = run_id(run)?;
        if !(1..=MAX_CONTINUATION_BYTES).contains(&payload.len()) {
            return Err(StoreError::InvalidRecord);
        }
        let name = format!("{run}{}", Sealed::Continuation.suffix());
        self.sealed_write(
            &name,
            payload,
            &continuation_aad(&self.team, run),
            Sealed::Continuation,
            false,
        )
    }

    /// A frozen run's continuation.
    ///
    /// # Errors
    /// [`StoreError::Unavailable`] when it is absent, or a read failure.
    pub fn continuation(&self, run: &str) -> Result<Vec<u8>, StoreError> {
        let run = run_id(run)?;
        let name = format!("{run}{}", Sealed::Continuation.suffix());
        self.sealed_read(
            &name,
            &continuation_aad(&self.team, run),
            Sealed::Continuation,
        )?
        .ok_or(StoreError::Unavailable)
    }

    /// Removes a run's continuation; an absent one is already removed.
    ///
    /// # Errors
    /// [`StoreError`] for an invalid run or a failed removal.
    pub fn delete_continuation(&self, run: &str) -> Result<(), StoreError> {
        self.sealed_delete(&format!(
            "{}{}",
            run_id(run)?,
            Sealed::Continuation.suffix()
        ))
    }

    /// The run ids with a stored continuation.
    ///
    /// # Errors
    /// [`StoreError::Unavailable`] when they cannot be listed.
    pub fn continuations(&self) -> Result<Vec<String>, StoreError> {
        self.sealed_names(Sealed::Continuation)
    }

    /// Seals one replayed run's cursor under exactly its binding (ADR-0092); a sealed loss of protection stays.
    ///
    /// # Errors
    /// [`StoreError`] for an invalid cursor, a reset, or a failed read or write.
    pub fn put_cursor(&self, cursor: &Cursor) -> Result<(), StoreError> {
        let sealed = if cursor.protection_lost {
            None
        } else {
            self.cursor(&cursor.binding)?
        };
        if sealed.is_some_and(|sealed| sealed.protection_lost) {
            let lost = cursor::lose_protection(cursor).map_err(|_| StoreError::InvalidRecord)?;
            return self.seal_cursor(&lost);
        }
        self.seal_cursor(cursor)
    }

    /// Marks the run's latest sealed cursor as having lost its protection, keeping its progress (ADR-0101).
    ///
    /// # Errors
    /// [`StoreError`] for a failed read or write.
    pub fn lose_cursor(&self, binding: &Binding) -> Result<(), StoreError> {
        if let Some(sealed) = self.cursor(binding)?
            && !sealed.protection_lost
        {
            let lost = cursor::lose_protection(&sealed).map_err(|_| StoreError::InvalidRecord)?;
            self.seal_cursor(&lost)?;
        }
        Ok(())
    }

    fn seal_cursor(&self, cursor: &Cursor) -> Result<(), StoreError> {
        let payload = cursor::encode(cursor).map_err(|_| StoreError::InvalidRecord)?;
        let binding = &cursor.binding;
        let name = format!("{}{}", run_id(&binding.run_id)?, Sealed::Cursor.suffix());
        self.sealed_write(
            &name,
            &payload,
            &cursor_aad(&self.team, binding),
            Sealed::Cursor,
            false,
        )
    }

    /// The run's cursor when one was sealed under exactly this binding; another binding never opens it.
    ///
    /// # Errors
    /// [`StoreError`] for a cursor that cannot be read, authenticated, or decoded.
    pub fn cursor(&self, binding: &Binding) -> Result<Option<Cursor>, StoreError> {
        let name = format!("{}{}", run_id(&binding.run_id)?, Sealed::Cursor.suffix());
        let Some(payload) =
            self.sealed_read(&name, &cursor_aad(&self.team, binding), Sealed::Cursor)?
        else {
            return Ok(None);
        };
        cursor::decode(&payload, binding)
            .map(Some)
            .map_err(|_| StoreError::Malformed)
    }

    /// Removes a run's cursor.
    ///
    /// # Errors
    /// [`StoreError`] for an invalid run or a failed removal.
    pub fn delete_cursor(&self, run: &str) -> Result<(), StoreError> {
        self.sealed_delete(&format!("{}{}", run_id(run)?, Sealed::Cursor.suffix()))
    }

    /// The run ids with a stored cursor.
    ///
    /// # Errors
    /// [`StoreError::Unavailable`] when they cannot be listed.
    pub fn cursors(&self) -> Result<Vec<String>, StoreError> {
        self.sealed_names(Sealed::Cursor)
    }

    /// Removes the Team's whole Routine directory, never following a link out of Routine storage.
    ///
    /// # Errors
    /// [`StoreError`] when it cannot be removed.
    pub fn delete(&self) -> Result<(), StoreError> {
        remove::remove_directory(&self.store.root, &RoutineStore::team_dir_name(&self.team))?;
        self.store.forget(&self.team);
        Ok(())
    }
}

/// Every Team's Routine lock held with writes refused, for a Space reset; writes are admitted again when dropped.
pub struct Exclusive<'s> {
    store: &'s RoutineStore,
    raw: HashMap<String, Held>,
    teams: HashMap<String, TeamLock<'s>>,
}

impl std::fmt::Debug for Exclusive<'_> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.debug_struct("Exclusive").finish_non_exhaustive()
    }
}

impl<'s> Exclusive<'s> {
    /// Every Team with Routine state.
    ///
    /// # Errors
    /// As [`RoutineStore::teams`].
    pub fn teams(&self) -> Result<Vec<String>, StoreError> {
        self.store.teams()
    }

    /// The held lock of `team`: the one taken when the reset began, or one taken now for a Team first seen since.
    ///
    /// # Errors
    /// [`StoreError::InvalidTeam`].
    pub fn team(&mut self, team: &str) -> Result<&TeamLock<'s>, StoreError> {
        let team = team_id(team)?.to_owned();
        if !self.teams.contains_key(&team) {
            let name = RoutineStore::team_dir_name(&team);
            let held = match self.raw.remove(&name) {
                Some(held) => held,
                None => Held::acquire(self.store.gate(&name).0),
            };
            let epoch = self.store.guarded().epoch;
            let lock = TeamLock {
                store: self.store,
                team: team.clone(),
                epoch,
                _held: held,
            };
            self.teams.insert(team.clone(), lock);
        }
        self.teams.get(&team).ok_or(StoreError::InvalidTeam)
    }

    /// Removes every Team's Routine state and the keyring.
    ///
    /// # Errors
    /// As [`RoutineStore::delete_all`].
    pub fn delete_all(&self) -> Result<(), StoreError> {
        self.store.delete_all()
    }
}

impl Drop for Exclusive<'_> {
    fn drop(&mut self) {
        self.teams.clear();
        self.raw.clear();
        self.store.guarded().closed = false;
    }
}

mod records;
#[cfg(test)]
mod tests;
