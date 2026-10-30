//! The Docker daemon over its Unix socket: bollard for every call but exec attach and the archive endpoint, which
//! [`super::exec`] and [`super::archive`] request on their own connections. No failure carries the daemon's text:
//! each maps to one closed [`DockerError`].

use std::collections::HashMap;
use std::future::poll_fn;
use std::path::{Path, PathBuf};
use std::pin::pin;

use bollard::errors::Error as BollardError;
use bollard::models::{
    ContainerCreateBody, ContainerInspectResponse, ContainerSummary, ImageInspect, ImageSummary,
    Network, NetworkCreateRequest, NetworkInspect, SystemInfo,
};
use bollard::query_parameters::{
    CreateContainerOptions, CreateImageOptions, InspectContainerOptions, ListContainersOptions,
    ListImagesOptions, ListNetworksOptions, RemoveContainerOptions, RemoveImageOptions,
    RestartContainerOptions,
};
use futures_core::Stream;
use team_core::future::BoxFuture;

use super::{Archive, DockerEngine, DockerError, Labels};

/// The socket the Team container mounts.
pub const SOCKET: &str = "/var/run/docker.sock";

/// How long one Docker call may take before it fails as unavailable.
const CALL_TIMEOUT_SECONDS: u64 = 60;

/// The daemon behind one Unix socket.
#[derive(Debug)]
pub struct Daemon {
    pub(super) api: bollard::Docker,
    pub(super) socket: PathBuf,
}

impl Daemon {
    /// The daemon at `socket`; nothing is sent yet.
    ///
    /// # Errors
    /// [`DockerError::Unavailable`] when the socket does not exist or its path is not UTF-8.
    pub fn connect(socket: &Path) -> Result<Self, DockerError> {
        let path = socket.to_str().ok_or(DockerError::Unavailable)?;
        let api = bollard::Docker::connect_with_unix(
            path,
            CALL_TIMEOUT_SECONDS,
            bollard::API_DEFAULT_VERSION,
        )
        .map_err(|_| DockerError::Unavailable)?;
        Ok(Self {
            api,
            socket: socket.to_owned(),
        })
    }
}

/// The closed failure of one bollard call: only the daemon's own 404 and 409 answers are distinguished.
pub(super) fn closed(error: &BollardError) -> DockerError {
    match error {
        BollardError::DockerResponseServerError {
            status_code: 404, ..
        } => DockerError::NotFound,
        BollardError::DockerResponseServerError {
            status_code: 409, ..
        } => DockerError::Conflict,
        _ => DockerError::Unavailable,
    }
}

/// Docker's label filter selecting exactly the objects that carry every one of `labels`.
fn label_filter(labels: Labels<'_>) -> HashMap<String, Vec<String>> {
    HashMap::from([(
        "label".to_owned(),
        labels
            .iter()
            .map(|(name, value)| format!("{name}={value}"))
            .collect(),
    )])
}

