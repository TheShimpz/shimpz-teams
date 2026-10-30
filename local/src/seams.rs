//! The composition root's injection points: what the production binary wires to the host, and the controller suite's
//! test-only runner may replace. Everything else is built from these and the [`crate::layout::StateLayout`].
//!
//! The live artifact-trust lane never replaces `resolver` or `verifier`: it runs the real ones with the shipping
//! cosign.

use std::sync::Arc;

use team_core::clock::Clock;
use team_core::entropy::Entropy;
use team_core::exec::ExecEngine;
use team_inference::brain::BrainClient;
use team_install::trust::{ArtifactVerifier, Resolver};

use crate::docker::DockerEngine;

/// The controller's external seams.
#[derive(Clone)]
pub struct Seams {
    /// The wall clock.
    pub clock: Arc<dyn Clock>,
    /// The random source.
    pub entropy: Arc<dyn Entropy>,
    /// The Docker daemon.
    pub docker: Arc<dyn DockerEngine>,
    /// Workload exec over the same daemon.
    pub exec: Arc<dyn ExecEngine>,
    /// The Brain runtime.
    pub brain: Arc<dyn BrainClient>,
    /// Publication resolution.
    pub resolver: Arc<dyn Resolver>,
    /// Artifact verification.
    pub verifier: Arc<dyn ArtifactVerifier>,
}

impl std::fmt::Debug for Seams {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Seams")
    }
}
