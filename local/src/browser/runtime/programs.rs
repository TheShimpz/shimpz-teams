//! The Browser's `browser-host` programs, each one `docker exec` as the Browser's user: readiness, credentials, the
//! capsule's import and export, Chromium's clean stop and relaunch, the downloads, the Cua Driver's MCP session, and
//! the display relay. Every opening and every program ends by a deadline no stalled Docker call extends; a program's
//! exit status 0 must be proven by that deadline too.

use std::time::Duration;

use strict_json::Value;
use team_core::exec::{ExecId, ExecSpec};
use team_core::stop::Stop;
use team_protocol::python::value::{has_exactly, object};
use tokio::time::{Instant, sleep, sleep_until, timeout_at};

use std::sync::Arc;

use team_core::future::BoxFuture;

use super::{BrowserError, BrowserRuntime, Capsule, Lease};
use crate::browser::channel::Channel;
use crate::browser::envelope::USER;
use crate::browser::view::{RelayUnavailable, Relays};

/// The Browser's own host program.
pub const HOST: &str = "/usr/local/bin/browser-host";
/// A downloads listing or read's bound.
pub const PROGRAM_SECONDS: Duration = Duration::from_secs(30);
/// `browser-host relaunch` proves a new Chromium within 30 seconds; this one more for the exec around it.
pub const RELAUNCH_SECONDS: Duration = Duration::from_secs(40);
/// Chromium's clean stop.
pub const STOP_SECONDS: Duration = Duration::from_secs(40);
/// A capsule's import or export, and the credentials' delivery.
pub const CAPSULE_SECONDS: Duration = Duration::from_secs(30);
/// The longest export: the whole archive, headers, padding, and end blocks included.
pub const MAX_CAPSULE_BYTES: u64 = 128 * 1024 * 1024;
/// The capsule crosses in chunks of this size, so Team never holds more than one at a time.
pub const CHUNK_BYTES: usize = 1024 * 1024;
const MAX_LISTING_BYTES: usize = 256 * 1024;
const EXITED_POLL: Duration = Duration::from_millis(100);

fn spec(container: &str, arguments: &[&str]) -> ExecSpec {
    let mut argv = vec![HOST.to_owned()];
    argv.extend(arguments.iter().map(|argument| (*argument).to_owned()));
    ExecSpec {
        container: container.to_owned(),
        argv,
        user: USER.to_owned(),
        working_dir: Some("/tmp".to_owned()),
        env: Vec::new(),
        stdin: true,
    }
}

async fn stopped(stop: Option<&Stop>) {
    match stop {
        Some(stop) => stop.stopped().await,
        None => std::future::pending().await,
    }
}

impl BrowserRuntime {
    /// One `browser-host` exec opened by `deadline` while not stopped: its id and its channel. An exec Docker opens
    /// only after the wait ended is dropped unused, so nothing ever reaches it.
    async fn open_exec(
        &self,
        container: &str,
        arguments: &[&str],
        deadline: Instant,
        stop: Option<&Stop>,
    ) -> Result<(String, Channel), BrowserError> {
        if Instant::now() >= deadline || stop.is_some_and(Stop::is_stopped) {
            return Err(BrowserError::LOST);
        }
        let spec = spec(container, arguments);
        tokio::select! {
            opened = self.host.exec.start_exec(&spec) => opened
                .map(|session| (session.id.0, Channel::new(session.stream)))
                .map_err(|_| BrowserError::LOST),
            () = sleep_until(deadline) => Err(BrowserError::LOST),
            () = stopped(stop) => Err(BrowserError::LOST),
        }
    }

    /// The exec's exit code, read once its stream ended, by `deadline`.
    async fn exit_code(&self, exec_id: &str, deadline: Instant) -> Option<i64> {
        let id = ExecId(exec_id.to_owned());
        match timeout_at(deadline, self.host.exec.inspect_exec(&id)).await {
            Ok(Ok(exit)) if !exit.running => exit.exit_code,
            _ => None,
        }
    }

    /// Whether Docker proved the exec ended before `deadline`; a stalled or failed inspection proves nothing.
    pub async fn exited(&self, exec_id: &str, deadline: Instant) -> bool {
        let id = ExecId(exec_id.to_owned());
        while Instant::now() < deadline {
            if let Ok(Ok(exit)) = timeout_at(deadline, self.host.exec.inspect_exec(&id)).await
                && !exit.running
            {
                return true;
            }
            sleep(EXITED_POLL).await;
        }
        false
    }

