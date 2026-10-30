//! The teardown participants this workstream owns: Assistant containers, their egress policies, Team names, and the
//! runtime state the coordination itself holds. (Preparation helpers and Team networks are their modules' own.)

use std::collections::BTreeSet;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use team_core::future::BoxFuture;

use super::network::TeamNetwork;
use crate::assistant::containers::{Containers, Removed};
use crate::assistant::isolation::{self, KIND};
use crate::assistant::{Bindings, ContainerSpec, Provenance};
use crate::coordination::residue::{ResidueKind, TeamRef, TeamTeardown, TeardownFailed};
use crate::docker::labels::{self, ASSISTANT, IMAGE, Space, TEAM, filter};
use crate::docker::problem::{
    DOCKER_UNAVAILABLE, INTERNAL, SPACE_RESOURCE_OWNERSHIP_CONFLICT,
    TEAM_RESOURCES_OWNERSHIP_CONFLICT,
};
use crate::docker::{DockerEngine, DockerError};
use crate::http::service::Problem;
use crate::names::TeamNames;

/// One Assistant container a teardown removed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RemovedAssistant {
    /// Its Team.
    pub team_id: String,
    /// Its Assistant.
    pub assistant_id: String,
    /// Its binding's provenance, when it had a binding.
    pub provenance: Option<Provenance>,
    /// What its removal reported.
    pub removed: Removed,
}

/// The Assistant containers a teardown removed, shared with the participants after it (the egress policies, and the
/// publication bindings' retired-image cleanup).
#[derive(Clone, Debug, Default)]
pub struct Removals(Arc<Mutex<Vec<RemovedAssistant>>>);

impl Removals {
    fn lock(&self) -> MutexGuard<'_, Vec<RemovedAssistant>> {
        self.0.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Every removal recorded since the last [`Removals::take`].
    #[must_use]
    pub fn take(&self) -> Vec<RemovedAssistant> {
        std::mem::take(&mut *self.lock())
    }

    fn identities(&self, team_id: Option<&str>) -> Vec<(String, String)> {
        self.lock()
            .iter()
            .filter(|removed| team_id.is_none_or(|team| removed.team_id == team))
            .map(|removed| (removed.team_id.clone(), removed.assistant_id.clone()))
            .collect()
    }
}

/// A labelled Assistant container of the Space, as a teardown inventories it.
#[derive(Clone, Debug)]
struct Labelled {
    id: String,
    team_id: String,
    assistant_id: String,
}

/// The Space's Assistant containers in a teardown: inventoried and proven owned before anything changes, then
/// removed in their step.
pub struct AssistantTeardown {
    docker: Arc<dyn DockerEngine>,
    containers: Arc<Containers>,
    bindings: Arc<dyn Bindings>,
    space: Space,
    removals: Removals,
}

impl std::fmt::Debug for AssistantTeardown {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("AssistantTeardown")
            .finish_non_exhaustive()
    }
}

fn label<'a>(
    labels: Option<&'a std::collections::HashMap<String, String>>,
    name: &str,
) -> Option<&'a String> {
    labels.and_then(|labels| labels.get(name))
}

impl AssistantTeardown {
    /// The teardown of `containers`' Assistants, bound by `bindings`.
    #[must_use]
    pub fn new(
        docker: Arc<dyn DockerEngine>,
        containers: Arc<Containers>,
        bindings: Arc<dyn Bindings>,
        space: Space,
        removals: Removals,
    ) -> Self {
        Self {
            docker,
            containers,
            bindings,
            space,
            removals,
        }
    }

    async fn labelled(&self, team_id: Option<&str>) -> Result<Vec<Labelled>, DockerError> {
        let ownership = self.space.labels(KIND, team_id);
        let listed = self.docker.list_containers(&filter(&ownership)).await?;
        Ok(listed
            .into_iter()
            .map(|summary| Labelled {
                id: summary.id.unwrap_or_default(),
                team_id: label(summary.labels.as_ref(), TEAM)
                    .cloned()
                    .unwrap_or_default(),
                assistant_id: label(summary.labels.as_ref(), ASSISTANT)
                    .cloned()
                    .unwrap_or_default(),
            })
            .collect())
    }

