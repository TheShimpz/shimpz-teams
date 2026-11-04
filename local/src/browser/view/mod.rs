//! One Browser's watched screen (ADR-0117, ADR-0126).
//!
//! A view belongs to exactly one task or sign-in session: its random id is drawn when the Browser is held, and it ends
//! when that is released or stopped, which ends every stream it carries at once. Each viewer's stream is one upgraded
//! connection to easi.run on Team's login, carrying the viewer's own WebSocket: Team never reads it.
//!
//! One person controls at a time; taking control ends every other stream of the view and admits none while it lasts. A
//! private view (a sign-in session's) admits its person's control alone, never a watcher. easi.run itself admits input
//! only from who holds control, and takes back what a holder held when their stream ends.

mod tunnel;

use std::collections::HashMap;
use std::pin::pin;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use team_core::entropy::{Entropy, token_hex};
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_protocol::browser_view::{BUSY, NOT_RUNNING, UNAVAILABLE};
use tokio::io::AsyncWriteExt;
use tokio::sync::Notify;
use tokio::time::{Instant, timeout_at};

use super::audit::Lines;
use super::easi::Upgraded as EasiStream;
use crate::http::service::{Relay, Upgraded};

pub use tunnel::{Carried, UPGRADE_SECONDS, answer};

/// The stream could not be opened.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RelayUnavailable;

/// Opens viewers' streams of one Browser.
pub trait Relays: Send + Sync {
    /// One upgraded stream of the session, for the viewer's WebSocket `key`, as a viewer of id `viewer`.
    ///
    /// # Errors
    /// [`RelayUnavailable`] when easi.run did not answer the upgrade.
    fn open_relay<'a>(
        &'a self,
        key: &'a str,
        viewer: &'a str,
        deadline: Instant,
    ) -> BoxFuture<'a, Result<EasiStream, RelayUnavailable>>;
}

#[derive(Debug)]
struct State {
    ended: bool,
    controlled: bool,
    // When the view last had no person in control: a sign-in session idles out from it.
    quiet_since: Instant,
    // Each stream's interrupt, by its number.
    tunnels: HashMap<u64, Stop>,
    next: u64,
}

/// What a view needs beside its relays.
pub struct ViewParts {
    /// The viewers' streams.
    pub relays: Arc<dyn Relays>,
    /// Its audit lines (`started`, `ended:…`, `control-ended`).
    pub lines: Lines,
    /// Whether a person may control this Browser at all.
    pub controllable: bool,
    /// Whether it admits its person's control alone.
    pub private: bool,
    /// The random source of its id.
    pub entropy: Arc<dyn Entropy>,
}

/// The watched screen of one held Browser.
pub struct View {
    id: String,
    private: bool,
    controllable: bool,
    relays: Arc<dyn Relays>,
    lines: Lines,
    entropy: Arc<dyn Entropy>,
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
            controllable: parts.controllable,
            relays: parts.relays,
            lines: parts.lines,
            entropy: parts.entropy,
            ended: Stop::new(),
            state: Mutex::new(State {
                ended: false,
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

    /// Once the view ended: whether every stream ended by `deadline`.
    pub async fn quiesce(&self, deadline: Instant) -> bool {
        loop {
            let mut changed = pin!(self.changed.notified());
            changed.as_mut().enable();
            {
                let state = self.state();
                if !state.controlled && state.tunnels.is_empty() {
                    return true;
                }
            }
            if timeout_at(deadline, changed).await.is_err() {
                return false;
            }
        }
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
        if self.private && !control {
            return Err(Refused(NOT_RUNNING));
        }
        if control && !self.controllable {
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

    /// Opens one viewer's stream with its WebSocket `key`; the stream then runs as [`Relay`] once the viewer has its
    /// 101. `control` asks for the person's own control, which only a Browser a person may control offers.
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
            self.control_ended().await;
        }
        opened
    }

    async fn upgrade(self: &Arc<Self>, key: &str, control: bool) -> Result<Stream, Refused> {
        let viewer = token_hex(self.entropy.as_ref(), 16).map_err(|_| Refused(UNAVAILABLE))?;
        self.lines
            .record("started")
            .await
            .map_err(|_| Refused(UNAVAILABLE))?;
        let Ok(easi) = self
            .relays
            .open_relay(key, &viewer, Instant::now() + UPGRADE_SECONDS)
            .await
        else {
            return Err(Refused(UNAVAILABLE));
        };
        let interrupt = Stop::new();
        let (number, ended, busy) = {
            let mut state = self.state();
            let number = state.next;
            state.next += 1;
            state.tunnels.insert(number, interrupt.clone());
            // A watcher whose stream opened while a person took control is refused here, before any frame reaches it.
            (number, state.ended, state.controlled && !control)
        };
        if ended || busy {
            self.finish(number, &Carried::default()).await;
            return Err(Refused(if ended { NOT_RUNNING } else { BUSY }));
        }
        Ok(Stream {
            view: Arc::clone(self),
            part: Some(Part {
                easi,
                interrupt,
                number,
            }),
            control,
        })
    }

    async fn finish(&self, number: u64, carried: &Carried) {
        self.state().tunnels.remove(&number);
        self.changed.notify_waiters();
        let _ = self.lines.record(&carried.ended()).await;
    }

    async fn control_ended(&self) {
        {
            let mut state = self.state();
            state.controlled = false;
            state.quiet_since = Instant::now();
        }
        self.changed.notify_waiters();
        let _ = self.lines.record("control-ended").await;
    }
}

struct Part {
    easi: EasiStream,
    interrupt: Stop,
    number: u64,
}

/// One viewer's stream that easi.run already upgraded; it ends with its view, its sides, or its life.
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
        let Some(part) = self.part.take() else {
            return;
        };
        let carried = Carried::default();
        let _ = tunnel::splice(
            part.easi,
            client.as_mut(),
            &part.interrupt,
            &self.view.ended,
            &carried,
        )
        .await;
        let _ = client.shutdown().await;
        self.view.finish(part.number, &carried).await;
        if self.control {
            self.view.control_ended().await;
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
        // A stream never run (its viewer's 101 could not be written) is still finished, and its control ended.
        let Some(part) = self.part.take() else {
            return;
        };
        let view = Arc::clone(&self.view);
        let control = self.control;
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
            runtime.spawn(async move {
                drop(part.easi);
                view.finish(part.number, &Carried::default()).await;
                if control {
                    view.control_ended().await;
                }
            });
        }
    }
}

#[cfg(test)]
mod tests;
