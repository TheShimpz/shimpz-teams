//! Team enforces every Routine lease and deadline itself, even if Admin's worker stalls or Team restarts
//! (`local/routine/watchdog.py`, ADR-0086).
//!
//! A leased run whose lease or active time ran out, or whose routine key is no longer current, is stopped when its
//! segment is running; otherwise nothing can be running it, so it is recovered from its sealed cursor, recovery
//! snapshot, and journal (ADR-0092): a run that completed every step ends done, one that may have acted is held as an
//! incident, and one that dispatched nothing ends interrupted. Nothing is replayed automatically.

use std::collections::{BTreeMap, BTreeSet};
use std::time::Duration;

use team_protocol::python::value::object_of;
use team_routine::hold;
use team_routine::record::{self, Run, RunStatus};
use team_routine::runs;

use super::replay::Progress;
use super::service::RoutineService;
use super::store::TeamLock;
use crate::chat::problem;
use crate::http::service::Problem;

/// How often a periodic pass runs.
pub const INTERVAL: Duration = Duration::from_secs(30);

/// The periodic watchdog task; it stops when dropped.
#[derive(Debug)]
pub struct Watchdog(tokio::task::JoinHandle<()>);

impl Drop for Watchdog {
    fn drop(&mut self) {
        self.0.abort();
    }
}

fn unavailable<E>(_error: E) -> Problem {
    problem::routine_state_unavailable()
}

/// Removes the Team's leftovers no live or queued run owns, under its held Routine lock: a crash's continuation or
/// snapshot, and a cursor no run, set-aside incident, or queued removal keeps.
fn remove_leftovers(lock: &TeamLock<'_>) -> Result<(), Problem> {
    let current = lock.load().map_err(unavailable)?;
    let queued: BTreeSet<&str> = current
        .discards
        .iter()
        .map(|(run, _)| run.as_str())
        .collect();
    let mut live: BTreeSet<&str> = record::live_runs(&current)
        .iter()
        .map(|item| item.run_id.as_str())
        .collect();
    live.extend(&queued);
    for run_id in lock.continuations().map_err(unavailable)? {
        if !live.contains(run_id.as_str()) {
            lock.delete_continuation(&run_id).map_err(unavailable)?;
        }
    }
    for run_id in lock.recoveries().map_err(unavailable)? {
        if !live.contains(run_id.as_str()) {
            lock.delete_recovery(&run_id).map_err(unavailable)?;
        }
    }
    let mut kept: BTreeSet<&str> = current
        .runs
        .iter()
        .map(|item| item.run_id.as_str())
        .collect();
    kept.extend(&queued);
    for run_id in lock.cursors().map_err(unavailable)? {
        if !kept.contains(run_id.as_str()) {
            lock.delete_cursor(&run_id).map_err(unavailable)?;
        }
    }
    Ok(())
}

impl RoutineService {
    /// The transition that ends one orphaned run as its durable progress shows, and how it ended.
    fn orphan_ended(
        &self,
        state: &record::TeamRoutines,
        value: &Run,
        progress: Progress,
        shown: Option<&strict_json::Value>,
    ) -> record::Transition<(record::TeamRoutines, &'static str)> {
        let now = self.now();
        let entropy = self.parts.entropy.as_ref();
        let (run_id, lease) = (&value.run_id, &value.lease_sha256);
        match progress {
            Progress::Done => runs::complete_recovered(state, run_id, lease, now, shown, entropy)
                .map(|next| (next, "done")),
            Progress::Partial => {
                hold::hold_recovered(state, run_id, lease).map(|next| (next, "held"))
            }
            Progress::None => {
                let detail = object_of(vec![(
                    "code",
                    strict_json::Value::String("interrupted".to_owned()),
                )]);
                runs::end(state, run_id, now, "failed", detail, None, entropy)
                    .map(|next| (next, "failed"))
            }
        }
    }

