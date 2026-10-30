//! A Team's Routines, runs, and notices, and the transitions of Routines and notices, without I/O (ADR-0086).
//!
//! Claiming due runs lives in [`crate::claim`] and a claimed run's transitions in [`crate::runs`]. All instants are
//! whole UTC epoch seconds. A transition validates the current state itself and returns a new [`TeamRoutines`]; the
//! caller persists it atomically before acting on it, so a crash leaves either the old or the new state, never a mix.
//! A refused transition leaves the state it was given as it was.

use std::fmt;
use std::sync::LazyLock;

use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use strict_json::Value;
use team_core::entropy::{Entropy, token_hex};
use team_protocol::python::regex::{Pattern, declared};
use team_protocol::routine_notice::MAX_UNRESOLVED_INCIDENTS;

pub use self::define::{
    add_routine, create, definition_valid, first_run, refine, scheduled, update,
};
pub(crate) use self::notice::put_notice_into;
pub use self::notice::{acknowledge, change_room, put_notice, run_notice, undelivered};
use crate::json::{as_str, at, int, object, sha256_hex};
use crate::plan;
use crate::schedule;
use crate::starts::Start;

mod define;
mod notice;

/// The most Routines of one Team.
pub const MAX_ROUTINES: usize = team_protocol::routine::MAX_ROUTINES;
/// The most frozen runs of one Team.
pub const MAX_FROZEN_RUNS: usize = 8;
/// The undelivered notices that stop claims and new skip reports.
pub const MAX_UNDELIVERED_NOTICES: usize = 32;
/// A claimed run's lease until its segment starts; the segment then extends it over the run's active time.
pub const LEASE_SECONDS: i64 = plan::START_LEASE_SECONDS;
/// The most active time any run may have left.
pub const ACTIVE_SECONDS: i64 = plan::MAX_ACTIVE_SECONDS;
/// How late a run may still start at most.
pub const MAX_GRACE_SECONDS: i64 = 12 * 3600;
/// Bounds the work of counting firings missed during a long outage; the count is reported, never replayed.
pub const MAX_COUNTED_MISSES: i64 = 24 * 400;
/// The lease key of a run leased for a person's answer, which no machine assertion knows.
pub const HUMAN_LEASE: &str = "human";
/// From a frozen run only these outcomes are possible: nobody answered it, or someone refused or stopped it.
pub const FROZEN_OUTCOMES: [&str; 3] = ["denied", "stopped", "failed"];
/// Ended runs whose journal generation, continuation, and cursor Team has yet to remove.
pub const MAX_DISCARDS: usize = 3 * MAX_ROUTINES;
/// Run records a Team keeps in all: its live runs and its incidents.
pub const MAX_RUNS: usize = 2 * MAX_UNRESOLVED_INCIDENTS;
/// Undelivered Routine outcomes beside every run's.
pub const MAX_ROUTINE_NOTICES: usize = 3 * MAX_ROUTINES;
/// Consecutive no-effect failures that pause a Routine (ADR-0092 section 6).
pub const MAX_FAILURE_STREAK: i64 = team_protocol::routine::MAX_FAILURE_STREAK;
/// A refusal for want of entropy, which mints every id and token.
pub const ENTROPY_UNAVAILABLE: &str = "entropy-unavailable";

/// Any of a run's journal generations, its verification's included: its network, then its run.
static GENERATION_RE: LazyLock<Pattern> = LazyLock::new(|| {
    declared(r"\A([A-Za-z0-9][A-Za-z0-9._/-]{0,127}):routine:([0-9a-f]{32})(?::[sv]1)?\Z")
});
pub(crate) static HEX32_RE: LazyLock<Pattern> = LazyLock::new(|| declared(r"[0-9a-f]{32}\Z"));

/// A requested Routine transition is not allowed in the current state; `code` is the stable reason.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RoutineStateError {
    /// The stable reason.
    pub code: &'static str,
}

impl RoutineStateError {
    /// A refusal for this reason.
    #[must_use]
    pub const fn new(code: &'static str) -> Self {
        Self { code }
    }
}

