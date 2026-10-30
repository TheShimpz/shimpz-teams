//! The paused turn that waits for a person to connect Integrations (`integrations/challenges.py`): one bounded,
//! process-local, one-use challenge per Team.

use std::sync::Arc;
use std::time::Duration;

use team_core::entropy::Entropy;

use crate::challenge::{ChallengeError, ChallengeStore, Limits, MAX_PENDING_CHALLENGES};

/// An Integration challenge's lifetime: it encloses the whole brokered OAuth transaction.
pub const DEFAULT_TTL: Duration = Duration::from_secs(600);

/// One Integration an Assistant's paused Actions need: its id, provider, and sorted scopes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequiredIntegration {
    /// The Assistant-declared Integration id.
    pub id: String,
    /// The reviewed provider.
    pub provider: String,
    /// The requested scopes, sorted.
    pub scopes: Vec<String>,
}

/// What one Assistant needs connected before the paused batch may run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IntegrationRequirement {
    /// The Assistant.
    pub assistant_id: String,
    /// Its public name when the turn paused.
    pub assistant_name: String,
    /// The Actions of the batch that need these Integrations, sorted.
    pub action_ids: Vec<String>,
    /// The Integrations, exactly one in each requirement the flow builds.
    pub integrations: Vec<RequiredIntegration>,
}

/// The paused turn's challenge store: metadata is the non-empty requirement list, `P` the private continuation.
pub type IntegrationChallengeStore<P> = ChallengeStore<Vec<IntegrationRequirement>, P>;

/// The store with its default capacity and lifetime; expired continuations are dropped.
///
/// # Errors
/// [`ChallengeError::Invalid`] for limits out of range.
pub fn store<P>(
    capacity: usize,
    ttl: Duration,
    entropy: Arc<dyn Entropy>,
) -> Result<IntegrationChallengeStore<P>, ChallengeError> {
    ChallengeStore::new(
        Limits {
            capacity,
            ttl,
            retain_expired: false,
        },
        |requirements: &Vec<IntegrationRequirement>| !requirements.is_empty(),
        entropy,
    )
}

/// [`store`] with [`MAX_PENDING_CHALLENGES`] and [`DEFAULT_TTL`].
///
/// # Errors
/// Never in practice: the defaults are in range.
pub fn default_store<P>(
    entropy: Arc<dyn Entropy>,
) -> Result<IntegrationChallengeStore<P>, ChallengeError> {
    store(MAX_PENDING_CHALLENGES, DEFAULT_TTL, entropy)
}
