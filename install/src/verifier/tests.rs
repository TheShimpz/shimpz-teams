//! The verifier's registry, DSSE, and provenance rules against a scripted registry, with a cosign that always proves.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, PoisonError};

use base64::Engine as _;
use strict_json::Value;
use team_core::future::BoxFuture;

use super::{
    BLOB_HOST, BUNDLE_TYPE, IN_TOTO_PAYLOAD, OCI_MANIFEST, PREDICATE_ANNOTATION,
    PROVENANCE_PREDICATE, PROVENANCE_STATEMENT, REGISTRY_HOST, SIGNATURE_PREDICATE,
    SigstoreVerifier, TRUST_NAME,
};
use crate::cosign::{Cosign, SIGNER_IDENTITY};
use crate::release::{ReadError, Response, Transport};
use crate::trust::{ArtifactVerifier, Resolution, TrustError};

fn sha(bytes: &[u8]) -> String {
    format!("sha256:{}", assistant_protocol::python::sha256_hex(bytes))
}

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("JSON")
}

/// One recorded request: its host, target, and headers.
type Request = (String, String, Vec<(String, String)>);

/// A registry answering fixed responses by host and target, recording every request's headers.
#[derive(Default)]
struct Registry {
    responses: Mutex<HashMap<(String, String), Response>>,
    requests: Mutex<Vec<Request>>,
}

impl Registry {
    fn answer(&self, host: &str, target: &str, status: u16, location: Option<&str>, body: Vec<u8>) {
        let response = Response {
            status,
            location: location.map(str::to_owned),
            body,
        };
        self.responses
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .insert((host.to_owned(), target.to_owned()), response);
    }

    fn requests(&self) -> Vec<Request> {
        self.requests
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }
}

impl Transport for Registry {
    fn get<'a>(
        &'a self,
        host: &'a str,
        target: &'a str,
        headers: &'a [(&'a str, String)],
        _maximum: usize,
    ) -> BoxFuture<'a, Result<Response, ReadError>> {
        let recorded = headers
            .iter()
            .map(|(name, value)| ((*name).to_owned(), value.clone()))
            .collect();
        self.requests
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push((host.to_owned(), target.to_owned(), recorded));
        let response = self
            .responses
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .get(&(host.to_owned(), target.to_owned()))
            .cloned();
        Box::pin(async move { response.ok_or(ReadError::Unavailable) })
    }
}

const OCI: &str = "sha256:1111111111111111111111111111111111111111111111111111111111111111";

fn resolution() -> Value {
    json(&format!(
        r#"{{"assistant_id": "shimpz-cloudflare", "assistant_version": "0.1.0", "source_digest": "{s}", "manifest_digest": "{s}", "machine_contract_digest": "{s}", "pack_digest": "{s}", "oci_digest": "{OCI}", "trust": {{"signer_identity": "{SIGNER_IDENTITY}", "signature_reference": "", "provenance_reference": ""}}}}"#,
        s = format!("sha256:{}", "2".repeat(64))
    ))
}

fn statement(predicate: &str, subject: &str) -> Value {
    let external = r#"{"assistant_id": "shimpz-cloudflare", "version": "0.1.0", "source_digest": "S", "manifest_digest": "S", "machine_contract_digest": "S", "pack_digest": "S"}"#
        .replace('S', &format!("sha256:{}", "2".repeat(64)));
    json(&format!(
        r#"{{"_type": "{PROVENANCE_STATEMENT}", "predicateType": "{predicate}", "subject": [{{"name": "image", "digest": {{"sha256": "{subject}"}}}}], "predicate": {{"buildDefinition": {{"buildType": "https://shimpz.com/build-types/assistant/v1", "externalParameters": {external}}}, "runDetails": {{"builder": {{"id": "{SIGNER_IDENTITY}"}}}}}}}}"#
    ))
}

