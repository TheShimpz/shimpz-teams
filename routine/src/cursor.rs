//! The cursor of one Routine run, without I/O (ADR-0092 sections 3 and 5, ADR-0101).
//!
//! A cursor binds the Team incarnation, the Routine and its revision, the run, the exact plan (whose digest covers every
//! step's complete pin), the run's one start instant, the current step, that step's logical `operation_id`, attempts,
//! and resolved-input commitment, the values later steps selected from completed ones, the shown step's bounded result
//! and its keyed comparison digest, and whether the run's one recovery episode opened and continued it. A completed
//! step is never run again: the successful prefix is durable even when a later step fails. Neither flag is ever
//! cleared, and no secret, human response, or complete output is ever held here.
//!
//! It also records the Team boot its run protection was bound in, and whether that protection was lost, which is never
//! undone.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::sync::LazyLock;

use strict_json::{Number, Value};
use team_protocol::payload::SHA256_RE;
use team_protocol::python::regex::{Pattern, declared};
use team_protocol::python::value::py_eq;
use team_protocol::routine::{PLAN_DIGEST_RE, ROUTINE_ID_RE, canonical_output, valid_revision};
use team_protocol::routine_run::OPERATION_ID_RE;

use crate::json::{as_str, at, canonical, has_fields, int, is_object, object, text};
use crate::plan::{
    self, Key, MAX_RETAINED_BYTES, MAX_STEPS, Plan, STEP_ID_RE, pointer_tokens_of, retained_bytes,
};

/// The cursor document version.
pub const VERSION: i64 = 7;
/// The canonical cursor at most.
pub const MAX_CURSOR_BYTES: usize = 256 * 1024;
/// Team's own classification of the dispatched operation's last failed attempt (ADR-0092 section 6): a handled failure
/// envelope, a transport fault after a proven fail-stop, a fault whose workload could not be proven stopped, a policy
/// fault, or any other refusal. Empty when the attempt has not failed, or its failure could not be classified.
pub const FAULTS: [&str; 6] = ["", "handled", "transport", "unquiesced", "policy", "other"];
/// The Team boot a cursor's run protection was bound in.
static BOOT_RE: LazyLock<Pattern> = LazyLock::new(|| declared(r"[0-9a-f]{32}\Z"));
/// A Docker container id or name: the workload an attempt was dispatched to.
static WORKLOAD_RE: LazyLock<Pattern> =
    LazyLock::new(|| declared(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z"));
const FIELDS: [&str; 22] = [
    "version",
    "boot",
    "protection_lost",
    "incarnation",
    "routine_id",
    "revision",
    "run_id",
    "plan",
    "started_at",
    "step",
    "operation_id",
    "attempts",
    "commitment",
    "selected",
    "episode",
    "continued",
    "absent",
    "carried",
    "fault",
    "workload",
    "dispatched_at",
    "shown",
];

/// A cursor transition or encoding was refused; `code` is the stable reason.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CursorError {
    /// The stable reason.
    pub code: &'static str,
}

impl CursorError {
    const fn new(code: &'static str) -> Self {
        Self { code }
    }
}

impl fmt::Display for CursorError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code)
    }
}

impl std::error::Error for CursorError {}

/// What a cursor belongs to: the Team incarnation, the Routine revision, and the run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Binding {
    /// The Team incarnation.
    pub incarnation: String,
    /// The Routine.
    pub routine_id: String,
    /// The Routine's revision.
    pub revision: i64,
    /// The run.
    pub run_id: String,
}

/// One selection a cursor retains: the step it selects from, its selector, and the selected value.
pub type Selected = (String, String, String, String, Value);

/// The run's one automatic recovery episode: sealed open before any of its work, so a restart never opens another;
/// its one verifier call and its one retry live inside it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Episode {
    /// Not opened.
    Closed,
    /// Opened, the run still in its own segment.
    Open,
    /// The episode continued the run in its one continuation segment, which runs in its own generation (`s1`).
    Continued {
        /// The dispatched operation came from an earlier segment: this segment may only retry it after proven absence.
        carried: bool,
    },
}

impl Episode {
    /// Whether the episode opened.
    #[must_use]
    pub const fn opened(self) -> bool {
        !matches!(self, Self::Closed)
    }

    /// Whether the episode continued the run.
    #[must_use]
    pub const fn continued(self) -> bool {
        matches!(self, Self::Continued { .. })
    }

