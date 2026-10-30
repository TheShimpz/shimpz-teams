//! One drive session: the in-memory history, the lease, the buffer of events not yet committed, and every write.
//!
//! Events produced by pure work (a model's answer, a local computation, a timer that fired) are buffered and
//! committed with the next write that must happen anyway: the dispatch of the next effect, a park, or the end of the
//! run. An activity's result is committed at once. A crash therefore loses at most pure work, which is redone.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use serde_json::Value;

use crate::definition::{Interrupt, Wait};
use crate::error::{Error, LimitKind};
use crate::event::{Event, Outcome};
use crate::failpoint;
use crate::history::History;
use crate::host::{Authority, Credentials, Proof, RunInfo};
use crate::policy::Containment;
use crate::store::{Dispatch, End, Lease, NewObligation, Obligation, Write, Written};
use crate::worker::Shared;

pub(crate) struct Session {
    pub(crate) shared: Arc<Shared>,
    pub(crate) run_id: String,
    pub(crate) history: History,
    pub(crate) lease: Lease,
    pub(crate) cancel: Arc<AtomicBool>,
    buffer: Vec<Event>,
    blobs: Vec<(u64, Vec<u8>)>,
    obligations: Vec<NewObligation>,
    credentials: Option<Credentials>,
    last_write_ms: i64,
    /// Set when a write found that another session moved the run on; the drive starts over from the ledger.
    pub(crate) stale: bool,
}

impl Session {
    pub(crate) fn new(
        shared: Arc<Shared>,
        run_id: String,
        history: History,
        seen: u64,
        cancel: Arc<AtomicBool>,
    ) -> Self {
        let lease = Lease { session: shared.session_id(), incarnation: shared.incarnation.clone(), fence: None, seen };
        let now = shared.clock.now_ms();
        Self {
            shared,
            run_id,
            history,
            lease,
            cancel,
            buffer: vec![],
            blobs: vec![],
            obligations: vec![],
            credentials: None,
            last_write_ms: now,
            stale: false,
        }
    }

    pub(crate) fn now(&self) -> i64 {
        self.shared.clock.now_ms()
    }

