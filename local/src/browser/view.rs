//! One Browser's watched screen (ADR-0117, watching over `KasmVNC`).
//!
//! A view belongs to exactly one task or sign-in session: its random id is drawn when the Browser is held, and it ends
//! when that is released or stopped, which ends every stream it carries at once. Each viewer's stream is one relay exec
//! into the display: the display must answer the upgrade exactly before the viewer gets its 101, and the exec must be
//! proven ended, within a deadline that a stalled Docker inspection cannot extend, once the stream ends; when that
//! proof does not come, the view stays unavailable for the rest of its Browser.
//!
//! A Browser a person may control (ADR-0117, Sign-ins amendment item 3) authenticates every stream: watching carries
//! the read-only credential and control the read-write one. One person controls at a time; taking control ends every
//! other stream of the view and admits none while it lasts, and when control ends Team releases every held key and
//! button before anything else acts. A release that cannot be proven leaves the view unavailable. A private view (a
//! sign-in session's) admits its person's control alone, never a watcher.

use std::collections::HashMap;
use std::pin::pin;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use team_core::entropy::{Entropy, token_hex};
use team_core::future::BoxFuture;
use team_core::secret::Secret;
use team_core::stop::Stop;
use team_protocol::browser_view::{BUSY, NOT_RUNNING, UNAVAILABLE};
use tokio::io::AsyncWriteExt;
use tokio::sync::Notify;
use tokio::time::{Instant, timeout_at};

use super::audit::Lines;
use super::channel::Channel;
use super::credentials::Credentials;
use super::release;
use super::tunnel::Tunnel;
use crate::http::service::{Relay, Upgraded};

/// The deadline to prove a stream's relay exec ended.
pub const EXIT_SECONDS: Duration = Duration::from_secs(2);

/// The relay exec could not be opened.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RelayUnavailable;

/// The relay execs into one Browser's display.
pub trait Relays: Send + Sync {
    /// Opens one relay exec into the display: its exec id and its channel.
    ///
    /// # Errors
    /// [`RelayUnavailable`] when it could not be opened.
    fn open_relay(&self) -> BoxFuture<'_, Result<(String, Channel), RelayUnavailable>>;

    /// Whether Docker proved the exec ended before `deadline`; a stalled or failed inspection proves nothing.
    fn exited<'a>(&'a self, exec_id: &'a str, deadline: Instant) -> BoxFuture<'a, bool>;
}

/// Releases what a person held in the display once their control ended.
pub trait InputRelease: Send + Sync {
    /// Releases every key and button through a fresh relay, as the read-write user; whether it was proven.
    fn release<'a>(&'a self, relays: &'a dyn Relays, authorization: &'a str)
    -> BoxFuture<'a, bool>;
}

/// The RFB release of [`release::release`].
pub struct RfbRelease(pub Arc<dyn Entropy>);

impl InputRelease for RfbRelease {
    fn release<'a>(
        &'a self,
        relays: &'a dyn Relays,
        authorization: &'a str,
    ) -> BoxFuture<'a, bool> {
        Box::pin(async move {
            release::release(relays, authorization, self.0.as_ref())
                .await
                .is_ok()
        })
    }
}

#[derive(Debug)]
struct State {
    ended: bool,
    unavailable: bool,
    controlled: bool,
    // When the view last had no person in control: a sign-in session idles out from it.
    quiet_since: Instant,
    // Each stream's interrupt, by its number.
    tunnels: HashMap<u64, Stop>,
    next: u64,
}

/// What a view needs beside its relays.
pub struct ViewParts {
    /// The relays into the display.
    pub relays: Arc<dyn Relays>,
    /// Its audit lines (`started`, `ended:…`, `control-ended:…`).
    pub lines: Lines,
    /// The display credentials, for a Browser a person may control.
    pub credentials: Option<Arc<Credentials>>,
    /// Whether it admits its person's control alone.
    pub private: bool,
    /// The random source of its id.
    pub entropy: Arc<dyn Entropy>,
    /// How held input is released.
    pub release: Arc<dyn InputRelease>,
}

/// The watched screen of one held Browser.
pub struct View {
    id: String,
    private: bool,
    relays: Arc<dyn Relays>,
    lines: Lines,
    credentials: Option<Arc<Credentials>>,
    release: Arc<dyn InputRelease>,
    ended: Stop,
    state: Mutex<State>,
    changed: Notify,
}

impl std::fmt::Debug for View {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("View")
            .field("id", &self.id)
            .field("private", &self.private)
            .finish_non_exhaustive()
    }
}

