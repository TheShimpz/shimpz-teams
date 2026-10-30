//! What a run executes, and how a step reports back to it.

use serde_json::Value;

use crate::ctx::Ctx;
use crate::error::{Error, LimitKind};
use crate::event::Outcome;
use crate::policy::Failure;

/// A durable program. It must be deterministic outside its [`Ctx`]: no clock, randomness, I/O, or mutable state
/// of its own; everything it learns comes from its input and from the results of its steps.
pub trait Definition: Send + Sync {
    /// The definition's stable identifier.
    fn id(&self) -> &str;

    /// The definition's version. A run recorded under another version is never replayed by this code.
    fn version(&self) -> u32;

    /// Executes the run. Propagate every [`Interrupt`] with `?`; return an [`Outcome`] to end the run.
    ///
    /// # Errors
    ///
    /// The [`Interrupt`] a step raised: the runtime parks or ends the run accordingly.
    fn run(&self, ctx: &mut Ctx<'_>) -> Result<Outcome, Interrupt>;
}

/// Why a run waits.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Wait {
    /// For the host to resolve deferred requests ([`crate::Worker::pending`]).
    Deferred,
    /// Until a wall-clock time (a timer, or the next retry of a step).
    Until(i64),
    /// For the host to supply the run's authority again ([`crate::Host::authorize`]).
    Authority,
    /// Until the host proves an earlier executor ended, or an abandoned attempt's thread returns; asked again at the
    /// given wall-clock time.
    Executor(i64),
    /// The worker is stopping; the run is runnable at once by the next worker.
    Shutdown,
}

/// Why a definition must stop executing now. Created only by the [`Ctx`]; propagate it with `?`.
#[derive(Debug, Clone, PartialEq)]
pub enum Interrupt {
    /// The run waits; the session parks it and returns.
    Suspend(Wait),
    /// The run was cancelled.
    Cancelled,
    /// The run's deadline passed.
    Expired,
    /// A bound was reached.
    Limited(LimitKind),
    /// The run cannot continue.
    Unrecoverable(String),
    /// The session cannot go on (its lease was lost, or the ledger failed); nothing more is written.
    Lost(Error),
}

impl From<Error> for Interrupt {
    fn from(error: Error) -> Self {
        match error {
            Error::Limit(kind) => Self::Limited(kind),
            other => Self::Lost(other),
        }
    }
}

/// What a finished step returned.
#[derive(Debug, Clone, PartialEq)]
pub enum StepResult {
    /// The step's result.
    Ok(Output),
    /// The step failed for good (no retry left, or not retryable).
    Failed(Failure),
    /// An interrupted non-idempotent activity: it may or may not have acted.
    Uncertain,
}

/// A successful step's result.
#[derive(Debug, Clone, PartialEq)]
pub struct Output {
    /// The result value.
    pub value: Value,
    /// Transient blobs the step produced (readable through [`Ctx::blob`] until released).
    pub blobs: Vec<u64>,
}

impl StepResult {
    /// The result value, or `None` when the step failed or is uncertain.
    #[must_use]
    pub fn value(&self) -> Option<&Value> {
        match self {
            Self::Ok(output) => Some(&output.value),
            Self::Failed(_) | Self::Uncertain => None,
        }
    }
}

/// How a drive call left the run.
#[derive(Debug, Clone, PartialEq)]
pub enum Driven {
    /// The run ended.
    Ended(Outcome),
    /// The run waits.
    Suspended(Wait),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn limits_become_limited_and_other_errors_lose_the_session() {
        assert_eq!(Interrupt::from(Error::Limit(LimitKind::History)), Interrupt::Limited(LimitKind::History));
        assert_eq!(Interrupt::from(Error::Busy("b".into())), Interrupt::Lost(Error::Busy("b".into())));
    }

    #[test]
    fn only_successful_steps_have_values() {
        let ok = StepResult::Ok(Output { value: Value::from(1), blobs: vec![] });
        assert_eq!(ok.value(), Some(&Value::from(1)));
        assert_eq!(StepResult::Failed(Failure::permanent("a", "b")).value(), None);
        assert_eq!(StepResult::Uncertain.value(), None);
    }
}
