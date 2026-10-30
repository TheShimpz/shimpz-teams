//! Claiming a Team's due Routine runs, without I/O (ADR-0086, ADR-0092): sweeps, readiness, leases, and generations.

pub mod starts;

use std::collections::BTreeSet;

use subtle::ConstantTimeEq;
use team_core::entropy::Entropy;
use team_protocol::payload::SHA256_RE;
use team_protocol::routine::daily_cap;
use team_protocol::turn::SAFE_ID_RE;

use crate::definition::run_units;
use crate::json::{int, object, sha256_hex};
use crate::plan;
use crate::record::{
    self, Claim, LEASE_SECONDS, Lease, MAX_COUNTED_MISSES, MAX_ROUTINES, MAX_RUNS,
    MAX_UNDELIVERED_NOTICES, Notice, Routine, RoutineStateError, Run, RunStatus, TeamRoutines,
    Transition, changed, continuous, live_runs, new_id, new_token, next_after, put_notice_into,
    replace_routine, replace_run,
};

/// The suffix of the generation a held run's one verification runs in, released by the verification itself.
pub const VERIFY_SUFFIX: &str = "v1";

/// The firings from `first` up to (not including) `limit`, bounded, and the first firing at or after it.
fn count_before(routine: &Routine, first: i64, limit: i64) -> Transition<(i64, i64)> {
    let (mut count, mut current) = (0, first);
    while current < limit && count < MAX_COUNTED_MISSES {
        count += 1;
        current = next_after(routine, current)?;
    }
    if current < limit {
        current = next_after(routine, limit - 1)?;
    }
    Ok((count, current))
}

fn gap_notice_id(routine: &Routine) -> String {
    sha256_hex(format!("skipped:{}:{}", routine.routine_id, routine.gap_started_at).as_bytes())
        [..32]
        .to_owned()
}

/// Publish or update the one skipped notice of the Routine's current gap when it has new misses and room.
fn report_gap(
    state: &mut TeamRoutines,
    routine: &Routine,
    now: i64,
    in_flight: bool,
) -> Transition<()> {
    if routine.missed == routine.reported_missed {
        return Ok(());
    }
    let notice_id = gap_notice_id(routine);
    let pending = state.notices.iter().any(|item| item.notice_id == notice_id);
    if !(pending || in_flight || record::undelivered(state) < MAX_UNDELIVERED_NOTICES) {
        return Ok(());
    }
    // The count only grows within a gap, so it doubles as the notice version Admin acknowledges.
    let mut notice = Notice::new(
        &notice_id,
        &routine.routine_id,
        "",
        "skipped",
        now,
        object(vec![("missed", int(routine.missed))]),
    );
    notice.version = routine.missed;
    put_notice_into(state, notice)?;
    let mut reported = routine.clone();
    reported.reported_missed = routine.missed;
    replace_routine(state, reported);
    Ok(())
}

fn miss(routine: &Routine, first: i64, count: i64, next_run_at: i64) -> Routine {
    let mut missed = routine.clone();
    if missed.gap_started_at == 0 {
        missed.gap_started_at = first;
    }
    missed.missed += count;
    missed.next_run_at = next_run_at;
    missed
}

/// Skip every firing too late to start; a Routine's continuous gap is one notice, updated as it grows.
///
/// # Errors
///
/// A refusal of a skipped notice, or `routine-invalid` for a schedule outside the contract.
pub fn sweep(state: &TeamRoutines, now: i64) -> Transition {
    changed(state, |next| sweep_into(next, now))
}

fn sweep_into(state: &mut TeamRoutines, now: i64) -> Transition<()> {
    for item in state.routines.clone() {
        if continuous(&item) {
            // A continuous Routine has no backlog to skip: it starts again once it may.
            continue;
        }
        let mut item = item;
        let cutoff = now - record::grace_seconds(&item);
        if item.next_run_at < cutoff {
            let (count, next_run_at) = count_before(&item, item.next_run_at, cutoff)?;
            item = miss(&item, item.next_run_at, count, next_run_at);
            replace_routine(state, item.clone());
        }
        report_gap(state, &item, now, false)?;
    }
    Ok(())
}

/// The earliest instant a due Routine may start under the Team ceiling and, for a continuous one, its own cap.
///
/// A scheduled Routine's own firings already bound its starts, and a late start never delays the next firing.
#[must_use]
pub fn free_at(state: &TeamRoutines, routine: &Routine, now: i64) -> i64 {
    let cap = continuous(routine).then(|| daily_cap(&routine.schedule));
    starts::free_at(
        &state.starts,
        &routine.routine_id,
        cap,
        now,
        run_units(routine),
    )
}

