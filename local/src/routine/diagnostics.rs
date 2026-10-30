//! Encrypted per-execution diagnostics of a Local Team's Routine runs (ADR-0092 section 8).
//!
//! Each attempt's Team-sanitized failure diagnostic, or its safe transport condition, is one AES-256-GCM file in its
//! own Team-owned blob family, apart from plaintext Routine state, continuations, and their keyring. The family lives
//! in the `diagnostics` directory of the Routine state volume and its keyring is `diagnostics.key` beside the Routine
//! keyring: neither name is a Routine Team directory or the Routine keyring, so each store ignores the other. The AAD
//! binds the Team and its incarnation (its network id) and the body's name (Routine, run, logical operation, attempt,
//! recording instant), so a body is readable only by the same incarnation of the same Team as exactly that attempt.
//! Each body names its incarnation under that authentication, so another incarnation's authentic body is left out
//! while any corrupted body fails the read. Beside them the family keeps each run's step records and its terminal
//! record (ADR-0092 amendment, 2026-10-05, scale). Every body expires after seven days, and a Team keeps at most 24 MiB
//! of them, the oldest giving way first whatever its kind. A body never holds a password or any other value Team
//! injected.

mod names;
pub mod view;

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use sha2::{Digest, Sha256};
use strict_json::{Number, Value};
use team_action::failure::ActionFailure;
use team_core::entropy::Entropy;
use team_protocol::identifiers::canonical_team_id;
use team_protocol::payload::SHA256_RE;
use team_protocol::python::json::{CANONICAL, Options, canonical, dumps};
use team_protocol::python::value::{object_of, small_int};
use team_protocol::routine::{
    MAX_ROUTINE_STEPS, MAX_STEP_VIEW_BYTES, PLAN_DIGEST_RE, ROUTINE_ID_RE, escaped, valid_revision,
    whole,
};
use team_protocol::routine_run::{
    CONDITION_RE, MAX_RUN_DIAGNOSTICS, RUN_STEP_STATUSES, canonical_diagnostic, canonical_run_step,
};
use team_routine::trace::Protection;
use team_storage::custody::DirectoryCustody;
use team_storage::durable::{DirectoryError, ensure_directory};
use team_storage::envelope::{Bounds, Envelope};
use team_storage::private::{Keyring, StateError, instant_text, read_private, write_private};

use self::names::{Entry, Kind, entries};

/// How long a body is kept.
pub const RETENTION_SECONDS: i64 = 7 * 86_400;
/// A diagnostic's plaintext bound.
pub const MAX_PLAINTEXT_BYTES: usize = 12 * 1024;
/// A step record: one run step's wire view and the run binding beside it.
pub const MAX_STEP_PLAINTEXT_BYTES: usize = MAX_STEP_VIEW_BYTES + 2 * 1024;
/// A run's terminal record: its binding, how far its replay went, and whether the next step was dispatched.
pub const MAX_RUN_PLAINTEXT_BYTES: usize = 1024;
/// The bodies one Team keeps at most.
pub const MAX_TEAM_BYTES: u64 = 24 * 1024 * 1024;

/// A sealed file's bytes for this much plaintext: its AES-GCM tag, base64, and JSON envelope.
#[must_use]
pub const fn sealed_bound(plaintext: usize) -> usize {
    4 * (plaintext + 16).div_ceil(3) + 256
}

/// The largest sealed body.
pub const MAX_FILE_BYTES: usize = sealed_bound(MAX_STEP_PLAINTEXT_BYTES);

/// Diagnostic state is invalid or unavailable; callers fail closed. It never carries a path, key, or content.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DiagnosticError {
    /// The Team id is not canonical.
    InvalidTeam,
    /// The incarnation is not a network id.
    InvalidIncarnation,
    /// The run id is not a Routine run id.
    InvalidRun,
    /// A record handed to the store is outside its contract.
    InvalidRecord,
    /// A record exceeds its fixed byte limit.
    TooLarge,
    /// A record would hold a protected value.
    Protected,
    /// A stored body is malformed, or an authentic body is outside the current record shape.
    Malformed,
    /// A body did not authenticate.
    Unauthentic,
    /// A file or directory failed its ownership contract.
    Unsafe,
    /// The bodies or the keyring could not be read, listed, or created.
    Unavailable,
    /// A write or removal could not be made durable.
    NotPersisted,
    /// A page named a snapshot of a run's records that no longer holds.
    RunChanged,
    /// A run's retained records disagree on the run they belong to.
    Disagree,
    /// The keyring lives inside the bodies' root.
    Layout,
}