    /// Whether the continuation carries an operation of an earlier segment.
    #[must_use]
    pub const fn carried(self) -> bool {
        matches!(self, Self::Continued { carried: true })
    }

    /// The episode of these persisted flags, or `None` for a continuation without an episode or a carried operation
    /// without a continuation.
    #[must_use]
    pub const fn of(episode: bool, continued: bool, carried: bool) -> Option<Self> {
        match (episode, continued, carried) {
            (false, false, false) => Some(Self::Closed),
            (true, false, false) => Some(Self::Open),
            (true, true, carried) => Some(Self::Continued { carried }),
            _ => None,
        }
    }
}

/// The cursor of one run.
#[derive(Clone, Debug, PartialEq)]
pub struct Cursor {
    /// What it belongs to.
    pub binding: Binding,
    /// The plan digest.
    pub plan: String,
    /// The run's one start instant.
    pub started_at: i64,
    /// The current step, by index; the plan's step count once done.
    pub step: usize,
    /// The current step's logical operation, once dispatched.
    pub operation_id: Option<String>,
    /// The current step's attempts.
    pub attempts: i64,
    /// The current step's resolved-input commitment, once dispatched.
    pub commitment: Option<String>,
    /// Every value later steps selected from completed ones.
    pub selected: Vec<Selected>,
    /// The run's one automatic recovery episode (ADR-0092 section 6).
    pub episode: Episode,
    /// Team-admitted evidence proved the dispatched operation had no business effect, which permits one retry of it.
    pub absent: bool,
    /// How the dispatched operation's last attempt failed, as Team classified it; see [`FAULTS`].
    pub fault: String,
    /// The workload the last attempt was dispatched to.
    pub workload: String,
    /// When the last attempt was dispatched.
    pub dispatched_at: i64,
    /// The shown step's result as its notice shows it, and the keyed digest of its safe form:
    /// `{"step", "output", "digest"}`; it outlives holds and continuations.
    pub shown: Option<Value>,
    /// The Team boot the run's protection was bound in (ADR-0101).
    pub boot: String,
    /// Whether that protection was lost; never regained.
    pub protection_lost: bool,
}

impl Cursor {
    /// The suffix of the generation the cursor's segment runs in.
    #[must_use]
    pub const fn generation_suffix(&self) -> &'static str {
        if self.episode.continued() { "s1" } else { "" }
    }

    /// The values the run retains, by complete selection key.
    #[must_use]
    pub fn selections(&self) -> BTreeMap<Key, Value> {
        self.selected
            .iter()
            .map(|(step, pointer, where_text, item, value)| {
                (
                    (
                        step.clone(),
                        pointer.clone(),
                        where_text.clone(),
                        item.clone(),
                    ),
                    value.clone(),
                )
            })
            .collect()
    }

    /// Whether the run is complete: every step replayed.
    #[must_use]
    pub fn done(&self, plan: &Plan) -> bool {
        self.step == plan.steps.len()
    }
}

/// A run's first cursor, at its first step with no recovery opened, its protection bound in this Team boot.
///
/// # Errors
///
/// `cursor-invalid` for a cursor outside its closed shape.
pub fn start(
    plan: &Plan,
    binding: Binding,
    started_at: i64,
    boot: &str,
) -> Result<Cursor, CursorError> {
    checked(Cursor {
        binding,
        plan: plan.digest.clone(),
        started_at,
        step: 0,
        operation_id: None,
        attempts: 0,
        commitment: None,
        selected: Vec::new(),
        episode: Episode::Closed,
        absent: false,
        fault: String::new(),
        workload: String::new(),
        dispatched_at: 0,
        shown: None,
        boot: boot.to_owned(),
        protection_lost: false,
    })
}

/// Record that the run's protection was lost; it is never regained (ADR-0101).
///
/// # Errors
///
/// `cursor-invalid` for a cursor outside its closed shape.
pub fn lose_protection(cursor: &Cursor) -> Result<Cursor, CursorError> {
    let mut lost = cursor.clone();
    lost.protection_lost = true;
    checked(lost)
}

