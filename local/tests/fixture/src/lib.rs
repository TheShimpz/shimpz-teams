//! The controller suite's test-only runner (ADR-0123, plan phase R7, workstream W9).
//!
//! `shimpz-team-fixture serve` runs the real controller with every production seam except two: publication resolution
//! answers the one publication the suite seeded at [`PUBLICATION_PATH`], and artifact verification admits exactly that
//! publication. The suite's Assistant image sits in a loopback registry no Developers publication or Sigstore bundle
//! names, so these two seams are the only ones it cannot reach for real; controller HTTP, Supervisor authority, the
//! registry, Docker pulls, isolation, and teardown stay real.
//!
//! It is built only by the Team Dockerfile's `fixture` target and never published. The live artifact-trust lane never
//! runs it: that lane proves the real resolver, the real verifier, and the shipping cosign.

use std::path::Path;
use std::sync::Arc;

use strict_json::Value;
use team_core::future::BoxFuture;
use team_install::trust::{ArtifactVerifier, Resolution, ResolveError, Resolver, TrustError};
use team_local::seams::Seams;

/// Where the suite seeds the publication, on the controller's own publications volume.
pub const PUBLICATION_PATH: &str = "/var/lib/shimpz-local/publications/test-resolution.json";
/// The icon the seeded publication's `icon_digest` names.
pub const ICON: &[u8] = b"canonical icon";
/// The largest seeded publication read.
const MAX_PUBLICATION_BYTES: u64 = 1024 * 1024;

/// The seeded publication could not be read.
#[derive(Debug)]
pub enum PublicationError {
    /// The file could not be read.
    Unreadable(std::io::Error),
    /// The file exceeds its bound.
    TooLarge,
    /// The file is not one strict JSON object carrying string `source_digest` and `icon_digest` members.
    Invalid,
}

impl std::fmt::Display for PublicationError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Unreadable(error) => {
                write!(formatter, "the seeded publication is unreadable: {error}")
            }
            Self::TooLarge => formatter.write_str("the seeded publication exceeds its bound"),
            Self::Invalid => formatter.write_str("the seeded publication is not a resolution"),
        }
    }
}

impl std::error::Error for PublicationError {}

/// The one publication the suite seeded.
#[derive(Clone, Debug, PartialEq)]
pub struct Publication {
    document: Value,
    source_digest: String,
    icon_digest: String,
}

impl Publication {
    /// Reads the seeded publication.
    ///
    /// # Errors
    /// [`PublicationError`] when it cannot be read or is not a resolution.
    pub fn read(path: &Path) -> Result<Self, PublicationError> {
        let metadata = std::fs::metadata(path).map_err(PublicationError::Unreadable)?;
        if metadata.len() > MAX_PUBLICATION_BYTES {
            return Err(PublicationError::TooLarge);
        }
        Self::parse(&std::fs::read(path).map_err(PublicationError::Unreadable)?)
    }

    /// Parses a seeded publication.
    ///
    /// # Errors
    /// [`PublicationError::Invalid`] unless `raw` is one strict JSON object with string `source_digest` and
    /// `icon_digest` members.
    pub fn parse(raw: &[u8]) -> Result<Self, PublicationError> {
        let document = strict_json::parse(raw).map_err(|_| PublicationError::Invalid)?;
        let text = |name: &str| match document.get(name) {
            Some(Value::String(text)) => Ok(text.clone()),
            _ => Err(PublicationError::Invalid),
        };
        let source_digest = text("source_digest")?;
        let icon_digest = text("icon_digest")?;
        Ok(Self {
            document,
            source_digest,
            icon_digest,
        })
    }
}

/// Answers only the seeded publication, as the Python runner's Developers double did: another source digest is not
/// installable, and an icon is served only under the seeded `icon_digest`.
#[derive(Debug)]
pub struct SeededResolver(Arc<Publication>);

impl SeededResolver {
    fn resolution(&self, source_digest: &str) -> Result<Resolution, ResolveError> {
        if source_digest == self.0.source_digest {
            Ok(Resolution::admitted(self.0.document.clone()))
        } else {
            Err(ResolveError::NotInstallable)
        }
    }
}

