//! The background scheduler: resumes every open run that is due and not held by a live lease, on a bounded number
//! of threads, until the worker stops.

use std::sync::atomic::Ordering;
use std::sync::{Arc, Condvar, Mutex, PoisonError};
use std::thread::JoinHandle;
use std::time::Duration;

use crate::error::Error;
use crate::worker::Worker;

/// The longest the scheduler sleeps without looking at the ledger again (another process may have woken a run).
const IDLE_MS: u64 = 500;

pub(crate) struct Scheduler {
    signal: Arc<(Mutex<bool>, Condvar)>,
    thread: JoinHandle<()>,
}

impl Scheduler {
    pub(crate) fn notify(&self) {
        let (lock, condvar) = &*self.signal;
        *lock.lock().unwrap_or_else(PoisonError::into_inner) = true;
        condvar.notify_all();
    }
}

/// Spawns the scheduler's thread; the system may refuse it (resource exhaustion), which cannot happen in a test.
#[cfg_attr(coverage_nightly, coverage(off))]
fn spawn_scheduler(work: impl FnOnce() + Send + 'static) -> Result<JoinHandle<()>, Error> {
    std::thread::Builder::new()
        .name("holdpoint-scheduler".into())
        .spawn(work)
        .map_err(|error| Error::State(format!("cannot start the scheduler: {error}")))
}

impl Worker {
    /// Starts resuming due runs in the background: every open run whose lease is free or expired is driven again,
    /// so a run interrupted by a crash continues as soon as its host process starts. Idempotent.
    ///
    /// # Errors
    ///
    /// [`Error::State`] when the scheduler thread cannot be spawned.
    pub fn start(self: &Arc<Self>) -> Result<(), Error> {
        let mut scheduler = self.scheduler.lock().unwrap_or_else(PoisonError::into_inner);
        if scheduler.is_some() {
            return Ok(());
        }
        self.shared.stopping.store(false, Ordering::SeqCst);
        let signal = Arc::new((Mutex::new(true), Condvar::new()));
        let worker = Arc::clone(self);
        let thread_signal = Arc::clone(&signal);
        let thread = spawn_scheduler(move || worker.schedule(&thread_signal))?;
        *scheduler = Some(Scheduler { signal, thread });
        Ok(())
    }

    /// Stops the scheduler: no new attempt is dispatched, runs being driven park at their next step, and this call
    /// returns once every scheduler thread ended.
    pub fn stop(&self) {
        self.shared.stopping.store(true, Ordering::SeqCst);
        let scheduler = self.scheduler.lock().unwrap_or_else(PoisonError::into_inner).take();
        if let Some(scheduler) = scheduler {
            scheduler.notify();
            let _ = scheduler.thread.join();
        }
    }

    fn schedule(self: Arc<Self>, signal: &(Mutex<bool>, Condvar)) {
        let mut drivers: Vec<JoinHandle<()>> = Vec::new();
        while !self.shared.stopping.load(Ordering::SeqCst) {
            drivers.retain(|driver| !driver.is_finished());
            let free = self.shared.options.concurrency.saturating_sub(drivers.len());
            let due = self.shared.store.due(self.shared.clock.now_ms(), free).unwrap_or_default();
            for run_id in due {
                let worker = Arc::clone(&self);
                let spawned = std::thread::Builder::new().name(format!("holdpoint-drive-{run_id}")).spawn(move || {
                    // A run another session holds, or one that fails here, is picked up again when it is next due.
                    let _ = worker.drive(&run_id);
                });
                // A driver the system refused a thread for is simply due again at the next look.
                drivers.extend(spawned.ok());
            }
            self.sleep(signal, &drivers);
        }
        for driver in drivers {
            let _ = driver.join();
        }
    }

    fn sleep(&self, signal: &(Mutex<bool>, Condvar), drivers: &[JoinHandle<()>]) {
        let now = self.shared.clock.now_ms();
        let next = self.shared.store.next_wake().ok().flatten();
        let until_next = next.map_or(IDLE_MS, |at| u64::try_from(at.saturating_sub(now)).unwrap_or(0));
        let busy = drivers.len() >= self.shared.options.concurrency;
        let wait = if busy { self.shared.options.poll_ms } else { until_next.min(IDLE_MS) };
        let (lock, condvar) = signal;
        let mut woken = lock.lock().unwrap_or_else(PoisonError::into_inner);
        if !*woken {
            woken = condvar
                .wait_timeout(woken, Duration::from_millis(wait.max(1)))
                .unwrap_or_else(PoisonError::into_inner)
                .0;
        }
        *woken = false;
    }
}
