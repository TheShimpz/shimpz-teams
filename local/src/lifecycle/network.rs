//! Team networks: one internal, non-attachable bridge per Team, named and labelled for its Space and Team. A network
//! that differs in any of these is never the Team's: it is an ownership conflict, never adopted or removed.

use std::collections::HashMap;
use std::sync::Arc;

use bollard::models::{Network, NetworkCreateRequest, NetworkInspect};
use team_core::future::BoxFuture;

use crate::coordination::residue::{ResidueKind, TeamRef, TeamTeardown, TeardownFailed};
use crate::docker::labels::{self, Space, TEAM, TEAM_NAME, filter, label_map};
use crate::docker::problem::{DOCKER_UNAVAILABLE, OWNERSHIP_CONFLICT};
use crate::docker::{DockerEngine, DockerError};
use crate::http::service::Problem;
use crate::names::label_name;

/// The ownership kind of a Team network.
pub const KIND: &str = "team";

/// A Team that does not exist.
pub const TEAM_NOT_FOUND: Problem = Problem::new(404, "Team not found", "team-not-found");

/// One validated Team network.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TeamNetwork {
    /// The Team's id.
    pub team_id: String,
    /// The network's id: the Team's incarnation.
    pub id: String,
    /// The network's name.
    pub name: String,
    /// The Team's creation name, from its label.
    pub label_name: String,
    /// Docker's creation instant, as Docker wrote it.
    pub created: Option<String>,
}

/// The attributes a list entry and an inspection share.
struct Attributes<'a> {
    name: Option<&'a String>,
    id: Option<&'a String>,
    driver: Option<&'a String>,
    internal: Option<bool>,
    attachable: Option<bool>,
    labels: Option<&'a HashMap<String, String>>,
    created: Option<&'a String>,
}

impl<'a> From<&'a Network> for Attributes<'a> {
    fn from(network: &'a Network) -> Self {
        Self {
            name: network.name.as_ref(),
            id: network.id.as_ref(),
            driver: network.driver.as_ref(),
            internal: network.internal,
            attachable: network.attachable,
            labels: network.labels.as_ref(),
            created: network.created.as_ref(),
        }
    }
}

impl<'a> From<&'a NetworkInspect> for Attributes<'a> {
    fn from(network: &'a NetworkInspect) -> Self {
        Self {
            name: network.name.as_ref(),
            id: network.id.as_ref(),
            driver: network.driver.as_ref(),
            internal: network.internal,
            attachable: network.attachable,
            labels: network.labels.as_ref(),
            created: network.created.as_ref(),
        }
    }
}

/// The Space's Team networks.
#[derive(Clone)]
pub struct TeamNetworks {
    docker: Arc<dyn DockerEngine>,
    space: Space,
}

impl std::fmt::Debug for TeamNetworks {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("TeamNetworks")
            .finish_non_exhaustive()
    }
}

impl TeamNetworks {
    /// The Team networks of `space` on `docker`.
    #[must_use]
    pub fn new(docker: Arc<dyn DockerEngine>, space: Space) -> Self {
        Self { docker, space }
    }

    /// The Space.
    #[must_use]
    pub fn space(&self) -> &Space {
        &self.space
    }

    fn validate(&self, team_id: &str, attributes: &Attributes<'_>) -> Result<TeamNetwork, Problem> {
        let expected = self.space.labels(KIND, Some(team_id));
        let name = self.space.network_name(team_id);
        let holds = labels::include(attributes.labels, &expected)
            && attributes.name == Some(&name)
            && attributes.driver.map(String::as_str) == Some("bridge")
            && attributes.internal == Some(true)
            && attributes.attachable == Some(false);
        let label = attributes
            .labels
            .and_then(|labels| label_name(labels.get(TEAM_NAME)));
        match (holds, label, attributes.id) {
            (true, Some(label_name), Some(id)) => Ok(TeamNetwork {
                team_id: team_id.to_owned(),
                id: id.clone(),
                name,
                label_name,
                created: attributes.created.cloned(),
            }),
            _ => Err(OWNERSHIP_CONFLICT),
        }
    }