/// An exact digest reference split as Docker's pull takes it: the repository, and the digest as its tag.
fn pull_parts(reference: &str) -> Option<(&str, &str)> {
    let (repository, digest) = reference.split_once('@')?;
    let hex = digest.strip_prefix("sha256:")?;
    (!repository.is_empty()
        && hex.len() == 64
        && hex
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f')))
    .then_some((repository, digest))
}

impl DockerEngine for Daemon {
    fn info(&self) -> BoxFuture<'_, Result<SystemInfo, DockerError>> {
        Box::pin(async move { self.api.info().await.map_err(|error| closed(&error)) })
    }

    fn list_containers<'a>(
        &'a self,
        labels: Labels<'a>,
    ) -> BoxFuture<'a, Result<Vec<ContainerSummary>, DockerError>> {
        Box::pin(async move {
            let options = ListContainersOptions {
                all: true,
                filters: Some(label_filter(labels)),
                ..ListContainersOptions::default()
            };
            self.api
                .list_containers(Some(options))
                .await
                .map_err(|error| closed(&error))
        })
    }

    fn inspect_container<'a>(
        &'a self,
        id: &'a str,
    ) -> BoxFuture<'a, Result<ContainerInspectResponse, DockerError>> {
        Box::pin(async move {
            self.api
                .inspect_container(id, None::<InspectContainerOptions>)
                .await
                .map_err(|error| closed(&error))
        })
    }

    fn create_container<'a>(
        &'a self,
        name: &'a str,
        body: ContainerCreateBody,
    ) -> BoxFuture<'a, Result<String, DockerError>> {
        Box::pin(async move {
            let options = CreateContainerOptions {
                name: Some(name.to_owned()),
                ..CreateContainerOptions::default()
            };
            let created = self
                .api
                .create_container(Some(options), body)
                .await
                .map_err(|error| closed(&error))?;
            Ok(created.id)
        })
    }

    fn start_container<'a>(&'a self, id: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            self.api
                .start_container(id, None)
                .await
                .map_err(|error| closed(&error))
        })
    }

    fn restart_container<'a>(
        &'a self,
        id: &'a str,
        grace_seconds: i32,
    ) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            let options = RestartContainerOptions {
                t: Some(grace_seconds),
                ..RestartContainerOptions::default()
            };
            self.api
                .restart_container(id, Some(options))
                .await
                .map_err(|error| closed(&error))
        })
    }

    fn remove_container<'a>(&'a self, id: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            let options = RemoveContainerOptions {
                force: true,
                ..RemoveContainerOptions::default()
            };
            self.api
                .remove_container(id, Some(options))
                .await
                .map_err(|error| closed(&error))
        })
    }

    fn list_networks<'a>(
        &'a self,
        labels: Labels<'a>,
    ) -> BoxFuture<'a, Result<Vec<Network>, DockerError>> {
        Box::pin(async move {
            let options = ListNetworksOptions {
                filters: Some(label_filter(labels)),
            };
            self.api
                .list_networks(Some(options))
                .await
                .map_err(|error| closed(&error))
        })
    }

    fn inspect_network<'a>(
        &'a self,
        id: &'a str,
    ) -> BoxFuture<'a, Result<NetworkInspect, DockerError>> {
        Box::pin(async move {
            self.api
                .inspect_network(id, None)
                .await
                .map_err(|error| closed(&error))
        })
    }

    fn create_network(
        &self,
        request: NetworkCreateRequest,
    ) -> BoxFuture<'_, Result<String, DockerError>> {
        Box::pin(async move {
            let created = self
                .api
                .create_network(request)
                .await
                .map_err(|error| closed(&error))?;
            Ok(created.id)
        })
    }

    fn remove_network<'a>(&'a self, id: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            self.api
                .remove_network(id)
                .await
                .map_err(|error| closed(&error))
        })
    }

    fn inspect_image<'a>(
        &'a self,
        reference: &'a str,
    ) -> BoxFuture<'a, Result<ImageInspect, DockerError>> {
        Box::pin(async move {
            self.api
                .inspect_image(reference)
                .await
                .map_err(|error| closed(&error))
        })
    }

    fn get_archive<'a>(
        &'a self,
        container: &'a str,
        path: &'a str,
        bound: usize,
    ) -> BoxFuture<'a, Result<Archive, DockerError>> {
        Box::pin(super::archive::get(&self.socket, container, path, bound))
    }

    fn list_images<'a>(
        &'a self,
        labels: Labels<'a>,
    ) -> BoxFuture<'a, Result<Vec<ImageSummary>, DockerError>> {
        Box::pin(async move {
            let options = ListImagesOptions {
                filters: Some(label_filter(labels)),
                ..ListImagesOptions::default()
            };
            self.api
                .list_images(Some(options))
                .await
                .map_err(|error| closed(&error))
        })
    }

    fn remove_image<'a>(&'a self, image: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            let options = RemoveImageOptions {
                force: false,
                noprune: true,
                ..RemoveImageOptions::default()
            };
            self.api
                .remove_image(image, Some(options), None)
                .await
                .map(|_| ())
                .map_err(|error| closed(&error))
        })
    }

    fn pull_image<'a>(&'a self, reference: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            let (repository, digest) = pull_parts(reference).ok_or(DockerError::NotFound)?;
            let options = CreateImageOptions {
                from_image: Some(repository.to_owned()),
                tag: Some(digest.to_owned()),
                ..CreateImageOptions::default()
            };
            let mut progress = pin!(self.api.create_image(Some(options), None, None));
            while let Some(step) = poll_fn(|context| progress.as_mut().poll_next(context)).await {
                step.map_err(|error| closed(&error))?;
            }
            Ok(())
        })
    }
}

#[cfg(test)]
mod tests {
    use bollard::errors::Error as BollardError;

    use super::{closed, label_filter, pull_parts};
    use crate::docker::DockerError;

    #[test]
    fn only_the_daemon_s_404_and_409_are_distinguished_and_no_text_survives() {
        let answer = |status_code| BollardError::DockerResponseServerError {
            status_code,
            message: "private daemon text".to_owned(),
        };
        assert_eq!(closed(&answer(404)), DockerError::NotFound);
        assert_eq!(closed(&answer(409)), DockerError::Conflict);
        assert_eq!(closed(&answer(500)), DockerError::Unavailable);
        assert_eq!(
            closed(&BollardError::RequestTimeoutError),
            DockerError::Unavailable
        );
        assert!(!format!("{:?}", closed(&answer(500))).contains("private"));
    }

    #[test]
    fn a_label_filter_selects_every_label_and_a_pull_takes_only_an_exact_digest() {
        let filter = label_filter(&[("a", "1"), ("b", "two")]);
        assert_eq!(filter.len(), 1);
        assert_eq!(filter["label"], vec!["a=1", "b=two"]);
        let digest = format!("sha256:{}", "a".repeat(64));
        let reference = format!("ghcr.io/theshimpz/shimpz-assistant@{digest}");
        assert_eq!(
            pull_parts(&reference),
            Some(("ghcr.io/theshimpz/shimpz-assistant", digest.as_str()))
        );
        assert_eq!(
            pull_parts("ghcr.io/theshimpz/shimpz-assistant:latest"),
            None
        );
        assert_eq!(pull_parts(&format!("@{digest}")), None);
        assert_eq!(pull_parts("repo@sha256:ABC"), None);
    }
}