impl std::fmt::Display for DiagnosticError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::InvalidTeam => "Routine diagnostic Team is invalid",
            Self::InvalidIncarnation => "Routine diagnostic incarnation is invalid",
            Self::InvalidRun => "Routine run is invalid",
            Self::InvalidRecord => "Routine diagnostic is invalid",
            Self::TooLarge => "Routine diagnostic exceeds its fixed byte limit",
            Self::Protected => "Routine diagnostic would hold a protected value",
            Self::Malformed => "Routine diagnostic is malformed",
            Self::Unauthentic => "Routine diagnostic authentication failed",
            Self::Unsafe => "Routine diagnostics failed their ownership contract",
            Self::Unavailable => "Routine diagnostics are unavailable",
            Self::NotPersisted => "Routine diagnostic could not be persisted",
            Self::RunChanged => "Routine run records changed",
            Self::Disagree => "Routine run records disagree",
            Self::Layout => "Routine diagnostic keyring must be separate from its bodies",
        })
    }
}

impl std::error::Error for DiagnosticError {}

impl From<StateError> for DiagnosticError {
    fn from(error: StateError) -> Self {
        match error {
            StateError::Unsafe => Self::Unsafe,
            StateError::TooLarge => Self::TooLarge,
            StateError::NotPersisted => Self::NotPersisted,
            StateError::Unauthentic => Self::Unauthentic,
            StateError::Layout => Self::Layout,
            StateError::Malformed | StateError::RecordLimit | StateError::InvalidKey => {
                Self::Malformed
            }
            StateError::Unavailable => Self::Unavailable,
        }
    }
}

type Stored<T> = Result<T, DiagnosticError>;

fn team_id(value: &str) -> Stored<&str> {
    canonical_team_id(&Value::String(value.to_owned()))
        .map(|_| value)
        .ok_or(DiagnosticError::InvalidTeam)
}

fn incarnation(value: &str) -> Stored<&str> {
    if SHA256_RE.is_full(value) {
        Ok(value)
    } else {
        Err(DiagnosticError::InvalidIncarnation)
    }
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn int(value: i64) -> Value {
    Value::Number(Number::Int(value))
}

/// The JSON text inside the quotes of `value`, unescaped beyond ASCII (`json.dumps(value, ensure_ascii=False)[1:-1]`).
fn escaped_once(value: &str) -> String {
    let quoted = dumps(&text(value), CANONICAL);
    quoted[1..quoted.len() - 1].to_owned()
}

/// Every form a protected value can take in a body: raw, JSON-escaped, or preview-escaped, each escaped again.
fn forms(secret: &str) -> Vec<String> {
    let once = escaped_once(secret);
    let shown = escaped(&once);
    let mut forms = Vec::with_capacity(6);
    for value in [secret.to_owned(), once, shown] {
        forms.push(escaped_once(&value));
        forms.push(value);
    }
    forms
}

fn holds_protected(payload: &str, protected: &[String]) -> bool {
    protected
        .iter()
        .filter(|secret| !secret.is_empty())
        .any(|secret| {
            forms(secret)
                .iter()
                .any(|form| payload.contains(form.as_str()))
        })
}

/// One attempt's diagnostic, keyed by the Routine run and logical operation it belongs to.
#[derive(Clone, Debug, PartialEq)]
pub struct Diagnostic {
    /// The Routine.
    pub routine_id: String,
    /// The run.
    pub run_id: String,
    /// The logical operation.
    pub operation_id: String,
    /// The attempt, from 1.
    pub attempt: i64,
    /// The Assistant.
    pub assistant_id: String,
    /// The Action.
    pub action: String,
    /// The attempt's call: a replay step by its position in the plan (ADR-0101).
    pub position: i64,
    /// When it was recorded.
    pub recorded_at: i64,
    /// The sanitized failure document, or `None`.
    pub failure: Option<Value>,
    /// The safe transport condition, or `None`.
    pub condition: Option<String>,
}

impl Diagnostic {
    /// The wire form a Supervisor reads, which `routine_run.canonical_diagnostic` admits.
    #[must_use]
    pub fn view(&self) -> Value {
        object_of(vec![
            ("operation_id", text(&self.operation_id)),
            ("attempt", int(self.attempt)),
            ("assistant_id", text(&self.assistant_id)),
            ("action", text(&self.action)),
            ("position", int(self.position)),
            ("recorded_at", text(&instant_text(self.recorded_at))),
            ("failure", self.failure.clone().unwrap_or(Value::Null)),
            (
                "condition",
                self.condition.as_deref().map_or(Value::Null, text),
            ),
        ])
    }

    /// Its body's file name.
    #[must_use]
    pub fn name(&self) -> String {
        format!(
            "{}.{}.{}.{}.{}.diagnostic",
            self.recorded_at, self.routine_id, self.run_id, self.operation_id, self.attempt
        )
    }
}

/// The run a record belongs to, the exact revision of its Routine it executed, and that revision's replay steps.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RunBinding {
    /// The Routine.
    pub routine_id: String,
    /// The run.
    pub run_id: String,
    /// The revision it executed.
    pub revision: i64,
    /// That revision's plan digest.
    pub plan_digest: String,
    /// That revision's replay steps.
    pub total: i64,
}