/// When the Routine is next due: its next firing, or sooner a person's pending Rodar.
const fn due_at(routine: &Routine) -> i64 {
    if routine.run_requested != 0 && routine.run_requested < routine.next_run_at {
        routine.run_requested
    } else {
        routine.next_run_at
    }
}

/// The Routines that may start once due and under their caps: confirmed, not paused, held, running, or long.
fn ready<'s>(state: &'s TeamRoutines, busy: &BTreeSet<String>, long: bool) -> Vec<&'s Routine> {
    state
        .routines
        .iter()
        .filter(|item| {
            !item.needs_reconfirm
                && !item.deleting
                && !item.paused
                && !busy.contains(&item.routine_id)
                && (long || !plan::long_run(run_units(item)))
        })
        .collect()
}

/// Whether one of the Team's runs is leased to drive a segment; frozen and held runs hold no slot.
fn segment_leased(state: &TeamRoutines) -> bool {
    state
        .runs
        .iter()
        .any(|item| item.status == RunStatus::Leased)
}

/// Whether the Team must catch up before any run starts: undelivered notices, cleanup, or incident room.
fn backpressured(state: &TeamRoutines) -> bool {
    record::undelivered(state) >= MAX_UNDELIVERED_NOTICES
        || state.discards.len() >= MAX_ROUTINES
        || !incident_capacity(state)
}

/// The Team's oldest due Routine that may start now, or `None`; the caller has already swept.
///
/// A Team leases one run at a time (ADR-0092 section 9); without `long`, only a short Routine may start.
#[must_use]
pub fn claimable(state: &TeamRoutines, now: i64, long: bool) -> Option<&Routine> {
    if backpressured(state) || segment_leased(state) {
        return None;
    }
    ready(state, &busy(state), long)
        .into_iter()
        .filter(|item| due_at(item) <= now && free_at(state, item, now) <= now)
        .min_by(|left, right| {
            (due_at(left), &left.routine_id).cmp(&(due_at(right), &right.routine_id))
        })
}

/// The earliest instant after `now` one of the Team's Routines becomes due to start, or `None`.
///
/// A capped Routine is due when its earliest start leaves the window; a paused, held, busy, deleting, or unconfirmed
/// one wakes nothing until its own resolution does.
#[must_use]
pub fn next_due(state: &TeamRoutines, now: i64) -> Option<i64> {
    if backpressured(state) || segment_leased(state) {
        // Nothing starts until notices are delivered or ended runs are cleaned up; the next reconciliation retries.
        return None;
    }
    ready(state, &busy(state), true)
        .into_iter()
        .map(|item| due_at(item).max(free_at(state, item, now)))
        .filter(|due| *due > now)
        .min()
}

/// Routines an unresolved incident holds: no cycle of theirs starts, whatever else resumes them.
#[must_use]
pub fn held_routines(state: &TeamRoutines) -> BTreeSet<String> {
    state
        .runs
        .iter()
        .filter(|item| item.status == RunStatus::Unresolved)
        .map(|item| item.routine_id.clone())
        .collect()
}

/// Routines a live run or an unresolved incident holds; a set-aside incident holds nothing.
fn busy(state: &TeamRoutines) -> BTreeSet<String> {
    let mut busy: BTreeSet<String> = live_runs(state)
        .into_iter()
        .map(|item| item.routine_id.clone())
        .collect();
    busy.extend(held_routines(state));
    busy
}

/// Whether one more run may start, reserving the room its incident would need.
///
/// Each live run could be held, so each reserves an unresolved incident's room, and every run keeps its own record.
#[must_use]
pub fn incident_capacity(state: &TeamRoutines) -> bool {
    let unresolved = state
        .runs
        .iter()
        .filter(|item| item.status == RunStatus::Unresolved)
        .count();
    let live = live_runs(state).len() + 1;
    unresolved + live <= team_protocol::routine_notice::MAX_UNRESOLVED_INCIDENTS
        && state.runs.len() < MAX_RUNS
}

