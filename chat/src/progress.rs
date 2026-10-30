//! Closed, metadata-only progress for one Team-owned chat execution.
//!
//! A [`Reporter`] measures real controller operations as `started`/`finished` pairs without carrying any protected
//! payload. Its sink never changes the turn: a closed or failing consumer is ignored, and at its sequence cap it emits
//! nothing rather than an unpaired span.

use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::Instant;

use strict_json::Value;
use team_protocol::identifiers::{canonical_action_id, canonical_assistant_id};
use team_protocol::progress::{MAX_ACTION_COUNT, MAX_ELAPSED_MS, MAX_EVENTS};
use team_protocol::python::value::{int, object_of, text};

/// Where progress events go; a sink must never block.
pub type Sink = Arc<dyn Fn(Value) + Send + Sync>;

/// A monotonic clock in nanoseconds.
pub type MonotonicNs = Arc<dyn Fn() -> i64 + Send + Sync>;

/// One measured phase, exactly the protocol's.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    /// One Action of a batch.
    Action,
    /// The acknowledgement that the model accepted a batch's results.
    ActionDelivery,
    /// A batch's validation and preparation.
    ActionPreparation,
    /// The turn's Browser task.
    Browser,
    /// One Brain call.
    Model,
    /// Reading or revalidating the Team's context.
    TeamContext,
}

impl Phase {
    /// The wire name.
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Action => "action",
            Self::ActionDelivery => "action-delivery",
            Self::ActionPreparation => "action-preparation",
            Self::Browser => "browser",
            Self::Model => "model",
            Self::TeamContext => "team-context",
        }
    }
}

/// An Action span's position within its batch.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Position {
    /// Its 1-based index.
    pub index: i64,
    /// The batch size.
    pub total: i64,
    /// The Assistant.
    pub assistant_id: String,
    /// The Action.
    pub action: String,
}

fn monotonic() -> MonotonicNs {
    static ORIGIN: OnceLock<Instant> = OnceLock::new();
    Arc::new(|| {
        let elapsed = ORIGIN.get_or_init(Instant::now).elapsed();
        i64::try_from(elapsed.as_nanos()).unwrap_or(i64::MAX)
    })
}

/// Measures one execution's operations into its sink.
#[derive(Clone)]
pub struct Reporter {
    sink: Option<Sink>,
    clock: MonotonicNs,
    sequence: Arc<AtomicI64>,
}

impl std::fmt::Debug for Reporter {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Reporter")
            .field("sequence", &self.sequence())
            .finish_non_exhaustive()
    }
}

impl Default for Reporter {
    fn default() -> Self {
        Self::silent()
    }
}

impl Reporter {
    /// A reporter that emits nothing.
    #[must_use]
    pub fn silent() -> Self {
        Self {
            sink: None,
            clock: monotonic(),
            sequence: Arc::new(AtomicI64::new(0)),
        }
    }

    /// A reporter into `sink`, measured by the process's monotonic clock.
    #[must_use]
    pub fn new(sink: Sink) -> Self {
        Self::with_clock(sink, monotonic(), 0)
    }

    /// A reporter into `sink`, measured by `clock`, continuing after `sequence` events.
    #[must_use]
    pub fn with_clock(sink: Sink, clock: MonotonicNs, sequence: i64) -> Self {
        Self {
            sink: Some(sink),
            clock,
            sequence: Arc::new(AtomicI64::new(sequence)),
        }
    }

    /// A reporter that emits into `sink` too (after this one's sink), continuing this one's sequence and clock.
    #[must_use]
    pub fn teed(&self, also: Sink) -> Self {
        let first = self.sink.clone();
        let sink: Sink = Arc::new(move |event: Value| {
            if let Some(first) = &first {
                first(event.clone());
            }
            also(event);
        });
        Self {
            sink: Some(sink),
            clock: Arc::clone(&self.clock),
            sequence: Arc::clone(&self.sequence),
        }
    }

    /// The events emitted so far.
    #[must_use]
    pub fn sequence(&self) -> i64 {
        self.sequence.load(Ordering::SeqCst)
    }

    /// Starts measuring one operation; the span emits its `finished` record when it is dropped, however the
    /// operation ends. Nothing is emitted without a sink, or when the pair would pass the sequence cap.
    #[must_use]
    pub fn span(&self, phase: Phase, position: Option<Position>) -> Span<'_> {
        let valid = position.as_ref().is_none_or(|position| {
            phase == Phase::Action
                && (1..=position.total).contains(&position.index)
                && position.total <= MAX_ACTION_COUNT
                && canonical_assistant_id(&text(&position.assistant_id)).is_some()
                && canonical_action_id(&text(&position.action)).is_some()
        });
        let active = valid
            && (position.is_some() || phase != Phase::Action)
            && self.sink.is_some()
            && self.sequence() <= MAX_EVENTS - 2;
        let started = (self.clock)();
        if active {
            self.emit(phase, "started", None, position.as_ref());
        }
        Span {
            reporter: self,
            phase,
            position,
            started,
            active,
        }
    }

    fn emit(
        &self,
        phase: Phase,
        state: &str,
        elapsed_ms: Option<i64>,
        position: Option<&Position>,
    ) {
        let Some(sink) = &self.sink else {
            return;
        };
        let sequence = self.sequence.fetch_add(1, Ordering::SeqCst) + 1;
        let mut members = vec![
            ("seq", int(sequence)),
            ("phase", text(phase.name())),
            ("state", text(state)),
        ];
        if let Some(elapsed_ms) = elapsed_ms {
            members.push(("elapsed_ms", int(elapsed_ms)));
        }
        if let Some(position) = position {
            members.push(("index", int(position.index)));
            members.push(("total", int(position.total)));
            members.push(("assistant_id", text(&position.assistant_id)));
            members.push(("action", text(&position.action)));
        }
        sink(object_of(members));
    }
}

/// One measured operation; dropping it emits its `finished` record.
#[derive(Debug)]
pub struct Span<'r> {
    reporter: &'r Reporter,
    phase: Phase,
    position: Option<Position>,
    started: i64,
    active: bool,
}

impl Drop for Span<'_> {
    fn drop(&mut self) {
        if !self.active {
            return;
        }
        let elapsed_ms = ((self.reporter.clock)() - self.started).max(0) / 1_000_000;
        self.reporter.emit(
            self.phase,
            "finished",
            Some(elapsed_ms.min(MAX_ELAPSED_MS)),
            self.position.as_ref(),
        );
    }
}

#[cfg(test)]
mod tests;