impl RunBinding {
    fn document(&self) -> Vec<(&'static str, Value)> {
        vec![
            ("routine_id", text(&self.routine_id)),
            ("revision", int(self.revision)),
            ("plan_digest", text(&self.plan_digest)),
            ("total", int(self.total)),
        ]
    }
}

/// What one step's attempt did: its status, how long it took, and the redacted inputs it was given.
#[derive(Clone, Debug, PartialEq)]
pub struct StepRecord {
    /// The run.
    pub binding: RunBinding,
    /// The step's 1-based position.
    pub position: i64,
    /// The Assistant.
    pub assistant_id: String,
    /// The Action.
    pub action: String,
    /// One of the run step statuses.
    pub status: String,
    /// The attempt.
    pub attempt: i64,
    /// How long it took, or `None`.
    pub duration_ms: Option<i64>,
    /// When it was recorded.
    pub recorded_at: i64,
    /// The redacted inputs it was given, or `None` when none were kept.
    pub inputs: Option<Value>,
}

impl StepRecord {
    /// The wire form of this step, which `routine_run.canonical_run_step` admits.
    #[must_use]
    pub fn view(&self) -> Vec<(&'static str, Value)> {
        vec![
            ("position", int(self.position)),
            ("status", text(&self.status)),
            ("assistant_id", text(&self.assistant_id)),
            ("action", text(&self.action)),
            ("attempt", int(self.attempt)),
            ("duration_ms", self.duration_ms.map_or(Value::Null, int)),
            ("inputs", self.inputs.clone().unwrap_or(Value::Null)),
        ]
    }
}

/// A run's terminal record: how many steps its sealed cursor completed and whether the next one was dispatched.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RunRecord {
    /// The run.
    pub binding: RunBinding,
    /// The steps completed.
    pub reached: i64,
    /// Whether the next step was dispatched.
    pub dispatched: bool,
    /// When it was recorded.
    pub recorded_at: i64,
}

/// One run's retained records as one snapshot: its identity, each position's latest step record, and the run record.
#[derive(Clone, Debug, PartialEq)]
pub struct RunSteps {
    /// The snapshot's identity.
    pub snapshot: String,
    /// Each position's latest step record.
    pub steps: BTreeMap<i64, Value>,
    /// The terminal record, if retained.
    pub run: Option<Value>,
}

/// Each position's latest step record, beside the sequence it was written under.
type Sequenced = BTreeMap<i64, (u64, Value)>;

/// A body about to be sealed: its name, kind, and recording instant.
struct Named {
    name: String,
    kind: Kind,
    recorded_at: i64,
}

/// Every Local Team's encrypted Routine diagnostics and run records, in one blob family with its own keyring.
pub struct DiagnosticStore {
    root: PathBuf,
    keyring: Keyring,
    entropy: Arc<dyn Entropy>,
    guard: Mutex<()>,
    max_team_bytes: u64,
}

impl std::fmt::Debug for DiagnosticStore {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("DiagnosticStore")
            .finish_non_exhaustive()
    }
}

/// The step record's binding members.
const BINDING_FIELDS: [&str; 4] = ["routine_id", "revision", "plan_digest", "total"];

impl DiagnosticStore {
    /// The family under `root` (`routines/state/diagnostics`), sealed under `key_path` (`routines/key/diagnostics.key`).
    ///
    /// # Errors
    /// [`DiagnosticError::Layout`] when the keyring lives inside the root.
    pub fn new(root: &Path, key_path: &Path, entropy: Arc<dyn Entropy>) -> Stored<Self> {
        if key_path.parent() == Some(root) || key_path.starts_with(root) {
            return Err(DiagnosticError::Layout);
        }
        Ok(Self {
            root: root.to_path_buf(),
            keyring: Keyring::new(key_path.to_path_buf()),
            entropy,
            guard: Mutex::new(()),
            max_team_bytes: MAX_TEAM_BYTES,
        })
    }