impl fmt::Display for RoutineStateError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code)
    }
}

impl std::error::Error for RoutineStateError {}

/// The result of a Routine transition.
pub type Transition<T = TeamRoutines> = Result<T, RoutineStateError>;

/// One Routine: its confirmed definition and its scheduling state.
#[derive(Clone, Debug, PartialEq)]
pub struct Routine {
    /// Its id.
    pub routine_id: String,
    /// Its short name.
    pub name: String,
    /// Its canonical schedule.
    pub schedule: Value,
    /// (Assistant id, scope pin) of each Assistant the plan uses, pinned at creation, sorted by Assistant id.
    pub assistants: Vec<(String, String)>,
    /// The canonical recorded plan document: its ordered steps, each Action's complete pin, and every input source.
    pub plan: Value,
    /// The instant its schedule counts from.
    pub anchor: i64,
    /// Its next firing.
    pub next_run_at: i64,
    /// The confirmed plain-language account of what every run does (ADR-0122).
    pub description: String,
    /// Its Assistants no longer match its pins: no claim until an authenticated update.
    pub needs_reconfirm: bool,
    /// It is being deleted.
    pub deleting: bool,
    /// When the current stretch of missed firings began.
    pub gap_started_at: i64,
    /// How many firings the current stretch missed.
    pub missed: i64,
    /// How many of them a notice already reports.
    pub reported_missed: i64,
    /// Each authenticated change of the Routine is a new revision, which a replay cursor binds (ADR-0092).
    pub revision: i64,
    /// Where its timezone came from: the person's browser, a zone the person wrote, or none (ADR-0101).
    pub timezone_source: String,
    /// Pausar: no dispatch until resumed.
    pub paused: bool,
    /// The person's authenticated confirmation of the card that defined this revision (ADR-0101).
    pub confirmation: Option<Value>,
    /// Every Action the Routine may call at its pin: `{assistant, action, pin, read_only, stored_inputs}`, sorted.
    pub permitted: Vec<Value>,
    /// Consecutive runs that failed with no effect; three pause the Routine (ADR-0092).
    pub failures: i64,
    /// The minute whose rollup notice a continuous Routine's healthy runs join.
    pub rollup_minute: i64,
    /// The healthy runs of that minute.
    pub rollup_runs: i64,
    /// The summed usage of that minute's runs.
    pub rollup_usage: Option<Value>,
    /// A person's Rodar: when they asked for one fresh run, kept until a claim starts it; 0 when none is pending.
    pub run_requested: i64,
    /// The keyed digest of the result the last queued notice showed; empty until one is shown.
    pub output_digest: String,
}

impl Routine {
    /// A Routine of this identity, schedule, Assistants, plan, and scheduling, with every other field at its default.
    #[must_use]
    pub fn new(
        routine_id: &str,
        name: &str,
        schedule: Value,
        assistants: Vec<(String, String)>,
        plan: Value,
        anchor: i64,
        next_run_at: i64,
    ) -> Self {
        Self {
            routine_id: routine_id.to_owned(),
            name: name.to_owned(),
            schedule,
            assistants,
            plan,
            anchor,
            next_run_at,
            description: String::new(),
            needs_reconfirm: false,
            deleting: false,
            gap_started_at: 0,
            missed: 0,
            reported_missed: 0,
            revision: 1,
            timezone_source: "browser".to_owned(),
            paused: false,
            confirmation: None,
            permitted: Vec::new(),
            failures: 0,
            rollup_minute: 0,
            rollup_runs: 0,
            rollup_usage: None,
            run_requested: 0,
            output_digest: String::new(),
        }
    }

    /// The zone the Routine fires and dates its runs in, which only its plan holds.
    #[must_use]
    pub fn timezone(&self) -> &str {
        as_str(at(&self.plan, "timezone")).unwrap_or_default()
    }
}

