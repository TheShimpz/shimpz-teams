//! Independent Sigstore verification of one published Assistant artifact (`install/artifact_trust.py`).
//!
//! The resolution names the signer and two recorded bundles in the trust repository: the signature and the SLSA
//! provenance. Each bundle's manifest and its single bundle layer are read once by content address (a read whose
//! bytes hash to another digest is refused, and only GHCR's storage redirect is followed, without the registry bearer),
//! so no mutable registry state can substitute another bundle. Their DSSE statements must name the Assistant's OCI
//! digest and predicate type, the provenance must bind the publication's exact parameters, and the shipping cosign
//! must then prove exactly those bytes.

use std::sync::Arc;

use base64::Engine as _;
use strict_json::Value;
use team_core::future::BoxFuture;
use team_protocol::payload::SOURCE_DIGEST_RE;
use team_protocol::python::value::py_eq;

use crate::cosign::{Cosign, CosignError};
use crate::release::{ReadError, Transport};
use crate::trust::{ArtifactVerifier, Resolution, TrustError};

/// The registry.
pub const REGISTRY_HOST: &str = "ghcr.io";
/// GHCR's storage CDN; only it may serve the bytes of a content-addressed read after a redirect.
pub const BLOB_HOST: &str = "pkg-containers.githubusercontent.com";
/// The trust repository's name in the registry.
pub const TRUST_NAME: &str = "theshimpz/shimpz-assistant-trust";
/// The predicate of the signature bundle.
pub const SIGNATURE_PREDICATE: &str = "https://sigstore.dev/cosign/sign/v1";
/// The predicate of the provenance bundle.
pub const PROVENANCE_PREDICATE: &str = "https://slsa.dev/provenance/v1";
/// The largest registry document.
pub const MAX_REGISTRY_BYTES: usize = 256 * 1024;
const BUNDLE_TYPE: &str = "application/vnd.dev.sigstore.bundle.v0.3+json";
const PREDICATE_ANNOTATION: &str = "dev.sigstore.bundle.predicateType";
const IN_TOTO_PAYLOAD: &str = "application/vnd.in-toto+json";
const PROVENANCE_STATEMENT: &str = "https://in-toto.io/Statement/v0.1";
const OCI_MANIFEST: &str = "application/vnd.oci.image.manifest.v1+json";
const BUILD_TYPE: &str = "https://shimpz.com/build-types/assistant/v1";

/// Proves artifacts through a [`Transport`] and the shipping cosign.
#[derive(Clone)]
pub struct SigstoreVerifier {
    transport: Arc<dyn Transport>,
    cosign: Cosign,
    trust_name: String,
}

impl std::fmt::Debug for SigstoreVerifier {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("SigstoreVerifier")
            .field("cosign", &self.cosign)
            .finish_non_exhaustive()
    }
}

fn untrusted<T>(_: T) -> TrustError {
    TrustError::Untrusted
}

fn text<'a>(value: &'a Value, key: &str) -> Option<&'a str> {
    match value.get(key) {
        Some(Value::String(text)) => Some(text),
        _ => None,
    }
}

impl SigstoreVerifier {
    /// Verification through `transport` with `cosign`.
    #[must_use]
    pub fn new(transport: Arc<dyn Transport>, cosign: Cosign) -> Self {
        Self {
            transport,
            cosign,
            trust_name: TRUST_NAME.to_owned(),
        }
    }

    /// The same verifier reading bundles from another trust repository of the registry (the live lane's own);
    /// production keeps [`TRUST_NAME`].
    #[must_use]
    pub fn with_trust_repository(mut self, name: &str) -> Self {
        name.clone_into(&mut self.trust_name);
        self
    }

