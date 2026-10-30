//! In-memory fakes of this crate's seams, for tests (feature `testing`). Never compiled into a shipped binary.

use std::collections::VecDeque;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{Duration, SystemTime};

use tokio::io::DuplexStream;

use crate::audit::{AuditEvent, AuditSink, AuditUnavailable};
use crate::clock::Clock;
use crate::entropy::{Entropy, EntropyUnavailable};
use crate::exec::{ExecEngine, ExecError, ExecExit, ExecId, ExecSession, ExecSpec, FailStop};
use crate::future::BoxFuture;

/// A wall clock that moves only when a test moves it.
#[derive(Debug)]
pub struct FakeClock(Mutex<SystemTime>);

impl FakeClock {
    /// A clock showing `now`.
    #[must_use]
    pub fn at(now: SystemTime) -> Self {
        Self(Mutex::new(now))
    }

    /// Moves the clock forward by `by`.
    ///
    /// # Panics
    /// When another holder of the clock panicked.
    pub fn advance(&self, by: Duration) {
        let mut now = self.0.lock().expect("the fake clock is usable");
        *now += by;
    }
}

impl Clock for FakeClock {
    fn now(&self) -> SystemTime {
        *self.0.lock().expect("the fake clock is usable")
    }
}

/// Deterministic bytes: each fill writes a counter's bytes, so every nonce and token of a test is reproducible.
#[derive(Debug, Default)]
pub struct CountingEntropy(AtomicU64);

impl Entropy for CountingEntropy {
    fn fill(&self, bytes: &mut [u8]) -> Result<(), EntropyUnavailable> {
        let mut counter = self.0.fetch_add(1, Ordering::Relaxed);
        for byte in bytes.iter_mut() {
            *byte = counter.to_le_bytes()[0];
            counter = counter.rotate_right(8) ^ 0x9e37_79b9_7f4a_7c15;
        }
        Ok(())
    }
}

/// An audit sink that keeps every event in memory and can be made to fail.
#[derive(Debug, Default)]
pub struct MemoryAudit {
    events: Mutex<Vec<AuditEvent>>,
    failing: AtomicBool,
}

impl MemoryAudit {
    /// From now on every record fails (`true`) or succeeds (`false`).
    pub fn fail(&self, failing: bool) {
        self.failing.store(failing, Ordering::SeqCst);
    }

    /// Every event recorded so far.
    ///
    /// # Panics
    /// When a recorder panicked.
    #[must_use]
    pub fn events(&self) -> Vec<AuditEvent> {
        self.events
            .lock()
            .expect("the audit fake is usable")
            .clone()
    }
}

impl AuditSink for MemoryAudit {
    fn record(&self, event: AuditEvent) -> BoxFuture<'_, Result<(), AuditUnavailable>> {
        Box::pin(async move {
            if self.failing.load(Ordering::SeqCst) {
                return Err(AuditUnavailable);
            }
            self.events
                .lock()
                .map_err(|_| AuditUnavailable)?
                .push(event);
            Ok(())
        })
    }
}

/// One exec a test scripted: the stream Team receives and the state Team then reads.
#[derive(Debug)]
pub struct ScriptedExec {
    /// Team's end of the exec stream; the test drives the other end of the duplex pair.
    pub stream: DuplexStream,
    /// What [`ExecEngine::inspect_exec`] answers for it.
    pub exit: ExecExit,
}

/// An exec engine that answers scripted execs in order and records every request.
#[derive(Debug, Default)]
pub struct FakeExec {
    scripted: Mutex<VecDeque<ScriptedExec>>,
    exits: Mutex<Vec<(String, ExecExit)>>,
    started: Mutex<Vec<ExecSpec>>,
    stopped: Mutex<Vec<String>>,
    blocked: AtomicBool,
}

impl FakeExec {
    /// Queues the next exec's stream and exit state.
    ///
    /// # Panics
    /// When a user of the fake panicked.
    pub fn push(&self, exec: ScriptedExec) {
        self.scripted
            .lock()
            .expect("the exec fake is usable")
            .push_back(exec);
    }

    /// Makes every later fail-stop answer [`FailStop::Blocked`].
    pub fn block_fail_stop(&self) {
        self.blocked.store(true, Ordering::SeqCst);
    }

    /// Every exec requested so far.
    ///
    /// # Panics
    /// When a user of the fake panicked.
    #[must_use]
    pub fn started(&self) -> Vec<ExecSpec> {
        self.started
            .lock()
            .expect("the exec fake is usable")
            .clone()
    }

    /// Every container fail-stopped so far.
    ///
    /// # Panics
    /// When a user of the fake panicked.
    #[must_use]
    pub fn stopped(&self) -> Vec<String> {
        self.stopped
            .lock()
            .expect("the exec fake is usable")
            .clone()
    }
}