/// Where one run record stands (ADR-0092 section 5).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RunStatus {
    /// It may run a segment.
    Leased,
    /// It waits for a person.
    Frozen,
    /// It is fenced while its incident is indexed.
    Held,
    /// Its incident, which recovery or a person resolves.
    Unresolved,
    /// Set aside, releasing what it kept.
    Skipped,
}

impl RunStatus {
    /// Its name as Team persists it.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Leased => "leased",
            Self::Frozen => "frozen",
            Self::Held => "held",
            Self::Unresolved => "unresolved",
            Self::Skipped => "skipped",
        }
    }

    /// The status of this persisted name.
    #[must_use]
    pub fn parse(name: &str) -> Option<Self> {
        [
            Self::Leased,
            Self::Frozen,
            Self::Held,
            Self::Unresolved,
            Self::Skipped,
        ]
        .into_iter()
        .find(|status| status.as_str() == name)
    }

    /// A live run: it may run a segment, waits for a person, or is fenced while its hold settles.
    #[must_use]
    pub const fn live(self) -> bool {
        matches!(self, Self::Leased | Self::Frozen | Self::Held)
    }
}

/// One run of a Routine, live or held as its incident: one record with a status (ADR-0092 section 5).
#[derive(Clone, Debug, PartialEq)]
pub struct Run {
    /// Its id, which also keys its one notice.
    pub run_id: String,
    /// Its Routine.
    pub routine_id: String,
    /// Where it stands.
    pub status: RunStatus,
    /// The digest of its lease token, or empty.
    pub lease_sha256: String,
    /// The routine key fingerprint it was claimed under, or [`HUMAN_LEASE`].
    pub lease_key: String,
    /// When its lease ends.
    pub lease_expires_at: i64,
    /// The active time it has left.
    pub active_seconds_left: i64,
    /// A frozen run's request kind; empty otherwise.
    pub request_kind: String,
    /// The Assistant it waits at or was held at.
    pub assistant_id: String,
    /// The Action it waits at or was held at.
    pub action: String,
    /// The run's own Action journal generation, bound at its first segment, or its continuation's after a hold.
    pub generation: String,
    /// The version of its one notice.
    pub notice_version: i64,
    /// The human requests the logical run has answered, through every freeze, hold, and continuation.
    pub requests_used: i64,
    /// An incident's call by its 1-based step position.
    pub position: Option<i64>,
    /// An incident's plan step count.
    pub steps: i64,
    /// The run's active time so far: `{"duration_ms": n}`.
    pub usage: Value,
    /// The run lost the protection of its secret values (ADR-0101 section 6).
    pub protection_lost: bool,
    /// The Routine revision the run executes, which its claim names and its cursor binds (ADR-0092).
    pub revision: i64,
}

impl Run {
    /// A run of this id, Routine, and status, with every other field at its default.
    #[must_use]
    pub fn new(run_id: &str, routine_id: &str, status: RunStatus) -> Self {
        Self {
            run_id: run_id.to_owned(),
            routine_id: routine_id.to_owned(),
            status,
            lease_sha256: String::new(),
            lease_key: String::new(),
            lease_expires_at: 0,
            active_seconds_left: plan::SHORT_ACTIVE_SECONDS,
            request_kind: String::new(),
            assistant_id: String::new(),
            action: String::new(),
            generation: String::new(),
            notice_version: 0,
            requests_used: 0,
            position: None,
            steps: 0,
            usage: object(vec![("duration_ms", int(0))]),
            protection_lost: false,
            revision: 1,
        }
    }
}

/// One undelivered notice.
#[derive(Clone, Debug, PartialEq)]
pub struct Notice {
    /// Its id.
    pub notice_id: String,
    /// Its Routine.
    pub routine_id: String,
    /// Its run, or empty for an outcome of the Routine itself.
    pub run_id: String,
    /// Its outcome.
    pub outcome: String,
    /// When it was written.
    pub created_at: i64,
    /// Its closed detail.
    pub detail: Value,
    /// Grows when a notice is updated; Admin acknowledges the exact version it delivered.
    pub version: i64,
    /// The Routine's name when this version was written.
    pub name: String,
    /// The run's usage, a rollup's summed usage, or a creation's recording usage.
    pub usage: Option<Value>,
    /// The run lost its protection.
    pub protection_lost: bool,
}