    /// Proves one recorded bundle's bytes: its DSSE statement names `oci_digest` and `predicate`, and cosign proves
    /// exactly those bytes against the configured signer.
    ///
    /// # Errors
    /// [`TrustError::Untrusted`] for any other bundle; [`TrustError::Unavailable`] when cosign cannot run in time.
    pub async fn verify_bundle(
        &self,
        bundle: &[u8],
        oci_digest: &str,
        predicate: &str,
    ) -> Result<(), TrustError> {
        bound_statement(bundle, oci_digest, predicate)?;
        self.cosign
            .verify(bundle, oci_digest, predicate)
            .await
            .map_err(|error| match error {
                CosignError::Refused => TrustError::Untrusted,
                CosignError::Unavailable => TrustError::Unavailable,
            })
    }

    async fn prove(&self, resolution: &Value) -> Result<(), TrustError> {
        let trust = resolution.get("trust").ok_or(TrustError::Untrusted)?;
        let signer = self.cosign.signer().identity.as_str();
        if text(trust, "signer_identity") != Some(signer) {
            return Err(TrustError::Untrusted);
        }
        let oci_digest = text(resolution, "oci_digest")
            .filter(|digest| SOURCE_DIGEST_RE.is_full(digest))
            .ok_or(TrustError::Untrusted)?;
        let signature_digest = trust_digest(&self.trust_name, text(trust, "signature_reference"))?;
        let provenance_digest =
            trust_digest(&self.trust_name, text(trust, "provenance_reference"))?;
        let token = self.pull_token().await?;
        let signature = self
            .recorded_bundle(&token, signature_digest, oci_digest, SIGNATURE_PREDICATE)
            .await?;
        let provenance = self
            .recorded_bundle(&token, provenance_digest, oci_digest, PROVENANCE_PREDICATE)
            .await?;
        bound_statement(&signature, oci_digest, SIGNATURE_PREDICATE)?;
        provenance_matches(
            &bound_statement(&provenance, oci_digest, PROVENANCE_PREDICATE)?,
            resolution,
            signer,
        )?;
        // The statements above are trusted only because cosign now proves those exact bytes.
        for (bundle, predicate) in [
            (&signature, SIGNATURE_PREDICATE),
            (&provenance, PROVENANCE_PREDICATE),
        ] {
            Box::pin(self.verify_bundle(bundle, oci_digest, predicate)).await?;
        }
        Ok(())
    }

    async fn pull_token(&self) -> Result<String, TrustError> {
        let target = format!(
            "/token?scope=repository:{}:pull&service={REGISTRY_HOST}",
            self.trust_name
        );
        let raw = self
            .registry_read(&target, "application/json", None, None)
            .await?;
        let document = strict_json::parse(&raw).map_err(untrusted)?;
        text(&document, "token")
            .filter(|token| bearer_token(token))
            .map(str::to_owned)
            .ok_or(TrustError::Untrusted)
    }

    /// One recorded bundle manifest and its single bundle layer, each read by its content address.
    async fn recorded_bundle(
        &self,
        token: &str,
        digest: &str,
        oci_digest: &str,
        predicate: &str,
    ) -> Result<Vec<u8>, TrustError> {
        let target = format!("/v2/{}/manifests/{digest}", self.trust_name);
        let manifest = strict_json::parse(
            &self
                .registry_read(&target, OCI_MANIFEST, Some(token), Some(digest))
                .await?,
        )
        .map_err(untrusted)?;
        if !bundle_matches(&manifest, oci_digest, predicate) {
            return Err(TrustError::Untrusted);
        }
        let layer = match manifest.get("layers") {
            Some(Value::Array(layers))
                if layers.len() == 1 && text(&layers[0], "mediaType") == Some(BUNDLE_TYPE) =>
            {
                &layers[0]
            }
            _ => return Err(TrustError::Untrusted),
        };
        let layer_digest = text(layer, "digest")
            .filter(|digest| SOURCE_DIGEST_RE.is_full(digest))
            .ok_or(TrustError::Untrusted)?;
        let target = format!("/v2/{}/blobs/{layer_digest}", self.trust_name);
        self.registry_read(&target, BUNDLE_TYPE, Some(token), Some(layer_digest))
            .await
    }

