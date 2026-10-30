//! What the chat and Routine services read of the rest of the controller: the Team as a turn runs against it (its
//! network incarnation, its running Assistants with their Genesis and language packs), and the replay transcripts the
//! Action executor replays a paused Action with.

use std::sync::Arc;

use assistant_protocol::human_request::AUTHORIZATION_KINDS;
use team_action::human::ActionTranscript;
use team_action::runner::{Binding, Runner};
use team_assistant::language::LanguagePack;
use team_assistant::spec::ActionSpec;
use team_core::future::BoxFuture;
use team_inference::runtime::{RuntimeAction, RuntimeAssistant, contract_digest};
use team_integrations::flow::{ActionView, AssistantView};
use team_integrations::store::Declaration;
use team_routine::pin::{PinnedAssistant, assistant_pin};

use crate::http::service::Problem;

/// One installed Assistant running for a Team, as a turn sees it.
#[derive(Clone, Debug)]
pub struct ActiveAssistant {
    /// Its reviewed binding and running workload.
    pub binding: Binding,
    /// Its verified Genesis.
    pub genesis: String,
    /// Its verified language pack (ADR-0091).
    pub pack: Arc<LanguagePack>,
    /// What a Routine pins of it.
    pub pinned: Arc<PinnedAssistant>,
}

/// One Action exactly as a Brain turn sees it, with its authorization and file-input declarations.
#[must_use]
pub fn runtime_action(action: &ActionSpec) -> RuntimeAction {
    RuntimeAction {
        id: action.id.clone(),
        summary: action.summary.clone(),
        input_schema: action.input_schema.clone(),
        authorization: action
            .human_requests
            .iter()
            .any(|kind| AUTHORIZATION_KINDS.contains(&kind.as_str())),
        input_files: action.input_files.clone(),
        output_schema: action.output_schema.clone(),
    }
}

impl ActiveAssistant {
    /// Its id.
    #[must_use]
    pub fn id(&self) -> &str {
        &self.binding.assistant_id
    }

    /// One of its declared Actions.
    #[must_use]
    pub fn action(&self, action_id: &str) -> Option<&ActionSpec> {
        self.binding.contract.action(action_id)
    }

    /// The Assistant exactly as a Brain turn sees it; its contract digest pins Routines.
    #[must_use]
    pub fn runtime(&self) -> RuntimeAssistant {
        let mut actions: Vec<RuntimeAction> = self
            .binding
            .contract
            .actions
            .iter()
            .map(runtime_action)
            .collect();
        actions.sort_by(|left, right| left.id.cmp(&right.id));
        RuntimeAssistant {
            id: self.binding.assistant_id.clone(),
            genesis: self.genesis.clone(),
            actions,
        }
    }

    /// The pin a Routine holds for it: its Brain-visible contract and every Action's complete pin; `None` when its
    /// contract cannot be pinned.
    #[must_use]
    pub fn routine_scope(&self) -> Option<String> {
        assistant_pin(&self.pinned, &contract_digest(&self.runtime()))
    }

    /// Its Integration view, as the authorization flow reads it.
    #[must_use]
    pub fn view(&self) -> AssistantView {
        let contract = &self.binding.contract;
        AssistantView {
            assistant_id: self.binding.assistant_id.clone(),
            version: self.binding.version.clone(),
            name: self.binding.name.clone(),
            summary: self.binding.summary.clone(),
            actions: contract
                .actions
                .iter()
                .map(|action| ActionView {
                    id: action.id.clone(),
                    summary: action.summary.clone(),
                    integrations: action.integrations.clone(),
                })
                .collect(),
            integrations: contract
                .integrations
                .iter()
                .map(|integration| {
                    let declaration = Declaration {
                        provider: integration.provider.clone(),
                        scopes: integration.scopes.clone(),
                    };
                    (integration.id.clone(), declaration)
                })
                .collect(),
        }
    }
}

/// The Team as one segment runs against it.
#[derive(Clone, Debug)]
pub struct TeamScope {
    /// Its immutable creation name.
    pub team_name: String,
    /// Its network's id: its incarnation and journal generation.
    pub network_id: String,
    /// Its running Assistants, sorted by id; one absent is proven not running.
    pub assistants: Vec<ActiveAssistant>,
}

impl TeamScope {
    /// The running Assistant `assistant_id`.
    #[must_use]
    pub fn assistant(&self, assistant_id: &str) -> Option<&ActiveAssistant> {
        self.assistants
            .iter()
            .find(|active| active.id() == assistant_id)
    }
}

/// The Team as the controller holds it. The caller holds the Team's stripe around a scope read; nothing here takes a
/// lock of its own.
pub trait TeamView: Send + Sync {
    /// The Team's network id, proving it exists.
    ///
    /// # Errors
    /// [`Problem`] for an absent Team (`404`), Docker, or an ownership failure.
    fn network<'a>(&'a self, team_id: &'a str) -> BoxFuture<'a, Result<String, Problem>>;

    /// The Team's creation name, network, and, with `scan`, its running Assistants.
    ///
    /// # Errors
    /// [`Problem`] when the Team or any running Assistant cannot be read or proven.
    fn scope<'a>(
        &'a self,
        team_id: &'a str,
        scan: bool,
    ) -> BoxFuture<'a, Result<TeamScope, Problem>>;

    /// Every Team the Space holds, with its network's id: what a Space reset removes.
    ///
    /// # Errors
    /// [`Problem`] when the Teams cannot be listed.
    fn teams(&self) -> BoxFuture<'_, Result<Vec<(String, String)>, Problem>>;

    /// The Team's current display name in its incarnation `network_id`, when it was renamed.
    ///
    /// # Errors
    /// [`Problem`] when the name record cannot be read.
    fn display_name(&self, team_id: &str, network_id: &str) -> Result<Option<String>, Problem>;
}

/// The memory-only replay transcripts the Action executor replays a paused Action with.
pub trait Transcripts: Send + Sync {
    /// Keeps the transcript a person's answer extended.
    fn set(&self, team_id: &str, transcript: ActionTranscript);

    /// Drops a Team's transcripts of `interrupts`, or all of them (`None`).
    fn clear(&self, team_id: &str, interrupts: Option<&[String]>);
}

impl Transcripts for Runner {
    fn set(&self, team_id: &str, transcript: ActionTranscript) {
        self.set_transcript(team_id, transcript);
    }

    fn clear(&self, team_id: &str, interrupts: Option<&[String]>) {
        self.clear_transcripts(team_id, interrupts);
    }
}
