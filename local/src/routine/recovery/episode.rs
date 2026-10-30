//! The held run's one automatic recovery episode (`recovery.automatic`), right after its hold in the same execution
//! slot: sealed open in the run's cursor first, so a restart never opens another; registered with a deadline that
//! cancels it at once, so Stop, deletion, and the watchdog reach it. Verification comes first; a proven occurrence or
//! absence continues the run once within the episode's time. A person's Stop outranks everything: it publishes nothing.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use team_chat::progress::Reporter;
use team_routine::claim::generation_for;
use team_routine::cursor;
use team_routine::hold::{self, PauseReason};
use team_routine::record::{self, HUMAN_LEASE};
use tokio::time::Instant;

use super::blocked;
use crate::chat::problem;
use crate::http::service::Problem;
use crate::routine::run::LeasedRun;
use crate::routine::service::RoutineService;

/// The most active time the run's one recovery episode may use, within the run's own time left.
pub const RECOVERY_SECONDS: i64 = 60;

/// The run's one recovery episode in this execution: its time, from when, and how it ended.
struct Episode {
    seconds: i64,
    started: Instant,
    /// Set by the deadline timer when the episode's time ran out, whatever the episode was doing.
    expired: Arc<AtomicBool>,
    /// Whether the episode already published why it paused the Routine.
    published: bool,
}

impl Episode {
    fn deadline(&self) -> Instant {
        self.started + Duration::from_secs(u64::try_from(self.seconds).unwrap_or(0))
    }

    fn expired(&self) -> bool {
        self.expired.load(Ordering::SeqCst) || Instant::now() >= self.deadline()
    }
}

/// What pauses the Routine when the episode ends on it.
fn pause_reason(verdict: &str) -> Option<PauseReason> {
    match verdict {
        "policy" => Some(PauseReason::Policy),
        "exhausted" => Some(PauseReason::Exhausted),
        "unclassified" => Some(PauseReason::Evidence),
        _ => None,
    }
}

/// A timer that cancels the episode at its absolute deadline, whatever it is doing; aborted when dropped.
struct Deadline(tokio::task::JoinHandle<()>);

impl Drop for Deadline {
    fn drop(&mut self) {
        self.0.abort();
    }
}

impl RoutineService {
    /// Opens the run's one episode durably, before any work; `None` when none may open. Its time is the smaller of
    /// the recovery bound and the held run's remaining active time.
    fn open_episode(&self, team_id: &str, incident_id: &str) -> Option<Episode> {
        let held = self.open_recovery(team_id, incident_id).ok()?.cursor;
        let state = self.load(team_id).ok()?;
        let left = hold::incident(&state, incident_id)
            .ok()?
            .active_seconds_left;
        let opened = cursor::open_episode(&held).ok()?;
        self.parts
            .store
            .lock(team_id)
            .and_then(|lock| lock.put_cursor(&opened))
            .ok()?;
        Some(Episode {
            seconds: RECOVERY_SECONDS.min(left).max(0),
            started: Instant::now(),
            expired: Arc::new(AtomicBool::new(false)),
            published: false,
        })
    }

    fn deadline(&self, run: &LeasedRun<'_>, episode: &Episode) -> Deadline {
        let (service, team_id, run_id) = (self.clone(), run.team_id.clone(), run.run_id.clone());
        let (token, expired, at) = (
            run.token().to_owned(),
            Arc::clone(&episode.expired),
            episode.deadline(),
        );
        Deadline(tokio::spawn(async move {
            tokio::time::sleep_until(at).await;
            // Bound to this execution's token: a late timer never stops a later recovery of the same incident.
            let mark = || expired.store(true, Ordering::SeqCst);
            service.expire_routine_run(&team_id, &run_id, &token, &mark);
        }))
    }

    /// Whether a person stopped the recovery: it was cancelled, and not by its own deadline.
    fn stopped(run: &LeasedRun<'_>, episode: &Episode) -> bool {
        run.cancelled() && !episode.expired.load(Ordering::SeqCst)
    }

    /// Pauses the Routine the held run belongs to and says why, once per episode, unless a person stopped it first.
    fn publish(
        &self,
        run: &LeasedRun<'_>,
        episode: &mut Episode,
        reason: PauseReason,
    ) -> Result<(), Problem> {
        let expired = Arc::clone(&episode.expired);
        let paused = self.unstopped(
            run.stop(),
            || expired.load(Ordering::SeqCst),
            || self.pause_incident(&run.team_id, &run.run_id, reason),
        );
        if let Some(paused) = paused {
            paused?;
            episode.published = true;
        }
        Ok(())
    }

    /// Verifies first; `true` when the run may go on. Exhaustion is judged on the episode's own clock.
    async fn verified(&self, run: &LeasedRun<'_>, episode: &mut Episode) -> Result<bool, Problem> {
        let verdict = if episode.expired() {
            Ok("exhausted")
        } else {
            self.verify(run).await
        };
        // Verification's whole seconds leave the run's balance, as a segment's do, even when it failed.
        let spent = i64::try_from(episode.started.elapsed().as_secs()).unwrap_or(i64::MAX);
        let incident_id = run.run_id.clone();
        self.update(&run.team_id, |state| {
            (
                hold::spend_incident(&state, &incident_id, spent).unwrap_or(state),
                (),
            )
        })?;
        let mut verdict = verdict?;
        if Self::stopped(run, episode) {
            return Ok(false);
        }
        if episode.expired() {
            verdict = "exhausted";
        }
        if let Some(reason) = pause_reason(verdict) {
            self.publish(run, episode, reason)?;
        }
        Ok(matches!(verdict, "occurred" | "none" | "absent") && !run.cancelled())
    }

