//! Local Routine runs (`local/routine/run.py`, ADR-0086): a fair claim across Teams, the lease and Stop of a run, and
//! one leased segment run in the Team's execution slot with no model.

use std::collections::{HashMap, HashSet};
use std::sync::{MutexGuard, PoisonError};
use std::time::Duration;

use strict_json::Value;
use team_action::human::ActionTranscript;
use team_chat::progress::Reporter;
use team_core::stop::Stop;
use team_protocol::python::value::{int, object_of};
use team_routine::claim;
use team_routine::plan;
use team_routine::record::{self, Lease, Routine, Run, RunStatus, TeamRoutines};
use team_routine::runs;
use tokio::time::Instant;

use super::replay::Protected;
use super::service::RoutineService;
use crate::chat::problem;
use crate::coordination::executions::{Demand, Execution, ExecutionKind, Refusal};
use crate::http::service::Problem;

pub mod finish;

/// How long after a Routine frees the slot a person's chat message, refused while that Routine held it, keeps further
/// runs of its Team waiting for the person's turn.
pub const CHAT_PRIORITY_SECONDS: u64 = 30;

/// A segment in the execution slot: what Stop needs to reach it, and when its active time runs out.
struct Registration {
    team_id: String,
    token: String,
    stop: Stop,
    deadline: Instant,
    overdue: bool,
}

/// Every registered Routine execution, and the runs a Stop fenced out before their worker registered.
#[derive(Default)]
pub struct Registry {
    runs: HashMap<String, Registration>,
    halting: HashSet<String>,
}

/// One leased run's segment: its Team, lease, execution, and Routine; it asks no model.
pub struct LeasedRun<'e> {
    /// The Team.
    pub team_id: String,
    /// The run.
    pub run_id: String,
    /// Its live lease.
    pub lease: Lease,
    /// The execution holding the Team's slot.
    pub execution: &'e Execution,
    /// Its Routine.
    pub routine: Routine,
    /// The human answers this logical run already gave, kept for deterministic replay if it freezes again.
    pub transcripts: Vec<ActionTranscript>,
    /// The human requests it answered.
    pub requests_used: usize,
    /// The private values each failed operation was given, for this execution's one recovery episode.
    pub protected: Protected,
}

impl LeasedRun<'_> {
    /// The execution's token.
    #[must_use]
    pub fn token(&self) -> &str {
        self.execution.token()
    }

    /// The execution's Stop.
    #[must_use]
    pub fn stop(&self) -> &Stop {
        self.execution.stop()
    }

    /// Whether the execution was cancelled.
    #[must_use]
    pub fn cancelled(&self) -> bool {
        self.execution.stop().is_stopped()
    }
}

/// A run made reachable by Stop until it recorded how it ended; dropping it unregisters the run.
pub struct Registered<'s> {
    service: &'s RoutineService,
    run_id: String,
}

impl Drop for Registered<'_> {
    fn drop(&mut self) {
        self.service.registry().runs.remove(&self.run_id);
    }
}

fn lease_invalid() -> Problem {
    problem::routine_lease_invalid()
}