/// A view refused a stream, with its closed code (`browser_view`'s).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Refused(pub &'static str);

impl View {
    /// A view with a fresh random id.
    ///
    /// # Errors
    /// [`Refused`] (unavailable) when no id could be drawn.
    pub fn new(parts: ViewParts) -> Result<Arc<Self>, Refused> {
        let id = token_hex(parts.entropy.as_ref(), 16).map_err(|_| Refused(UNAVAILABLE))?;
        Ok(Arc::new(Self {
            id,
            private: parts.private,
            relays: parts.relays,
            lines: parts.lines,
            credentials: parts.credentials,
            release: parts.release,
            ended: Stop::new(),
            state: Mutex::new(State {
                ended: false,
                unavailable: false,
                controlled: false,
                quiet_since: Instant::now(),
                tunnels: HashMap::new(),
                next: 0,
            }),
            changed: Notify::new(),
        }))
    }

    /// Its id.
    #[must_use]
    pub fn id(&self) -> &str {
        &self.id
    }

    /// Whether it admits its person's control alone.
    #[must_use]
    pub fn private(&self) -> bool {
        self.private
    }

    fn state(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Its Browser was released or stopped: every stream of this view stops at once, and no new one starts.
    pub fn end(&self) {
        let tunnels: Vec<Stop> = {
            let mut state = self.state();
            state.ended = true;
            state.tunnels.values().cloned().collect()
        };
        self.ended.stop();
        for tunnel in tunnels {
            tunnel.stop();
        }
    }

    /// Once the view ended: whether every stream ended and the person's input was proven released by `deadline`.
    pub async fn quiesce(&self, deadline: Instant) -> bool {
        loop {
            let mut changed = pin!(self.changed.notified());
            changed.as_mut().enable();
            {
                let state = self.state();
                if !state.controlled && state.tunnels.is_empty() {
                    return !state.unavailable;
                }
            }
            if timeout_at(deadline, changed).await.is_err() {
                return false;
            }
        }
    }

    /// Whether a stream or a release could not be proven ended: the view admits no stream any more.
    #[must_use]
    pub fn unavailable(&self) -> bool {
        self.state().unavailable
    }

    /// How long no person has controlled the Browser; zero while one does.
    #[must_use]
    pub fn quiet_for(&self) -> Duration {
        let state = self.state();
        if state.controlled {
            Duration::ZERO
        } else {
            state.quiet_since.elapsed()
        }
    }

    fn admit(&self, control: bool) -> Result<Vec<Stop>, Refused> {
        let mut state = self.state();
        if state.ended {
            return Err(Refused(NOT_RUNNING));
        }
        if state.unavailable {
            return Err(Refused(UNAVAILABLE));
        }
        if self.private && !control {
            return Err(Refused(NOT_RUNNING));
        }
        if control && self.credentials.is_none() {
            return Err(Refused(UNAVAILABLE));
        }
        if state.controlled {
            return Err(Refused(BUSY));
        }
        if !control {
            return Ok(Vec::new());
        }
        state.controlled = true;
        Ok(state.tunnels.values().cloned().collect())
    }

    /// Opens one viewer's stream: the relay is opened and the display upgraded with `key`; the stream then runs as a
    /// [`Relay`] once the viewer has its 101. `control` asks for the person's own control, which only a Browser a
    /// person may control offers.
    ///
    /// # Errors
    /// [`Refused`] with a closed code before any byte reaches the viewer.
    pub async fn open(self: &Arc<Self>, key: &str, control: bool) -> Result<Stream, Refused> {
        let watching = self.admit(control)?;
        // The person's control is private: every other stream ends, and none starts while it lasts.
        for tunnel in watching {
            tunnel.stop();
        }
        let opened = self.upgrade(key, control).await;
        if opened.is_err() && control {
            self.release_input().await;
        }
        opened
    }

    async fn upgrade(self: &Arc<Self>, key: &str, control: bool) -> Result<Stream, Refused> {
        let Ok((exec_id, display)) = self.relays.open_relay().await else {
            return Err(Refused(UNAVAILABLE));
        };
        let interrupt = Stop::new();
        let (number, ended, busy) = {
            let mut state = self.state();
            let number = state.next;
            state.next += 1;
            state.tunnels.insert(number, interrupt.clone());
            // A watcher whose relay opened while a person took control is refused here, before any frame reaches it.
            (number, state.ended, state.controlled && !control)
        };
        let mut tunnel = Tunnel::new(display, interrupt);
        let upgraded = if ended {
            Err(NOT_RUNNING)
        } else if busy {
            Err(BUSY)
        } else {
            self.answered(&mut tunnel, key, control).await
        };
        match upgraded {
            Ok(early) => Ok(Stream {
                view: Arc::clone(self),
                part: Some(Part {
                    exec_id,
                    tunnel,
                    number,
                    early,
                }),
                control,
            }),
            Err(code) => {
                self.finish(exec_id, tunnel, number).await;
                Err(Refused(code))
            }
        }
    }

    /// Records the stream's start and upgrades the display with the stream's own credential; the display bytes read
    /// with its 101.
    async fn answered(
        &self,
        tunnel: &mut Tunnel,
        key: &str,
        control: bool,
    ) -> Result<Vec<u8>, &'static str> {
        self.lines
            .record("started")
            .await
            .map_err(|_| UNAVAILABLE)?;
        let authorization = self
            .credentials
            .as_ref()
            .map(|credentials| credentials.authorization(control));
        tunnel
            .upgrade(key, authorization.as_ref().map(Secret::expose))
            .await
            .map(|(_answer, early)| early)
            .map_err(|_| UNAVAILABLE)
    }

