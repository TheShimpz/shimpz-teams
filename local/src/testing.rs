//! An in-memory Docker daemon for tests (feature `testing`). Never compiled into a shipped binary.
//!
//! It keeps what was created and answers inspections derived from it; a test seeds the daemon information, images,
//! and outages it needs. Workstreams extend it as their tests need more of the daemon's behavior.

use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard, PoisonError};

use bollard::models::{
    ContainerConfig, ContainerCreateBody, ContainerInspectResponse, ContainerState,
    ContainerSummary, ImageInspect, Network, NetworkCreateRequest, NetworkInspect, SystemInfo,
};
use team_core::future::BoxFuture;

use crate::docker::{DockerEngine, DockerError, Labels};

/// The in-memory daemon.
#[derive(Debug, Default)]
pub struct FakeDocker {
    state: Mutex<State>,
}

#[derive(Debug, Default)]
struct State {
    info: SystemInfo,
    containers: Vec<Container>,
    networks: Vec<NetworkInspect>,
    images: HashMap<String, ImageInspect>,
    pulled: Vec<String>,
    next: u64,
    unavailable: bool,
}

#[derive(Clone, Debug)]
struct Container {
    id: String,
    name: String,
    body: ContainerCreateBody,
    running: bool,
}

fn carries(labels: Option<&HashMap<String, String>>, wanted: Labels<'_>) -> bool {
    wanted.iter().all(|(name, value)| {
        labels
            .and_then(|labels| labels.get(*name))
            .is_some_and(|found| found == value)
    })
}

impl FakeDocker {
    fn state(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn reachable(&self) -> Result<MutexGuard<'_, State>, DockerError> {
        let state = self.state();
        if state.unavailable {
            Err(DockerError::Unavailable)
        } else {
            Ok(state)
        }
    }

    /// Sets what [`DockerEngine::info`] answers.
    pub fn set_info(&self, info: SystemInfo) {
        self.state().info = info;
    }

    /// Makes every later call fail as an unreachable daemon (`true`) or succeed again (`false`).
    pub fn set_unavailable(&self, unavailable: bool) {
        self.state().unavailable = unavailable;
    }

    /// Makes `image` inspectable as `reference`.
    pub fn add_image(&self, reference: &str, image: ImageInspect) {
        self.state().images.insert(reference.to_owned(), image);
    }

    /// Every reference pulled so far.
    #[must_use]
    pub fn pulled(&self) -> Vec<String> {
        self.state().pulled.clone()
    }

    /// The creation body and running state of the container named `name`.
    #[must_use]
    pub fn container(&self, name: &str) -> Option<(ContainerCreateBody, bool)> {
        let state = self.state();
        state
            .containers
            .iter()
            .find(|container| container.name == name)
            .map(|found| (found.body.clone(), found.running))
    }

    fn find(state: &State, id: &str) -> Result<Container, DockerError> {
        state
            .containers
            .iter()
            .find(|container| container.id == id || container.name == id)
            .cloned()
            .ok_or(DockerError::NotFound)
    }

    fn set_running(&self, id: &str, running: bool) -> Result<(), DockerError> {
        let mut state = self.reachable()?;
        let container = state
            .containers
            .iter_mut()
            .find(|container| container.id == id || container.name == id)
            .ok_or(DockerError::NotFound)?;
        container.running = running;
        Ok(())
    }
}

fn inspection(container: &Container) -> ContainerInspectResponse {
    let body = &container.body;
    ContainerInspectResponse {
        id: Some(container.id.clone()),
        name: Some(format!("/{}", container.name)),
        image: body.image.clone(),
        state: Some(ContainerState {
            running: Some(container.running),
            ..ContainerState::default()
        }),
        host_config: body.host_config.clone(),
        config: Some(ContainerConfig {
            image: body.image.clone(),
            user: body.user.clone(),
            env: body.env.clone(),
            labels: body.labels.clone(),
            ..ContainerConfig::default()
        }),
        ..ContainerInspectResponse::default()
    }
}

fn summary(container: &Container) -> ContainerSummary {
    ContainerSummary {
        id: Some(container.id.clone()),
        names: Some(vec![format!("/{}", container.name)]),
        image: container.body.image.clone(),
        labels: container.body.labels.clone(),
        ..ContainerSummary::default()
    }
}

fn listed(network: &NetworkInspect) -> Network {
    Network {
        name: network.name.clone(),
        id: network.id.clone(),
        driver: network.driver.clone(),
        internal: network.internal,
        attachable: network.attachable,
        labels: network.labels.clone(),
        ..Network::default()
    }
}