impl Resolver for SeededResolver {
    fn resolve<'a>(
        &'a self,
        source_digest: &'a str,
    ) -> BoxFuture<'a, Result<Resolution, ResolveError>> {
        Box::pin(async move { self.resolution(source_digest) })
    }

    fn latest<'a>(
        &'a self,
        source_digest: &'a str,
    ) -> BoxFuture<'a, Result<Resolution, ResolveError>> {
        Box::pin(async move { self.resolution(source_digest) })
    }

    fn icon<'a>(
        &'a self,
        source_digest: &'a str,
        icon_digest: &'a str,
    ) -> BoxFuture<'a, Result<Vec<u8>, ResolveError>> {
        Box::pin(async move {
            self.resolution(source_digest)?;
            if icon_digest == self.0.icon_digest {
                Ok(ICON.to_vec())
            } else {
                Err(ResolveError::Protocol)
            }
        })
    }
}

/// Admits exactly the seeded publication and refuses any other resolution as untrusted.
#[derive(Debug)]
pub struct SeededVerifier(Arc<Publication>);

impl ArtifactVerifier for SeededVerifier {
    fn verify<'a>(&'a self, resolution: &'a Resolution) -> BoxFuture<'a, Result<(), TrustError>> {
        Box::pin(async move {
            if *resolution.document() == self.0.document {
                Ok(())
            } else {
                Err(TrustError::Untrusted)
            }
        })
    }
}

/// The production `seams` with resolution and verification answered by the seeded `publication`, and a registry that
/// admits the suite's loopback image; every other seam is left as it is.
#[must_use]
pub fn adapt(seams: Seams, publication: Publication) -> Seams {
    let publication = Arc::new(publication);
    Seams {
        resolver: Arc::new(SeededResolver(Arc::clone(&publication))),
        verifier: Arc::new(SeededVerifier(publication)),
        any_resolution: true,
        ..seams
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use strict_json::Value;
    use team_install::trust::{ArtifactVerifier, Resolution, ResolveError, Resolver, TrustError};

    use super::{ICON, Publication, PublicationError, SeededResolver, SeededVerifier};

    const SEEDED: &[u8] =
        br#"{"version":1,"source_digest":"sha256:aa","icon_digest":"sha256:bb","name":"Seeded"}"#;

    fn seeded() -> Arc<Publication> {
        Arc::new(Publication::parse(SEEDED).expect("a resolution"))
    }

    #[tokio::test]
    async fn only_the_seeded_source_digest_resolves_and_only_its_icon_is_served() {
        let resolver = SeededResolver(seeded());
        let document = strict_json::parse(SEEDED).expect("json");
        assert_eq!(
            resolver.resolve("sha256:aa").await,
            Ok(Resolution::admitted(document.clone()))
        );
        assert_eq!(
            resolver.latest("sha256:aa").await,
            Ok(Resolution::admitted(document))
        );
        assert_eq!(
            resolver.resolve("sha256:cc").await,
            Err(ResolveError::NotInstallable)
        );
        assert_eq!(
            resolver.icon("sha256:aa", "sha256:bb").await,
            Ok(ICON.to_vec())
        );
        assert_eq!(
            resolver.icon("sha256:aa", "sha256:cc").await,
            Err(ResolveError::Protocol)
        );
        assert_eq!(
            resolver.icon("sha256:cc", "sha256:bb").await,
            Err(ResolveError::NotInstallable)
        );
    }

    #[tokio::test]
    async fn the_verifier_admits_exactly_the_seeded_publication() {
        let verifier = SeededVerifier(seeded());
        let document = strict_json::parse(SEEDED).expect("json");
        assert_eq!(
            verifier.verify(&Resolution::admitted(document)).await,
            Ok(())
        );
        let other =
            strict_json::parse(br#"{"source_digest":"sha256:aa","icon_digest":"sha256:bb"}"#)
                .expect("json");
        assert_eq!(
            verifier.verify(&Resolution::admitted(other)).await,
            Err(TrustError::Untrusted)
        );
        assert_eq!(
            verifier.verify(&Resolution::admitted(Value::Null)).await,
            Err(TrustError::Untrusted)
        );
    }

    #[test]
    fn a_seed_that_is_not_a_resolution_is_refused() {
        for raw in [
            &b"[]"[..],
            br#"{"source_digest":"sha256:aa"}"#,
            br#"{"source_digest":1,"icon_digest":"sha256:bb"}"#,
            br#"{"source_digest":"a","source_digest":"b","icon_digest":"c"}"#,
            b"not json",
        ] {
            assert!(matches!(
                Publication::parse(raw),
                Err(PublicationError::Invalid)
            ));
        }
    }
}