    pub(crate) fn info(&self) -> RunInfo<'_> {
        let start = &self.history.start;
        RunInfo { id: &self.run_id, definition: &start.definition, version: start.version, input: &start.input }
    }

    /// Applies an event to the history and queues it for the next write.
    pub(crate) fn record(&mut self, event: Event) -> Result<(), Interrupt> {
        self.history.apply(&event)?;
        if matches!(event, Event::StepHeartbeat { .. }) {
            // Only the newest progress matters. One attempt runs at a time and every dispatch commits the buffer, so
            // any queued heartbeat is this attempt's: replace it rather than queue another. One too large to store
            // is dropped, never allowed to fail the attempt.
            self.buffer.retain(|queued| !matches!(queued, Event::StepHeartbeat { .. }));
            if event.encode(self.shared.store.event_bytes()).is_err() {
                return Ok(());
            }
        }
        self.buffer.push(event);
        Ok(())
    }

    pub(crate) fn add_blob(&mut self, id: u64, data: Vec<u8>) {
        self.blobs.push((id, data));
    }

    pub(crate) fn add_obligation(&mut self, step: u32, attempt: u32) {
        let fence = self.lease.fence.unwrap_or(0);
        self.obligations.push(NewObligation { fence, attempt: Some((step, attempt)) });
    }

    /// Refuses to start new work when the worker stops, the run was cancelled, or its deadline passed.
    pub(crate) fn gate(&self) -> Result<(), Interrupt> {
        if self.shared.stopping.load(Ordering::SeqCst) {
            return Err(Interrupt::Suspend(Wait::Shutdown));
        }
        if self.cancelled() {
            return Err(Interrupt::Cancelled);
        }
        if self.expired() {
            return Err(Interrupt::Expired);
        }
        Ok(())
    }

    pub(crate) fn cancelled(&self) -> bool {
        self.history.cancel || self.cancel.load(Ordering::SeqCst)
    }

    pub(crate) fn expired(&self) -> bool {
        self.history.start.deadline_ms.is_some_and(|deadline| self.now() >= deadline)
    }

    /// The host's credentials for this session, asked for once, before the session's first dispatch.
    pub(crate) fn authorize(&mut self) -> Result<Credentials, Interrupt> {
        if let Some(credentials) = &self.credentials {
            return Ok(credentials.clone());
        }
        match self.shared.host.authorize(&self.info()) {
            Authority::Granted(credentials) => {
                self.credentials = Some(credentials.clone());
                Ok(credentials)
            }
            Authority::Wait => Err(Interrupt::Suspend(Wait::Authority)),
            Authority::Revoked => Err(Interrupt::Unrecoverable("authority-revoked".into())),
        }
    }

    fn write(&mut self, dispatch: Option<Dispatch>, end: End<'_>, proven: &[i64]) -> Result<Written, Error> {
        let blobs: Vec<(u64, &[u8])> = self.blobs.iter().map(|(id, data)| (*id, data.as_slice())).collect();
        let now_ms = self.now();
        let write = Write {
            events: &self.buffer,
            dispatch,
            end,
            blobs: &blobs,
            obligations: &self.obligations,
            proven,
            now_ms,
            lease_ms: self.shared.options.lease_ms,
        };
        let written = self.shared.store.write(&self.run_id, &mut self.lease, &write)?;
        if written.stale {
            self.stale = true;
            return Err(Error::Busy(format!("run {} moved on; reloading it", self.run_id)));
        }
        self.last_write_ms = now_ms;
        if !written.refused_park {
            self.buffer.clear();
            self.blobs.clear();
            self.obligations.clear();
        }
        if let Some(started) = &written.dispatched {
            self.history.apply(started)?;
        }
        for event in &written.foreign {
            self.history.apply(event)?;
        }
        Ok(written)
    }

    /// Commits the buffer and keeps the lease.
    pub(crate) fn flush(&mut self) -> Result<(), Interrupt> {
        self.write(None, End::Hold, &[])?;
        Ok(())
    }

    /// Commits an effect's receipt (an activity's result): it gets the run end's bounded headroom past the history
    /// bounds, so what an executor did is never dropped. Each dispatch is checked without headroom, so at most one
    /// receipt ever lands past the bounds.
    pub(crate) fn flush_receipt(&mut self) -> Result<(), Interrupt> {
        self.write(None, End::Receipt, &[])?;
        Ok(())
    }

    /// Renews the lease when a third of it has passed (committing anything buffered).
    /// A heartbeat is expendable progress: one that does not fit the history is dropped so the lease still renews.
    pub(crate) fn renew_if_due(&mut self) -> Result<(), Interrupt> {
        let due = self.last_write_ms + i64::try_from(self.shared.options.lease_ms / 3).unwrap_or(i64::MAX);
        if self.lease.fence.is_none() || self.now() < due {
            return Ok(());
        }
        match self.write(None, End::Hold, &[]) {
            Err(Error::Limit(_)) => {
                self.buffer.retain(|queued| !matches!(queued, Event::StepHeartbeat { .. }));
                self.flush()
            }
            written => written.map(|_| ()).map_err(Interrupt::from),
        }
    }

    /// Reads whether another process cancelled the run.
    pub(crate) fn poll_cancel(&mut self) -> Result<bool, Interrupt> {
        let cancelled = self.shared.store.cancel_requested(&self.run_id)?;
        if cancelled {
            self.cancel.store(true, Ordering::SeqCst);
        }
        Ok(self.cancelled())
    }

    /// Commits the buffer together with the dispatch of `dispatch`, after every obligation the run carries is
    /// proven ended.
    pub(crate) fn dispatch(&mut self, dispatch: Dispatch) -> Result<u64, Interrupt> {
        let mut proven = Vec::new();
        loop {
            // Proofs and the host's other hooks take time: check the run's bounds again before every dispatch.
            self.gate()?;
            let written = self.write(Some(dispatch), End::Hold, &proven)?;
            if written.dispatched.is_some() {
                failpoint::hit("dispatch.committed");
                return Ok(self.lease.fence.unwrap_or(0));
            }
            // The write merged any cancellation committed since: the history knows it now.
            if self.cancelled() {
                return Err(Interrupt::Cancelled);
            }
            proven = self.prove(&written.obligations);
            if proven.len() < written.obligations.len() {
                self.write(None, End::Hold, &proven)?;
                let retry_ms = i64::try_from(self.shared.options.proof_retry_ms).unwrap_or(i64::MAX);
                return Err(Interrupt::Suspend(Wait::Executor(self.now().saturating_add(retry_ms))));
            }
        }
    }

    fn prove(&self, obligations: &[Obligation]) -> Vec<i64> {
        let info = self.info();
        obligations
            .iter()
            .filter(|obligation| match self.local_thread(obligation) {
                Some(ended) => ended,
                None => self.shared.host.prove_ended(&info, obligation) == Proof::Ended,
            })
            .map(|obligation| obligation.id)
            .collect()
    }

    /// For an in-thread attempt this worker abandoned, whether its thread returned; `None` for every obligation only
    /// the host can prove (an external executor, a whole session, or another worker's attempt).
    fn local_thread(&self, obligation: &Obligation) -> Option<bool> {
        let (step, attempt) = obligation.attempt?;
        let in_thread = self
            .history
            .steps
            .get(usize::try_from(step).unwrap_or(usize::MAX))
            .and_then(|record| self.shared.activities.get(&record.name))
            .is_some_and(|(_, options)| options.containment == Containment::InThread);
        (in_thread && obligation.incarnation == self.shared.incarnation)
            .then(|| !self.shared.is_abandoned(&self.run_id, step, attempt))
    }

    /// Waits until `until_ms` inside the drive when the wait is short; otherwise suspends the run until then.
    pub(crate) fn wait_until(&mut self, until_ms: i64) -> Result<(), Interrupt> {
        self.gate()?;
        let inline = i64::try_from(self.shared.options.inline_wait_ms).unwrap_or(i64::MAX);
        if until_ms - self.now() > inline {
            return Err(Interrupt::Suspend(Wait::Until(until_ms)));
        }
        let poll = self.shared.options.poll_ms.max(1);
        loop {
            self.gate()?;
            let left = until_ms - self.now();
            if left <= 0 {
                return Ok(());
            }
            self.shared.clock.sleep(Duration::from_millis(u64::try_from(left).unwrap_or(0).min(poll)));
            self.poll_cancel()?;
            self.renew_if_due()?;
        }
    }

    /// Asks the host to end or hand off the session's executors; a failure leaves an obligation for the whole session.
    fn release(&mut self) {
        let Some(fence) = self.lease.fence else {
            return;
        };
        if self.shared.host.release(&self.info(), fence).is_err() {
            self.obligations.push(NewObligation { fence, attempt: None });
        }
    }

    /// Releases the run until it may continue; `false` when a host input arrived after the decision to wait, so the
    /// definition must run again on the newer history.
    pub(crate) fn park(&mut self, wait: Wait) -> Result<bool, Error> {
        let wake_ms = match wait {
            Wait::Deferred | Wait::Authority => None,
            Wait::Until(at) | Wait::Executor(at) => Some(at),
            Wait::Shutdown => Some(self.now()),
        };
        let deadline = self.history.start.deadline_ms;
        let wake_ms = match (wake_ms, deadline) {
            (Some(wake), Some(deadline)) => Some(wake.min(deadline)),
            (wake, deadline) => wake.or(deadline),
        };
        failpoint::hit("park.before");
        // A failed release still parks: the obligation it leaves makes the next session prove the executors ended.
        self.release();
        let written = self.write(None, End::Park { wake_ms }, &[])?;
        failpoint::hit("park.committed");
        Ok(!written.refused_park)
    }

    /// Ends the run with `outcome`; a cancellation committed before the end wins, keeping `progress`. A run whose
    /// end does not fit a bound ends `limited` without its buffered pure results and blobs.
    pub(crate) fn finish(&mut self, outcome: Outcome, progress: &Value) -> Result<Outcome, Error> {
        self.release();
        failpoint::hit("finish.before");
        let mut written = self.write(None, End::Finish { outcome: &outcome, cancelled: progress }, &[]);
        if let Err(Error::Limit(kind)) = written {
            // The buffer holds only pure work (an effect's receipt commits on its own, with headroom): drop it and the
            // transient blobs, keep the obligations. A full history ends the run `limited(history)`; any other bound
            // is retried as is, then without the outcome's payload.
            self.buffer.clear();
            self.blobs.clear();
            let retried = if kind == LimitKind::History {
                Outcome::Limited { kind, progress: progress.clone() }
            } else {
                outcome.clone()
            };
            written = self.write(None, End::Finish { outcome: &retried, cancelled: progress }, &[]);
            if let Err(Error::Limit(kind)) = written {
                let bare = stripped(&retried, kind);
                written = self.write(None, End::Finish { outcome: &bare, cancelled: &Value::Null }, &[]);
            }
        }
        let written = written?;
        let ended = written.finished.unwrap_or(outcome);
        self.history.apply(&Event::RunCompleted { outcome: ended.clone() })?;
        failpoint::hit("finish.committed");
        Ok(ended)
    }

    pub(crate) fn committed_spend(&self) -> u64 {
        self.history.committed_spend()
    }

    /// The reservation of an attempt: refused past the run's spend ceiling, or when the model cannot estimate its
    /// cost and the run has a ceiling.
    pub(crate) fn reserve(&self, reserve: Option<u64>) -> Result<u64, Interrupt> {
        let ceiling = self.history.start.spend_ceiling;
        match (reserve, ceiling) {
            (None, Some(_)) => Err(Interrupt::Limited(LimitKind::Spend)),
            (None, None) => Ok(0),
            (Some(reserve), Some(ceiling)) if self.committed_spend().saturating_add(reserve) > ceiling => {
                Err(Interrupt::Limited(LimitKind::Spend))
            }
            (Some(reserve), _) => Ok(reserve),
        }
    }
}