    async fn spec(
        &self,
        team_id: &str,
        assistant_id: &str,
    ) -> Result<Option<ContainerSpec>, Problem> {
        self.bindings
            .spec(team_id, assistant_id)
            .await
            .map_err(|_| INTERNAL)
    }

    /// Proves every Assistant container of `team_id` the Team's own before its destruction changes anything: each is
    /// bound, and admitted against its profile on the Team's network (no egress or manifest admission: a retiring
    /// Assistant's contract is never required). Returns how many there are.
    ///
    /// # Errors
    /// `ownership-conflict` for an unbound or drifted container, or containers without a network;
    /// `docker-unavailable`.
    pub async fn inventory_team(
        &self,
        team_id: &str,
        network: Option<&TeamNetwork>,
    ) -> Result<usize, Problem> {
        let labelled = self
            .labelled(Some(team_id))
            .await
            .map_err(|_| DOCKER_UNAVAILABLE)?;
        for container in &labelled {
            let spec = self.spec(team_id, &container.assistant_id).await?;
            let (Some(spec), Some(network)) = (spec, network) else {
                return Err(TEAM_RESOURCES_OWNERSHIP_CONFLICT);
            };
            let inspection = self
                .docker
                .inspect_container(&container.id)
                .await
                .map_err(|_| DOCKER_UNAVAILABLE)?;
            self.containers
                .admit_profile(&inspection, team_id, &network.name, &spec)
                .map_err(|_| TEAM_RESOURCES_OWNERSHIP_CONFLICT)?;
        }
        Ok(labelled.len())
    }

    /// Proves every labelled Assistant container of the Space owned before the reset changes anything: a canonical
    /// Team and Assistant, an image label, every ownership label, and its deterministic name. Returns how many.
    ///
    /// # Errors
    /// `ownership-conflict`; `docker-reset-failed` when Docker fails.
    pub async fn inventory_space(&self) -> Result<usize, Problem> {
        let labelled = self
            .labelled(None)
            .await
            .map_err(|_| super::SPACE_RESET_FAILED)?;
        for container in &labelled {
            let inspection = self
                .docker
                .inspect_container(&container.id)
                .await
                .map_err(|_| super::SPACE_RESET_FAILED)?;
            if !self.owned(&inspection) {
                return Err(SPACE_RESOURCE_OWNERSHIP_CONFLICT);
            }
        }
        Ok(labelled.len())
    }

    fn owned(&self, inspection: &bollard::models::ContainerInspectResponse) -> bool {
        let labels = inspection
            .config
            .as_ref()
            .and_then(|config| config.labels.as_ref());
        let (Some(team_id), Some(assistant_id)) = (label(labels, TEAM), label(labels, ASSISTANT))
        else {
            return false;
        };
        let assistant_valid = team_protocol::payload::canonical_assistant_id(
            &strict_json::Value::String(assistant_id.clone()),
        )
        .is_some();
        let name = self.space.container_name(team_id, assistant_id);
        super::team_id_valid(team_id)
            && assistant_valid
            && label(labels, IMAGE).is_some()
            && labels::include(
                labels,
                &isolation::ownership(&self.space, team_id, assistant_id),
            )
            && inspection
                .name
                .as_deref()
                .and_then(|name| name.strip_prefix('/'))
                == Some(name.as_str())
    }

    async fn remove_all(&self, team_id: Option<&str>) -> Result<(), TeardownFailed> {
        let labelled = self
            .labelled(team_id)
            .await
            .map_err(|_| TeardownFailed::Failed)?;
        for container in labelled {
            let inspection = match self.docker.inspect_container(&container.id).await {
                Ok(inspection) => inspection,
                Err(DockerError::NotFound) => continue,
                Err(_) => return Err(TeardownFailed::Failed),
            };
            if !self.owned(&inspection) {
                return Err(TeardownFailed::OwnershipConflict);
            }
            let spec = self
                .spec(&container.team_id, &container.assistant_id)
                .await
                .map_err(|_| TeardownFailed::StateUnavailable)?;
            let image_id = inspection
                .image
                .clone()
                .filter(|image| team_protocol::payload::SOURCE_DIGEST_RE.is_full(image));
            let removed = self
                .containers
                .remove_container(&container.id, image_id)
                .await
                .map_err(|_| TeardownFailed::Failed)?;
            self.removals
                .0
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .push(RemovedAssistant {
                    team_id: container.team_id,
                    assistant_id: container.assistant_id,
                    provenance: spec.map(|spec| spec.provenance),
                    removed,
                });
        }
        Ok(())
    }
}

