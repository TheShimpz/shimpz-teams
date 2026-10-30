//! The seam every Brain call goes through (frozen in W0).

use strict_json::Value;
use team_core::future::BoxFuture;
use team_core::secret::Secret;
use team_core::stop::Stop;
use tokio::time::Instant;

/// One Brain operation, by the path Team posts it to.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum BrainOperation {
    /// Start one turn.
    Turn,
    /// Resume a suspended turn with its Action results.
    Resume,
    /// One Action request's plain purpose.
    Purpose,
    /// Delete one Brain thread.
    DeleteThread,
    /// Select a bounded subset of public candidates.
    CapabilityPlan,
    /// Classify one lifecycle intent.
    IntentRoute,
    /// The Browser task's next step.
    BrowserStep,
    /// Fold the Browser task's thread into one memory.
    BrowserFold,
    /// Check whether the Browser task is complete.
    BrowserCheck,
    /// The Browser task's final summary.
    BrowserFinal,
}

impl BrainOperation {
    /// Every operation.
    pub const ALL: [Self; 10] = [
        Self::Turn,
        Self::Resume,
        Self::Purpose,
        Self::DeleteThread,
        Self::CapabilityPlan,
        Self::IntentRoute,
        Self::BrowserStep,
        Self::BrowserFold,
        Self::BrowserCheck,
        Self::BrowserFinal,
    ];

    /// The Brain path Team posts the operation to.
    #[must_use]
    pub const fn path(self) -> &'static str {
        match self {
            Self::Turn => "/v1/turns",
            Self::Resume => "/v1/turns/resume",
            Self::Purpose => "/v1/turns/purpose",
            Self::DeleteThread => "/v1/threads/delete",
            Self::CapabilityPlan => "/v1/capability-plan",
            Self::IntentRoute => "/v1/intent-route",
            Self::BrowserStep => "/v1/browser-step",
            Self::BrowserFold => "/v1/browser-fold",
            Self::BrowserCheck => "/v1/browser-check",
            Self::BrowserFinal => "/v1/browser-final",
        }
    }
}

/// The request-only model credential Admin supplies with a model-bound operation; never persisted or forwarded to a
/// workload.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ModelCredential {
    /// The provider (`openai` or `anthropic`).
    pub provider: String,
    /// The Team's selected model.
    pub model: String,
    /// The provider API key.
    pub api_key: Secret,
}

/// One Brain call.
#[derive(Debug)]
pub struct BrainRequest<'a> {
    /// The operation.
    pub operation: BrainOperation,
    /// Its body without the credential; the client places the credential where the operation carries it.
    pub body: Value,
    /// The model credential, for a model-bound operation.
    pub credential: Option<&'a ModelCredential>,
    /// When the call is abandoned (its connection closed) if it has not answered.
    pub deadline: Instant,
    /// The turn's or task's Stop: setting it closes the connection, which cancels the Brain's provider I/O.
    pub stop: &'a Stop,
}

/// A closed Brain failure; it never carries the Brain's text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BrainError {
    /// The Stop was set; the request was abandoned.
    Stopped,
    /// The deadline passed; the request was abandoned.
    Deadline,
    /// The Brain could not be reached, or its token is unavailable.
    Unavailable,
    /// The Brain answered this non-success HTTP status.
    Rejected(u16),
    /// The Brain's answer violates the protocol.
    Protocol,
}

/// The Brain runtime as Team calls it.
pub trait BrainClient: Send + Sync {
    /// Posts one operation and returns its strictly parsed answer.
    ///
    /// # Errors
    /// [`BrainError`] for every unsuccessful outcome.
    fn call<'a>(&'a self, request: BrainRequest<'a>) -> BoxFuture<'a, Result<Value, BrainError>>;
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;

    use super::BrainOperation;

    #[test]
    fn every_operation_has_its_own_path() {
        let paths: HashSet<_> = BrainOperation::ALL
            .iter()
            .map(|operation| operation.path())
            .collect();
        assert_eq!(paths.len(), BrainOperation::ALL.len());
    }
}