/// `outcome` without its payload, for a run whose end does not fit: a result or a request too large becomes
/// `limited(kind)`.
fn stripped(outcome: &Outcome, kind: LimitKind) -> Outcome {
    match outcome {
        Outcome::Done { .. } | Outcome::NeedsPerson { .. } => Outcome::Limited { kind, progress: Value::Null },
        Outcome::Limited { kind, .. } => Outcome::Limited { kind: *kind, progress: Value::Null },
        Outcome::Expired { .. } => Outcome::Expired { progress: Value::Null },
        Outcome::Cancelled { .. } => Outcome::Cancelled { progress: Value::Null },
        Outcome::Unrecoverable { reason } => Outcome::Unrecoverable { reason: reason.clone() },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_outcome_without_its_payload_keeps_its_kind() {
        let progress = Value::from(1);
        let limited = Outcome::Limited { kind: LimitKind::Value, progress: Value::Null };
        assert_eq!(stripped(&Outcome::Done { result: progress.clone() }, LimitKind::Value), limited);
        assert_eq!(stripped(&Outcome::NeedsPerson { request: progress.clone() }, LimitKind::Value), limited);
        let steps = Outcome::Limited { kind: LimitKind::Steps, progress: progress.clone() };
        assert_eq!(
            stripped(&steps, LimitKind::Value),
            Outcome::Limited { kind: LimitKind::Steps, progress: Value::Null }
        );
        assert_eq!(
            stripped(&Outcome::Expired { progress: progress.clone() }, LimitKind::Value),
            Outcome::Expired { progress: Value::Null }
        );
        assert_eq!(
            stripped(&Outcome::Cancelled { progress }, LimitKind::Value),
            Outcome::Cancelled { progress: Value::Null }
        );
        let lost = Outcome::Unrecoverable { reason: "r".into() };
        assert_eq!(stripped(&lost, LimitKind::Value), lost);
    }
}