fn bundle(statement: &Value) -> Vec<u8> {
    let payload = base64::engine::general_purpose::STANDARD
        .encode(team_protocol::python::json::canonical(statement));
    format!(r#"{{"mediaType": "{BUNDLE_TYPE}", "dsseEnvelope": {{"payloadType": "{IN_TOTO_PAYLOAD}", "payload": "{payload}", "signatures": []}}}}"#).into_bytes()
}

fn manifest(predicate: &str, layer: &str) -> Vec<u8> {
    format!(
        r#"{{"schemaVersion": 2, "mediaType": "{OCI_MANIFEST}", "artifactType": "{BUNDLE_TYPE}", "annotations": {{"{PREDICATE_ANNOTATION}": "{predicate}"}}, "subject": {{"mediaType": "{OCI_MANIFEST}", "digest": "{OCI}"}}, "layers": [{{"mediaType": "{BUNDLE_TYPE}", "digest": "{layer}"}}]}}"#
    )
    .into_bytes()
}

/// Publishes one recorded bundle; the blob is served through GHCR's storage redirect. Returns its manifest digest.
fn publish(registry: &Registry, predicate: &str, statement: &Value) -> String {
    let bundle = bundle(statement);
    let layer = sha(&bundle);
    let manifest = manifest(predicate, &layer);
    let digest = sha(&manifest);
    registry.answer(
        REGISTRY_HOST,
        &format!("/v2/{TRUST_NAME}/manifests/{digest}"),
        200,
        None,
        manifest,
    );
    let storage = format!("/blobs/{}?sig=x", &layer[7..]);
    registry.answer(
        REGISTRY_HOST,
        &format!("/v2/{TRUST_NAME}/blobs/{layer}"),
        307,
        Some(&format!("https://{BLOB_HOST}{storage}")),
        Vec::new(),
    );
    registry.answer(BLOB_HOST, &storage, 200, None, bundle);
    digest
}

struct Fixture {
    registry: Arc<Registry>,
    verifier: SigstoreVerifier,
    resolution: Value,
}

fn fixture(provenance_statement: &Value) -> Fixture {
    let registry = Arc::new(Registry::default());
    registry.answer(
        REGISTRY_HOST,
        &format!("/token?scope=repository:{TRUST_NAME}:pull&service={REGISTRY_HOST}"),
        200,
        None,
        br#"{"token": "pull.token-value="}"#.to_vec(),
    );
    let signature = publish(
        &registry,
        SIGNATURE_PREDICATE,
        &statement(SIGNATURE_PREDICATE, &OCI[7..]),
    );
    let provenance = publish(&registry, PROVENANCE_PREDICATE, provenance_statement);
    let mut resolution = resolution();
    let Value::Object(members) = &mut resolution else {
        panic!("object")
    };
    let trust = &mut members
        .iter_mut()
        .find(|(name, _)| name == "trust")
        .expect("trust")
        .1;
    let Value::Object(trust) = trust else {
        panic!("object")
    };
    for (name, digest) in [
        ("signature_reference", signature),
        ("provenance_reference", provenance),
    ] {
        trust
            .iter_mut()
            .find(|(key, _)| key == name)
            .expect("reference")
            .1 = Value::String(format!("{REGISTRY_HOST}/{TRUST_NAME}@{digest}"));
    }
    let cosign = Cosign::with_bounds(
        std::path::Path::new("/bin/true"),
        std::path::Path::new("/tmp"),
        "http://proxy:8888",
        std::time::Duration::from_secs(5),
        std::time::Duration::from_secs(1),
        1024,
    );
    let verifier = SigstoreVerifier::new(Arc::clone(&registry) as Arc<dyn Transport>, cosign);
    Fixture {
        registry,
        verifier,
        resolution,
    }
}

async fn verify(fixture: &Fixture) -> Result<(), TrustError> {
    fixture
        .verifier
        .verify(&Resolution::admitted(fixture.resolution.clone()))
        .await
}

#[tokio::test]
async fn a_recorded_signed_publication_is_proved_and_the_bearer_never_leaves_ghcr() {
    let fixture = fixture(&statement(PROVENANCE_PREDICATE, &OCI[7..]));
    assert_eq!(verify(&fixture).await, Ok(()));
    for (host, _, headers) in fixture.registry.requests() {
        let bearer = headers.iter().any(|(name, _)| name == "authorization");
        assert_eq!(
            bearer,
            host == REGISTRY_HOST
                && headers
                    .iter()
                    .any(|(_, value)| value.starts_with("Bearer pull.token")),
            "{host}"
        );
        assert!(host == REGISTRY_HOST || host == BLOB_HOST);
        if host == BLOB_HOST {
            assert!(!bearer);
        }
    }
}

#[tokio::test]
async fn a_provenance_of_another_publication_digest_or_builder_is_untrusted() {
    let mut other = statement(PROVENANCE_PREDICATE, &OCI[7..]);
    let text = team_protocol::python::json::canonical(&other);
    other = strict_json::parse(
        &String::from_utf8(text)
            .expect("utf-8")
            .replacen("0.1.0", "0.2.0", 1)
            .into_bytes(),
    )
    .expect("JSON");
    assert_eq!(verify(&fixture(&other)).await, Err(TrustError::Untrusted));
    let wrong_subject = statement(PROVENANCE_PREDICATE, &"3".repeat(64));
    assert_eq!(
        verify(&fixture(&wrong_subject)).await,
        Err(TrustError::Untrusted)
    );
    let wrong_predicate = statement(SIGNATURE_PREDICATE, &OCI[7..]);
    assert_eq!(
        verify(&fixture(&wrong_predicate)).await,
        Err(TrustError::Untrusted)
    );
}

fn set_trust(fixture: &mut Fixture, key: &str, value: &str) {
    let Value::Object(members) = &mut fixture.resolution else {
        panic!("object")
    };
    let Value::Object(trust) = &mut members
        .iter_mut()
        .find(|(name, _)| name == "trust")
        .expect("trust")
        .1
    else {
        panic!("object")
    };
    trust
        .iter_mut()
        .find(|(name, _)| name == key)
        .expect("member")
        .1 = Value::String(value.to_owned());
}

#[tokio::test]
async fn the_signer_and_both_references_must_be_the_reviewed_ones() {
    let mut other_signer = fixture(&statement(PROVENANCE_PREDICATE, &OCI[7..]));
    set_trust(
        &mut other_signer,
        "signer_identity",
        "https://github.com/someone/else/.github/workflows/x.yml@refs/heads/main",
    );
    assert_eq!(verify(&other_signer).await, Err(TrustError::Untrusted));
    for reference in [
        format!("ghcr.io/theshimpz/other@sha256:{}", "4".repeat(64)),
        format!("ghcr.io/{TRUST_NAME}:latest"),
    ] {
        let mut outside = fixture(&statement(PROVENANCE_PREDICATE, &OCI[7..]));
        set_trust(&mut outside, "signature_reference", &reference);
        assert_eq!(
            verify(&outside).await,
            Err(TrustError::Untrusted),
            "{reference}"
        );
        assert_eq!(outside.registry.requests(), Vec::new());
    }
}

#[tokio::test]
async fn a_content_addressed_read_of_other_bytes_or_an_untrusted_redirect_is_refused() {
    let tampered = fixture(&statement(PROVENANCE_PREDICATE, &OCI[7..]));
    let storage: Vec<String> = tampered
        .registry
        .responses
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .keys()
        .filter(|(host, _)| host == BLOB_HOST)
        .map(|(_, target)| target.clone())
        .collect();
    for target in &storage {
        tampered
            .registry
            .answer(BLOB_HOST, target, 200, None, b"{}".to_vec());
    }
    assert_eq!(verify(&tampered).await, Err(TrustError::Untrusted));
    let redirected = fixture(&statement(PROVENANCE_PREDICATE, &OCI[7..]));
    let blobs: Vec<String> = redirected
        .registry
        .responses
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .keys()
        .filter(|(host, target)| host == REGISTRY_HOST && target.contains("/blobs/"))
        .map(|(_, target)| target.clone())
        .collect();
    for location in [
        "https://evil.example.com/blob",
        "http://pkg-containers.githubusercontent.com/blob",
        "https://pkg-containers.githubusercontent.com:8443/blob",
        "https://pkg-containers.githubusercontent.com@evil.example.com/blob",
    ] {
        for target in &blobs {
            redirected
                .registry
                .answer(REGISTRY_HOST, target, 302, Some(location), Vec::new());
        }
        assert_eq!(
            verify(&redirected).await,
            Err(TrustError::Untrusted),
            "{location}"
        );
        assert!(
            redirected
                .registry
                .requests()
                .iter()
                .all(|(host, _, _)| host != "evil.example.com")
        );
    }
}

#[tokio::test]
async fn a_manifest_must_carry_exactly_one_bundle_layer_for_the_subject_and_predicate() {
    let mut fixture = fixture(&statement(PROVENANCE_PREDICATE, &OCI[7..]));
    let layer = sha(&bundle(&statement(SIGNATURE_PREDICATE, &OCI[7..])));
    let good = String::from_utf8(manifest(SIGNATURE_PREDICATE, &layer)).expect("utf-8");
    let second = format!(
        r#""layers": [{{"mediaType": "{BUNDLE_TYPE}", "digest": "{}"}}, "#,
        sha(b"y")
    );
    let refused = [
        good.replace(r#""layers": ["#, &second),
        good.replace(r#""layers": [{"#, r#""layers": [{"size": 1, "#)
            .replace(BUNDLE_TYPE, "application/json"),
        good.replace(&OCI[7..], &"5".repeat(64)),
        good.replace(SIGNATURE_PREDICATE, PROVENANCE_PREDICATE),
        good.replace(OCI_MANIFEST, "application/vnd.oci.image.index.v1+json"),
    ];
    assert_eq!(verify(&fixture).await, Ok(()));
    for body in refused {
        let digest = sha(body.as_bytes());
        fixture.registry.answer(
            REGISTRY_HOST,
            &format!("/v2/{TRUST_NAME}/manifests/{digest}"),
            200,
            None,
            body.into_bytes(),
        );
        set_trust(
            &mut fixture,
            "signature_reference",
            &format!("{REGISTRY_HOST}/{TRUST_NAME}@{digest}"),
        );
        assert_eq!(verify(&fixture).await, Err(TrustError::Untrusted));
    }
}
