//! A run's Cua Driver sessions on its one Browser.
//!
//! Each opens while the turn holds the Browser. A call easi.run never answered may still be acting on the page, so no
//! other session opens on that Browser: the run replaces it. A session's channel sends nothing once the turn no longer
//! holds its Browser (Stop).

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use strict_json::Value;
use team_browser::cua::{Channel as CuaChannel, ChannelLost, CuaError, CuaSession};
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use tokio::time::Instant;

use super::Running;
use crate::browser::runtime::{AgentCalls, BrowserRuntime, Lease};

/// Whether the turn still runs and still holds this very lease.
#[derive(Clone)]
struct HeldBy {
    running: Running,
    team_id: String,
    token: String,
    session_id: String,
    stop: Stop,
}

impl HeldBy {
    async fn holds(&self) -> bool {
        if self.stop.is_stopped() {
            return false;
        }
        let running = self.running.lock().await;
        running.get(&self.team_id).is_some_and(|held| {
            held.token == self.token && held.lease.session_id == self.session_id
        })
    }
}

/// A Driver session's channel that sends nothing once the turn no longer holds its Browser.
struct HeldChannel {
    calls: AgentCalls,
    held: HeldBy,
}

impl CuaChannel for HeldChannel {
    fn send<'a>(
        &'a mut self,
        data: &'a [u8],
        deadline: Instant,
    ) -> BoxFuture<'a, Result<(), ChannelLost>> {
        Box::pin(async move {
            if !self.held.holds().await {
                return Err(ChannelLost);
            }
            self.calls.send(data, deadline).await
        })
    }

    fn receive_line(
        &mut self,
        deadline: Instant,
        limit: usize,
    ) -> BoxFuture<'_, Result<Vec<u8>, ChannelLost>> {
        self.calls.receive_line(deadline, limit)
    }
}

/// The task's Browser as its loop sees it.
pub(super) struct Drivers {
    runtime: Arc<BrowserRuntime>,
    lease: Lease,
    held: HeldBy,
    // Set while a call has no answer; one that never got any leaves it set.
    uncertain: Arc<AtomicBool>,
}

impl Drivers {
    pub(super) fn new(
        runtime: Arc<BrowserRuntime>,
        running: Running,
        lease: Lease,
        token: String,
        stop: Stop,
    ) -> Self {
        let held = HeldBy {
            running,
            team_id: lease.team_id.clone(),
            token,
            session_id: lease.session_id.clone(),
            stop,
        };
        Self {
            runtime,
            lease,
            held,
            uncertain: Arc::new(AtomicBool::new(false)),
        }
    }

    /// One Driver session on the task's Browser, opened by `deadline`; none reaches a Browser Stop revoked.
    pub(super) async fn fresh(&self, deadline: Instant) -> Result<CuaSession, CuaError> {
        if !self.held.holds().await {
            return Err(CuaError::fatal("stopped"));
        }
        let mut session = CuaSession::new(Box::new(HeldChannel {
            calls: AgentCalls::new(
                Arc::clone(&self.runtime),
                self.lease.session_id.clone(),
                Arc::clone(&self.uncertain),
            ),
            held: self.held.clone(),
        }));
        session.set_deadline(Some(deadline));
        session.open().await?;
        Ok(session)
    }

    /// Whether a call of this Browser got no answer, so it may still act.
    pub(super) fn uncertain(&self) -> bool {
        self.uncertain.load(Ordering::SeqCst)
    }

    /// A fresh session in place of `failed`, once no call of it can still act.
    pub(super) async fn replace(
        &self,
        failed: CuaSession,
        deadline: Instant,
    ) -> Result<CuaSession, CuaError> {
        drop(failed);
        self.fresh(deadline).await
    }

    /// The task's downloads listing as the Browser reports it, read by `deadline` while the turn holds it.
    pub(super) async fn listing(&self, deadline: Instant) -> Option<Value> {
        if !self.held.holds().await {
            return None;
        }
        self.runtime
            .files(&self.lease, Some(deadline), Some(&self.held.stop))
            .await
            .ok()
    }
}
