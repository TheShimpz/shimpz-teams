//! A run's Cua Driver sessions on its one Browser.
//!
//! Each opens while the turn holds the Browser and is proven ended before the next; Chromium is started again when a
//! page no session can read, or a session that cannot open, needs it. A session's channel sends nothing once the turn
//! no longer holds its Browser (Stop).

use std::sync::Arc;
use std::time::Duration;

use strict_json::Value;
use team_browser::cua::{Channel as CuaChannel, ChannelLost, CuaError, CuaSession};
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use tokio::sync::Mutex;
use tokio::time::Instant;

use super::Running;
use crate::browser::channel::Channel;
use crate::browser::runtime::{BrowserError, BrowserRuntime, Lease};

/// The deadline to prove a failed Driver session's exec ended once its channel is shut.
pub const DRIVER_END_SECONDS: Duration = Duration::from_secs(5);

/// Why a fresh session did not open.
pub(super) enum Fresh {
    /// Docker could not open its exec.
    Runtime(BrowserError),
    /// It opened but could not start, or the turn was stopped.
    Driver(CuaError),
}

/// Whether the turn still runs and still holds this very lease.
#[derive(Clone)]
struct HeldBy {
    running: Running,
    team_id: String,
    token: String,
    container_id: String,
    stop: Stop,
}

impl HeldBy {
    async fn holds(&self) -> bool {
        if self.stop.is_stopped() {
            return false;
        }
        let running = self.running.lock().await;
        running.get(&self.team_id).is_some_and(|held| {
            held.token == self.token && held.lease.container_id == self.container_id
        })
    }
}

/// A Driver session's channel that sends nothing once the turn no longer holds its Browser.
struct HeldChannel {
    channel: Channel,
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
            self.channel
                .send(data, deadline)
                .await
                .map_err(|_| ChannelLost)
        })
    }

    fn receive_line(
        &mut self,
        deadline: Instant,
        limit: usize,
    ) -> BoxFuture<'_, Result<Vec<u8>, ChannelLost>> {
        Box::pin(async move {
            self.channel
                .receive_line(deadline, limit)
                .await
                .map_err(|_| ChannelLost)
        })
    }
}

/// A Driver session's channel with no hold to check (a sign-in session's window count).
struct PlainChannel(Channel);

impl CuaChannel for PlainChannel {
    fn send<'a>(
        &'a mut self,
        data: &'a [u8],
        deadline: Instant,
    ) -> BoxFuture<'a, Result<(), ChannelLost>> {
        Box::pin(async move { self.0.send(data, deadline).await.map_err(|_| ChannelLost) })
    }

    fn receive_line(
        &mut self,
        deadline: Instant,
        limit: usize,
    ) -> BoxFuture<'_, Result<Vec<u8>, ChannelLost>> {
        Box::pin(async move {
            self.0
                .receive_line(deadline, limit)
                .await
                .map_err(|_| ChannelLost)
        })
    }
}

/// A Driver channel over `channel` with no hold to check.
pub(crate) fn plain(channel: Channel) -> Box<dyn CuaChannel> {
    Box::new(PlainChannel(channel))
}

/// The task's Browser as its loop sees it.
pub(super) struct Drivers {
    runtime: Arc<BrowserRuntime>,
    lease: Lease,
    held: HeldBy,
    // The execs of sessions not yet proven ended.
    open: Mutex<Vec<String>>,
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
            container_id: lease.container_id.clone(),
            stop,
        };
        Self {
            runtime,
            lease,
            held,
            open: Mutex::new(Vec::new()),
        }
    }

    /// One Driver session on the task's Browser, opened by `deadline`; none reaches a Browser Stop revoked.
    pub(super) async fn fresh(&self, deadline: Instant) -> Result<CuaSession, Fresh> {
        if !self.held.holds().await {
            return Err(Fresh::Driver(CuaError::fatal("stopped")));
        }
        let (exec_id, channel) = self
            .runtime
            .open_stream(&self.lease, deadline, &self.held.stop)
            .await
            .map_err(Fresh::Runtime)?;
        self.open.lock().await.push(exec_id);
        if !self.held.holds().await {
            return Err(Fresh::Driver(CuaError::fatal("stopped")));
        }
        let mut session = CuaSession::new(Box::new(HeldChannel {
            channel,
            held: self.held.clone(),
        }));
        session.set_deadline(Some(deadline));
        session.open().await.map_err(Fresh::Driver)?;
        Ok(session)
    }

    /// Ends every session not yet proven ended; until Docker proves each ended, no other session opens.
    async fn end(&self, deadline: Instant) -> Result<(), CuaError> {
        let open: Vec<String> = std::mem::take(&mut *self.open.lock().await);
        let until = deadline.min(Instant::now() + DRIVER_END_SECONDS);
        for exec_id in open {
            if !self.runtime.exited(&exec_id, until).await {
                return Err(CuaError::fatal("driver-not-ended"));
            }
        }
        Ok(())
    }

    /// Starts the task's Chromium again while the turn still holds its Browser; Stop wins before and during it.
    async fn relaunch(&self, deadline: Instant) -> Result<(), CuaError> {
        if !self.held.holds().await {
            return Err(CuaError::fatal("stopped"));
        }
        self.runtime
            .relaunch(&self.lease, deadline, &self.held.stop)
            .await
            .map_err(|_| CuaError::fatal("relaunch-failed"))
    }

    async fn replacement(&self, deadline: Instant) -> Result<CuaSession, CuaError> {
        match self.fresh(deadline).await {
            Ok(session) => Ok(session),
            Err(Fresh::Runtime(_)) => Err(CuaError::fatal("driver-unavailable")),
            Err(Fresh::Driver(error)) => Err(error),
        }
    }

    /// A fresh session in place of `failed`, and whether Chromium was started again for it.
    ///
    /// With `relaunch`, Chromium starts again first. Otherwise, a session that opened but cannot start on this
    /// Chromium is ended too, and Chromium starts again once before another opens; a channel that could not even be
    /// opened relaunches nothing.
    pub(super) async fn replace(
        &self,
        failed: CuaSession,
        deadline: Instant,
        relaunch: bool,
    ) -> Result<(CuaSession, bool), CuaError> {
        // Dropping the session closes its channel: its exec's stdin ends and nothing more is read from it.
        drop(failed);
        self.end(deadline).await?;
        if relaunch {
            self.relaunch(deadline).await?;
            return Ok((self.replacement(deadline).await?, true));
        }
        match self.fresh(deadline).await {
            Ok(session) => Ok((session, false)),
            // Docker may have started an exec it could not hand over: unproven, it leaves nothing more to open here.
            Err(Fresh::Runtime(_)) => Err(CuaError::fatal("driver-unavailable")),
            Err(Fresh::Driver(error)) if error.code == "stopped" => Err(error),
            Err(Fresh::Driver(_)) => {
                // The session opened but could not start: its exec, proven ended, makes way for Chromium started again.
                self.end(deadline).await?;
                self.relaunch(deadline).await?;
                Ok((self.replacement(deadline).await?, true))
            }
        }
    }
}

impl Drivers {
    /// The task's downloads listing as the Browser reports it, read by `deadline` while the turn holds its Browser.
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