impl RoutineService {
    pub(super) fn registry(&self) -> MutexGuard<'_, Registry> {
        self.registry.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Registers a run's worker; a Stop that already found the run unregistered fences it out instead.
    ///
    /// The same execution registering again, as a recovery's continuation does, keeps a deadline that already cancelled
    /// it, so its ending is still recorded as out of time, never as stopped.
    ///
    /// # Errors
    /// `chat-stopped` for a run a Stop fenced out.
    pub(super) fn register(
        &self,
        run: (&str, &str),
        execution: &Execution,
        active_seconds: i64,
    ) -> Result<Registered<'_>, Problem> {
        let (team_id, run_id) = run;
        let mut registry = self.registry();
        if registry.halting.contains(run_id) {
            return Err(problem::chat_stopped());
        }
        let overdue = registry
            .runs
            .get(run_id)
            .is_some_and(|previous| previous.token == execution.token() && previous.overdue);
        let seconds = u64::try_from(active_seconds.max(0)).unwrap_or(0);
        registry.runs.insert(
            run_id.to_owned(),
            Registration {
                team_id: team_id.to_owned(),
                token: execution.token().to_owned(),
                stop: execution.stop().clone(),
                deadline: Instant::now() + Duration::from_secs(seconds),
                overdue,
            },
        );
        Ok(Registered {
            service: self,
            run_id: run_id.to_owned(),
        })
    }

    /// Whether this execution was cancelled by its own deadline, not by a person.
    pub(super) fn deadline_cut(&self, run_id: &str, token: &str) -> bool {
        self.registry()
            .runs
            .get(run_id)
            .is_some_and(|item| item.token == token && item.overdue)
    }

    /// Whether a segment of the run is registered as out of its active time.
    pub(super) fn overdue(&self, run_id: &str) -> bool {
        self.registry()
            .runs
            .get(run_id)
            .is_some_and(|item| item.overdue)
    }

    /// Whether a verification, recovery episode, or person's answer is still registered for the run.
    pub(super) fn executing(&self, run_id: &str) -> bool {
        self.registry().runs.contains_key(run_id)
    }

    /// Commits an outcome only if no person stopped `stop`'s execution first; whether it was committed. The decision
    /// and `commit` run under the guard a person's Stop cancels under; a cancellation `deadline` says its own deadline
    /// caused is no person's Stop.
    pub(super) fn unstopped<T>(
        &self,
        stop: &Stop,
        deadline: impl FnOnce() -> bool,
        commit: impl FnOnce() -> T,
    ) -> Option<T> {
        let _registry = self.registry();
        if stop.is_stopped() && !deadline() {
            return None;
        }
        Some(commit())
    }

    fn stop_registered(
        &self,
        team_id: &str,
        run_id: &str,
        expected: Option<(&str, &dyn Fn())>,
    ) -> bool {
        let mut registry = self.registry();
        let Some(running) = registry.runs.get_mut(run_id) else {
            return false;
        };
        if running.team_id != team_id {
            return false;
        }
        if let Some((token, mark)) = expected {
            if running.token != token || running.stop.is_stopped() {
                return false;
            }
            mark();
            // A deadline, not a person: the run's ending records it as out of time, never as stopped.
            running.overdue = true;
        }
        running.stop.stop();
        let _token = self.parts.coordination.executions.cancel(team_id, false);
        true
    }

    /// Stops exactly one running Routine run: its segment and the Action it runs; a run asks no Brain.
    #[must_use]
    pub fn stop_routine_run(&self, team_id: &str, run_id: &str) -> bool {
        self.stop_registered(team_id, run_id, None)
    }

    /// A deadline's Stop of exactly the execution it was set for; one a person already stopped stays a person's
    /// Stop, and `mark` records the deadline as the cause only when it is.
    pub(super) fn expire_routine_run(
        &self,
        team_id: &str,
        run_id: &str,
        token: &str,
        mark: &dyn Fn(),
    ) -> bool {
        self.stop_registered(team_id, run_id, Some((token, mark)))
    }

    /// Stops a leased run whether or not its worker holds the slot: a running segment is stopped, and a run not
    /// started yet ends stopped, its worker fenced out in the same step.
    ///
    /// # Errors
    /// `routine-state-unavailable`.
    pub fn halt_routine_run(&self, team_id: &str, run_id: &str) -> Result<bool, Problem> {
        let running = {
            let mut registry = self.registry();
            let running = registry.runs.contains_key(run_id);
            if !running {
                registry.halting.insert(run_id.to_owned());
            }
            running
        };
        if running {
            return Ok(self.stop_routine_run(team_id, run_id));
        }
        let now = self.now();
        let entropy = self.parts.entropy.clone();
        let ended = self.applied(team_id, |state| {
            let stopped = object_of(Vec::new());
            runs::end(
                state,
                run_id,
                now,
                "stopped",
                stopped,
                Some(RunStatus::Leased),
                entropy.as_ref(),
            )
        });
        self.registry().halting.remove(run_id);
        ended
    }

    /// Marks every running segment whose run spent its active time, so each ends failed once stopped; returns each
    /// one's Team and run for the watchdog to stop.
    pub(super) fn mark_overdue(&self) -> Vec<(String, String)> {
        let now = Instant::now();
        let mut registry = self.registry();
        let mut overdue = Vec::new();
        for (run_id, item) in &mut registry.runs {
            if item.deadline <= now {
                item.overdue = true;
                overdue.push((item.team_id.clone(), run_id.clone()));
            }
        }
        overdue
    }

    /// Chat has priority at admission (ADR-0092 section 9): no Routine starts beside a chat turn or a pending chat
    /// challenge, nor while a person who found the slot held by a Routine still waits for their turn.
    pub(super) fn chat_busy(&self, team_id: &str) -> bool {
        let executions = &self.parts.coordination.executions;
        match executions.demand(team_id) {
            Some(Demand::Waiting) => return true,
            Some(Demand::Since(freed)) => {
                if freed.elapsed() < Duration::from_secs(CHAT_PRIORITY_SECONDS) {
                    return true;
                }
                executions.clear_demand(team_id);
            }
            None => {}
        }
        let chat = self.parts.chat.clone();
        let pending = matches!(chat.human_challenges().current(team_id), Ok(Some(_)))
            || matches!(
                chat.parts().integration_challenges.current(team_id),
                Ok(Some(_))
            );
        // The slot is probed by admitting and at once releasing it; a held slot refuses.
        let probe = ExecutionKind::Routine {
            routine_id: String::new(),
        };
        pending || executions.admit(team_id, probe, String::new()).is_err()
    }
}