    fn guarded(&self) -> MutexGuard<'_, ()> {
        self.guard.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn team_dir(&self, team: &str) -> PathBuf {
        let digest = Sha256::digest(team.as_bytes());
        self.root.join(hex(&digest))
    }

    /// Seals one attempt's diagnostic, first removing expired bodies, the attempt's earlier one, and then the oldest
    /// beyond the Team bound.
    ///
    /// # Errors
    /// [`DiagnosticError`] for an invalid diagnostic, a protected value, or a failed write.
    pub fn record(
        &self,
        team: &str,
        incarnation_id: &str,
        diagnostic: &Diagnostic,
        protected: &[String],
    ) -> Stored<()> {
        let (team, incarnation_id) = (team_id(team)?, incarnation(incarnation_id)?);
        let view = diagnostic.view();
        if canonical_diagnostic(&view).is_none()
            || !ROUTINE_ID_RE.is_full(&diagnostic.routine_id)
            || !ROUTINE_ID_RE.is_full(&diagnostic.run_id)
        {
            return Err(DiagnosticError::InvalidRecord);
        }
        let attempt = diagnostic.attempt.to_string();
        let replaced = |entry: &Entry| {
            entry.kind == Kind::Diagnostic
                && entry.routine == diagnostic.routine_id
                && entry.run == diagnostic.run_id
                && entry.operation.as_deref() == Some(diagnostic.operation_id.as_str())
                && entry.number.as_deref() == Some(attempt.as_str())
        };
        let named = Named {
            name: diagnostic.name(),
            kind: Kind::Diagnostic,
            recorded_at: diagnostic.recorded_at,
        };
        let _held = self.guarded();
        self.seal(team, incarnation_id, &named, &view, protected, &replaced)
    }

    /// Seals one step record under the run's next sequence; a later one of the same position supersedes it.
    ///
    /// # Errors
    /// [`DiagnosticError`] for an invalid record, a protected value, or a failed write.
    pub fn record_step(
        &self,
        team: &str,
        incarnation_id: &str,
        step: &StepRecord,
        protected: &[String],
    ) -> Stored<()> {
        let (team, incarnation_id) = (team_id(team)?, incarnation(incarnation_id)?);
        let binding = &step.binding;
        let mut members = binding.document();
        members.extend(step.view());
        let document = object_of(members);
        if !step_document(&document, &binding.run_id) {
            return Err(DiagnosticError::InvalidRecord);
        }
        let _held = self.guarded();
        let sequence = 1 + entries(&self.team_dir(team))?
            .iter()
            .filter(|entry| entry.kind == Kind::Step && entry.run == binding.run_id)
            .filter_map(|entry| entry.number.as_deref()?.parse::<u64>().ok())
            .max()
            .unwrap_or(0);
        let named = Named {
            name: format!(
                "{}.{}.{}.{sequence}.step",
                step.recorded_at, binding.routine_id, binding.run_id
            ),
            kind: Kind::Step,
            recorded_at: step.recorded_at,
        };
        self.seal(
            team,
            incarnation_id,
            &named,
            &document,
            protected,
            &|_entry: &Entry| false,
        )
    }

    /// Seals a run's terminal record, which proves which of its steps never started.
    ///
    /// # Errors
    /// [`DiagnosticError`] for an invalid record or a failed write.
    pub fn record_run(&self, team: &str, incarnation_id: &str, run: &RunRecord) -> Stored<()> {
        let (team, incarnation_id) = (team_id(team)?, incarnation(incarnation_id)?);
        let binding = &run.binding;
        let mut members = binding.document();
        members.push(("reached", int(run.reached)));
        members.push(("dispatched", Value::Bool(run.dispatched)));
        let document = object_of(members);
        if !run_document(&document, &binding.run_id) {
            return Err(DiagnosticError::InvalidRecord);
        }
        let named = Named {
            name: format!(
                "{}.{}.{}.run",
                run.recorded_at, binding.routine_id, binding.run_id
            ),
            kind: Kind::Run,
            recorded_at: run.recorded_at,
        };
        let replaced = |entry: &Entry| entry.kind == Kind::Run && entry.run == binding.run_id;
        let _held = self.guarded();
        self.seal(team, incarnation_id, &named, &document, &[], &replaced)
    }

