//! The Integrations service group (`local/chat/private.py`, `local/http/server.py`): the Team's Integration and Stored
//! Input inventories, OAuth authorization start, cancellation, and broker completion, and Stored Input clearing, each
//! over `team_action::operations` under the Team's stripe (the completion under the stripe of the Team its callback
//! state names, as uninstall and destruction take it).

use std::sync::Arc;

use strict_json::Value;
use team_action::operations::{self, Problem as OperationProblem};
use team_action::stored_input::{Declaration, StoredInputStore};
use team_core::future::BoxFuture;
use team_integrations::flow::{ActionView, AssistantView};
use team_integrations::pkce::{CallbackBinding, PkceStore};
use team_integrations::service::OAuthService;
use team_integrations::store::{Declaration as IntegrationDeclaration, IntegrationStore};

use super::team::Team;
use crate::assistant::problem::NOT_FOUND;
use crate::chat::inference::team_id;
use crate::chat::problem::internal_error;
use crate::chat::service::IntegrationChallenges;
use crate::coordination::locks::SpaceLocks;
use crate::http::operation::Operation;
use crate::http::service::{Answer, Body, Call, Problem, Service};
use crate::install::spec::AssistantSpec;

/// What the group runs with.
pub struct Integrations {
    /// The Team view.
    pub team: Arc<Team>,
    /// The Team stripes.
    pub locks: Arc<SpaceLocks>,
    /// Integration credentials.
    pub store: Arc<IntegrationStore>,
    /// Stored Inputs.
    pub stored_inputs: Arc<StoredInputStore>,
    /// The paused turns' Integration challenges.
    pub challenges: Arc<IntegrationChallenges>,
    /// The brokered OAuth flow.
    pub oauth: Arc<OAuthService>,
    /// The OAuth PKCE state.
    pub pkce: Arc<PkceStore>,
}

impl std::fmt::Debug for Integrations {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Integrations")
    }
}

fn problem(found: OperationProblem) -> Problem {
    Problem::new(found.status, found.message, found.code)
}

fn json(body: Value) -> Answer {
    Answer::Json { status: 200, body }
}

fn body(call: &Call<'_>) -> Value {
    match &call.body {
        Body::Json(value) => value.clone(),
        _ => Value::Null,
    }
}

/// The Integration flow's view of `spec`.
fn view(spec: &AssistantSpec) -> AssistantView {
    let runtime = &spec.runtime;
    AssistantView {
        assistant_id: spec.assistant_id.clone(),
        version: spec.version.clone(),
        name: spec.name.clone(),
        summary: spec.summary.clone(),
        actions: runtime
            .actions
            .iter()
            .map(|action| ActionView {
                id: action.id.clone(),
                summary: action.summary.clone(),
                integrations: action.integrations.clone(),
            })
            .collect(),
        integrations: runtime
            .integrations
            .iter()
            .map(|declared| {
                let declaration = IntegrationDeclaration {
                    provider: declared.provider.clone(),
                    scopes: declared.scopes.clone(),
                };
                (declared.id.clone(), declaration)
            })
            .collect(),
    }
}

/// The Stored Input declarations of `spec`, as its inventory lists them.
fn declarations(spec: &AssistantSpec) -> (String, Vec<(String, Declaration)>) {
    let declared = spec
        .runtime
        .stored_inputs
        .iter()
        .map(|declared| {
            let declaration = Declaration {
                kind: declared.kind.clone(),
                label: declared.label.clone(),
                description: declared.description.clone(),
            };
            (declared.id.clone(), declaration)
        })
        .collect();
    (spec.assistant_id.clone(), declared)
}