/// Record one dispatch of the current step before its RPC: its logical operation and exact resolved input.
///
/// A repeated dispatch of the same step keeps its logical operation and must carry the same input commitment, so a
/// replay or permitted retry never changes the business payload of an operation. The attempt's workload and instant
/// are kept with it; an unknown workload can never be proven stopped.
///
/// # Errors
///
/// `cursor-plan-changed`, `cursor-dispatch-invalid`, `cursor-operation-changed`, or `cursor-invalid`.
pub fn dispatch(
    cursor: &Cursor,
    plan: &Plan,
    operation_id: &str,
    commitment: &str,
    workload: &str,
    dispatched_at: i64,
) -> Result<Cursor, CursorError> {
    same_plan(cursor, plan)?;
    if cursor.done(plan) || !OPERATION_ID_RE.is_full(operation_id) || !SHA256_RE.is_full(commitment)
    {
        return Err(CursorError::new("cursor-dispatch-invalid"));
    }
    if cursor.operation_id.is_some()
        && (cursor.operation_id.as_deref(), cursor.commitment.as_deref())
            != (Some(operation_id), Some(commitment))
    {
        return Err(CursorError::new("cursor-operation-changed"));
    }
    let mut dispatched = cursor.clone();
    dispatched.operation_id = Some(operation_id.to_owned());
    dispatched.attempts += 1;
    dispatched.commitment = Some(commitment.to_owned());
    dispatched.fault = String::new();
    workload.clone_into(&mut dispatched.workload);
    dispatched.dispatched_at = dispatched_at;
    checked(dispatched)
}

/// Record Team's classification of the dispatched operation's failed attempt before the segment unwinds.
///
/// # Errors
///
/// `cursor-not-dispatched` for an undispatched cursor or a fault outside [`FAULTS`].
pub fn failed(cursor: &Cursor, fault: &str) -> Result<Cursor, CursorError> {
    if cursor.operation_id.is_none() || !FAULTS[1..].contains(&fault) {
        return Err(CursorError::new("cursor-not-dispatched"));
    }
    let mut classified = cursor.clone();
    fault.clone_into(&mut classified.fault);
    checked(classified)
}

/// Whether a completing step's shown slot is exactly what the plan admits: present only for the plan's shown step,
/// naming that step, and its output naming the step by position.
fn shown_matches(cursor: &Cursor, plan: &Plan, step_id: &str, shown: Option<&Value>) -> bool {
    let shown_here = plan.shown().is_some_and(|step| step.step_id == step_id);
    match shown {
        None => !shown_here,
        Some(shown) => {
            let position = int(i64::try_from(cursor.step + 1).unwrap_or(i64::MAX));
            shown_here
                && is_object(shown)
                && as_str(at(shown, "step")) == Some(step_id)
                && is_object(at(shown, "output"))
                && at(at(shown, "output"), "step").ne(&Value::Null)
                && py_eq(at(at(shown, "output"), "step"), &position)
        }
    }
}

/// Advance past the current step, keeping only the values later steps select from its result.
///
/// The plan's shown step also keeps `shown`, its bounded result and comparison digest; no other step may.
///
/// # Errors
///
/// `cursor-plan-changed`, `cursor-not-dispatched`, a selection's plan code, `cursor-shown-invalid`, or `cursor-invalid`.
pub fn complete(
    cursor: &Cursor,
    plan: &Plan,
    result: &Value,
    shown: Option<&Value>,
) -> Result<Cursor, CursorError> {
    same_plan(cursor, plan)?;
    if cursor.done(plan) || cursor.operation_id.is_none() {
        return Err(CursorError::new("cursor-not-dispatched"));
    }
    let step_id = plan.steps[cursor.step].step_id.clone();
    let chosen =
        plan::selections(plan, &step_id, result).map_err(|error| CursorError::new(error.code))?;
    if !shown_matches(cursor, plan, &step_id, shown) {
        return Err(CursorError::new("cursor-shown-invalid"));
    }
    let mut advanced = cursor.clone();
    advanced.selected.extend(
        chosen
            .into_iter()
            .map(|((pointer, where_text, item), value)| {
                (step_id.clone(), pointer, where_text, item, value)
            }),
    );
    advanced.step += 1;
    advanced.operation_id = None;
    advanced.attempts = 0;
    advanced.commitment = None;
    advanced.absent = false;
    if let Episode::Continued { carried } = &mut advanced.episode {
        *carried = false;
    }
    advanced.fault = String::new();
    advanced.workload = String::new();
    advanced.dispatched_at = 0;
    advanced.shown = shown.cloned();
    checked(advanced)
}

