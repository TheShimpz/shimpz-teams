//! Assistant containers (workstream W4a): the isolation profile each one is created with and admitted against
//! ([`isolation`]), the identity its image must prove ([`image`]), and its lifecycle on the Team network
//! ([`containers`]): the egress policy written before the container exists, the complete inspection before it starts
//! or receives any input, readiness, fail-stop, and removal. Every refusal is a closed [`Problem`] in [`problem`].
//!
//! What an Assistant may be (its reviewed binding, manifest, and publication) is the install workstream's; this module
//! sees only the [`ContainerSpec`] a binding projects, through the [`Bindings`] seam.

pub mod containers;
pub mod image;
pub mod isolation;
pub mod problem;

use team_core::future::BoxFuture;

#[cfg(doc)]
use crate::http::service::Problem;

/// Where an Assistant's image comes from.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Provenance {
    /// Published through Developers: the image is the exact digest reference (`repository@sha256:…`) its trust
    /// verification proved, pulled when absent.
    Published,
    /// Staged on this Space: the image is the exact local image id (`sha256:…`), never pulled.
    Local,
}

/// What a container needs to know of one bound Assistant.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ContainerSpec {
    /// The Assistant's id.
    pub assistant_id: String,
    /// The reviewed image: a digest reference (published) or an image id (local).
    pub image: String,
    /// Where the image comes from.
    pub provenance: Provenance,
    /// The reviewed hosts of its egress policy, canonical; empty when it calls no provider.
    pub allowed_hosts: Vec<String>,
    /// The labels the image must carry with exactly these values.
    pub required_image_labels: Vec<(String, String)>,
    /// For a staged image, the platform it was built for (`linux/amd64`).
    pub platform: Option<String>,
}

/// The bindings store failed; nothing may be inferred from it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BindingsUnavailable;

/// The Team's Assistant bindings, as the install workstream's registry keeps them.
pub trait Bindings: Send + Sync {
    /// The bound Assistant `assistant_id` of `team_id`, or `None` when it is not bound.
    ///
    /// # Errors
    /// [`BindingsUnavailable`] when the bindings cannot be read.
    fn spec<'a>(
        &'a self,
        team_id: &'a str,
        assistant_id: &'a str,
    ) -> BoxFuture<'a, Result<Option<ContainerSpec>, BindingsUnavailable>>;

    /// Every bound `(team_id, assistant_id)` of the Space.
    ///
    /// # Errors
    /// [`BindingsUnavailable`] when the bindings cannot be read.
    fn identities(&self) -> BoxFuture<'_, Result<Vec<(String, String)>, BindingsUnavailable>>;
}