impl DockerEngine for FakeDocker {
    fn info(&self) -> BoxFuture<'_, Result<SystemInfo, DockerError>> {
        Box::pin(async move { Ok(self.reachable()?.info.clone()) })
    }

    fn list_containers<'a>(
        &'a self,
        labels: Labels<'a>,
    ) -> BoxFuture<'a, Result<Vec<ContainerSummary>, DockerError>> {
        Box::pin(async move {
            let state = self.reachable()?;
            Ok(state
                .containers
                .iter()
                .filter(|found| carries(found.body.labels.as_ref(), labels))
                .map(summary)
                .collect())
        })
    }

    fn inspect_container<'a>(
        &'a self,
        id: &'a str,
    ) -> BoxFuture<'a, Result<ContainerInspectResponse, DockerError>> {
        Box::pin(async move {
            let state = self.reachable()?;
            Ok(inspection(&Self::find(&state, id)?))
        })
    }

    fn create_container<'a>(
        &'a self,
        name: &'a str,
        body: ContainerCreateBody,
    ) -> BoxFuture<'a, Result<String, DockerError>> {
        Box::pin(async move {
            let mut state = self.reachable()?;
            if state
                .containers
                .iter()
                .any(|container| container.name == name)
            {
                return Err(DockerError::Conflict);
            }
            state.next += 1;
            let id = format!("{:064x}", state.next);
            state.containers.push(Container {
                id: id.clone(),
                name: name.to_owned(),
                body,
                running: false,
            });
            Ok(id)
        })
    }

    fn start_container<'a>(&'a self, id: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move { self.set_running(id, true) })
    }

    fn restart_container<'a>(
        &'a self,
        id: &'a str,
        _grace_seconds: i32,
    ) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move { self.set_running(id, true) })
    }

    fn remove_container<'a>(&'a self, id: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            let mut state = self.reachable()?;
            let before = state.containers.len();
            state
                .containers
                .retain(|container| container.id != id && container.name != id);
            if state.containers.len() == before {
                Err(DockerError::NotFound)
            } else {
                Ok(())
            }
        })
    }

    fn list_networks<'a>(
        &'a self,
        labels: Labels<'a>,
    ) -> BoxFuture<'a, Result<Vec<Network>, DockerError>> {
        Box::pin(async move {
            let state = self.reachable()?;
            Ok(state
                .networks
                .iter()
                .filter(|network| carries(network.labels.as_ref(), labels))
                .map(listed)
                .collect())
        })
    }

    fn inspect_network<'a>(
        &'a self,
        id: &'a str,
    ) -> BoxFuture<'a, Result<NetworkInspect, DockerError>> {
        Box::pin(async move {
            let state = self.reachable()?;
            let found = state.networks.iter().find(|network| {
                network.id.as_deref() == Some(id) || network.name.as_deref() == Some(id)
            });
            found.cloned().ok_or(DockerError::NotFound)
        })
    }

    fn create_network(
        &self,
        request: NetworkCreateRequest,
    ) -> BoxFuture<'_, Result<String, DockerError>> {
        Box::pin(async move {
            let mut state = self.reachable()?;
            if state
                .networks
                .iter()
                .any(|network| network.name.as_deref() == Some(request.name.as_str()))
            {
                return Err(DockerError::Conflict);
            }
            state.next += 1;
            let id = format!("{:064x}", state.next);
            state.networks.push(NetworkInspect {
                name: Some(request.name),
                id: Some(id.clone()),
                driver: request.driver,
                internal: request.internal,
                attachable: request.attachable,
                labels: request.labels,
                ..NetworkInspect::default()
            });
            Ok(id)
        })
    }

    fn remove_network<'a>(&'a self, id: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            let mut state = self.reachable()?;
            let before = state.networks.len();
            state.networks.retain(|network| {
                network.id.as_deref() != Some(id) && network.name.as_deref() != Some(id)
            });
            if state.networks.len() == before {
                Err(DockerError::NotFound)
            } else {
                Ok(())
            }
        })
    }

    fn inspect_image<'a>(
        &'a self,
        reference: &'a str,
    ) -> BoxFuture<'a, Result<ImageInspect, DockerError>> {
        Box::pin(async move {
            self.reachable()?
                .images
                .get(reference)
                .cloned()
                .ok_or(DockerError::NotFound)
        })
    }

    fn pull_image<'a>(&'a self, reference: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            let mut state = self.reachable()?;
            if !state.images.contains_key(reference) {
                return Err(DockerError::NotFound);
            }
            state.pulled.push(reference.to_owned());
            Ok(())
        })
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use bollard::models::{ContainerCreateBody, NetworkCreateRequest};

    use super::FakeDocker;
    use crate::docker::{DockerEngine, DockerError};

    #[tokio::test]
    async fn created_resources_are_listed_by_label_inspected_and_removed() {
        let docker = FakeDocker::default();
        let labels = HashMap::from([("com.shimpz.local.managed".to_owned(), "1".to_owned())]);
        let body = ContainerCreateBody {
            labels: Some(labels.clone()),
            ..ContainerCreateBody::default()
        };
        let id = docker
            .create_container("one", body.clone())
            .await
            .expect("created");
        assert_eq!(
            docker.create_container("one", body).await,
            Err(DockerError::Conflict)
        );
        docker.start_container(&id).await.expect("started");
        let listed = docker
            .list_containers(&[("com.shimpz.local.managed", "1")])
            .await
            .expect("listed");
        assert_eq!(listed.len(), 1);
        let other = docker
            .list_containers(&[("com.shimpz.local.managed", "0")])
            .await;
        assert_eq!(other.map(|found| found.len()), Ok(0));
        let inspected = docker.inspect_container("one").await.expect("inspected");
        assert_eq!(inspected.state.and_then(|state| state.running), Some(true));
        docker.remove_container(&id).await.expect("removed");
        assert_eq!(
            docker.remove_container(&id).await,
            Err(DockerError::NotFound)
        );
        let request = NetworkCreateRequest {
            name: "net".to_owned(),
            labels: Some(labels),
            ..Default::default()
        };
        let network = docker.create_network(request).await.expect("created");
        assert_eq!(
            docker.inspect_network("net").await.map(|found| found.id),
            Ok(Some(network))
        );
        docker.set_unavailable(true);
        assert_eq!(docker.info().await.err(), Some(DockerError::Unavailable));
    }
}