/// Lease one run of the Team's oldest claimable Routine, rechecked on this exact state.
///
/// Its next firing moves past `now` so it can never be claimed twice; only this late firing is made up, and any
/// others since it join the Routine's gap, which ends here. With nothing due, the swept state is still returned: its
/// skipped notices and advanced schedules must be persisted.
///
/// # Errors
///
/// `routine-key-invalid` for a key fingerprint that is no SHA-256, or the refusal of a sweep or a notice.
pub fn claim(
    state: &TeamRoutines,
    now: i64,
    key_fingerprint: &str,
    long: bool,
    entropy: &dyn Entropy,
) -> Transition<(TeamRoutines, Option<Claim>)> {
    if !SHA256_RE.is_full(key_fingerprint) {
        return Err(RoutineStateError::new("routine-key-invalid"));
    }
    // Sweep first, so a firing too late to start is skipped here even if no sweep ran since it was due.
    let mut next = sweep(state, now)?;
    let Some(due) = claimable(&next, now, long).cloned() else {
        return Ok((next, None));
    };
    if due.run_requested != 0 && due.next_run_at > now {
        // A person's Rodar starts on its own: the standing cadence and any gap it reports stay as they are.
        let mut requested = due;
        requested.run_requested = 0;
        let claim = lease(&mut next, requested, now, key_fingerprint, entropy)?;
        return Ok((next, Some(claim)));
    }
    let scheduled_at = due.next_run_at;
    // A firing due now also serves any pending Rodar: one run, never two.
    let mut due = due;
    due.run_requested = 0;
    if continuous(&due) {
        // Provisional: the run's end sets the next one its gap after it, and nothing starts while it runs.
        due.next_run_at = next_after(&due, now)?;
    } else {
        let following = next_after(&due, scheduled_at)?;
        let (extra, next_run_at) = count_before(&due, following, now + 1)?;
        if extra == 0 {
            due.next_run_at = next_run_at;
        } else {
            due = miss(&due, following, extra, next_run_at);
        }
    }
    replace_routine(&mut next, due.clone());
    report_gap(&mut next, &due, now, true)?;
    let mut ended = record::routine(&next, &due.routine_id)?.clone();
    ended.gap_started_at = 0;
    ended.missed = 0;
    ended.reported_missed = 0;
    let claim = lease(&mut next, ended, now, key_fingerprint, entropy)?;
    Ok((next, Some(claim)))
}

/// Start one run of the claimed Routine under every start cap, its lease covering only its segment's start.
fn lease(
    state: &mut TeamRoutines,
    due: Routine,
    now: i64,
    key_fingerprint: &str,
    entropy: &dyn Entropy,
) -> Transition<Claim> {
    let token = new_token(entropy)?;
    let units = run_units(&due);
    let mut leased = Run::new(&new_id(entropy)?, &due.routine_id, RunStatus::Leased);
    leased.lease_sha256 = record::lease_sha256(&token);
    key_fingerprint.clone_into(&mut leased.lease_key);
    leased.lease_expires_at = now + LEASE_SECONDS;
    leased.active_seconds_left = plan::active_seconds(units);
    leased.revision = due.revision;
    let digest = plan::digest(&due.plan);
    state.starts = starts::started(&state.starts, &due.routine_id, now, units);
    state.served_at = now;
    state.runs.push(leased.clone());
    let revision = due.revision;
    replace_routine(state, due);
    let active_seconds = leased.active_seconds_left;
    Ok(Claim {
        run: leased,
        lease_token: token,
        revision,
        plan_digest: digest,
        active_seconds,
    })
}

/// A running segment's lease: the run's active time left, and a margin, so a deadline cuts it before the lease.
const fn lease_until(value: &Run, now: i64) -> i64 {
    let left = if value.active_seconds_left > 0 {
        value.active_seconds_left
    } else {
        0
    };
    now + left + plan::LEASE_MARGIN_SECONDS
}

/// The run leased again at once for a person's answer, under a fresh internal lease no machine assertion knows.
///
/// Its segment runs over the run's active time left, and no call is waiting any more; returns the run and its token.
///
/// # Errors
///
/// [`record::ENTROPY_UNAVAILABLE`] when the random source fails.
pub fn human_lease(value: &Run, now: i64, entropy: &dyn Entropy) -> Transition<(Run, String)> {
    let token = new_token(entropy)?;
    let mut resumed = value.clone();
    resumed.status = RunStatus::Leased;
    resumed.lease_sha256 = record::lease_sha256(&token);
    record::HUMAN_LEASE.clone_into(&mut resumed.lease_key);
    resumed.lease_expires_at = lease_until(value, now);
    resumed.request_kind = String::new();
    resumed.assistant_id = String::new();
    resumed.action = String::new();
    resumed.position = None;
    resumed.steps = 0;
    Ok((resumed, token))
}