    /// Continues the already-authorized run inside what is left of the episode, or holds it.
    async fn go_on(
        &self,
        run: &LeasedRun<'_>,
        episode: &mut Episode,
        progress: &Reporter,
    ) -> Result<&'static str, Problem> {
        let left = episode
            .deadline()
            .saturating_duration_since(Instant::now())
            .as_secs();
        if Self::stopped(run, episode) {
            return Ok("held");
        }
        if left == 0 {
            self.publish(run, episode, PauseReason::Exhausted)?;
            return Ok("held");
        }
        self.continue_run(run, progress, i64::try_from(left).unwrap_or(i64::MAX))
            .await
    }

    /// Resumes a held run as its one continuation, in its next generation under a fresh internal lease; `held` when
    /// it cannot. Only a completed step or a proven absence continues.
    async fn continue_run(
        &self,
        run: &LeasedRun<'_>,
        progress: &Reporter,
        seconds: i64,
    ) -> Result<&'static str, Problem> {
        let (team_id, incident_id) = (&run.team_id, &run.run_id);
        let opened = self.open_recovery(team_id, incident_id)?;
        if blocked(&opened.cursor) {
            return Ok("held");
        }
        let Ok(continued) = cursor::continued(&opened.cursor) else {
            return Ok("held");
        };
        self.parts
            .store
            .lock(team_id)
            .and_then(|lock| lock.put_cursor(&continued))
            .map_err(|_| problem::routine_state_unavailable())?;
        let incarnation = &opened.recovery.binding.incarnation;
        let generation = generation_for(incarnation, incident_id, continued.generation_suffix());
        let routine_id = opened.recovery.binding.routine_id.clone();
        // A Stop, deletion, or deadline that reached the recovery is final: the reopening commits under its guard.
        let reopened = self.unstopped(
            run.stop(),
            || false,
            || self.reopen(team_id, incident_id, &generation, &routine_id),
        );
        let Some(Some((value, routine, token))) = reopened.transpose()? else {
            return Ok("held");
        };
        // The resolved hold's evidence goes; a later hold of this continuation seals its own.
        let _removed = self
            .parts
            .store
            .lock(team_id)
            .and_then(|lock| lock.delete_incident(incident_id));
        let continuation = LeasedRun {
            team_id: team_id.clone(),
            run_id: incident_id.clone(),
            lease: record::lease_of(&token, HUMAN_LEASE),
            execution: run.execution,
            routine,
            transcripts: Vec::new(),
            requests_used: usize::try_from(value.requests_used).unwrap_or(0),
            protected: Arc::clone(&run.protected),
        };
        let left = seconds.min(value.active_seconds_left);
        let _registered = self.register((team_id, incident_id), run.execution, left)?;
        self.execute(&continuation, &value, progress, None).await
    }

    fn reopen(
        &self,
        team_id: &str,
        incident_id: &str,
        generation: &str,
        routine_id: &str,
    ) -> Result<Option<(record::Run, record::Routine, String)>, Problem> {
        let now = self.now();
        let entropy = self.parts.entropy.clone();
        self.update(team_id, |state| {
            let Ok((reopened, token)) =
                hold::reopen_incident(&state, incident_id, now, generation, entropy.as_ref())
            else {
                return (state, None);
            };
            let found = record::run(&reopened, incident_id).cloned().ok();
            let routine = record::routine(&reopened, routine_id).cloned().ok();
            match found.zip(routine) {
                Some((found, routine)) => (reopened, Some((found, routine, token))),
                None => (state, None),
            }
        })
    }

    /// The run's one automatic recovery episode, right after its hold, in the same execution slot (ADR-0092).
    pub(crate) async fn recover_run(
        &self,
        run: &LeasedRun<'_>,
        progress: &Reporter,
    ) -> &'static str {
        // A person who stopped the run before its episode opened: nothing is opened or published.
        if run.cancelled() {
            return "held";
        }
        let Some(mut episode) = self.open_episode(&run.team_id, &run.run_id) else {
            return "held";
        };
        if episode.seconds == 0 {
            // The run has no active time left: the episode is exhausted before it starts.
            let _published = self.publish(run, &mut episode, PauseReason::Exhausted);
            return "held";
        }
        let Ok(_registered) =
            self.register((&run.team_id, &run.run_id), run.execution, episode.seconds)
        else {
            return "held";
        };
        let _deadline = self.deadline(run, &episode);
        let outcome = match self.verified(run, &mut episode).await {
            Ok(true) => self
                .go_on(run, &mut episode, progress)
                .await
                .unwrap_or("held"),
            Ok(false) | Err(_) => "held",
        };
        // A held run its deadline cut, however it got there, pauses the Routine as exhausted; its evidence stays.
        if outcome == "held" && episode.expired.load(Ordering::SeqCst) && !episode.published {
            let _published = self.publish(run, &mut episode, PauseReason::Exhausted);
        }
        outcome
    }
}