impl ExecEngine for FakeExec {
    fn start_exec<'a>(
        &'a self,
        spec: &'a ExecSpec,
    ) -> BoxFuture<'a, Result<ExecSession, ExecError>> {
        Box::pin(async move {
            self.started
                .lock()
                .map_err(|_| ExecError::Unavailable)?
                .push(spec.clone());
            let next = self
                .scripted
                .lock()
                .map_err(|_| ExecError::Unavailable)?
                .pop_front();
            let scripted = next.ok_or(ExecError::Unavailable)?;
            let mut exits = self.exits.lock().map_err(|_| ExecError::Unavailable)?;
            let id = format!("exec-{}", exits.len());
            exits.push((id.clone(), scripted.exit));
            Ok(ExecSession {
                id: ExecId(id),
                stream: Box::new(scripted.stream),
            })
        })
    }

    fn inspect_exec<'a>(&'a self, id: &'a ExecId) -> BoxFuture<'a, Result<ExecExit, ExecError>> {
        Box::pin(async move {
            let exits = self.exits.lock().map_err(|_| ExecError::Unavailable)?;
            exits
                .iter()
                .find(|(known, _)| *known == id.0)
                .map(|(_, exit)| *exit)
                .ok_or(ExecError::NotFound)
        })
    }

    fn fail_stop<'a>(&'a self, container: &'a str) -> BoxFuture<'a, Result<FailStop, ExecError>> {
        Box::pin(async move {
            self.stopped
                .lock()
                .map_err(|_| ExecError::Unavailable)?
                .push(container.to_owned());
            Ok(if self.blocked.load(Ordering::SeqCst) {
                FailStop::Blocked
            } else {
                FailStop::Stopped
            })
        })
    }
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, UNIX_EPOCH};

    use tokio::io::{AsyncReadExt, AsyncWriteExt, duplex};

    use super::{CountingEntropy, FakeClock, FakeExec, MemoryAudit, ScriptedExec};
    use crate::audit::{AuditEvent, AuditSink, AuditUnavailable};
    use crate::clock::Clock;
    use crate::entropy::token_hex;
    use crate::exec::{ExecEngine, ExecExit, ExecSpec, FailStop};

    #[test]
    fn the_fake_clock_moves_only_when_advanced() {
        let clock = FakeClock::at(UNIX_EPOCH);
        clock.advance(Duration::from_secs(5));
        assert_eq!(clock.now(), UNIX_EPOCH + Duration::from_secs(5));
    }

    #[test]
    fn counting_entropy_is_reproducible_and_differs_per_fill() {
        let first = (
            token_hex(&CountingEntropy::default(), 16),
            token_hex(&CountingEntropy::default(), 16),
        );
        assert_eq!(first.0, first.1);
        let entropy = CountingEntropy::default();
        assert_ne!(token_hex(&entropy, 16), token_hex(&entropy, 16));
    }

    #[tokio::test]
    async fn a_failing_audit_records_nothing() {
        let audit = MemoryAudit::default();
        audit.fail(true);
        assert_eq!(audit.record(AuditEvent::new()).await, Err(AuditUnavailable));
        audit.fail(false);
        assert_eq!(audit.record(AuditEvent::new()).await, Ok(()));
        assert_eq!(audit.events().len(), 1);
    }

    #[tokio::test]
    async fn a_scripted_exec_carries_bytes_both_ways() {
        let engine = FakeExec::default();
        let (team_end, mut workload_end) = duplex(64);
        let exit = ExecExit {
            running: false,
            exit_code: Some(0),
        };
        engine.push(ScriptedExec {
            stream: team_end,
            exit,
        });
        let spec = ExecSpec {
            container: "c".to_owned(),
            argv: vec!["/usr/local/bin/shimpz-action".to_owned()],
            user: "10001:10001".to_owned(),
            working_dir: None,
            env: Vec::new(),
            stdin: true,
        };
        let mut session = engine.start_exec(&spec).await.expect("scripted");
        session.stream.write_all(b"request").await.expect("written");
        session.stream.shutdown().await.expect("half-closed");
        let mut received = Vec::new();
        workload_end.read_to_end(&mut received).await.expect("read");
        assert_eq!(received, b"request");
        assert_eq!(engine.inspect_exec(&session.id).await, Ok(exit));
        assert_eq!(engine.started(), [spec]);
        engine.block_fail_stop();
        assert_eq!(engine.fail_stop("c").await, Ok(FailStop::Blocked));
        assert_eq!(engine.stopped(), ["c"]);
    }
}
