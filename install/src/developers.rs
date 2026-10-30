//! The bounded public Developers client of a Local Space (`local/install/developers.py`): exact publication
//! resolution, the latest publication for automatic updates, and the canonical icon, all through the release proxy.
//!
//! A resolution is read with the strict JSON reader and admitted against the Assistant-install contract with Team's
//! own rules before anything else reads it; an exact resolution must echo the requested source digest.

use std::sync::Arc;

use strict_json::Value;
use team_core::future::BoxFuture;
use team_protocol::payload::SOURCE_DIGEST_RE;

use crate::release::{ReadError, Transport};
use crate::trust::{Resolution, ResolveError, Resolver};

/// The Developers host.
pub const DEVELOPERS_HOST: &str = "developers.shimpz.com";
/// The largest Developers answer.
pub const MAX_RESPONSE_BYTES: usize = 1024 * 1024;
const PUBLICATIONS: &str = "/api/v1/assistant-publications";

/// Resolves publications from Developers through a [`Transport`].
#[derive(Clone)]
pub struct DevelopersResolver {
    transport: Arc<dyn Transport>,
}

impl std::fmt::Debug for DevelopersResolver {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("DevelopersResolver")
    }
}

impl DevelopersResolver {
    /// Resolution through `transport`.
    #[must_use]
    pub fn new(transport: Arc<dyn Transport>) -> Self {
        Self { transport }
    }

    async fn fetch(&self, target: &str, accept: &str) -> Result<(u16, Vec<u8>), ResolveError> {
        let headers = [("accept", accept.to_owned())];
        let response = self
            .transport
            .get(DEVELOPERS_HOST, target, &headers, MAX_RESPONSE_BYTES)
            .await
            .map_err(|error| match error {
                ReadError::Unavailable | ReadError::TooLarge => ResolveError::Unavailable,
            })?;
        Ok((response.status, response.body))
    }

    async fn resolution(&self, target: &str) -> Result<Resolution, ResolveError> {
        let (status, body) = self.fetch(target, "application/json").await?;
        match status {
            404 => return Err(ResolveError::NotInstallable),
            200 => {}
            _ => return Err(ResolveError::Unavailable),
        }
        let document = strict_json::parse(&body).map_err(|_| ResolveError::Protocol)?;
        if !matches!(document, Value::Object(_)) || crate::contract::validate(&document).is_err() {
            return Err(ResolveError::Protocol);
        }
        Ok(Resolution::admitted(document))
    }
}

impl Resolver for DevelopersResolver {
    fn resolve<'a>(
        &'a self,
        source_digest: &'a str,
    ) -> BoxFuture<'a, Result<Resolution, ResolveError>> {
        Box::pin(async move {
            if !SOURCE_DIGEST_RE.is_full(source_digest) {
                return Err(ResolveError::NotInstallable);
            }
            let resolution = self
                .resolution(&format!("{PUBLICATIONS}/{source_digest}"))
                .await?;
            if resolution.document().get("source_digest")
                != Some(&Value::String(source_digest.to_owned()))
            {
                return Err(ResolveError::Protocol);
            }
            Ok(resolution)
        })
    }

    fn latest<'a>(
        &'a self,
        source_digest: &'a str,
    ) -> BoxFuture<'a, Result<Resolution, ResolveError>> {
        Box::pin(async move {
            if !SOURCE_DIGEST_RE.is_full(source_digest) {
                return Err(ResolveError::NotInstallable);
            }
            self.resolution(&format!("{PUBLICATIONS}/{source_digest}/latest"))
                .await
        })
    }

    fn icon<'a>(
        &'a self,
        source_digest: &'a str,
        icon_digest: &'a str,
    ) -> BoxFuture<'a, Result<Vec<u8>, ResolveError>> {
        Box::pin(async move {
            if !SOURCE_DIGEST_RE.is_full(source_digest) || !SOURCE_DIGEST_RE.is_full(icon_digest) {
                return Err(ResolveError::NotInstallable);
            }
            let (status, body) = self
                .fetch(
                    &format!("{PUBLICATIONS}/{source_digest}/icon.png"),
                    "image/png",
                )
                .await?;
            if status == 404 {
                return Err(ResolveError::NotInstallable);
            }
            let digest = format!("sha256:{}", assistant_protocol::python::sha256_hex(&body));
            if status != 200 || digest != icon_digest {
                return Err(ResolveError::Protocol);
            }
            Ok(body)
        })
    }
}
