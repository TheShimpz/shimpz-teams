//! The Docker daemon as the controller uses it (frozen in W0; workstream W4a implements it over bollard and the
//! socket, with exec attach done by its own upgrade so [`team_core::exec::ExecEngine`] streams stay unparsed).
//!
//! Every resource Team creates carries the complete ownership label set and a deterministic name; nothing unlabeled,
//! partly labeled, foreign to the Space, or misnamed is ever adopted or removed. Every container Team creates is
//! inspected against its complete expected envelope before it receives untrusted input (Assistants, preparation
//! helpers, Browsers), and drift refuses and removes it.

pub mod archive;
pub mod daemon;
pub mod exec;
pub mod helpers;
pub mod labels;
pub mod policy;
pub mod problem;
#[cfg(all(test, feature = "testing"))]
pub(crate) mod recorded;

use bollard::models::{
    ContainerCreateBody, ContainerInspectResponse, ContainerSummary, ImageInspect, ImageSummary,
    Network, NetworkCreateRequest, NetworkInspect, SystemInfo,
};
use strict_json::Value;
use team_core::future::BoxFuture;

/// A closed Docker failure; it never carries the daemon's text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DockerError {
    /// The object does not exist.
    NotFound,
    /// The object exists in a state that refuses the operation (a name in use, a network with members).
    Conflict,
    /// The daemon could not be reached or failed.
    Unavailable,
}

impl std::fmt::Display for DockerError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::NotFound => "the Docker object does not exist",
            Self::Conflict => "the Docker object is in a conflicting state",
            Self::Unavailable => "Docker is unavailable",
        })
    }
}

impl std::error::Error for DockerError {}

/// Label filters: every `(name, value)` must be present on the object.
pub type Labels<'a> = &'a [(&'a str, &'a str)];

/// One path of a container as the archive endpoint answers it.
#[derive(Clone, Debug, PartialEq)]
pub struct Archive {
    /// The path's stat metadata (`name`, `size`, `mode`, `mtime`, `linkTarget`), as Docker reports it.
    pub metadata: Value,
    /// The tar stream's first bytes: at most the bound plus one, so a longer stream shows as longer than the bound.
    pub tar: Vec<u8>,
}

/// The Docker operations the controller performs, beyond exec. The archive and image-inventory operations answer
/// [`DockerError::Unavailable`] unless an implementation provides them (a test fake that never reaches them).
pub trait DockerEngine: Send + Sync {
    /// The daemon's system information (its default seccomp profile, `NCPU`).
    ///
    /// # Errors
    /// [`DockerError`] when it cannot be read.
    fn info(&self) -> BoxFuture<'_, Result<SystemInfo, DockerError>>;

    /// Every container, running or not, carrying all of `labels`.
    ///
    /// # Errors
    /// [`DockerError`] when they cannot be listed.
    fn list_containers<'a>(
        &'a self,
        labels: Labels<'a>,
    ) -> BoxFuture<'a, Result<Vec<ContainerSummary>, DockerError>>;

    /// One container's complete inspection.
    ///
    /// # Errors
    /// [`DockerError`] when it cannot be inspected.
    fn inspect_container<'a>(
        &'a self,
        id: &'a str,
    ) -> BoxFuture<'a, Result<ContainerInspectResponse, DockerError>>;

    /// Creates a container named `name` from `body`, not started, and returns its id.
    ///
    /// # Errors
    /// [`DockerError`] when it cannot be created.
    fn create_container<'a>(
        &'a self,
        name: &'a str,
        body: ContainerCreateBody,
    ) -> BoxFuture<'a, Result<String, DockerError>>;

    /// Starts a created container.
    ///
    /// # Errors
    /// [`DockerError`] when it cannot be started.
    fn start_container<'a>(&'a self, id: &'a str) -> BoxFuture<'a, Result<(), DockerError>>;

    /// Restarts a container within `grace_seconds` (the Browser egress between tasks).
    ///
    /// # Errors
    /// [`DockerError`] when it cannot be restarted.
    fn restart_container<'a>(
        &'a self,
        id: &'a str,
        grace_seconds: i32,
    ) -> BoxFuture<'a, Result<(), DockerError>>;

    /// Force-removes a container; an absent one is [`DockerError::NotFound`], which teardown treats as success.
    ///
    /// # Errors
    /// [`DockerError`] when it cannot be removed.
    fn remove_container<'a>(&'a self, id: &'a str) -> BoxFuture<'a, Result<(), DockerError>>;

    /// Every network carrying all of `labels`.
    ///
    /// # Errors
    /// [`DockerError`] when they cannot be listed.
    fn list_networks<'a>(
        &'a self,
        labels: Labels<'a>,
    ) -> BoxFuture<'a, Result<Vec<Network>, DockerError>>;

    /// One network's inspection, with its members.
    ///
    /// # Errors
    /// [`DockerError`] when it cannot be inspected.
    fn inspect_network<'a>(
        &'a self,
        id: &'a str,
    ) -> BoxFuture<'a, Result<NetworkInspect, DockerError>>;

    /// Creates a network and returns its id.
    ///
    /// # Errors
    /// [`DockerError`] when it cannot be created.
    fn create_network(
        &self,
        request: NetworkCreateRequest,
    ) -> BoxFuture<'_, Result<String, DockerError>>;

    /// Removes a network.
    ///
    /// # Errors
    /// [`DockerError`] when it cannot be removed.
    fn remove_network<'a>(&'a self, id: &'a str) -> BoxFuture<'a, Result<(), DockerError>>;

    /// One image's inspection, by id or exact reference.
    ///
    /// # Errors
    /// [`DockerError`] when it cannot be inspected.
    fn inspect_image<'a>(
        &'a self,
        reference: &'a str,
    ) -> BoxFuture<'a, Result<ImageInspect, DockerError>>;

    /// The path `path` of container `container`: its stat metadata and its tar stream, read no further than `bound`
    /// bytes (one more byte proves the stream longer than the bound).
    ///
    /// # Errors
    /// [`DockerError::NotFound`] when the container or path does not exist; [`DockerError::Unavailable`] for any
    /// other failure, including metadata that is not one strict JSON object.
    fn get_archive<'a>(
        &'a self,
        _container: &'a str,
        _path: &'a str,
        _bound: usize,
    ) -> BoxFuture<'a, Result<Archive, DockerError>> {
        Box::pin(async { Err(DockerError::Unavailable) })
    }

    /// Every image carrying all of `labels`.
    ///
    /// # Errors
    /// [`DockerError`] when they cannot be listed.
    fn list_images<'a>(
        &'a self,
        _labels: Labels<'a>,
    ) -> BoxFuture<'a, Result<Vec<ImageSummary>, DockerError>> {
        Box::pin(async { Err(DockerError::Unavailable) })
    }

    /// Removes one image by id or exact reference, never forced and without pruning its parents; an image a
    /// container still uses is [`DockerError::Conflict`].
    ///
    /// # Errors
    /// [`DockerError`] when it was not removed.
    fn remove_image<'a>(&'a self, _image: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async { Err(DockerError::Unavailable) })
    }

    /// Pulls an exact digest reference (`repository@sha256:…`).
    ///
    /// # Errors
    /// [`DockerError`] when it cannot be pulled.
    fn pull_image<'a>(&'a self, reference: &'a str) -> BoxFuture<'a, Result<(), DockerError>>;
}
