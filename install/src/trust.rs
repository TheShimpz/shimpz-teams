//! Publication resolution and artifact verification (frozen in W0).
//!
//! The controller's install service resolves an exact `source_digest`, verifies the resolution's artifact, binds it,
//! and re-resolves the same digest immediately before start (the OCI digest must be unchanged). Only the controller
//! suite's test-only runner substitutes these seams; the live artifact-trust lane runs the real resolver, the real
//! verifier, and the shipping cosign.

use strict_json::Value;
use team_core::future::BoxFuture;

/// One publication resolution Developers answered for an exact source digest, admitted against the
/// Assistant-install v1 resolve schema with its `source_digest` echoing the request.
#[derive(Clone, Debug, PartialEq)]
pub struct Resolution {
    document: Value,
}

impl Resolution {
    /// A resolution whose `document` the caller has admitted.
    #[must_use]
    pub fn admitted(document: Value) -> Self {
        Self { document }
    }

    /// The admitted document.
    #[must_use]
    pub fn document(&self) -> &Value {
        &self.document
    }
}

/// A closed resolution failure; it never carries upstream text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ResolveError {
    /// Developers or the release proxy could not be reached.
    Unavailable,
    /// The answer violates the protocol (strict JSON, schema, echo, bound, or icon digest).
    Protocol,
    /// The publication exists but is not installable.
    NotInstallable,
}

/// A closed verification failure.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TrustError {
    /// The artifact's signature or signed provenance could not be proved.
    Untrusted,
    /// The registry, Sigstore, or the release proxy could not be reached, or cosign could not run in time.
    Unavailable,
}

/// Resolves exact publications from Developers.
pub trait Resolver: Send + Sync {
    /// The publication of `source_digest`.
    ///
    /// # Errors
    /// [`ResolveError`] for every unsuccessful outcome.
    fn resolve<'a>(
        &'a self,
        source_digest: &'a str,
    ) -> BoxFuture<'a, Result<Resolution, ResolveError>>;

    /// The latest publication of the Assistant `source_digest` belongs to, for automatic updates.
    ///
    /// # Errors
    /// [`ResolveError`] for every unsuccessful outcome.
    fn latest<'a>(
        &'a self,
        source_digest: &'a str,
    ) -> BoxFuture<'a, Result<Resolution, ResolveError>>;

    /// The icon bytes of `source_digest`, verified against `icon_digest`.
    ///
    /// # Errors
    /// [`ResolveError`] for every unsuccessful outcome.
    fn icon<'a>(
        &'a self,
        source_digest: &'a str,
        icon_digest: &'a str,
    ) -> BoxFuture<'a, Result<Vec<u8>, ResolveError>>;
}

/// Proves a resolution's artifact.
pub trait ArtifactVerifier: Send + Sync {
    /// Succeeds only when the signer identity, both recorded bundles, their DSSE statements, the provenance's bound
    /// parameters, and cosign's verification of those exact bytes all hold.
    ///
    /// # Errors
    /// [`TrustError`] otherwise.
    fn verify<'a>(&'a self, resolution: &'a Resolution) -> BoxFuture<'a, Result<(), TrustError>>;
}
