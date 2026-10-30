//! A fixed resolver and a scripted verifier (feature `testing`): for unit tests and the controller suite's test-only
//! runner. The live artifact-trust lane never uses them. Never compiled into a shipped binary.

use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard, PoisonError};

use team_core::future::BoxFuture;

use crate::trust::{ArtifactVerifier, Resolution, ResolveError, Resolver, TrustError};

/// Answers from fixed maps: a resolution and a latest resolution per source digest, and icons per icon digest.
#[derive(Debug, Default)]
pub struct FixedResolver {
    resolutions: Mutex<HashMap<String, Resolution>>,
    latest: Mutex<HashMap<String, Resolution>>,
    icons: Mutex<HashMap<String, Vec<u8>>>,
}

fn locked<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

impl FixedResolver {
    /// `source_digest` resolves to `resolution`.
    pub fn resolves(&self, source_digest: &str, resolution: Resolution) {
        locked(&self.resolutions).insert(source_digest.to_owned(), resolution);
    }

    /// The latest publication after `source_digest` is `resolution`.
    pub fn latest_is(&self, source_digest: &str, resolution: Resolution) {
        locked(&self.latest).insert(source_digest.to_owned(), resolution);
    }

    /// `icon_digest` names `icon`.
    pub fn icon_is(&self, icon_digest: &str, icon: Vec<u8>) {
        locked(&self.icons).insert(icon_digest.to_owned(), icon);
    }
}

impl Resolver for FixedResolver {
    fn resolve<'a>(
        &'a self,
        source_digest: &'a str,
    ) -> BoxFuture<'a, Result<Resolution, ResolveError>> {
        Box::pin(async move {
            locked(&self.resolutions)
                .get(source_digest)
                .cloned()
                .ok_or(ResolveError::NotInstallable)
        })
    }

    fn latest<'a>(
        &'a self,
        source_digest: &'a str,
    ) -> BoxFuture<'a, Result<Resolution, ResolveError>> {
        Box::pin(async move {
            locked(&self.latest)
                .get(source_digest)
                .cloned()
                .ok_or(ResolveError::NotInstallable)
        })
    }

    fn icon<'a>(
        &'a self,
        _source_digest: &'a str,
        icon_digest: &'a str,
    ) -> BoxFuture<'a, Result<Vec<u8>, ResolveError>> {
        Box::pin(async move {
            locked(&self.icons)
                .get(icon_digest)
                .cloned()
                .ok_or(ResolveError::Protocol)
        })
    }
}

/// Answers every verification with one scripted outcome and counts the calls.
#[derive(Debug, Default)]
pub struct ScriptedVerifier {
    refusal: Mutex<Option<TrustError>>,
    calls: Mutex<usize>,
}

impl ScriptedVerifier {
    /// Every later verification fails with `refusal`, or succeeds with `None`.
    pub fn refuse(&self, refusal: Option<TrustError>) {
        *locked(&self.refusal) = refusal;
    }

    /// How many verifications ran.
    #[must_use]
    pub fn calls(&self) -> usize {
        *locked(&self.calls)
    }
}

impl ArtifactVerifier for ScriptedVerifier {
    fn verify<'a>(&'a self, _resolution: &'a Resolution) -> BoxFuture<'a, Result<(), TrustError>> {
        Box::pin(async move {
            *locked(&self.calls) += 1;
            locked(&self.refusal).map_or(Ok(()), Err)
        })
    }
}