/// Record Team-admitted proof that the dispatched operation had no business effect; a policy fault never has it.
///
/// # Errors
///
/// `cursor-not-dispatched`, `cursor-policy-hold`, or `cursor-invalid`.
pub fn proven_absent(cursor: &Cursor) -> Result<Cursor, CursorError> {
    if cursor.operation_id.is_none() {
        return Err(CursorError::new("cursor-not-dispatched"));
    }
    if cursor.fault == "policy" {
        return Err(CursorError::new("cursor-policy-hold"));
    }
    let mut absent = cursor.clone();
    absent.absent = true;
    checked(absent)
}

/// Permit the retry of the dispatched operation, under the same logical operation, only after proven absence.
///
/// The proof is spent here, before its dispatch; the run's one continuation makes it the only retry of the run.
///
/// # Errors
///
/// `cursor-operation-uncertain` or `cursor-invalid`.
pub fn retry(cursor: &Cursor) -> Result<Cursor, CursorError> {
    if cursor.operation_id.is_none() || !cursor.absent {
        return Err(CursorError::new("cursor-operation-uncertain"));
    }
    let mut spent = cursor.clone();
    spent.absent = false;
    checked(spent)
}

/// Open the run's one automatic recovery episode; a run whose episode already opened never gets another.
///
/// # Errors
///
/// `cursor-episode-spent` or `cursor-invalid`.
pub fn open_episode(cursor: &Cursor) -> Result<Cursor, CursorError> {
    if cursor.episode.opened() {
        return Err(CursorError::new("cursor-episode-spent"));
    }
    let mut opened = cursor.clone();
    opened.episode = Episode::Open;
    checked(opened)
}

/// The cursor of the run's one continuation segment, after its episode archived the held generation.
///
/// # Errors
///
/// `cursor-continued` or `cursor-invalid`.
pub fn continued(cursor: &Cursor) -> Result<Cursor, CursorError> {
    match cursor.episode {
        Episode::Continued { .. } => Err(CursorError::new("cursor-continued")),
        // Only an episode continues a run.
        Episode::Closed => Err(CursorError::new("cursor-invalid")),
        Episode::Open => {
            let mut next = cursor.clone();
            next.episode = Episode::Continued {
                carried: cursor.operation_id.is_some(),
            };
            checked(next)
        }
    }
}

fn same_plan(cursor: &Cursor, plan: &Plan) -> Result<(), CursorError> {
    if cursor.plan != plan.digest {
        return Err(CursorError::new("cursor-plan-changed"));
    }
    Ok(())
}

fn optional(value: Option<&String>) -> Value {
    value.map_or(Value::Null, |text_value| text(text_value))
}

fn document(cursor: &Cursor) -> Value {
    let binding = &cursor.binding;
    object(vec![
        ("version", int(VERSION)),
        ("incarnation", text(&binding.incarnation)),
        ("routine_id", text(&binding.routine_id)),
        ("revision", int(binding.revision)),
        ("run_id", text(&binding.run_id)),
        ("plan", text(&cursor.plan)),
        ("started_at", int(cursor.started_at)),
        ("step", int(i64::try_from(cursor.step).unwrap_or(i64::MAX))),
        ("operation_id", optional(cursor.operation_id.as_ref())),
        ("attempts", int(cursor.attempts)),
        ("commitment", optional(cursor.commitment.as_ref())),
        (
            "selected",
            Value::Array(
                cursor
                    .selected
                    .iter()
                    .map(|(step, pointer, where_text, item, value)| {
                        Value::Array(vec![
                            text(step),
                            text(pointer),
                            text(where_text),
                            text(item),
                            value.clone(),
                        ])
                    })
                    .collect(),
            ),
        ),
        ("episode", Value::Bool(cursor.episode.opened())),
        ("continued", Value::Bool(cursor.episode.continued())),
        ("absent", Value::Bool(cursor.absent)),
        ("carried", Value::Bool(cursor.episode.carried())),
        ("fault", text(&cursor.fault)),
        ("workload", text(&cursor.workload)),
        ("dispatched_at", int(cursor.dispatched_at)),
        ("shown", cursor.shown.clone().unwrap_or(Value::Null)),
        ("boot", text(&cursor.boot)),
        ("protection_lost", Value::Bool(cursor.protection_lost)),
    ])
}

/// The canonical plaintext a store seals; at most 256 KiB, with its selections within their own bound.
///
/// # Errors
///
/// `cursor-invalid` or `cursor-too-large`.
pub fn encode(cursor: &Cursor) -> Result<Vec<u8>, CursorError> {
    Ok(canonical(&document(&checked(cursor.clone())?)))
}

