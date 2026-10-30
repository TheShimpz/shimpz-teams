//! Running one attempt on its own thread, and waiting for it without ever trusting that an abandoned attempt
//! stopped.
//!
//! The driving thread waits on a channel with a timeout. While it waits it renews the lease, journals heartbeats,
//! and watches the attempt's deadline, its heartbeat timeout, the run's deadline, and cancellation. On any of those
//! it abandons the attempt: the attempt's [`Attempt::check`] starts refusing, and the attempt gets a grace period to
//! return. An attempt still running after the grace is recorded in the worker's registry of abandoned attempts (and,
//! by the caller, as an obligation); its late result is discarded.

use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{Receiver, RecvTimeoutError, sync_channel};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::Value;

use crate::definition::Interrupt;
use crate::event::Event;
use crate::failpoint;
use crate::handler::Attempt;
use crate::policy::Failure;
use crate::session::Session;
use crate::worker::Abandoned;

/// What the attempt's thread sends back: its result, and whether its handler panicked.
type Done<R> = (Result<R, Failure>, bool);

/// Why the session gave up on an attempt.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Why {
    Timeout,
    Heartbeat,
    Cancelled,
    Expired,
    Lost(Interrupt),
}

/// How an attempt ended.
pub(crate) enum Ended<R> {
    /// The attempt returned; `panicked` when its handler panicked.
    Returned { result: Result<R, Failure>, panicked: bool },
    /// The session gave up; `returned` holds what the attempt returned within the grace period, if anything.
    Abandoned { why: Why, returned: Option<(Result<R, Failure>, bool)> },
}

/// What an attempt is bounded by.
pub(crate) struct Bounds {
    pub(crate) deadline_ms: Option<i64>,
    pub(crate) heartbeat_ms: Option<u64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Slot {
    Running,
    Finished,
    Orphaned,
}

type Work<R> = Box<dyn FnOnce(&Attempt) -> Result<R, Failure> + Send>;

/// Runs `work` as attempt `attempt` of `step` and waits for it.
pub(crate) fn run<R: Send + 'static>(
    session: &mut Session,
    attempt: Attempt,
    bounds: &Bounds,
    work: Work<R>,
) -> Ended<R> {
    let (sender, receiver) = sync_channel::<Done<R>>(1);
    let (beats, beat_receiver) = sync_channel::<Value>(1);
    let abandoned = Arc::new(AtomicBool::new(false));
    let attempt = Attempt { abandoned: Arc::clone(&abandoned), beats, ..attempt };
    let (step, number) = (attempt.step, attempt.attempt);
    let slot = Arc::new(Mutex::new(Slot::Running));
    let registry = Arc::clone(&session.shared.abandoned);
    let key = (session.run_id.clone(), step, number);
    let thread_slot = Arc::clone(&slot);
    let clock = Arc::clone(&session.shared.clock);
    let spawned = std::thread::Builder::new().name(format!("holdpoint-attempt-{step}-{number}")).spawn(move || {
        failpoint::hit("attempt.running");
        let late = attempt.deadline_ms.is_some_and(|deadline| clock.now_ms() >= deadline);
        let outcome = if late {
            Ok(Err(Failure::permanent("not-started", "the attempt's deadline passed before it started").with_ended()))
        } else {
            catch_unwind(AssertUnwindSafe(|| work(&attempt)))
        };
        let (result, panicked) = match outcome {
            Ok(result) => (result, false),
            Err(_) => (Err(Failure::permanent("panic", "the handler panicked")), true),
        };
        let _ = sender.send((result, panicked));
        let mut state = thread_slot.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
        if *state == Slot::Orphaned {
            registry.remove(&key);
        }
        *state = Slot::Finished;
    });
    if spawned.is_err() {
        return no_thread();
    }
    let ended = wait(session, &receiver, &beat_receiver, step, number, bounds);
    match ended {
        Ok(ended) => ended,
        Err(why) => abandon(session, &receiver, &abandoned, &slot, step, number, why),
    }
}