    /// The Team's validated network, or `None` when it is proven absent.
    ///
    /// # Errors
    /// `docker-unavailable` (a Docker failure proves nothing about absence); `ownership-conflict`.
    pub async fn find(&self, team_id: &str) -> Result<Option<TeamNetwork>, Problem> {
        match self
            .docker
            .inspect_network(&self.space.network_name(team_id))
            .await
        {
            Ok(network) => self
                .validate(team_id, &Attributes::from(&network))
                .map(Some),
            Err(DockerError::NotFound) => Ok(None),
            Err(_) => Err(DOCKER_UNAVAILABLE),
        }
    }

    /// The Team's validated network.
    ///
    /// # Errors
    /// `team-not-found`, and as [`TeamNetworks::find`].
    pub async fn require(&self, team_id: &str) -> Result<TeamNetwork, Problem> {
        self.find(team_id).await?.ok_or(TEAM_NOT_FOUND)
    }

    /// Every network carrying the Space's Team labels, each validated as its labelled Team's.
    ///
    /// # Errors
    /// `docker-unavailable`; `ownership-conflict` for a labelled network that is not exactly a Team's.
    pub async fn list(&self) -> Result<Vec<TeamNetwork>, Problem> {
        let labels = self.space.labels(KIND, None);
        let networks = self
            .docker
            .list_networks(&filter(&labels))
            .await
            .map_err(|_| DOCKER_UNAVAILABLE)?;
        networks
            .iter()
            .map(|network| {
                let team_id = network
                    .labels
                    .as_ref()
                    .and_then(|labels| labels.get(TEAM))
                    .filter(|team_id| crate::lifecycle::team_id_valid(team_id))
                    .ok_or(OWNERSHIP_CONFLICT)?;
                self.validate(team_id, &Attributes::from(network))
            })
            .collect()
    }

    /// Creates the Team's network labelled with its creation name and returns it validated, and whether this call
    /// created it: when Docker refuses, a network a concurrent creator made is returned instead, once proven the
    /// Team's.
    ///
    /// # Errors
    /// `docker-create-failed` when none exists; `ownership-conflict`; `docker-unavailable`.
    pub async fn create(
        &self,
        team_id: &str,
        team_name: &str,
    ) -> Result<(TeamNetwork, bool), Problem> {
        let mut labels = self.space.labels(KIND, Some(team_id));
        labels.push((TEAM_NAME, team_name.to_owned()));
        let request = NetworkCreateRequest {
            name: self.space.network_name(team_id),
            driver: Some("bridge".to_owned()),
            internal: Some(true),
            attachable: Some(false),
            labels: Some(label_map(&labels)),
            ..NetworkCreateRequest::default()
        };
        if self.docker.create_network(request).await.is_err() {
            let concurrent = self.find(team_id).await?.ok_or(Problem::new(
                503,
                "Docker could not create the Team",
                "docker-create-failed",
            ))?;
            return Ok((concurrent, false));
        }
        Ok((self.require(team_id).await?, true))
    }

    /// Removes the network named for `team_id`; absence is success.
    ///
    /// # Errors
    /// [`DockerError`] when it may remain.
    pub async fn remove(&self, team_id: &str) -> Result<(), DockerError> {
        match self
            .docker
            .remove_network(&self.space.network_name(team_id))
            .await
        {
            Ok(()) | Err(DockerError::NotFound) => Ok(()),
            Err(error) => Err(error),
        }
    }
}

impl TeamTeardown for TeamNetworks {
    fn kinds(&self) -> &'static [ResidueKind] {
        &[ResidueKind::TeamNetworks]
    }

    fn remove_team<'a>(&'a self, team: &'a TeamRef) -> BoxFuture<'a, Result<(), TeardownFailed>> {
        Box::pin(async move {
            if team.generation.is_none() {
                return Ok(());
            }
            self.remove(&team.team_id)
                .await
                .map_err(|_| TeardownFailed::Failed)
        })
    }

    fn remove_space(&self) -> BoxFuture<'_, Result<(), TeardownFailed>> {
        Box::pin(async move {
            let networks = self.list().await.map_err(|problem| {
                if problem == OWNERSHIP_CONFLICT {
                    TeardownFailed::OwnershipConflict
                } else {
                    TeardownFailed::Failed
                }
            })?;
            for network in networks {
                self.remove(&network.team_id)
                    .await
                    .map_err(|_| TeardownFailed::Failed)?;
            }
            Ok(())
        })
    }
}
