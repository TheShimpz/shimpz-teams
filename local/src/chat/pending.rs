//! The secret-free state that replays one paused Team turn: a chat turn's, or a frozen Routine run's.

use strict_json::Value;
use team_action::challenges::HumanRequirement;
use team_action::human::ActionTranscript;
use team_chat::orchestrator::ChatContinuation;
use team_inference::memory::Source;
use team_inference::usage::TurnUsage;
use team_integrations::pending::IntegrationRequirement;

/// One Assistant of an identity: its id, its exact release image, and its container.
pub type IdentityAssistant = (String, String, String);

/// The Team a paused turn bound: a chat turn's whole context, or a frozen Routine run's network and Assistants
/// alone, which never names the Team's name or model (ADR-0092).
#[derive(Clone, Debug, PartialEq)]
pub enum Identity {
    /// A chat turn's.
    Chat {
        /// The Team's immutable creation name.
        team_name: String,
        /// The network incarnation.
        network_id: String,
        /// Each selected Assistant.
        assistants: Vec<IdentityAssistant>,
        /// Each selected file's metadata (`id`, `name`, `media_type`, `size`, `sha256`).
        files: Vec<Value>,
        /// The Team's provider and model.
        inference: (String, String),
    },
    /// A frozen Routine run's.
    Routine {
        /// The network incarnation.
        network_id: String,
        /// Each Assistant the run names.
        assistants: Vec<IdentityAssistant>,
    },
}

impl Identity {
    /// The network incarnation it bound.
    #[must_use]
    pub fn network_id(&self) -> &str {
        match self {
            Self::Chat { network_id, .. } | Self::Routine { network_id, .. } => network_id,
        }
    }

    /// The Assistants it bound.
    #[must_use]
    pub fn assistants(&self) -> &[IdentityAssistant] {
        match self {
            Self::Chat { assistants, .. } | Self::Routine { assistants, .. } => assistants,
        }
    }
}

/// Secret-free state required to replay one paused local Team turn.
#[derive(Clone, Debug, PartialEq)]
pub struct PendingLocalChat {
    /// Where the turn resumes.
    pub continuation: ChatContinuation,
    /// The Assistants it selected, sorted.
    pub assistant_ids: Vec<String>,
    /// The files it selected.
    pub file_ids: Vec<String>,
    /// The chat turn's model provider; `None` for a frozen Routine run, which asks no model.
    pub provider: Option<String>,
    /// The Team it bound.
    pub identity: Identity,
    /// The memory-only replay responses of its unfinished Actions.
    pub transcripts: Vec<ActionTranscript>,
    /// The human requests the turn answered.
    pub requests_used: usize,
    /// The interface language the turn's start pinned (ADR-0091).
    pub locale: Option<String>,
    /// What a paused chat turn consumed so far (ADR-0082); a Routine run carries none.
    pub usage: Option<TurnUsage>,
    /// The id of the Action batch a human request paused, which ending the turn removes exactly.
    pub paused_batch: Option<String>,
    /// The memory-only recording a new turn may define a Routine from (ADR-0101).
    pub recording: Option<String>,
    /// The model the turn's start ran on, which every resume runs and is metered on.
    pub model: Option<String>,
    /// The chat turn's author, captured at its start (ADR-0121).
    pub source: Option<Source>,
}

/// What a paused turn waits for.
#[derive(Clone, Debug, PartialEq)]
pub enum Requirements {
    /// Integrations its batch needs.
    Integrations(Vec<IntegrationRequirement>),
    /// One person's answer to one Action's request.
    Human(Box<HumanRequirement>),
}

impl Requirements {
    /// The continuation kind.
    #[must_use]
    pub const fn kind(&self) -> &'static str {
        match self {
            Self::Integrations(_) => "integrations",
            Self::Human(_) => "human",
        }
    }
}

/// The id of the Action batch a human request paused: the SHA-256 of its generation, thread, and interrupts, so ending
/// the turn removes exactly that batch.
#[must_use]
pub fn paused_batch_id(
    scope: &(String, String),
    actions: &[team_inference::runtime::ActionRequest],
) -> String {
    use sha2::{Digest, Sha256};
    let mut identity = vec![
        Value::String(scope.0.clone()),
        Value::String(scope.1.clone()),
    ];
    identity.extend(
        actions
            .iter()
            .map(|request| Value::String(request.interrupt_id.clone())),
    );
    let canonical = team_protocol::python::json::canonical(&Value::Array(identity));
    super::hex(&Sha256::digest(canonical))
}