impl Notice {
    /// A first version of a notice, named when it is put.
    #[must_use]
    pub fn new(
        notice_id: &str,
        routine_id: &str,
        run_id: &str,
        outcome: &str,
        created_at: i64,
        detail: Value,
    ) -> Self {
        Self {
            notice_id: notice_id.to_owned(),
            routine_id: routine_id.to_owned(),
            run_id: run_id.to_owned(),
            outcome: outcome.to_owned(),
            created_at,
            detail,
            version: 1,
            name: String::new(),
            usage: None,
            protection_lost: false,
        }
    }
}

/// One Team's Routine state.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct TeamRoutines {
    /// Its Routines.
    pub routines: Vec<Routine>,
    /// Its run records.
    pub runs: Vec<Run>,
    /// Its undelivered notices.
    pub notices: Vec<Notice>,
    /// When it last served a claim.
    pub served_at: i64,
    /// Every start in the last rolling 24 hours (ADR-0092 section 9).
    pub starts: Vec<Start>,
    /// (run id, generation) of ended runs whose journal generation, continuation, and cursor are still held.
    pub discards: Vec<(String, String)>,
}

/// One claimed run, as its worker receives it.
#[derive(Clone, Debug, PartialEq)]
pub struct Claim {
    /// The run.
    pub run: Run,
    /// The lease token, which only the worker holds.
    pub lease_token: String,
    /// The Routine revision the run was claimed at.
    pub revision: i64,
    /// The plan digest the run was claimed at.
    pub plan_digest: String,
    /// The run's active time.
    pub active_seconds: i64,
}

/// What a worker proves to drive a leased run: its lease digest and the key it was claimed under.
#[derive(Clone, PartialEq, Eq)]
pub struct Lease {
    /// The digest of the lease token.
    pub sha256: String,
    /// The key it was claimed under.
    pub key: String,
}

impl fmt::Debug for Lease {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("Lease(..)")
    }
}

/// The Team's live runs: each holds its Routine's one slot.
#[must_use]
pub fn live_runs(state: &TeamRoutines) -> Vec<&Run> {
    state
        .runs
        .iter()
        .filter(|item| item.status.live())
        .collect()
}

/// The Team's held runs' incidents, unresolved or set aside.
#[must_use]
pub fn incidents(state: &TeamRoutines) -> Vec<&Run> {
    state
        .runs
        .iter()
        .filter(|item| !item.status.live())
        .collect()
}

/// The lease a token proves under a key.
#[must_use]
pub fn lease_of(token: &str, key: &str) -> Lease {
    Lease {
        sha256: lease_sha256(token),
        key: key.to_owned(),
    }
}

/// A new 32-hex id.
///
/// # Errors
///
/// [`ENTROPY_UNAVAILABLE`] when the random source fails.
pub fn new_id(entropy: &dyn Entropy) -> Transition<String> {
    token_hex(entropy, 16).map_err(|_| RoutineStateError::new(ENTROPY_UNAVAILABLE))
}

/// A new lease token: 32 random bytes in unpadded URL-safe base64, as Python's `secrets.token_urlsafe(32)`.
///
/// # Errors
///
/// [`ENTROPY_UNAVAILABLE`] when the random source fails.
pub fn new_token(entropy: &dyn Entropy) -> Transition<String> {
    let mut bytes = [0_u8; 32];
    entropy
        .fill(&mut bytes)
        .map_err(|_| RoutineStateError::new(ENTROPY_UNAVAILABLE))?;
    Ok(URL_SAFE_NO_PAD.encode(bytes))
}

/// The digest Team keeps of a lease token.
#[must_use]
pub fn lease_sha256(token: &str) -> String {
    sha256_hex(token.as_bytes())
}