    /// Encrypts one body of its kind, then makes room for it and writes it; the caller holds the guard.
    fn seal(
        &self,
        team: &str,
        incarnation_id: &str,
        named: &Named,
        document: &Value,
        protected: &[String],
        replaced: &dyn Fn(&Entry) -> bool,
    ) -> Stored<()> {
        let payload = dumps(document, CANONICAL);
        if payload.len() > named.kind.bound() {
            return Err(DiagnosticError::TooLarge);
        }
        if holds_protected(&payload, protected) {
            return Err(DiagnosticError::Protected);
        }
        let key = self.keyring.load(true, self.entropy.as_ref())?;
        let aad = aad(team, incarnation_id, &named.name);
        let sealed = Envelope::seal(&key, payload.as_bytes(), &aad, self.entropy.as_ref())
            .map_err(|_| DiagnosticError::Unavailable)?;
        // The authenticated origin: the AAD binds it, so a reader can tell another incarnation's body from a
        // corrupted one.
        let Value::Object(mut members) = sealed.to_value() else {
            return Err(DiagnosticError::Malformed);
        };
        members.push(("incarnation".to_owned(), text(incarnation_id)));
        let envelope = canonical(&Value::Object(members));
        // The family's own directory is as private as each Team's, not left to the process umask.
        ensure_directory(&self.root, &DirectoryCustody::private()).map_err(
            |error| match error {
                DirectoryError::Unsafe => DiagnosticError::Unsafe,
                DirectoryError::Unavailable => DiagnosticError::Unavailable,
            },
        )?;
        let directory = self.team_dir(team);
        self.make_room(
            &directory,
            named.recorded_at,
            envelope.len() as u64,
            replaced,
        )?;
        write_private(&directory.join(&named.name), &envelope)?;
        Ok(())
    }

    /// Removes expired and replaced bodies, then the oldest of any kind while the new one does not fit.
    fn make_room(
        &self,
        directory: &Path,
        recorded_at: i64,
        size: u64,
        replaced: &dyn Fn(&Entry) -> bool,
    ) -> Stored<()> {
        let mut kept = Vec::new();
        for entry in entries(directory)? {
            if replaced(&entry) || entry.at <= recorded_at - RETENTION_SECONDS {
                unlink(&directory.join(&entry.name))?;
            } else {
                kept.push(entry);
            }
        }
        let mut total: u64 = kept.iter().map(|entry| entry.size).sum();
        for entry in kept {
            if total + size <= self.max_team_bytes {
                break;
            }
            unlink(&directory.join(&entry.name))?;
            total -= entry.size;
        }
        Ok(())
    }

    /// One run's unexpired diagnostics that open under this exact Team incarnation, oldest first, at most the
    /// protocol's count (the newest kept).
    ///
    /// # Errors
    /// [`DiagnosticError`] for an invalid request or any body that cannot be read, authenticated, or admitted.
    pub fn read(
        &self,
        team: &str,
        incarnation_id: &str,
        run: &str,
        now: i64,
    ) -> Stored<Vec<Diagnostic>> {
        let (team, incarnation_id) = (team_id(team)?, incarnation(incarnation_id)?);
        if !ROUTINE_ID_RE.is_full(run) {
            return Err(DiagnosticError::InvalidRun);
        }
        let directory = self.team_dir(team);
        let mut found = Vec::new();
        {
            let _held = self.guarded();
            for entry in entries(&directory)? {
                if entry.kind != Kind::Diagnostic
                    || entry.run != run
                    || entry.at <= now - RETENTION_SECONDS
                {
                    continue;
                }
                let Some(opened) = self.open(&directory, team, incarnation_id, &entry)? else {
                    continue;
                };
                if canonical_diagnostic(&opened).is_none() {
                    return Err(DiagnosticError::Malformed);
                }
                found.push(diagnostic(&entry, &opened));
            }
        }
        found.sort_by(|left, right| {
            (left.recorded_at, &left.operation_id, left.attempt).cmp(&(
                right.recorded_at,
                &right.operation_id,
                right.attempt,
            ))
        });
        let skip = found.len().saturating_sub(MAX_RUN_DIAGNOSTICS);
        Ok(found.split_off(skip))
    }