    /// Ends a leased run nothing is running, as its sealed cursor, snapshot, and journal show it went; a run that
    /// ended or changed lease since the pass read it is left alone.
    fn recover_orphan(&self, team_id: &str, value: &Run) -> Result<Option<&'static str>, Problem> {
        let progress = self.progress(team_id, value);
        let shown = if progress == Progress::Done {
            self.sealed_shown(team_id, value)
        } else {
            None
        };
        let (run_id, lease) = (&value.run_id, &value.lease_sha256);
        self.update(team_id, |state| {
            let same = state.runs.iter().any(|item| {
                item.run_id == *run_id
                    && item.status == RunStatus::Leased
                    && item.lease_sha256 == *lease
            });
            if !same {
                return (state, None);
            }
            let changed = self.orphan_ended(&state, value, progress, shown.as_ref());
            match changed {
                Ok((next, outcome)) => (next, Some(outcome)),
                Err(_) => (state, None),
            }
        })
    }

    async fn check_team(
        &self,
        team_id: &str,
        key: Option<&str>,
        startup: bool,
    ) -> Result<(), Problem> {
        let now = self.now();
        let state = self.load(team_id)?;
        let mut found: BTreeMap<String, Run> = BTreeMap::new();
        let leased = state
            .runs
            .iter()
            .filter(|item| startup && item.status == RunStatus::Leased);
        let rekeyed = key
            .map(|key| hold::rekeyed(&state, key))
            .unwrap_or_default();
        for value in hold::expired(&state, now)
            .into_iter()
            .chain(rekeyed)
            .chain(leased)
        {
            found.insert(value.run_id.clone(), value.clone());
        }
        for value in found.values() {
            if self.stop_routine_run(team_id, &value.run_id) {
                continue;
            }
            if let Some(outcome) = self.recover_orphan(team_id, value)? {
                self.audit("routine-recover", false, team_id, outcome).await;
            }
        }
        // A held run finishes its hold from its last durable step; one that fails is retried by the next pass.
        for run_id in self.reconcile_team(team_id)? {
            self.audit("routine-hold", false, team_id, &run_id).await;
        }
        {
            let lock = self.parts.store.lock(team_id).map_err(unavailable)?;
            remove_leftovers(&lock)?;
        }
        self.settle_team(team_id)
    }

    /// One pass: stops segments out of active time, then recovers and cleans up every Team's Routine runs. A periodic
    /// pass audits one Team's failure and goes on; at startup any failure is fatal.
    ///
    /// # Errors
    /// At startup, the first Team's failure, or a Routine directory whose Team cannot be identified.
    pub async fn check_routines(&self, startup: bool) -> Result<(), Problem> {
        for (team_id, run_id) in self.mark_overdue() {
            let _stopped = self.stop_routine_run(&team_id, &run_id);
        }
        let key = self.parts.key.fingerprint();
        let teams = match self.parts.store.teams() {
            Ok(teams) => teams,
            Err(_) if startup => return Err(problem::routine_state_unavailable()),
            Err(_) => {
                self.audit("routine-watchdog", false, "", "check-failed")
                    .await;
                return Ok(());
            }
        };
        for team_id in teams {
            if let Err(failure) = self.check_team(&team_id, key.as_deref(), startup).await {
                if startup {
                    return Err(failure);
                }
                self.audit("routine-watchdog", false, &team_id, "team-check-failed")
                    .await;
            }
        }
        Ok(())
    }

    /// Starts the periodic watchdog: a pass every `interval` until the returned handle drops. A failed pass leaves
    /// every run as it was, is audited, and the next pass retries.
    #[must_use]
    pub fn spawn_watchdog(&self, interval: Duration) -> Watchdog {
        let service = self.clone();
        Watchdog(tokio::spawn(async move {
            let mut ticks =
                tokio::time::interval_at(tokio::time::Instant::now() + interval, interval);
            loop {
                ticks.tick().await;
                let _checked = service.check_routines(false).await;
            }
        }))
    }
}