/// The public problem of a refused admission.
#[must_use]
pub fn admission_problem(refusal: Refusal) -> Problem {
    match refusal {
        Refusal::ChatActive => problem::chat_active(),
        Refusal::RoutineActive => problem::routine_active(),
        Refusal::SpaceResetting => problem::space_resetting(),
        Refusal::SpaceBusy => problem::space_busy(),
    }
}

impl RoutineService {
    /// Each identified Team's Routine state; one that cannot be read is audited and left out.
    async fn readable_states(&self) -> Result<Vec<(String, TeamRoutines)>, Problem> {
        let teams = self
            .parts
            .store
            .teams()
            .map_err(|_| problem::routine_state_unavailable())?;
        let mut states = Vec::new();
        for team_id in teams {
            match self.load(&team_id) {
                Ok(state) => states.push((team_id, state)),
                Err(_) => {
                    self.audit(
                        "routine-claim",
                        false,
                        &team_id,
                        "routine-state-unavailable",
                    )
                    .await;
                }
            }
        }
        Ok(states)
    }

    /// Leases one due run, the least recently served Team first; `None` when nothing may start now. Without `long`,
    /// Admin already holds a long run in a worker, so only a short run is claimed.
    ///
    /// # Errors
    /// `routine-state-unavailable` when the Teams cannot be enumerated.
    pub async fn claim_routine_run(&self, long: bool) -> Result<Option<Value>, Problem> {
        let Some(key) = self.parts.key.fingerprint() else {
            return Ok(None);
        };
        let now = self.now();
        let mut states = self.readable_states().await?;
        states
            .sort_by(|left, right| (left.1.served_at, &left.0).cmp(&(right.1.served_at, &right.0)));
        for (team_id, state) in states {
            if self.chat_busy(&team_id) {
                continue;
            }
            if !state.discards.is_empty() && self.drain(&team_id).is_err() {
                // Nothing more starts for this Team until what its ended runs hold is removed.
                continue;
            }
            if let Some(claimed) = self.claim_team(&team_id, now, &key, long).await {
                self.audit("routine-claim", true, &team_id, &claimed.run.run_id)
                    .await;
                return Ok(Some(claimed_view(&team_id, &claimed)));
            }
        }
        Ok(None)
    }

