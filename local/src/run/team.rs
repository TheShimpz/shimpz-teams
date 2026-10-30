//! The Team as the composed controller reads it (`local/chat/state.py` `team_assistants`): its network incarnation,
//! its creation and display names, and its running Assistants admitted completely (isolation, egress, manifest, and
//! current artifact) with their Genesis, language pack, and Routine pin.

use std::collections::BTreeMap;
use std::sync::Arc;

use bollard::models::ContainerInspectResponse;
use strict_json::Value;
use team_action::runner::Binding;
use team_assistant::language::catalog_digest;
use team_core::future::BoxFuture;
use team_routine::pin::{Integration, PinnedAssistant, StoredInput};

use crate::assistant::containers::Containers;
use crate::assistant::problem::ACTION_BLOCKED;
use crate::browser::sign_in::Incarnations;
use crate::chat::ports::{ActiveAssistant, TeamScope, TeamView};
use crate::http::service::Problem;
use crate::install::admission::ImageAdmission;
use crate::install::problem::{MANIFEST_INVALID, MANIFEST_UNAVAILABLE};
use crate::install::registry::Registry;
use crate::install::spec::AssistantSpec;
use crate::lifecycle::network::TeamNetworks;
use crate::names::TeamNames;
use crate::prepare::store::TeamPresence;

/// An installed Assistant's Genesis could not be read; nothing about it is proven.
const GENESIS_UNAVAILABLE: Problem = Problem::new(
    503,
    "installed Assistant Genesis could not be verified",
    "assistant-genesis-unavailable",
);
/// An installed Assistant's Genesis failed its contract.
const GENESIS_INVALID: Problem = Problem::new(
    409,
    "installed Assistant Genesis failed its contract",
    "assistant-genesis-invalid",
);

/// The Team view over the Space's networks, names, Assistant containers, registry, and image admission.
pub struct Team {
    /// The Team networks.
    pub networks: Arc<TeamNetworks>,
    /// The Team display names.
    pub names: TeamNames,
    /// The Assistant containers.
    pub containers: Arc<Containers>,
    /// The publication registry.
    pub registry: Arc<Registry>,
    /// Manifest, Genesis, and pack admission.
    pub admission: Arc<ImageAdmission>,
}

impl std::fmt::Debug for Team {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Team")
    }
}

/// What a Routine pins of `spec`.
#[must_use]
pub fn pinned(spec: &AssistantSpec) -> PinnedAssistant {
    let runtime = &spec.runtime;
    let actions = match runtime.machine_contract.get("actions") {
        Some(Value::Array(actions)) => actions.clone(),
        _ => Vec::new(),
    };
    let integrations: BTreeMap<String, Integration> = runtime
        .integrations
        .iter()
        .map(|declared| {
            let integration = Integration {
                provider: declared.provider.clone(),
                scopes: declared.scopes.clone(),
            };
            (declared.id.clone(), integration)
        })
        .collect();
    let stored_inputs: BTreeMap<String, StoredInput> = runtime
        .stored_inputs
        .iter()
        .map(|declared| {
            let stored = StoredInput {
                kind: declared.kind.clone(),
                label: declared.label.clone(),
                description: declared.description.clone(),
                help_url: Some(declared.help_url.clone()),
            };
            (declared.id.clone(), stored)
        })
        .collect();
    PinnedAssistant {
        assistant_id: spec.assistant_id.clone(),
        version: spec.version.clone(),
        image: spec.image.clone(),
        image_labels: spec.required_image_labels.clone(),
        allowed_hosts: runtime.allowed_hosts.clone(),
        actions,
        integrations,
        stored_inputs,
        catalog: catalog_digest(&runtime.machine_contract),
        pack_digest: spec.pack_digest.clone(),
    }
}

/// The Action executor's binding of `spec`'s admitted container `container`.
#[must_use]
pub fn binding(spec: &AssistantSpec, container: &str) -> Binding {
    Binding {
        contract: Arc::new(spec.runtime.clone()),
        assistant_id: spec.assistant_id.clone(),
        name: spec.name.clone(),
        version: spec.version.clone(),
        summary: spec.summary.clone(),
        container: container.to_owned(),
        image: spec.image.clone(),
    }
}

fn genesis_problem(problem: Problem) -> Problem {
    if problem == MANIFEST_UNAVAILABLE {
        GENESIS_UNAVAILABLE
    } else if problem == MANIFEST_INVALID {
        GENESIS_INVALID
    } else {
        problem
    }
}