/// The consented declaration of a callback's Integration, provided its Assistant still runs its current artifact.
async fn declaration(
    view: Arc<Team>,
    binding: &CallbackBinding,
) -> Result<IntegrationDeclaration, ()> {
    let (team, assistant) = (&binding.team_id, &binding.assistant_id);
    let network = view.networks.require(team).await.map_err(|_| ())?;
    let spec = view.spec(team, assistant).await.map_err(|_| ())?;
    let declared = spec
        .runtime
        .integrations
        .iter()
        .find(|declared| declared.id == binding.integration_id)
        .ok_or(())?;
    view.containers
        .ready_for_input(team, &network.name, &spec.container_spec())
        .await
        .map_err(|_| ())?;
    Ok(IntegrationDeclaration {
        provider: declared.provider.clone(),
        scopes: declared.scopes.clone(),
    })
}

impl Integrations {
    async fn list(&self, team: &str) -> Result<Value, Problem> {
        let _stripe = self.locks.team(team).await;
        let specs = self.team.installed(team).await?;
        let views: Vec<AssistantView> = specs.iter().map(view).collect();
        operations::list_integrations(team, &views, &self.store).map_err(problem)
    }

    async fn stored(&self, team: &str) -> Result<Value, Problem> {
        let _stripe = self.locks.team(team).await;
        let specs = self.team.installed(team).await?;
        let declared: Vec<_> = specs.iter().map(declarations).collect();
        operations::list_stored_inputs(&self.stored_inputs, team, &declared).map_err(problem)
    }

    async fn clear(&self, team: &str, assistant: &str, id: &str) -> Result<Value, Problem> {
        let _stripe = self.locks.team(team).await;
        let binding = self
            .team
            .registry
            .binding(team, assistant)
            .await?
            .ok_or(NOT_FOUND)?;
        let spec = crate::install::registry::Registry::spec(&binding)?;
        let declared: Vec<String> = spec
            .runtime
            .stored_inputs
            .iter()
            .map(|declared| declared.id.clone())
            .collect();
        operations::clear_stored_input(&self.stored_inputs, team, &spec.assistant_id, &declared, id)
            .map_err(problem)
    }

    async fn authorize(&self, team: &str, challenge: &str, body: &Value) -> Result<Value, Problem> {
        // Under the Team's stripe, as Stop, destruction, and reset withdraw it: no PKCE state outlives its challenge.
        let _stripe = self.locks.team(team).await;
        let pending = self.challenges.get(team, challenge);
        operations::authorize(pending, &self.oauth, body).map_err(problem)
    }

    async fn complete(&self, body: &Value) -> Result<Value, Problem> {
        let team = operations::callback_team(&self.pkce, body).map_err(problem)?;
        // The stripe spans the declaration check, the broker exchange, and the seal: an uninstall or destruction
        // deleting these credentials either finishes first, so the declaration fails, or deletes the new seal.
        let _stripe = self.locks.team(&team).await;
        let view = Arc::clone(&self.team);
        operations::complete(&self.oauth, &team, body, move |binding| {
            Box::pin(declaration(view, binding))
        })
        .await
        .map_err(problem)
    }

    async fn answer(&self, call: &Call<'_>) -> Result<Value, Problem> {
        if call.operation() == Operation::AssistantIntegrationComplete {
            return self.complete(&body(call)).await;
        }
        let team = team_id(call)?;
        let param = |name: &str| call.route.param(name).ok_or_else(internal_error);
        match call.operation() {
            Operation::AssistantIntegrationList => self.list(team).await,
            Operation::AssistantIntegrationAuthorize => {
                self.authorize(team, param("challenge_id")?, &body(call))
                    .await
            }
            Operation::AssistantIntegrationCancel => {
                operations::cancel(&self.oauth, &body(call)).map_err(problem)
            }
            Operation::AssistantStoredInputList => self.stored(team).await,
            Operation::AssistantStoredInputClear => {
                self.clear(team, param("assistant_id")?, param("stored_input_id")?)
                    .await
            }
            _ => Err(internal_error()),
        }
    }
}

impl Service for Integrations {
    fn call<'a>(&'a self, call: Call<'a>) -> BoxFuture<'a, Result<Answer, Problem>> {
        Box::pin(async move { self.answer(&call).await.map(json) })
    }
}