    /// Leases one due run of the Team after checking its pinned contracts, or `None`; a Team whose state cannot be
    /// changed is audited and passed over.
    async fn claim_team(
        &self,
        team_id: &str,
        now: i64,
        key: &str,
        long: bool,
    ) -> Option<record::Claim> {
        let _team = self.parts.coordination.locks.team(team_id).await;
        let due = match self.load(team_id) {
            Ok(state) => claim::sweep(&state, now)
                .ok()
                .and_then(|swept| claim::claimable(&swept, now, long).cloned()),
            Err(_) => None,
        };
        let pinned = match &due {
            Some(due) => Some((
                due.routine_id.clone(),
                self.pins_current(team_id, due).await,
            )),
            None => None,
        };
        let claimed = self.update(team_id, |state| {
            self.claimed(state, now, (key, long), pinned)
        });
        if let Ok(claimed) = claimed {
            return claimed;
        }
        self.audit("routine-claim", false, team_id, "routine-state-unavailable")
            .await;
        None
    }

    /// Whether the due Routine's pinned contracts are exactly current, `None` when the Team cannot be read; the caller
    /// holds the Team's stripe.
    async fn pins_current(&self, team_id: &str, due: &Routine) -> Option<bool> {
        let scope = self.parts.team.scope(team_id, true).await.ok()?;
        let current: Vec<(String, String)> = due
            .assistants
            .iter()
            .filter_map(|(id, _pin)| Some((id.clone(), scope.assistant(id)?.routine_scope()?)))
            .collect();
        Some(current == due.assistants)
    }

    /// The claim's write: a swept state, the due Routine marked changed, or one run leased.
    fn claimed(
        &self,
        state: TeamRoutines,
        now: i64,
        claim: (&str, bool),
        pinned: Option<(String, Option<bool>)>,
    ) -> (TeamRoutines, Option<record::Claim>) {
        let (key, long) = claim;
        let entropy = self.parts.entropy.as_ref();
        let Ok(swept) = claim::sweep(&state, now) else {
            return (state, None);
        };
        let due = claim::claimable(&swept, now, long).map(|due| due.routine_id.clone());
        match (due, pinned) {
            (Some(due), Some((checked, Some(true)))) if due == checked => {
                match claim::claim(&swept, now, key, long, entropy) {
                    Ok((next, claimed)) => (next, claimed),
                    Err(_) => (state, None),
                }
            }
            (Some(due), Some((checked, Some(false)))) if due == checked => {
                match record::mark_scope_changed(&swept, &due, now, entropy) {
                    Ok(next) => (next, None),
                    Err(_) => (swept, None),
                }
            }
            // Nothing is proven: the Routine stays due, unchanged, and the next claim checks it again.
            _ => (swept, None),
        }
    }

    /// When Admin should next claim: the earliest instant a Routine of a readable Team becomes due.
    ///
    /// # Errors
    /// `routine-state-unavailable` when the Teams cannot be enumerated.
    pub async fn next_routine_due(&self) -> Result<Option<i64>, Problem> {
        let now = self.now();
        let states = self.readable_states().await?;
        Ok(states
            .iter()
            .filter_map(|(_team, state)| claim::next_due(state, now))
            .min())
    }

    /// The run and its Routine, while `lease` is its live lease.
    ///
    /// # Errors
    /// `routine-lease-invalid`, or `routine-state-unavailable`.
    pub(super) fn live_run(
        &self,
        team_id: &str,
        run_id: &str,
        lease: &Lease,
    ) -> Result<(Run, Routine), Problem> {
        let state = self.load(team_id)?;
        let value = record::run(&state, run_id).map_err(|_| lease_invalid())?;
        claim::require_lease(value, lease, self.now()).map_err(|_| lease_invalid())?;
        let routine = record::routine(&state, &value.routine_id).map_err(|_| lease_invalid())?;
        Ok((value.clone(), routine.clone()))
    }