/// The Routine's next firing strictly after `after`.
///
/// # Errors
///
/// `routine-invalid` for a schedule or zone outside the contract.
pub fn next_after(routine: &Routine, after: i64) -> Transition<i64> {
    schedule::next_run(
        &routine.schedule,
        at(&routine.plan, "timezone"),
        routine.anchor,
        after,
    )
    .map_err(|_| RoutineStateError::new("routine-invalid"))
}

/// Whether the Routine runs continuously.
#[must_use]
pub fn continuous(routine: &Routine) -> bool {
    as_str(at(&routine.schedule, "kind")) == Some("continuous")
}

/// How late a run may still start: one period, and at most 12 hours.
#[must_use]
pub fn grace_seconds(routine: &Routine) -> i64 {
    let period = match as_str(at(&routine.schedule, "kind")) {
        Some("daily") => 86_400,
        Some("weekly") => 7 * 86_400,
        Some("monthly") => 28 * 86_400,
        _ => MAX_GRACE_SECONDS,
    };
    period.min(MAX_GRACE_SECONDS)
}

/// Applies a transition to a copy of the state, which is returned only when every part of it succeeds.
pub(crate) fn changed(
    state: &TeamRoutines,
    change: impl FnOnce(&mut TeamRoutines) -> Transition<()>,
) -> Transition {
    let mut next = state.clone();
    change(&mut next)?;
    Ok(next)
}

/// The Routine of this id.
///
/// # Errors
///
/// `routine-not-found`.
pub fn routine<'s>(state: &'s TeamRoutines, routine_id: &str) -> Transition<&'s Routine> {
    state
        .routines
        .iter()
        .find(|item| item.routine_id == routine_id)
        .ok_or(RoutineStateError::new("routine-not-found"))
}

/// The run of this id.
///
/// # Errors
///
/// `run-not-found`.
pub fn run<'s>(state: &'s TeamRoutines, run_id: &str) -> Transition<&'s Run> {
    state
        .runs
        .iter()
        .find(|item| item.run_id == run_id)
        .ok_or(RoutineStateError::new("run-not-found"))
}

/// Replaces the Routine of the same id.
pub fn replace_routine(state: &mut TeamRoutines, updated: Routine) {
    if let Some(slot) = state
        .routines
        .iter_mut()
        .find(|item| item.routine_id == updated.routine_id)
    {
        *slot = updated;
    }
}

/// Replaces the run of the same id.
pub fn replace_run(state: &mut TeamRoutines, updated: Run) {
    if let Some(slot) = state
        .runs
        .iter_mut()
        .find(|item| item.run_id == updated.run_id)
    {
        *slot = updated;
    }
}

/// Remove an ended run and, in the same write, queue the removal of everything it held.
///
/// # Errors
///
/// `run-not-found`, or a refusal of the Routine's rescheduling.
pub fn without_run(state: &mut TeamRoutines, run_id: &str, now: i64) -> Transition<()> {
    let value = run(state, run_id)?.clone();
    state.runs.retain(|item| item.run_id != run_id);
    ended(state, &value, now);
    Ok(())
}

/// Queue the removal of what a run's generation held, as the run ends or is held.
///
/// A continuous Routine keeps its cadence from this run's start, so its runs never overlap or drift.
pub fn ended(state: &mut TeamRoutines, value: &Run, now: i64) {
    state
        .discards
        .push((value.run_id.clone(), value.generation.clone()));
    rebase_continuous_into(state, &value.routine_id, now);
}

/// A continuous Routine's next run once its run ended at `now`; any other Routine is unchanged.
///
/// The next is due when its claim set it, its gap after the run started, or now when the run outlasted the gap.
#[must_use]
pub fn rebase_continuous(state: &TeamRoutines, routine_id: &str, now: i64) -> TeamRoutines {
    let mut next = state.clone();
    rebase_continuous_into(&mut next, routine_id, now);
    next
}