/// Admit one opened cursor only for exactly the binding its seal was opened under.
///
/// # Errors
///
/// `cursor-invalid` for anything but the canonical encoding of a valid cursor of exactly this binding.
pub fn decode(raw: &[u8], binding: &Binding) -> Result<Cursor, CursorError> {
    let invalid = CursorError::new("cursor-invalid");
    let value = strict_json::parse(raw).map_err(|_| invalid)?;
    if !has_fields(&value, &FIELDS)
        || !matches!(
            value.get("version"),
            Some(Value::Number(Number::Int(VERSION)))
        )
    {
        return Err(invalid);
    }
    let cursor = read(&value).ok_or(invalid)?;
    if cursor.binding != *binding || canonical(&document(&cursor)) != raw {
        return Err(invalid);
    }
    checked(cursor)
}

fn integer(value: &Value) -> Option<i64> {
    match value {
        Value::Number(Number::Int(integer)) => Some(*integer),
        _ => None,
    }
}

fn flag(value: &Value) -> Option<bool> {
    match value {
        Value::Bool(flag) => Some(*flag),
        _ => None,
    }
}

fn string(value: &Value) -> Option<String> {
    as_str(value).map(str::to_owned)
}

/// A nullable string field: its text or `None`, or `Err` for another type.
fn nullable(value: &Value) -> Result<Option<String>, ()> {
    match value {
        Value::Null => Ok(None),
        Value::String(text_value) => Ok(Some(text_value.clone())),
        _ => Err(()),
    }
}

fn selection(item: &Value) -> Option<Selected> {
    let Value::Array(parts) = item else {
        return None;
    };
    let [step, pointer, where_text, item, value] = parts.as_slice() else {
        return None;
    };
    Some((
        string(step)?,
        string(pointer)?,
        string(where_text)?,
        string(item)?,
        value.clone(),
    ))
}

/// A cursor of the decoded fields' types, or `None` when one has another type.
fn read(value: &Value) -> Option<Cursor> {
    let (plan, started_at, step, selected) = read_progress(value)?;
    let (operation_id, attempts, commitment) = read_operation(value)?;
    let (fault, workload, dispatched_at) = read_attempt(value)?;
    let (episode, absent, boot, protection_lost) = read_state(value)?;
    Some(Cursor {
        binding: read_binding(value)?,
        plan,
        started_at,
        step,
        operation_id,
        attempts,
        commitment,
        selected,
        episode,
        absent,
        fault,
        workload,
        dispatched_at,
        shown: match at(value, "shown") {
            Value::Null => None,
            shown => Some(shown.clone()),
        },
        boot,
        protection_lost,
    })
}

fn read_binding(value: &Value) -> Option<Binding> {
    Some(Binding {
        incarnation: string(at(value, "incarnation"))?,
        routine_id: string(at(value, "routine_id"))?,
        revision: integer(at(value, "revision"))?,
        run_id: string(at(value, "run_id"))?,
    })
}

fn read_progress(value: &Value) -> Option<(String, i64, usize, Vec<Selected>)> {
    let Value::Array(selected) = at(value, "selected") else {
        return None;
    };
    Some((
        string(at(value, "plan"))?,
        integer(at(value, "started_at"))?,
        usize::try_from(integer(at(value, "step"))?).ok()?,
        selected.iter().map(selection).collect::<Option<_>>()?,
    ))
}

fn read_operation(value: &Value) -> Option<(Option<String>, i64, Option<String>)> {
    Some((
        nullable(at(value, "operation_id")).ok()?,
        integer(at(value, "attempts"))?,
        nullable(at(value, "commitment")).ok()?,
    ))
}

fn read_attempt(value: &Value) -> Option<(String, String, i64)> {
    Some((
        string(at(value, "fault"))?,
        string(at(value, "workload"))?,
        integer(at(value, "dispatched_at"))?,
    ))
}

fn read_state(value: &Value) -> Option<(Episode, bool, String, bool)> {
    let flags = ["episode", "continued", "carried"].map(|key| flag(at(value, key)));
    let [Some(episode), Some(continued), Some(carried)] = flags else {
        return None;
    };
    Some((
        Episode::of(episode, continued, carried)?,
        flag(at(value, "absent"))?,
        string(at(value, "boot"))?,
        flag(at(value, "protection_lost"))?,
    ))
}