fn same_digest(left: &str, right: &str) -> bool {
    bool::from(left.as_bytes().ct_eq(right.as_bytes()))
}

/// Only the live lease of a leased run, claimed under the current routine key, may drive it.
///
/// # Errors
///
/// `lease-invalid`.
pub fn require_lease(value: &Run, lease: &Lease, now: i64) -> Transition<()> {
    if value.status != RunStatus::Leased
        || !same_digest(&value.lease_sha256, &lease.sha256)
        || value.lease_key != lease.key
        || value.lease_expires_at <= now
        || value.active_seconds_left <= 0
    {
        return Err(RoutineStateError::new("lease-invalid"));
    }
    Ok(())
}

fn leased<'s>(state: &'s TeamRoutines, run_id: &str) -> Transition<&'s Run> {
    let value = record::run(state, run_id)?;
    if value.status != RunStatus::Leased {
        return Err(RoutineStateError::new("run-not-running"));
    }
    Ok(value)
}

/// A worker transition: only the live lease of that exact run may make it.
///
/// # Errors
///
/// `run-not-found`, `run-not-running`, or `lease-invalid`.
pub fn live_lease<'s>(
    state: &'s TeamRoutines,
    run_id: &str,
    lease: &Lease,
    now: i64,
) -> Transition<&'s Run> {
    let value = leased(state, run_id)?;
    require_lease(value, lease, now)?;
    Ok(value)
}

/// A watchdog transition: only the exact lease the watchdog read, so a run claimed again meanwhile is untouched.
///
/// # Errors
///
/// `run-not-found`, `run-not-running`, or `run-changed`.
pub fn read_lease<'s>(
    state: &'s TeamRoutines,
    run_id: &str,
    lease_sha256: &str,
) -> Transition<&'s Run> {
    let value = leased(state, run_id)?;
    if !same_digest(&value.lease_sha256, lease_sha256) {
        return Err(RoutineStateError::new("run-changed"));
    }
    Ok(value)
}

/// A run's journal generation in the Team's network.
///
/// The run's one continuation (`s1`) or verification (`v1`) after a hold gets its own: the held one is archived.
#[must_use]
pub fn generation_for(network_id: &str, run_id: &str, suffix: &str) -> String {
    if suffix.is_empty() {
        format!("{network_id}:routine:{run_id}")
    } else {
        format!("{network_id}:routine:{run_id}:{suffix}")
    }
}

/// The Team network a run's generation, or one of its continuation or verification generations, belongs to.
#[must_use]
pub fn network_of(generation: &str, run_id: &str) -> Option<String> {
    record::generation_parts(generation)
        .and_then(|(network, run)| (run == run_id).then_some(network))
}

/// The run's verification generation, in the network its own generation names.
///
/// # Errors
///
/// `routine-generation-invalid` when the generation belongs to no network of this run.
pub fn verification_of(generation: &str, run_id: &str) -> Transition<String> {
    let network = network_of(generation, run_id)
        .ok_or(RoutineStateError::new("routine-generation-invalid"))?;
    Ok(generation_for(&network, run_id, VERIFY_SUFFIX))
}

/// Bind the run's own journal generation, derived from the Team's trusted network id, once; it never changes.
///
/// A continuation after a hold already names its own generation, which must belong to the same network.
///
/// # Errors
///
/// A lease refusal, or `generation-invalid`.
pub fn bind_generation(
    state: &TeamRoutines,
    run_id: &str,
    lease: &Lease,
    now: i64,
    network_id: &str,
) -> Transition {
    let value = live_lease(state, run_id, lease, now)?.clone();
    let generation = if value.generation.is_empty() {
        generation_for(network_id, run_id, "")
    } else {
        value.generation.clone()
    };
    if !SAFE_ID_RE.is_full(&generation)
        || network_of(&generation, run_id).as_deref() != Some(network_id)
    {
        return Err(RoutineStateError::new("generation-invalid"));
    }
    // The claimed run's segment has started: its lease now covers the run's active time left, once; a later bind of
    // the same run (a retried request) never renews it.
    let extended = if value.generation.is_empty() {
        value.lease_expires_at.max(lease_until(&value, now))
    } else {
        value.lease_expires_at
    };
    let mut bound = value;
    bound.generation = generation;
    bound.lease_expires_at = extended;
    changed(state, |next| {
        replace_run(next, bound);
        Ok(())
    })
}

#[cfg(test)]
mod tests;