    /// Runs one bounded `browser-host` program with no input and no output; its exit status, or `None`.
    pub async fn program(
        &self,
        container: &str,
        arguments: &[&str],
        deadline: Instant,
    ) -> Option<i64> {
        let (exec_id, mut channel) = self
            .open_exec(container, arguments, deadline, None)
            .await
            .ok()?;
        channel.close_input().await;
        channel.receive_end(deadline).await.ok()?;
        drop(channel);
        self.exit_code(&exec_id, deadline).await
    }

    /// Runs one `browser-host` program on `chunks` as its input, all by one deadline; it must end with status 0.
    pub(super) async fn feed(
        &self,
        container: &str,
        arguments: &[&str],
        chunks: Capsule,
    ) -> Result<(), BrowserError> {
        let deadline = Instant::now() + CAPSULE_SECONDS;
        let failed = |_| BrowserError::UNAVAILABLE;
        let (exec_id, mut channel) = self
            .open_exec(container, arguments, deadline, None)
            .await
            .map_err(failed)?;
        for chunk in chunks {
            let chunk = chunk.map_err(|_| BrowserError::UNAVAILABLE)?;
            channel
                .send(&chunk, deadline)
                .await
                .map_err(|_| BrowserError::UNAVAILABLE)?;
        }
        channel.close_input().await;
        channel
            .receive_end(deadline)
            .await
            .map_err(|_| BrowserError::UNAVAILABLE)?;
        drop(channel);
        match self.exit_code(&exec_id, deadline).await {
            Some(0) => Ok(()),
            _ => Err(BrowserError::UNAVAILABLE),
        }
    }

    /// One short answer line of a program with no input, then its end and its status 0, all by `deadline`.
    async fn answered(
        &self,
        container: &str,
        arguments: &[&str],
        deadline: Instant,
        stop: Option<&Stop>,
    ) -> Result<Vec<u8>, BrowserError> {
        let (exec_id, mut channel) = self.open_exec(container, arguments, deadline, stop).await?;
        channel.close_input().await;
        let answer = channel
            .receive_line(deadline, 16)
            .await
            .map_err(|_| BrowserError::LOST)?;
        channel
            .receive_end(deadline)
            .await
            .map_err(|_| BrowserError::LOST)?;
        drop(channel);
        match self.exit_code(&exec_id, deadline).await {
            Some(0) => Ok(answer),
            _ => Err(BrowserError::LOST),
        }
    }

    /// Ends the Browser's Chromium session cleanly, keeping its container for the export; any doubt is lost.
    ///
    /// # Errors
    /// [`BrowserError::LOST`] unless `stopped` and status 0 were proven in time.
    pub async fn stop(&self, lease: &Lease) -> Result<(), BrowserError> {
        let deadline = Instant::now() + STOP_SECONDS;
        let answer = self
            .answered(&lease.container_id, &["stop"], deadline, None)
            .await?;
        if answer == b"stopped" {
            Ok(())
        } else {
            Err(BrowserError::LOST)
        }
    }

    /// Starts an agent Browser's Chromium again in place, by `deadline` and while not stopped; any doubt is lost.
    ///
    /// # Errors
    /// [`BrowserError::LOST`] unless `relaunched` and status 0 were proven in time and the task is still not stopped.
    pub async fn relaunch(
        &self,
        lease: &Lease,
        deadline: Instant,
        stop: &Stop,
    ) -> Result<(), BrowserError> {
        let end = deadline.min(Instant::now() + RELAUNCH_SECONDS);
        let answer = self
            .answered(&lease.container_id, &["relaunch"], end, Some(stop))
            .await?;
        if answer == b"relaunched" && !stop.is_stopped() {
            Ok(())
        } else {
            Err(BrowserError::LOST)
        }
    }

    /// Hands the stopped Browser's capsule to `sink` as it arrives; its size, once the export ended with status 0.
    ///
    /// # Errors
    /// [`BrowserError::LOST`] when it failed, was larger than allowed, or `sink` refused a chunk.
    pub async fn export(
        &self,
        lease: &Lease,
        sink: &mut (dyn FnMut(&[u8]) -> Result<(), ()> + Send),
    ) -> Result<u64, BrowserError> {
        let deadline = Instant::now() + CAPSULE_SECONDS;
        let lost = |_| BrowserError::LOST;
        let (exec_id, mut channel) = self
            .open_exec(&lease.container_id, &["profile", "export"], deadline, None)
            .await?;
        channel.close_input().await;
        let mut total: u64 = 0;
        loop {
            let chunk = channel
                .receive_chunk(CHUNK_BYTES, deadline)
                .await
                .map_err(lost)?;
            if chunk.is_empty() {
                break;
            }
            total += u64::try_from(chunk.len()).unwrap_or(u64::MAX);
            if total > MAX_CAPSULE_BYTES {
                return Err(BrowserError::LOST);
            }
            sink(&chunk).map_err(|()| BrowserError::LOST)?;
        }
        drop(channel);
        match self.exit_code(&exec_id, deadline).await {
            Some(0) => Ok(total),
            _ => Err(BrowserError::LOST),
        }
    }