/// Refuse any cursor outside its closed shape and bounds.
fn checked(cursor: Cursor) -> Result<Cursor, CursorError> {
    checked_within(cursor, MAX_RETAINED_BYTES)
}

/// [`checked`] with the retained selections bounded by `retained`.
fn checked_within(cursor: Cursor, retained: usize) -> Result<Cursor, CursorError> {
    let valid = binding_valid(&cursor.binding)
        && PLAN_DIGEST_RE.is_full(&cursor.plan)
        && cursor.started_at >= 0
        && cursor.step <= MAX_STEPS
        && operation_valid(&cursor)
        && selections_valid(&cursor.selected, retained)
        && attempt_valid(&cursor)
        && shown_valid(cursor.shown.as_ref())
        && BOOT_RE.is_full(&cursor.boot);
    if !valid {
        return Err(CursorError::new("cursor-invalid"));
    }
    if canonical(&document(&cursor)).len() > MAX_CURSOR_BYTES {
        return Err(CursorError::new("cursor-too-large"));
    }
    Ok(cursor)
}

/// The dispatched operation: a logical id, at least one attempt, and a commitment, or none of them.
fn operation_valid(cursor: &Cursor) -> bool {
    match (&cursor.operation_id, &cursor.commitment) {
        (Some(operation), Some(commitment)) => {
            OPERATION_ID_RE.is_full(operation)
                && cursor.attempts >= 1
                && SHA256_RE.is_full(commitment)
        }
        (None, None) => cursor.attempts == 0,
        _ => false,
    }
}

/// What the last attempt recorded: proven absence and a carried operation only of a dispatched one, a closed fault
/// that is never a policy fault beside absence, and its workload and instant only once dispatched.
fn attempt_valid(cursor: &Cursor) -> bool {
    let dispatched = cursor.operation_id.is_some();
    let fault_valid =
        FAULTS.contains(&cursor.fault.as_str()) && !(cursor.absent && cursor.fault == "policy");
    let workload_valid = (cursor.workload.is_empty() || WORKLOAD_RE.is_full(&cursor.workload))
        && cursor.dispatched_at >= 0;
    let undispatched_clear = !(cursor.absent || cursor.episode.carried())
        && cursor.fault.is_empty()
        && cursor.workload.is_empty()
        && cursor.dispatched_at == 0;
    fault_valid && workload_valid && (dispatched || undispatched_clear)
}

/// Whether a binding names one Team incarnation, one Routine revision, and one run.
#[must_use]
pub fn binding_valid(binding: &Binding) -> bool {
    SHA256_RE.is_full(&binding.incarnation)
        && ROUTINE_ID_RE.is_full(&binding.routine_id)
        && valid_revision(&int(binding.revision))
        && ROUTINE_ID_RE.is_full(&binding.run_id)
}

/// No shown result, or one step's shown output with its keyed digest, or no digest when it was too large.
fn shown_valid(shown: Option<&Value>) -> bool {
    let Some(shown) = shown else {
        return true;
    };
    if !has_fields(shown, &["step", "output", "digest"]) {
        return false;
    }
    let Some(output) = canonical_output(at(shown, "output")) else {
        return false;
    };
    let state = as_str(at(output, "state"));
    let digest = at(shown, "digest");
    STEP_ID_RE.is_full_value(at(shown, "step"))
        && matches!(state, Some("shown" | "unavailable"))
        && (matches!(digest, Value::Null) || SHA256_RE.is_full_value(digest))
        && (matches!(digest, Value::Null) || state == Some("shown"))
}

fn selections_valid(selected: &[Selected], retained: usize) -> bool {
    let mut keys = BTreeSet::new();
    for (step, pointer, where_text, item, _) in selected {
        if !STEP_ID_RE.is_full(step)
            || pointer_tokens_of(pointer).is_none()
            || (where_text.is_empty() && !item.is_empty())
            || pointer_tokens_of(item).is_none()
            || !keys.insert((step, pointer, where_text, item))
        {
            return false;
        }
    }
    let map: BTreeMap<Key, Value> = selected
        .iter()
        .map(|(step, pointer, where_text, item, value)| {
            (
                (
                    step.clone(),
                    pointer.clone(),
                    where_text.clone(),
                    item.clone(),
                ),
                value.clone(),
            )
        })
        .collect();
    retained_bytes(&map) <= retained
}

#[cfg(test)]
mod tests;