    /// Ends the relay's input and proves its exec ended before the deadline; then closes the channel.
    async fn finish(&self, exec_id: String, tunnel: Tunnel, number: u64) {
        let to_viewer = tunnel
            .carried
            .to_viewer
            .load(std::sync::atomic::Ordering::Relaxed);
        let to_display = tunnel
            .carried
            .to_display
            .load(std::sync::atomic::Ordering::Relaxed);
        let mut display = tunnel.into_display();
        display.close_input().await;
        let proven = self
            .relays
            .exited(&exec_id, Instant::now() + EXIT_SECONDS)
            .await;
        drop(display);
        {
            let mut state = self.state();
            if !proven {
                state.unavailable = true;
            }
            state.tunnels.remove(&number);
        }
        self.changed.notify_waiters();
        let _ = self
            .lines
            .record(&format!(
                "ended:proven={}:to_viewer={to_viewer}:to_display={to_display}",
                u8::from(proven)
            ))
            .await;
    }

    /// Releases what the person held before anything else acts; an unproven release leaves the view unavailable.
    async fn release_input(&self) {
        let released = match &self.credentials {
            Some(credentials) => {
                let authorization = credentials.authorization(true);
                self.release
                    .release(self.relays.as_ref(), authorization.expose())
                    .await
            }
            None => false,
        };
        {
            let mut state = self.state();
            state.controlled = false;
            state.quiet_since = Instant::now();
            if !released {
                state.unavailable = true;
            }
        }
        self.changed.notify_waiters();
        let _ = self
            .lines
            .record(&format!("control-ended:released={}", u8::from(released)))
            .await;
    }
}

struct Part {
    exec_id: String,
    tunnel: Tunnel,
    number: u64,
    early: Vec<u8>,
}

/// One viewer's stream whose display already answered its upgrade; it ends with its view, its sides, or its life.
pub struct Stream {
    view: Arc<View>,
    part: Option<Part>,
    control: bool,
}

impl std::fmt::Debug for Stream {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Stream")
            .field("control", &self.control)
            .finish_non_exhaustive()
    }
}

impl Stream {
    async fn carry(mut self, mut client: Box<dyn Upgraded>) {
        let Some(mut part) = self.part.take() else {
            return;
        };
        // The display bytes read with its 101 reach the viewer first; a side that fails after that only ends it.
        if client.write_all(&part.early).await.is_ok() {
            part.tunnel.carried.to_viewer.fetch_add(
                u64::try_from(part.early.len()).unwrap_or(u64::MAX),
                std::sync::atomic::Ordering::Relaxed,
            );
            let _ = part.tunnel.splice(client, &self.view.ended).await;
        }
        self.view
            .finish(part.exec_id, part.tunnel, part.number)
            .await;
        if self.control {
            self.view.release_input().await;
        }
    }
}

impl Relay for Stream {
    fn run(self: Box<Self>, client: Box<dyn Upgraded>) -> BoxFuture<'static, ()> {
        Box::pin(self.carry(client))
    }
}

impl Drop for Stream {
    fn drop(&mut self) {
        // A stream never run (its viewer's 101 could not be written) is still finished, and control released.
        let Some(part) = self.part.take() else {
            return;
        };
        let view = Arc::clone(&self.view);
        let control = self.control;
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
            runtime.spawn(async move {
                view.finish(part.exec_id, part.tunnel, part.number).await;
                if control {
                    view.release_input().await;
                }
            });
        }
    }
}

#[cfg(test)]
mod tests;