    /// One bounded registry read through the release proxy; a content-addressed read follows the one storage
    /// redirect without the bearer and must hash to its digest.
    async fn registry_read(
        &self,
        target: &str,
        accept: &str,
        token: Option<&str>,
        digest: Option<&str>,
    ) -> Result<Vec<u8>, TrustError> {
        let mut headers = vec![("accept", accept.to_owned())];
        if let Some(token) = token {
            headers.push(("authorization", format!("Bearer {token}")));
        }
        let mut response = self
            .transport
            .get(REGISTRY_HOST, target, &headers, MAX_REGISTRY_BYTES)
            .await
            .map_err(read_error)?;
        if digest.is_some() && matches!(response.status, 302 | 307) {
            let location = blob_location(response.location.as_deref())?;
            let plain = [("accept", accept.to_owned())];
            response = self
                .transport
                .get(BLOB_HOST, &location, &plain, MAX_REGISTRY_BYTES)
                .await
                .map_err(read_error)?;
        }
        if response.status != 200 {
            return Err(TrustError::Untrusted);
        }
        if let Some(digest) = digest
            && format!(
                "sha256:{}",
                assistant_protocol::python::sha256_hex(&response.body)
            ) != digest
        {
            return Err(TrustError::Untrusted);
        }
        Ok(response.body)
    }
}

impl ArtifactVerifier for SigstoreVerifier {
    fn verify<'a>(&'a self, resolution: &'a Resolution) -> BoxFuture<'a, Result<(), TrustError>> {
        Box::pin(self.prove(resolution.document()))
    }
}

const fn read_error(error: ReadError) -> TrustError {
    match error {
        ReadError::Unavailable => TrustError::Unavailable,
        ReadError::TooLarge => TrustError::Untrusted,
    }
}

/// `[A-Za-z0-9._~+/-]+=*`.
fn bearer_token(token: &str) -> bool {
    let body = token.trim_end_matches('=');
    !body.is_empty()
        && body
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._~+/-".contains(&byte))
}

/// The digest of a reference inside the trust repository.
fn trust_digest<'a>(trust_name: &str, reference: Option<&'a str>) -> Result<&'a str, TrustError> {
    reference
        .and_then(|reference| reference.strip_prefix(&format!("{REGISTRY_HOST}/{trust_name}@")))
        .filter(|digest| SOURCE_DIGEST_RE.is_full(digest))
        .ok_or(TrustError::Untrusted)
}

/// The path and query of a redirect to exactly `https://pkg-containers.githubusercontent.com/...`.
fn blob_location(location: Option<&str>) -> Result<String, TrustError> {
    let rest = location
        .and_then(|location| location.strip_prefix("https://"))
        .and_then(|rest| rest.strip_prefix(BLOB_HOST))
        .filter(|rest| rest.starts_with('/'))
        .ok_or(TrustError::Untrusted)?;
    let without_fragment = rest.split('#').next().unwrap_or_default();
    if without_fragment
        .bytes()
        .any(|byte| byte <= b' ' || byte == 0x7f)
    {
        return Err(TrustError::Untrusted);
    }
    Ok(without_fragment.to_owned())
}

fn bundle_matches(manifest: &Value, oci_digest: &str, predicate: &str) -> bool {
    let annotations = manifest.get("annotations");
    let subject = manifest.get("subject");
    matches!(manifest, Value::Object(_))
        && text(manifest, "mediaType") == Some(OCI_MANIFEST)
        && text(manifest, "artifactType") == Some(BUNDLE_TYPE)
        && annotations.is_some_and(|annotations| {
            matches!(annotations, Value::Object(_))
                && text(annotations, PREDICATE_ANNOTATION) == Some(predicate)
        })
        && subject.is_some_and(|subject| {
            matches!(subject, Value::Object(_)) && text(subject, "digest") == Some(oci_digest)
        })
}