    /// Binds the run's own journal generation under its live lease.
    ///
    /// # Errors
    /// `team-context-unavailable` when the Team cannot be read, `routine-lease-invalid`, or a state problem.
    pub(super) async fn bind_run(
        &self,
        team_id: &str,
        run_id: &str,
        lease: &Lease,
    ) -> Result<String, Problem> {
        let network = self
            .parts
            .team
            .network(team_id)
            .await
            .map_err(|_| super::contracts::context_unavailable())?;
        let now = self.now();
        let generation = self.update(team_id, |state| {
            match claim::bind_generation(&state, run_id, lease, now, &network) {
                Ok(bound) => {
                    let generation = record::run(&bound, run_id)
                        .ok()
                        .map(|run| run.generation.clone());
                    (bound, generation)
                }
                Err(_) => (state, None),
            }
        })?;
        generation.ok_or_else(lease_invalid)
    }

    /// Why a leased run may not start under its pinned contracts, or `None` when they are exactly current.
    pub(super) async fn context_refusal(
        &self,
        team_id: &str,
        routine: &Routine,
    ) -> Option<&'static str> {
        let pinned: std::collections::BTreeMap<String, String> =
            routine.assistants.iter().cloned().collect();
        let ids: Vec<String> = pinned.keys().cloned().collect();
        let parts = &self.parts;
        let locks = &parts.coordination.locks;
        match super::contracts::current_pins(parts.team.as_ref(), locks, team_id, &ids).await {
            Err(_) => Some("team-context-unavailable"),
            Ok(current) if current == pinned => None,
            Ok(_) => Some("team-context-changed"),
        }
    }

    /// Charges one segment's active time: whole seconds to its budget, milliseconds to its usage.
    pub(super) fn spend_time(&self, team_id: &str, run_id: &str, lease: &Lease, elapsed: Duration) {
        let now = self.now();
        let seconds = i64::try_from(elapsed.as_secs()).unwrap_or(i64::MAX);
        let milliseconds = i64::try_from(elapsed.as_millis()).unwrap_or(i64::MAX);
        let _spent = self.update(team_id, |state| {
            match runs::spend(&state, run_id, lease, now, (seconds, milliseconds)) {
                Ok(next) => (next, ()),
                Err(_) => (state, ()),
            }
        });
    }

    /// Ends a run with `outcome`; its refusal means the run is no longer the caller's to end.
    ///
    /// # Errors
    /// `routine-lease-invalid`, or `routine-state-unavailable`.
    pub(super) fn end_run(
        &self,
        team_id: &str,
        run_id: &str,
        outcome: &'static str,
        detail: Value,
    ) -> Result<&'static str, Problem> {
        let now = self.now();
        let entropy = self.parts.entropy.clone();
        let ended = self.applied(team_id, |state| {
            runs::end(state, run_id, now, outcome, detail, None, entropy.as_ref())
        })?;
        if ended {
            Ok(outcome)
        } else {
            Err(lease_invalid())
        }
    }

    /// Removes what an ended run held and finishes a deletion the run was blocking; audits how it ended. A removal
    /// that fails now waits for the watchdog's next pass.
    pub(super) async fn after_run(&self, team_id: &str, routine_id: &str, outcome: &str) {
        let ok = matches!(outcome, "done" | "recovered");
        self.audit("routine-run", ok, team_id, outcome).await;
        let _settled = self.settle(team_id, routine_id);
    }

    /// Runs one segment of a leased run in the Team's execution slot, with no model. The signed request names the
    /// revision and plan digest the run was claimed at; any other is refused before anything runs.
    ///
    /// # Errors
    /// `routine-lease-invalid`, `routine-revision-stale`, the slot's refusal, or a state problem.
    pub async fn run_routine(
        &self,
        team_id: &str,
        run_id: &str,
        lease: Lease,
        claimed: (i64, String),
        progress: &Reporter,
    ) -> Result<Value, Problem> {
        let (value, routine) = self.live_run(team_id, run_id, &lease)?;
        if claimed != (routine.revision, plan::digest(&routine.plan)) {
            return Err(Problem::new(
                409,
                "Routine revision changed since the claim",
                "routine-revision-stale",
            ));
        }
        let execution = self.admit_routine(team_id, &routine.routine_id)?;
        let outcome = {
            let _registered =
                self.register((team_id, run_id), &execution, value.active_seconds_left)?;
            let run = LeasedRun {
                team_id: team_id.to_owned(),
                run_id: run_id.to_owned(),
                lease,
                execution: &execution,
                routine: routine.clone(),
                transcripts: Vec::new(),
                requests_used: 0,
                protected: Protected::default(),
            };
            self.start_run(&run, value, progress).await?
        };
        drop(execution);
        self.after_run(team_id, &routine.routine_id, outcome).await;
        Ok(segment_answer(team_id, run_id, outcome))
    }

    /// Admits one Routine execution to the Team's slot.
    ///
    /// # Errors
    /// The slot's refusal as its public problem.
    pub(super) fn admit_routine(
        &self,
        team_id: &str,
        routine_id: &str,
    ) -> Result<Execution, Problem> {
        let token = team_core::entropy::token_hex(self.parts.entropy.as_ref(), 16)
            .map_err(|_| problem::internal_error())?;
        let kind = ExecutionKind::Routine {
            routine_id: routine_id.to_owned(),
        };
        self.parts
            .coordination
            .executions
            .admit(team_id, kind, token)
            .map_err(admission_problem)
    }

    /// Rechecks the run's contracts in the slot, binds its generation, and runs it; a held run goes straight into its
    /// one automatic recovery episode.
    async fn start_run(
        &self,
        run: &LeasedRun<'_>,
        value: Run,
        progress: &Reporter,
    ) -> Result<&'static str, Problem> {
        // An Assistant changed since the claim never runs under a contract nobody pinned.
        let refused = match self.context_refusal(&run.team_id, &run.routine).await {
            Some(refused) => Some(refused),
            None => match self.bind_run(&run.team_id, &run.run_id, &run.lease).await {
                Ok(generation) => {
                    let bound = Run {
                        generation,
                        ..value
                    };
                    let outcome = self.execute(run, &bound, progress, None).await?;
                    if outcome != "held" {
                        return Ok(outcome);
                    }
                    return Ok(self.recover_run(run, progress).await);
                }
                Err(problem) if problem.code == Some("team-context-unavailable") => {
                    Some("team-context-unavailable")
                }
                Err(problem) => return Err(problem),
            },
        };
        let detail = object_of(vec![(
            "code",
            Value::String(refused.unwrap_or_default().to_owned()),
        )]);
        self.end_run(&run.team_id, &run.run_id, "failed", detail)
    }
}

fn claimed_view(team_id: &str, claimed: &record::Claim) -> Value {
    let text = |value: &str| Value::String(value.to_owned());
    object_of(vec![
        ("team_id", text(team_id)),
        ("run_id", text(&claimed.run.run_id)),
        ("lease_token", text(&claimed.lease_token)),
        ("revision", int(claimed.revision)),
        ("plan_digest", text(&claimed.plan_digest)),
        ("active_seconds", int(claimed.active_seconds)),
    ])
}

/// How one segment of a run ended, as its caller sees it.
#[must_use]
pub fn segment_answer(team_id: &str, run_id: &str, outcome: &str) -> Value {
    let text = |value: &str| Value::String(value.to_owned());
    object_of(vec![
        ("team_id", text(team_id)),
        ("run_id", text(run_id)),
        ("status", text(outcome)),
    ])
}
