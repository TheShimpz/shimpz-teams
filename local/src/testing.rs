//! An in-memory Docker daemon for tests (feature `testing`). Never compiled into a shipped binary.
//!
//! It keeps what was created and answers inspections derived from it; a test seeds the daemon information, images,
//! and outages it needs. Workstreams extend it as their tests need more of the daemon's behavior.

use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard, PoisonError};

use bollard::models::{
    ContainerConfig, ContainerCreateBody, ContainerInspectResponse, ContainerState,
    ContainerStateStatusEnum, ContainerSummary, EndpointSettings, ImageInspect, ImageSummary,
    Network, NetworkCreateRequest, NetworkInspect, NetworkSettings, SystemInfo,
};
use team_core::future::BoxFuture;

use crate::docker::{Archive, DockerEngine, DockerError, Labels};

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
    remote: HashMap<String, ImageInspect>,
    pulled: Vec<String>,
    removed_images: Vec<String>,
    archives: HashMap<(String, String), Archive>,
    next: u64,
    unavailable: bool,
}

#[derive(Clone, Debug)]
struct Container {
    id: String,
    name: String,
    body: ContainerCreateBody,
    running: bool,
    // The id of the image the reference resolved to when it was created, as Docker reports it at the top level.
    image_id: Option<String>,
    // An inspection a test substituted for the one derived from the creation body.
    inspection: Option<ContainerInspectResponse>,
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

    /// Makes `image` pullable as `reference`; it is inspectable only once pulled.
    pub fn add_remote_image(&self, reference: &str, image: ImageInspect) {
        self.state().remote.insert(reference.to_owned(), image);
    }

    /// Makes `path` of the container named `container` answer `archive` (its whole tar stream).
    pub fn add_archive(&self, container: &str, path: &str, archive: Archive) {
        self.state()
            .archives
            .insert((container.to_owned(), path.to_owned()), archive);
    }

    /// Every image removed so far, as named.
    #[must_use]
    pub fn removed_images(&self) -> Vec<String> {
        self.state().removed_images.clone()
    }

    /// Every reference pulled so far.
    #[must_use]
    pub fn pulled(&self) -> Vec<String> {
        self.state().pulled.clone()
    }