impl TeamTeardown for AssistantTeardown {
    fn kinds(&self) -> &'static [ResidueKind] {
        &[ResidueKind::AssistantContainers]
    }

    fn remove_team<'a>(&'a self, team: &'a TeamRef) -> BoxFuture<'a, Result<(), TeardownFailed>> {
        Box::pin(self.remove_all(Some(&team.team_id)))
    }

    fn remove_space(&self) -> BoxFuture<'_, Result<(), TeardownFailed>> {
        Box::pin(self.remove_all(None))
    }
}

/// The egress policies of every Assistant a teardown retires: those whose containers it removed and every bound one.
pub struct EgressTeardown {
    containers: Arc<Containers>,
    bindings: Arc<dyn Bindings>,
    removals: Removals,
}

impl std::fmt::Debug for EgressTeardown {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("EgressTeardown")
            .finish_non_exhaustive()
    }
}

impl EgressTeardown {
    /// The policies of `containers`' Assistants.
    #[must_use]
    pub fn new(
        containers: Arc<Containers>,
        bindings: Arc<dyn Bindings>,
        removals: Removals,
    ) -> Self {
        Self {
            containers,
            bindings,
            removals,
        }
    }

    async fn remove_all(&self, team_id: Option<&str>) -> Result<(), TeardownFailed> {
        let bound = self
            .bindings
            .identities()
            .await
            .map_err(|_| TeardownFailed::StateUnavailable)?;
        let mut identities: BTreeSet<(String, String)> =
            self.removals.identities(team_id).into_iter().collect();
        identities.extend(
            bound
                .into_iter()
                .filter(|(team, _)| team_id.is_none_or(|wanted| team == wanted)),
        );
        for (team, assistant) in identities {
            self.containers
                .remove_policy(&team, &assistant)
                .map_err(|problem| {
                    if problem.code == Some("egress-policy-drift") {
                        TeardownFailed::OwnershipConflict
                    } else {
                        TeardownFailed::StateUnavailable
                    }
                })?;
        }
        Ok(())
    }
}

impl TeamTeardown for EgressTeardown {
    fn kinds(&self) -> &'static [ResidueKind] {
        &[ResidueKind::EgressPolicies]
    }

    fn remove_team<'a>(&'a self, team: &'a TeamRef) -> BoxFuture<'a, Result<(), TeardownFailed>> {
        Box::pin(self.remove_all(Some(&team.team_id)))
    }

    fn remove_space(&self) -> BoxFuture<'_, Result<(), TeardownFailed>> {
        Box::pin(self.remove_all(None))
    }
}

impl TeamTeardown for TeamNames {
    fn kinds(&self) -> &'static [ResidueKind] {
        &[ResidueKind::TeamNames]
    }

    fn remove_team<'a>(&'a self, team: &'a TeamRef) -> BoxFuture<'a, Result<(), TeardownFailed>> {
        Box::pin(async move {
            self.delete(&team.team_id)
                .map_err(|_| TeardownFailed::StateUnavailable)
        })
    }

    fn remove_space(&self) -> BoxFuture<'_, Result<(), TeardownFailed>> {
        Box::pin(async move {
            self.delete_all()
                .map_err(|_| TeardownFailed::StateUnavailable)
        })
    }
}

/// The runtime state of a Team's executions: once its slot is held (destruction) or every slot drained (reset), the
/// coordination holds no execution of it, so there is nothing left to remove.
#[derive(Clone, Copy, Debug, Default)]
pub struct RuntimeState;

impl TeamTeardown for RuntimeState {
    fn kinds(&self) -> &'static [ResidueKind] {
        &[ResidueKind::RuntimeState]
    }

    fn remove_team<'a>(&'a self, _team: &'a TeamRef) -> BoxFuture<'a, Result<(), TeardownFailed>> {
        Box::pin(async { Ok(()) })
    }

    fn remove_space(&self) -> BoxFuture<'_, Result<(), TeardownFailed>> {
        Box::pin(async { Ok(()) })
    }
}