    /// The downloads listing `browser-host files list` returns, strictly parsed; opening and reading it end by one
    /// deadline, and a Stop at any point leaves nothing of it.
    ///
    /// # Errors
    /// [`BrowserError::LOST`] when it could not be read whole, broke its shape, or the task was stopped.
    pub async fn files(
        &self,
        lease: &Lease,
        deadline: Option<Instant>,
        stop: Option<&Stop>,
    ) -> Result<Value, BrowserError> {
        let end = deadline.map_or(Instant::now() + PROGRAM_SECONDS, |deadline| {
            deadline.min(Instant::now() + PROGRAM_SECONDS)
        });
        let lost = |_| BrowserError::LOST;
        let (_exec_id, mut channel) = self
            .open_exec(&lease.container_id, &["files", "list"], end, stop)
            .await?;
        channel.close_input().await;
        let line = channel
            .receive_line(end, MAX_LISTING_BYTES)
            .await
            .map_err(lost)?;
        channel.receive_end(end).await.map_err(lost)?;
        let listing = strict_json::parse(&line).map_err(|_| BrowserError::LOST)?;
        let shaped = object(&listing)
            .is_some_and(|members| has_exactly(members, &["files", "refused", "skipped"]));
        if !shaped || stop.is_some_and(Stop::is_stopped) {
            return Err(BrowserError::LOST);
        }
        Ok(listing)
    }

    /// One listed download's exact bytes, framed by the length the program states first, by `deadline` if given.
    ///
    /// # Errors
    /// [`BrowserError::LOST`] when the stated length or the bytes differ, or anything follows them.
    pub async fn read(
        &self,
        lease: &Lease,
        name: &str,
        size: u64,
        deadline: Option<Instant>,
    ) -> Result<Vec<u8>, BrowserError> {
        let end = deadline.map_or(Instant::now() + PROGRAM_SECONDS, |deadline| {
            deadline.min(Instant::now() + PROGRAM_SECONDS)
        });
        let lost = |_| BrowserError::LOST;
        let (_exec_id, mut channel) = self
            .open_exec(&lease.container_id, &["files", "read", name], end, None)
            .await?;
        channel.close_input().await;
        let stated = channel.receive_line(end, 20).await.map_err(lost)?;
        if stated != size.to_string().into_bytes() {
            return Err(BrowserError::LOST);
        }
        let size = usize::try_from(size).map_err(|_| BrowserError::LOST)?;
        let data = channel.receive_exact(size, end).await.map_err(lost)?;
        channel.receive_end(end).await.map_err(lost)?;
        Ok(data)
    }

    /// The Cua Driver's stdio MCP session in this Browser, opened by `deadline` and while not stopped.
    ///
    /// # Errors
    /// [`BrowserError::LOST`] when it did not open in time.
    pub async fn open_stream(
        &self,
        lease: &Lease,
        deadline: Instant,
        stop: &Stop,
    ) -> Result<(String, Channel), BrowserError> {
        self.open_exec(&lease.container_id, &["mcp"], deadline, Some(stop))
            .await
    }

    /// One viewer's relay into this Browser's display.
    ///
    /// # Errors
    /// [`BrowserError::LOST`] when it did not open in time.
    pub async fn open_relay(&self, lease: &Lease) -> Result<(String, Channel), BrowserError> {
        self.open_exec(
            &lease.container_id,
            &["relay"],
            Instant::now() + PROGRAM_SECONDS,
            None,
        )
        .await
    }
}

/// The relays into one leased Browser's display.
#[derive(Clone, Debug)]
pub struct LeaseRelays {
    /// The runtime.
    pub runtime: Arc<BrowserRuntime>,
    /// The Browser.
    pub lease: Lease,
}

impl Relays for LeaseRelays {
    fn open_relay(&self) -> BoxFuture<'_, Result<(String, Channel), RelayUnavailable>> {
        Box::pin(async move {
            self.runtime
                .open_relay(&self.lease)
                .await
                .map_err(|_| RelayUnavailable)
        })
    }

    fn exited<'a>(&'a self, exec_id: &'a str, deadline: Instant) -> BoxFuture<'a, bool> {
        Box::pin(self.runtime.exited(exec_id, deadline))
    }
}