    /// Makes the container named `name` stop running, as when its process exits.
    ///
    /// # Panics
    /// When no container is named `name`.
    pub fn exit(&self, name: &str) {
        self.set_running(name, false)
            .expect("a container of that name");
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

    /// The inspection of the container named `name`, changed by `change`; later inspections answer it.
    ///
    /// # Panics
    /// When no container is named `name`.
    pub fn tamper(&self, name: &str, change: impl FnOnce(&mut ContainerInspectResponse)) {
        let mut state = self.state();
        let container = state
            .containers
            .iter_mut()
            .find(|container| container.name == name)
            .expect("a container of that name");
        let mut changed = container
            .inspection
            .take()
            .unwrap_or_else(|| inspection(container));
        change(&mut changed);
        container.inspection = Some(changed);
    }

    /// The network named `name` (or with that id), changed by `change`.
    ///
    /// # Panics
    /// When no network is named `name`.
    pub fn tamper_network(&self, name: &str, change: impl FnOnce(&mut NetworkInspect)) {
        let mut state = self.state();
        let network = state
            .networks
            .iter_mut()
            .find(|network| {
                network.name.as_deref() == Some(name) || network.id.as_deref() == Some(name)
            })
            .expect("a network of that name");
        change(network);
    }

    /// Every container's name, in creation order.
    #[must_use]
    pub fn container_names(&self) -> Vec<String> {
        self.state()
            .containers
            .iter()
            .map(|container| container.name.clone())
            .collect()
    }

    /// Every network's name, in creation order.
    #[must_use]
    pub fn network_names(&self) -> Vec<String> {
        self.state()
            .networks
            .iter()
            .filter_map(|network| network.name.clone())
            .collect()
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

/// A created container that never ran, or one that runs.
fn status(running: bool) -> ContainerStateStatusEnum {
    if running {
        ContainerStateStatusEnum::RUNNING
    } else {
        ContainerStateStatusEnum::CREATED
    }
}

fn inspection(container: &Container) -> ContainerInspectResponse {
    if let Some(substituted) = &container.inspection {
        let mut substituted = substituted.clone();
        substituted.state = Some(ContainerState {
            running: Some(container.running),
            status: Some(status(container.running)),
            ..substituted.state.unwrap_or_default()
        });
        return substituted;
    }
    let body = &container.body;
    // Docker attaches a container to the network its host configuration names, and reports no mount for a tmpfs.
    let networks = body
        .host_config
        .as_ref()
        .and_then(|host| host.network_mode.clone())
        .map(|mode| HashMap::from([(mode, EndpointSettings::default())]));
    ContainerInspectResponse {
        id: Some(container.id.clone()),
        name: Some(format!("/{}", container.name)),
        image: container.image_id.clone().or_else(|| body.image.clone()),
        state: Some(ContainerState {
            running: Some(container.running),
            status: Some(status(container.running)),
            ..ContainerState::default()
        }),
        mounts: Some(Vec::new()),
        network_settings: Some(NetworkSettings {
            networks,
            ..NetworkSettings::default()
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
        image_id: container.image_id.clone(),
        labels: container.body.labels.clone(),
        ..ContainerSummary::default()
    }
}

fn listed(network: &NetworkInspect) -> Network {
    Network {
        name: network.name.clone(),
        id: network.id.clone(),
        created: network.created.clone(),
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
            let image_id = body
                .image
                .as_ref()
                .and_then(|reference| state.images.get(reference))
                .and_then(|image| image.id.clone());
            state.containers.push(Container {
                id: id.clone(),
                name: name.to_owned(),
                body,
                running: false,
                image_id,
                inspection: None,
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
            // Each network is created one second after the one before.
            let created = format!(
                "2026-10-30T00:{:02}:{:02}Z",
                state.next / 60,
                state.next % 60
            );
            state.networks.push(NetworkInspect {
                name: Some(request.name),
                id: Some(id.clone()),
                created: Some(created),
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

    fn get_archive<'a>(
        &'a self,
        container: &'a str,
        path: &'a str,
        bound: usize,
    ) -> BoxFuture<'a, Result<Archive, DockerError>> {
        Box::pin(async move {
            let state = self.reachable()?;
            let name = Self::find(&state, container)?.name;
            let mut archive = state
                .archives
                .get(&(name, path.to_owned()))
                .cloned()
                .ok_or(DockerError::NotFound)?;
            archive.tar.truncate(bound.saturating_add(1));
            Ok(archive)
        })
    }

    fn list_images<'a>(
        &'a self,
        labels: Labels<'a>,
    ) -> BoxFuture<'a, Result<Vec<ImageSummary>, DockerError>> {
        Box::pin(async move {
            let state = self.reachable()?;
            let mut listed: Vec<ImageSummary> = Vec::new();
            for image in state.images.values() {
                let image_labels = image
                    .config
                    .as_ref()
                    .and_then(|config| config.labels.as_ref());
                let id = image.id.clone().unwrap_or_default();
                if !carries(image_labels, labels) || listed.iter().any(|known| known.id == id) {
                    continue;
                }
                listed.push(ImageSummary {
                    id,
                    repo_tags: image.repo_tags.clone().unwrap_or_default(),
                    repo_digests: image.repo_digests.clone().unwrap_or_default(),
                    labels: image_labels.cloned().unwrap_or_default(),
                    ..ImageSummary::default()
                });
            }
            listed.sort_by(|left, right| left.id.cmp(&right.id));
            Ok(listed)
        })
    }

    fn remove_image<'a>(&'a self, image: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            let mut state = self.reachable()?;
            let id = state
                .images
                .iter()
                .find(|(reference, found)| {
                    *reference == image || found.id.as_deref() == Some(image)
                })
                .and_then(|(_, found)| found.id.clone())
                .ok_or(DockerError::NotFound)?;
            if state
                .containers
                .iter()
                .any(|container| container.image_id.as_deref() == Some(id.as_str()))
            {
                return Err(DockerError::Conflict);
            }
            state
                .images
                .retain(|_, found| found.id.as_deref() != Some(id.as_str()));
            state.removed_images.push(image.to_owned());
            Ok(())
        })
    }

    fn pull_image<'a>(&'a self, reference: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            let mut state = self.reachable()?;
            if !state.images.contains_key(reference) {
                let image = state
                    .remote
                    .get(reference)
                    .cloned()
                    .ok_or(DockerError::NotFound)?;
                state.images.insert(reference.to_owned(), image);
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

    #[tokio::test]
    async fn images_list_by_label_remove_unless_used_and_archives_stop_at_their_bound() {
        use bollard::models::{ImageConfig, ImageInspect};

        use crate::docker::Archive;

        let docker = FakeDocker::default();
        let labelled = |id: &str, value: &str| ImageInspect {
            id: Some(id.to_owned()),
            config: Some(ImageConfig {
                labels: Some(HashMap::from([("stage".to_owned(), value.to_owned())])),
                ..ImageConfig::default()
            }),
            ..ImageInspect::default()
        };
        docker.add_image("one:tag", labelled("sha256:1", "a"));
        docker.add_image("two:tag", labelled("sha256:2", "b"));
        let listed = docker.list_images(&[("stage", "a")]).await.expect("listed");
        assert_eq!(
            listed
                .iter()
                .map(|image| image.id.as_str())
                .collect::<Vec<_>>(),
            ["sha256:1"]
        );
        let body = ContainerCreateBody {
            image: Some("two:tag".to_owned()),
            ..ContainerCreateBody::default()
        };
        docker
            .create_container("user", body)
            .await
            .expect("created");
        assert_eq!(
            docker.remove_image("sha256:2").await,
            Err(DockerError::Conflict)
        );
        assert_eq!(docker.remove_image("one:tag").await, Ok(()));
        assert_eq!(
            docker.remove_image("one:tag").await,
            Err(DockerError::NotFound)
        );
        assert_eq!(docker.removed_images(), ["one:tag"]);
        let archive = Archive {
            metadata: strict_json::Value::Object(Vec::new()),
            tar: b"0123456789".to_vec(),
        };
        docker.add_archive("user", "/file", archive);
        let read = docker.get_archive("user", "/file", 3).await.expect("read");
        assert_eq!(read.tar, b"0123");
        assert_eq!(
            docker.get_archive("user", "/other", 3).await.err(),
            Some(DockerError::NotFound)
        );
    }
}