fn rebase_continuous_into(state: &mut TeamRoutines, routine_id: &str, now: i64) {
    if let Some(current) = state
        .routines
        .iter_mut()
        .find(|item| item.routine_id == routine_id)
        && continuous(current)
    {
        current.next_run_at = now.max(current.next_run_at);
    }
}

/// Team removed what one ended generation of a run held; a resumed run may queue more than one.
#[must_use]
pub fn discarded(state: &TeamRoutines, run_id: &str, generation: &str) -> TeamRoutines {
    let mut next = state.clone();
    next.discards
        .retain(|(run, held)| !(run == run_id && held == generation));
    next
}

/// Pausar, or resume: resuming never bypasses an unresolved incident, which still holds the Routine.
///
/// A resume starts a fresh failure streak, so the person's decision is not undone by the failures before it.
///
/// # Errors
///
/// `routine-not-found`, also for a Routine being deleted.
pub fn set_paused(state: &TeamRoutines, routine_id: &str, paused: bool) -> Transition {
    changed(state, |next| set_paused_into(next, routine_id, paused))
}

pub(crate) fn set_paused_into(
    state: &mut TeamRoutines,
    routine_id: &str,
    paused: bool,
) -> Transition<()> {
    let mut value = routine(state, routine_id)?.clone();
    if value.deleting {
        return Err(RoutineStateError::new("routine-not-found"));
    }
    value.paused = paused;
    if !paused {
        value.failures = 0;
    }
    replace_routine(state, value);
    Ok(())
}

/// Mark a Routine as deleting, so it is never claimed or resumed again; its live runs are returned for the caller.
///
/// # Errors
///
/// `routine-not-found`.
pub fn begin_delete(
    state: &TeamRoutines,
    routine_id: &str,
) -> Transition<(TeamRoutines, Vec<Run>)> {
    let mut value = routine(state, routine_id)?.clone();
    let runs: Vec<Run> = live_runs(state)
        .into_iter()
        .filter(|item| item.routine_id == routine_id)
        .cloned()
        .collect();
    value.deleting = true;
    let mut next = state.clone();
    replace_routine(&mut next, value);
    Ok((next, runs))
}

/// Remove a deleting Routine once none of its runs remains, publishing its `deleted` notice in the same write.
///
/// # Errors
///
/// `routine-not-found`, `routine-busy`, or a refusal of its notice.
pub fn complete_delete(
    state: &TeamRoutines,
    routine_id: &str,
    now: i64,
    entropy: &dyn Entropy,
) -> Transition {
    let value = routine(state, routine_id)?;
    if !value.deleting || state.runs.iter().any(|item| item.routine_id == routine_id) {
        return Err(RoutineStateError::new("routine-busy"));
    }
    changed(state, |next| {
        put_notice_into(
            next,
            Notice::new(
                &new_id(entropy)?,
                routine_id,
                "",
                "deleted",
                now,
                object(vec![]),
            ),
        )?;
        next.routines.retain(|item| item.routine_id != routine_id);
        Ok(())
    })
}

/// The Routine's Assistants no longer match its pins: no claim until an authenticated update.
///
/// # Errors
///
/// `routine-not-found`, or a refusal of its notice.
pub fn mark_scope_changed(
    state: &TeamRoutines,
    routine_id: &str,
    now: i64,
    entropy: &dyn Entropy,
) -> Transition {
    let mut value = routine(state, routine_id)?.clone();
    changed(state, |next| {
        put_notice_into(
            next,
            Notice::new(
                &new_id(entropy)?,
                routine_id,
                "",
                "scope-changed",
                now,
                object(vec![]),
            ),
        )?;
        value.needs_reconfirm = true;
        replace_routine(next, value);
        Ok(())
    })
}

/// A run's journal generation in the Team's network, and its run, when it is one.
#[must_use]
pub fn generation_parts(generation: &str) -> Option<(String, String)> {
    let found = GENERATION_RE.search(generation).ok()??;
    Some((
        found.group(generation, 1)?.to_owned(),
        found.group(generation, 2)?.to_owned(),
    ))
}

#[cfg(test)]
mod tests;