fn wait<R>(
    session: &mut Session,
    receiver: &Receiver<Done<R>>,
    beats: &Receiver<Value>,
    step: u32,
    attempt: u32,
    bounds: &Bounds,
) -> Result<Ended<R>, Why> {
    let poll = session.shared.options.poll_ms.max(1);
    let mut last_beat = session.now();
    loop {
        if let Ok(progress) = beats.try_recv() {
            last_beat = session.now();
            session.record(Event::StepHeartbeat { step, attempt, progress }).map_err(Why::Lost)?;
        }
        check(session, bounds, last_beat)?;
        match receiver.recv_timeout(Duration::from_millis(poll)) {
            Ok(returned) => return Ok(settled(session, returned)),
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => return Ok(vanished()),
        }
    }
}

/// The system refused a thread for the attempt (resource exhaustion): a retryable failure, nothing dispatched.
#[cfg_attr(coverage_nightly, coverage(off))]
fn no_thread<R>() -> Ended<R> {
    Ended::Returned { result: Err(Failure::retryable("spawn", "no thread for the attempt")), panicked: false }
}

/// The attempt's thread ended without sending a result, which only a killed thread can do: it may have acted.
#[cfg_attr(coverage_nightly, coverage(off))]
fn vanished<R>() -> Ended<R> {
    let result = Err(Failure::permanent("panic", "the attempt's thread ended without a result"));
    Ended::Returned { result, panicked: true }
}

/// A result received after the run's deadline passed, or after it was cancelled, is settled as such: the step
/// records it, and the run still ends expired or cancelled.
fn settled<R>(session: &Session, (result, panicked): Done<R>) -> Ended<R> {
    let late = if session.expired() {
        Some(Why::Expired)
    } else if session.cancelled() {
        Some(Why::Cancelled)
    } else {
        None
    };
    match late {
        Some(why) => Ended::Abandoned { why, returned: Some((result, panicked)) },
        None => Ended::Returned { result, panicked },
    }
}

fn check(session: &mut Session, bounds: &Bounds, last_beat: i64) -> Result<(), Why> {
    let now = session.now();
    if session.poll_cancel().map_err(Why::Lost)? {
        return Err(Why::Cancelled);
    }
    if session.expired() {
        return Err(Why::Expired);
    }
    if bounds.deadline_ms.is_some_and(|deadline| now >= deadline) {
        return Err(Why::Timeout);
    }
    let heartbeat = bounds.heartbeat_ms.map(|ms| i64::try_from(ms).unwrap_or(i64::MAX));
    if heartbeat.is_some_and(|ms| now.saturating_sub(last_beat) >= ms) {
        return Err(Why::Heartbeat);
    }
    session.renew_if_due().map_err(Why::Lost)
}

fn abandon<R>(
    session: &Session,
    receiver: &Receiver<Done<R>>,
    abandoned: &AtomicBool,
    slot: &Mutex<Slot>,
    step: u32,
    attempt: u32,
    why: Why,
) -> Ended<R> {
    abandoned.store(true, Ordering::SeqCst);
    let deadline = std::time::Instant::now() + Duration::from_millis(session.shared.options.abandon_grace_ms);
    loop {
        let mut state = slot.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
        // The thread sends its result before it marks itself finished: under the lock, a result is either queued,
        // or the thread is still running and will see that it was orphaned.
        if let Ok(returned) = receiver.try_recv() {
            return Ended::Abandoned { why, returned: Some(returned) };
        }
        if std::time::Instant::now() >= deadline {
            *state = Slot::Orphaned;
            session.shared.abandoned.insert((session.run_id.clone(), step, attempt));
            return Ended::Abandoned { why, returned: None };
        }
        drop(state);
        std::thread::sleep(Duration::from_millis(1));
    }
}

impl Abandoned {
    fn remove(&self, key: &(String, u32, u32)) {
        self.0.lock().unwrap_or_else(std::sync::PoisonError::into_inner).remove(key);
    }

    fn insert(&self, key: (String, u32, u32)) {
        self.0.lock().unwrap_or_else(std::sync::PoisonError::into_inner).insert(key);
    }
}
