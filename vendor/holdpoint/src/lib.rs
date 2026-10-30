//! Holdpoint: a crash-proof agent runtime. Every run resumes from its last committed step.
//!
//! A run is a [`Definition`] executed through a [`Ctx`]: every model call, local computation, activity, deferred
//! request, and timer is a journaled step in the run's history, stored in SQLite. After any crash the run is
//! re-executed from the top; recorded steps return their stored results without running again, and execution
//! continues live from the first step that had not finished. An interrupted non-idempotent activity is never
//! repeated: it resolves as uncertain. Every run ends in exactly one explicit [`Outcome`].
//!
//! See `docs/architecture.md` for the run model, the crash table, and the invariants.

#![cfg_attr(coverage_nightly, feature(coverage_attribute))]

pub mod agent;
mod attempt;
pub mod clock;
mod ctx;
mod definition;
pub mod error;
pub mod event;
mod failpoint;
mod handler;
mod history;
mod host;
pub mod json;
pub mod limits;
pub mod policy;
#[cfg(any(feature = "openai", feature = "anthropic"))]
pub mod providers;
mod scheduler;
pub mod schema;
mod session;
pub mod store;
#[cfg(feature = "testing")]
pub mod testing;
mod worker;

pub use agent::{AGENT, Agent, AgentInput, AgentLimits, Fold};
pub use clock::{Clock, ManualClock, SystemClock};
pub use ctx::{BLOBS_PER_STEP, Blobs, Ctx};
pub use definition::{Definition, Driven, Interrupt, Output, StepResult, Wait};
pub use error::{Error, LimitKind};
pub use event::{Event, Outcome, StepKind};
pub use handler::{Abandoned, Activity, ActivityCall, ActivityReply, Attempt, Model, ModelCall, ModelReply};
pub use host::{Authority, Credentials, Host, Proof, RunInfo};
pub use limits::{Limits, WorkerOptions};
pub use policy::{ActivityOptions, Containment, Failure, RetryPolicy, Timeouts};
pub use schema::Schema;
pub use store::{Obligation, PendingRequest, Resolution, Store, Submitted};
pub use worker::{ModelOptions, RunOptions, Worker, WorkerBuilder};