/// The in-toto statement of a v0.3 DSSE bundle, read strictly so no verifier can read duplicate keys apart.
fn bundle_statement(bundle: &[u8]) -> Result<Value, TrustError> {
    let document = strict_json::parse(bundle).map_err(untrusted)?;
    if text(&document, "mediaType") != Some(BUNDLE_TYPE) {
        return Err(TrustError::Untrusted);
    }
    let envelope = document.get("dsseEnvelope").ok_or(TrustError::Untrusted)?;
    let (Some(IN_TOTO_PAYLOAD), Some(payload)) =
        (text(envelope, "payloadType"), text(envelope, "payload"))
    else {
        return Err(TrustError::Untrusted);
    };
    let statement = strict_json::parse(&decode_base64(payload)?).map_err(untrusted)?;
    if matches!(statement, Value::Object(_)) {
        Ok(statement)
    } else {
        Err(TrustError::Untrusted)
    }
}

/// Python's `base64.b64decode(payload, validate=True)`: the standard alphabet with its padding, any trailing bits.
fn decode_base64(payload: &str) -> Result<Vec<u8>, TrustError> {
    let engine = base64::engine::GeneralPurpose::new(
        &base64::alphabet::STANDARD,
        base64::engine::GeneralPurposeConfig::new()
            .with_decode_allow_trailing_bits(true)
            .with_decode_padding_mode(base64::engine::DecodePaddingMode::RequireCanonical),
    );
    engine.decode(payload).map_err(untrusted)
}

fn bound_statement(bundle: &[u8], oci_digest: &str, predicate: &str) -> Result<Value, TrustError> {
    let statement = bundle_statement(bundle)?;
    let expected = oci_digest.trim_start_matches("sha256:");
    let subject_matches = matches!(statement.get("subject"), Some(Value::Array(subjects)) if subjects.iter().any(|subject| {
        matches!(subject.get("digest"), Some(digest @ Value::Object(_)) if text(digest, "sha256") == Some(expected))
    }));
    if text(&statement, "predicateType") != Some(predicate) || !subject_matches {
        return Err(TrustError::Untrusted);
    }
    Ok(statement)
}

/// The signed provenance binds the publication: the build type, the builder, and the exact external parameters.
fn provenance_matches(
    statement: &Value,
    resolution: &Value,
    signer: &str,
) -> Result<(), TrustError> {
    let predicate = statement
        .get("predicate")
        .filter(|predicate| matches!(predicate, Value::Object(_)));
    let build = predicate
        .and_then(|predicate| predicate.get("buildDefinition"))
        .filter(|value| matches!(value, Value::Object(_)));
    let run = predicate
        .and_then(|predicate| predicate.get("runDetails"))
        .filter(|value| matches!(value, Value::Object(_)));
    let builder = run
        .and_then(|run| run.get("builder"))
        .filter(|value| matches!(value, Value::Object(_)));
    let field = |key: &str| resolution.get(key).cloned().unwrap_or(Value::Null);
    let expected = Value::Object(vec![
        ("assistant_id".to_owned(), field("assistant_id")),
        ("version".to_owned(), field("assistant_version")),
        ("source_digest".to_owned(), field("source_digest")),
        ("manifest_digest".to_owned(), field("manifest_digest")),
        (
            "machine_contract_digest".to_owned(),
            field("machine_contract_digest"),
        ),
        ("pack_digest".to_owned(), field("pack_digest")),
    ]);
    let matched = text(statement, "_type") == Some(PROVENANCE_STATEMENT)
        && build.is_some_and(|build| {
            text(build, "buildType") == Some(BUILD_TYPE)
                && build
                    .get("externalParameters")
                    .is_some_and(|external| py_eq(external, &expected))
        })
        && builder.is_some_and(|builder| text(builder, "id") == Some(signer));
    if matched {
        Ok(())
    } else {
        Err(TrustError::Untrusted)
    }
}

#[cfg(test)]
mod tests;