    /// One run's retained records as one snapshot, assembled under the guard. `snapshot` names the one a reader
    /// holds (`latest` for a fresh one).
    ///
    /// # Errors
    /// [`DiagnosticError::RunChanged`] when the named snapshot no longer holds, or a body that cannot be read,
    /// authenticated, or admitted.
    pub fn run_steps(
        &self,
        team: &str,
        incarnation_id: &str,
        run: &str,
        now: i64,
        snapshot: &str,
    ) -> Stored<RunSteps> {
        let (team, incarnation_id) = (team_id(team)?, incarnation(incarnation_id)?);
        let directory = self.team_dir(team);
        let _held = self.guarded();
        let retained: Vec<Entry> = entries(&directory)?
            .into_iter()
            .filter(|entry| {
                matches!(entry.kind, Kind::Step | Kind::Run)
                    && entry.run == run
                    && entry.at > now - RETENTION_SECONDS
            })
            .collect();
        let current = snapshot_of(&directory, &retained)?;
        if snapshot != "latest" && snapshot != current {
            return Err(DiagnosticError::RunChanged);
        }
        let (steps, run_record) =
            self.records(&directory, (team, incarnation_id), run, &retained)?;
        Ok(RunSteps {
            snapshot: current,
            steps: steps
                .into_iter()
                .map(|(position, (_, record))| (position, record))
                .collect(),
            run: run_record,
        })
    }

    /// Each position's latest step record by sequence, and the run's terminal record, of `retained`.
    fn records(
        &self,
        directory: &Path,
        (team, incarnation_id): (&str, &str),
        run: &str,
        retained: &[Entry],
    ) -> Stored<(Sequenced, Option<Value>)> {
        let mut steps: BTreeMap<i64, (u64, Value)> = BTreeMap::new();
        let mut run_record = None;
        for entry in retained {
            let Some(opened) = self.open(directory, team, incarnation_id, entry)? else {
                continue;
            };
            // An authentic body outside the current record shape never reads as one: the page fails closed.
            if entry.kind == Kind::Run {
                if !run_document(&opened, run) {
                    return Err(DiagnosticError::Malformed);
                }
                run_record = Some(opened);
                continue;
            }
            if !step_document(&opened, run) {
                return Err(DiagnosticError::Malformed);
            }
            let sequence = entry
                .number
                .as_deref()
                .and_then(|number| number.parse().ok())
                .unwrap_or(0);
            let position = small_int(opened.get("position").unwrap_or(&Value::Null)).unwrap_or(0);
            if steps
                .get(&position)
                .is_none_or(|(kept, _)| *kept < sequence)
            {
                steps.insert(position, (sequence, opened));
            }
        }
        Ok((steps, run_record))
    }

    /// The run of a Routine whose unexpired record is the newest, or `None` when none is retained or the newest
    /// second holds records of several runs (ADR-0122).
    ///
    /// # Errors
    /// [`DiagnosticError`] for an invalid Team or an unlistable family.
    pub fn latest_run(&self, team: &str, routine: &str, now: i64) -> Stored<Option<String>> {
        let directory = self.team_dir(team_id(team)?);
        let found: Vec<(i64, String)> = {
            let _held = self.guarded();
            entries(&directory)?
                .into_iter()
                .filter(|entry| {
                    matches!(entry.kind, Kind::Step | Kind::Run)
                        && entry.routine == routine
                        && entry.at > now - RETENTION_SECONDS
                })
                .map(|entry| (entry.at, entry.run))
                .collect()
        };
        let Some(last) = found.iter().map(|(at, _)| *at).max() else {
            return Ok(None);
        };
        let mut newest: Vec<&String> = found
            .iter()
            .filter(|(at, _)| *at == last)
            .map(|(_, run)| run)
            .collect();
        newest.sort();
        newest.dedup();
        Ok((newest.len() == 1).then(|| newest[0].clone()))
    }

    /// Decrypts one body under the incarnation it names; another incarnation's authentic body is `None`.
    fn open(
        &self,
        directory: &Path,
        team: &str,
        incarnation_id: &str,
        entry: &Entry,
    ) -> Stored<Option<Value>> {
        let maximum = entry.kind.bound();
        let Some(raw) = read_private(&directory.join(&entry.name), sealed_bound(maximum) as u64)?
        else {
            return Ok(None);
        };
        let value = strict_json::parse(&raw).map_err(|_| DiagnosticError::Malformed)?;
        let Value::Object(members) = &value else {
            return Err(DiagnosticError::Malformed);
        };
        let named = match value.get("incarnation") {
            Some(Value::String(named)) if SHA256_RE.is_full(named) && members.len() == 4 => {
                named.clone()
            }
            _ => return Err(DiagnosticError::Malformed),
        };
        let plain = Value::Object(
            members
                .iter()
                .filter(|(name, _)| name != "incarnation")
                .cloned()
                .collect(),
        );
        let bounds = Bounds {
            maximum_encoded_part: MAX_FILE_BYTES,
            maximum_plaintext: maximum,
        };
        let envelope = Envelope::admit(&plain, bounds).map_err(|_| DiagnosticError::Malformed)?;
        let key = self.keyring.load(false, self.entropy.as_ref())?;
        let payload = envelope
            .open(&key, &aad(team, &named, &entry.name))
            .map_err(|_| DiagnosticError::Unauthentic)?;
        if named != incarnation_id {
            return Ok(None);
        }
        match strict_json::parse(&payload) {
            Ok(document @ Value::Object(_)) => Ok(Some(document)),
            _ => Err(DiagnosticError::Malformed),
        }
    }