impl Team {
    /// The bound spec of `assistant_id` in `team_id`.
    ///
    /// # Errors
    /// `assistant-registry-drift` when it has no binding, or the registry's problem.
    pub async fn spec(&self, team_id: &str, assistant_id: &str) -> Result<AssistantSpec, Problem> {
        let binding = self
            .registry
            .binding(team_id, assistant_id)
            .await?
            .ok_or(crate::assistant::problem::REGISTRY_DRIFT)?;
        Registry::spec(&binding)
    }

    /// One bound container admitted completely, with what a turn reads of it; `None` when it does not run.
    async fn active(
        &self,
        team_id: &str,
        network: &str,
        (inspection, container): (&ContainerInspectResponse, &crate::assistant::ContainerSpec),
    ) -> Result<Option<ActiveAssistant>, Problem> {
        let spec = self.spec(team_id, &container.assistant_id).await?;
        let admitted = self
            .containers
            .admit(inspection, team_id, network, container, true)
            .await?;
        if self.containers.is_blocked(&admitted.id) {
            return Err(ACTION_BLOCKED);
        }
        if !admitted.running {
            return Ok(None);
        }
        let genesis = self
            .admission
            .genesis_text(&admitted.id)
            .await
            .map_err(genesis_problem)?;
        let pack = self.admission.pack(&admitted.id, &spec).await?;
        Ok(Some(ActiveAssistant {
            binding: binding(&spec, &admitted.id),
            genesis,
            pack: Arc::new(pack),
            pinned: Arc::new(pinned(&spec)),
        }))
    }

    /// The Team's running Assistants, sorted by id; one absent is proven not running.
    ///
    /// # Errors
    /// Docker's, the registry's, and every admission refusal.
    pub async fn running(
        &self,
        team_id: &str,
        network: &str,
    ) -> Result<Vec<ActiveAssistant>, Problem> {
        let mut running = Vec::new();
        for (inspection, container) in self
            .containers
            .bound(team_id, self.registry.as_ref())
            .await?
        {
            if let Some(active) = self
                .active(team_id, network, (&inspection, &container))
                .await?
            {
                running.push(active);
            }
        }
        running.sort_by(|left, right| left.id().cmp(right.id()));
        Ok(running)
    }

    /// The bound specs of the Team's Assistant containers, whatever their state, after the ownership gates.
    ///
    /// # Errors
    /// `team-not-found`, Docker's, the registry's, and every isolation refusal.
    pub async fn installed(&self, team_id: &str) -> Result<Vec<AssistantSpec>, Problem> {
        self.networks.require(team_id).await?;
        let mut specs = Vec::new();
        for (_, container) in self
            .containers
            .bound(team_id, self.registry.as_ref())
            .await?
        {
            specs.push(self.spec(team_id, &container.assistant_id).await?);
        }
        Ok(specs)
    }
}

impl TeamView for Team {
    fn network<'a>(&'a self, team_id: &'a str) -> BoxFuture<'a, Result<String, Problem>> {
        Box::pin(async move { Ok(self.networks.require(team_id).await?.id) })
    }

    fn scope<'a>(
        &'a self,
        team_id: &'a str,
        scan: bool,
    ) -> BoxFuture<'a, Result<TeamScope, Problem>> {
        Box::pin(async move {
            let network = self.networks.require(team_id).await?;
            let assistants = if scan {
                self.running(team_id, &network.name).await?
            } else {
                Vec::new()
            };
            Ok(TeamScope {
                team_name: network.label_name,
                network_id: network.id,
                assistants,
            })
        })
    }

    fn teams(&self) -> BoxFuture<'_, Result<Vec<(String, String)>, Problem>> {
        Box::pin(async move {
            Ok(self
                .networks
                .list()
                .await?
                .into_iter()
                .map(|network| (network.team_id, network.id))
                .collect())
        })
    }

    fn display_name(&self, team_id: &str, network_id: &str) -> Result<Option<String>, Problem> {
        self.names.load(team_id, network_id)
    }
}

impl TeamPresence for Team {
    fn require<'a>(&'a self, team_id: &'a str) -> BoxFuture<'a, Result<(), Problem>> {
        Box::pin(async move { self.networks.require(team_id).await.map(|_| ()) })
    }
}

impl Incarnations for Team {
    fn current<'a>(&'a self, team_id: &'a str) -> BoxFuture<'a, Option<String>> {
        Box::pin(async move {
            self.networks
                .find(team_id)
                .await
                .ok()
                .flatten()
                .map(|network| network.id)
        })
    }
}