    /// Removes a deleted Routine's bodies of every kind; its incidents keep the safety records.
    ///
    /// # Errors
    /// [`DiagnosticError`] for an invalid Team or a failed removal.
    pub fn delete_routine(&self, team: &str, routine: &str) -> Stored<()> {
        let directory = self.team_dir(team_id(team)?);
        let _held = self.guarded();
        for entry in entries(&directory)? {
            if entry.routine == routine {
                unlink(&directory.join(&entry.name))?;
            }
        }
        Ok(())
    }

    /// Removes every body of a deleted Team; an absent Team is already clean.
    ///
    /// # Errors
    /// [`DiagnosticError`] for an invalid Team or a failed removal.
    pub fn delete(&self, team: &str) -> Stored<()> {
        let directory = self.team_dir(team_id(team)?);
        let _held = self.guarded();
        remove(&directory)
    }

    /// Removes every Team's bodies and the diagnostic keyring, as a Space reset does.
    ///
    /// # Errors
    /// [`DiagnosticError`] for an unlistable family or a failed removal.
    pub fn delete_all(&self) -> Stored<()> {
        let _held = self.guarded();
        let listed = match std::fs::read_dir(&self.root) {
            Ok(listed) => listed,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return unlink(self.keyring.path());
            }
            Err(_) => return Err(DiagnosticError::Unavailable),
        };
        let mut names = Vec::new();
        for entry in listed {
            let name = entry.map_err(|_| DiagnosticError::Unavailable)?.file_name();
            if let Some(name) = name.to_str().filter(|name| SHA256_RE.is_full(name)) {
                names.push(name.to_owned());
            }
        }
        names.sort();
        for name in names {
            remove(&self.root.join(name))?;
        }
        unlink(self.keyring.path())
    }
}

fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    let mut text = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        let _ = write!(text, "{byte:02x}");
    }
    text
}

fn aad(team: &str, incarnation_id: &str, name: &str) -> Vec<u8> {
    let framing = Options {
        ensure_ascii: true,
        sort_keys: false,
        compact: true,
    };
    let parts = Value::Array(vec![
        text("shimpz-local-routine-diagnostic-v1"),
        text(team),
        text(incarnation_id),
        text(name),
    ]);
    dumps(&parts, framing).into_bytes()
}

fn unlink(path: &Path) -> Stored<()> {
    match std::fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(_) => Err(DiagnosticError::NotPersisted),
    }
}

fn remove(directory: &Path) -> Stored<()> {
    for entry in entries(directory)? {
        unlink(&directory.join(&entry.name))?;
    }
    match std::fs::remove_dir(directory) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(_) => Err(DiagnosticError::NotPersisted),
    }
}

/// The identity of exactly these sealed bodies: their names and the digests of their sealed bytes. Every write seals
/// under a fresh nonce, so a body rewritten or recreated under a reused name never reproduces it.
fn snapshot_of(directory: &Path, retained: &[Entry]) -> Stored<String> {
    let default = Options {
        ensure_ascii: true,
        sort_keys: false,
        compact: false,
    };
    let mut digest = Sha256::new();
    for entry in retained {
        let raw =
            read_private(&directory.join(&entry.name), MAX_FILE_BYTES as u64)?.unwrap_or_default();
        let sealed = hex(&Sha256::digest(&raw));
        let line = dumps(
            &Value::Array(vec![text(&entry.name), text(&sealed)]),
            default,
        );
        digest.update(line.as_bytes());
        digest.update(b"\n");
    }
    Ok(hex(&digest.finalize())[..32].to_owned())
}

fn bound(document: &Value) -> bool {
    let member = |name: &str| document.get(name).unwrap_or(&Value::Null);
    ROUTINE_ID_RE.is_full_value(member("routine_id"))
        && valid_revision(member("revision"))
        && PLAN_DIGEST_RE.is_full_value(member("plan_digest"))
        && whole(member("total"), 1, MAX_ROUTINE_STEPS)
}

/// One sealed step record: its run binding and exactly one run entry's wire view at its own position.
fn step_document(document: &Value, run: &str) -> bool {
    let Value::Object(members) = document else {
        return false;
    };
    let view = Value::Object(
        members
            .iter()
            .filter(|(name, _)| !BINDING_FIELDS.contains(&name.as_str()))
            .cloned()
            .collect(),
    );
    let status = view.get("status").and_then(|status| match status {
        Value::String(status) => Some(status.as_str()),
        _ => None,
    });
    let position = view.get("position").and_then(small_int);
    ROUTINE_ID_RE.is_full(run)
        && bound(document)
        && status.is_some_and(|status| RUN_STEP_STATUSES.contains(&status))
        && position.is_some_and(|position| {
            canonical_run_step(
                &view,
                position,
                document.get("total").unwrap_or(&Value::Null),
            )
            .is_some()
        })
}

fn run_document(document: &Value, run: &str) -> bool {
    let fields = [
        "routine_id",
        "revision",
        "plan_digest",
        "total",
        "reached",
        "dispatched",
    ];
    let Value::Object(members) = document else {
        return false;
    };
    let total = document.get("total").and_then(small_int).unwrap_or(0);
    ROUTINE_ID_RE.is_full(run)
        && bound(document)
        && members.len() == fields.len()
        && members
            .iter()
            .all(|(name, _)| fields.contains(&name.as_str()))
        && document
            .get("reached")
            .and_then(small_int)
            .is_some_and(|reached| (0..=total).contains(&reached))
        && matches!(document.get("dispatched"), Some(Value::Bool(_)))
}

fn diagnostic(entry: &Entry, view: &Value) -> Diagnostic {
    let member = |name: &str| view.get(name).cloned().unwrap_or(Value::Null);
    let string = |name: &str| match view.get(name) {
        Some(Value::String(text)) => text.clone(),
        _ => String::new(),
    };
    let number = |name: &str| view.get(name).and_then(small_int).unwrap_or(0);
    Diagnostic {
        routine_id: entry.routine.clone(),
        run_id: entry.run.clone(),
        operation_id: string("operation_id"),
        attempt: number("attempt"),
        assistant_id: string("assistant_id"),
        action: string("action"),
        position: number("position"),
        recorded_at: entry.at,
        failure: Some(member("failure")).filter(|failure| !matches!(failure, Value::Null)),
        condition: match view.get("condition") {
            Some(Value::String(condition)) => Some(condition.clone()),
            _ => None,
        },
    }
}

/// How a failed attempt failed, as its caller knows it.
#[derive(Clone, Copy, Debug)]
pub enum Fault<'a> {
    /// The Action answered this Team-admitted handled failure.
    Failure(&'a ActionFailure),
    /// The exec exchange failed with this transport condition.
    Condition(&'a str),
    /// Anything else, which keeps no evidence.
    Other,
}

/// The sanitized failure or safe transport condition of a failed attempt, or `None`.
///
/// Only Team's own admitted failure document or a closed transport condition is ever kept, never a message. A failure
/// is re-redacted against the run's whole `protection` before anything keeps it, and after the run lost its
/// protection only its status is kept (ADR-0101).
#[must_use]
pub fn evidence(
    fault: Fault<'_>,
    protection: &Protection,
) -> Option<(Option<Value>, Option<String>)> {
    match fault {
        Fault::Failure(failure) if protection.lost => {
            Some((Some(failure.withheld().document()), None))
        }
        Fault::Failure(failure) => {
            let values: Vec<&String> = protection.values.iter().collect();
            Some((Some(failure.redacted_with(&values).document()), None))
        }
        Fault::Condition(condition) if CONDITION_RE.is_full(condition) => {
            Some((None, Some(condition.to_owned())))
        }
        Fault::Condition(_) | Fault::Other => None,
    }
}

/// Every value Team injected into one attempt, which its diagnostic must never hold: its Stored Input values and
/// every string of its resolved Integrations.
#[must_use]
pub fn protected<'a>(
    stored_inputs: impl IntoIterator<Item = &'a str>,
    integrations: &Value,
) -> Vec<String> {
    let mut found: Vec<String> = stored_inputs.into_iter().map(str::to_owned).collect();
    let mut pending = vec![integrations];
    while let Some(value) = pending.pop() {
        match value {
            Value::String(text) => found.push(text.clone()),
            Value::Object(members) => pending.extend(members.iter().map(|(_, member)| member)),
            Value::Array(items) => pending.extend(items.iter()),
            _ => {}
        }
    }
    found
}

#[cfg(test)]
mod tests;
