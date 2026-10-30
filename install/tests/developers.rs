//! The Developers resolver: strict, contract-admitted resolutions that echo the requested digest, and verified icons.

use std::path::PathBuf;
use std::sync::{Arc, Mutex, PoisonError};

use strict_json::Value;
use team_core::future::BoxFuture;
use team_install::developers::{DEVELOPERS_HOST, DevelopersResolver};
use team_install::release::{ReadError, Response, Transport};
use team_install::trust::{ResolveError, Resolver};

/// Answers every read with one scripted outcome and records the targets.
struct Scripted {
    answer: Result<Response, ReadError>,
    targets: Mutex<Vec<(String, String)>>,
}

impl Transport for Scripted {
    fn get<'a>(
        &'a self,
        host: &'a str,
        target: &'a str,
        _headers: &'a [(&'a str, String)],
        _maximum: usize,
    ) -> BoxFuture<'a, Result<Response, ReadError>> {
        self.targets
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push((host.to_owned(), target.to_owned()));
        let answer = self.answer.clone();
        Box::pin(async move { answer })
    }
}

fn resolver(status: u16, body: Vec<u8>) -> (Arc<Scripted>, DevelopersResolver) {
    let scripted = Arc::new(Scripted {
        answer: Ok(Response {
            status,
            location: None,
            body,
        }),
        targets: Mutex::new(Vec::new()),
    });
    (Arc::clone(&scripted), DevelopersResolver::new(scripted))
}

fn fixture() -> Value {
    let path =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../protocol/install/v1/vectors.json");
    let vectors = strict_json::parse(&std::fs::read(path).expect("vectors")).expect("JSON");
    vectors
        .get("fixtures")
        .and_then(|fixtures| fixtures.get("resolve_response"))
        .and_then(|fixture| fixture.get("value"))
        .expect("fixture")
        .clone()
}

fn source_digest(resolution: &Value) -> String {
    match resolution.get("source_digest") {
        Some(Value::String(digest)) => digest.clone(),
        _ => panic!("digest"),
    }
}

#[tokio::test]
async fn an_exact_resolution_is_admitted_only_when_it_echoes_the_requested_digest() {
    let resolution = fixture();
    let body = team_protocol::python::json::canonical(&resolution);
    let digest = source_digest(&resolution);
    let (scripted, resolver) = resolver(200, body.clone());
    let admitted = resolver.resolve(&digest).await.expect("admitted");
    assert!(team_protocol::python::value::py_eq(
        admitted.document(),
        &resolution
    ));
    assert_eq!(
        scripted
            .targets
            .lock()
            .unwrap_or_else(PoisonError::into_inner)[0],
        (
            DEVELOPERS_HOST.to_owned(),
            format!("/api/v1/assistant-publications/{digest}")
        )
    );
    let other = format!("sha256:{}", "9".repeat(64));
    assert_eq!(resolver.resolve(&other).await, Err(ResolveError::Protocol));
    assert!(resolver.latest(&other).await.is_ok());
    assert_eq!(
        resolver.resolve("sha256:short").await,
        Err(ResolveError::NotInstallable)
    );
}

#[tokio::test]
async fn a_resolution_outside_the_strict_reader_or_the_contract_is_a_protocol_violation() {
    let digest = source_digest(&fixture());
    let duplicated = br#"{"source_digest": "x", "source_digest": "y"}"#.to_vec();
    let mut unknown = fixture();
    let Value::Object(members) = &mut unknown else {
        panic!("object")
    };
    members.push(("extra".to_owned(), Value::Null));
    for body in [
        duplicated,
        team_protocol::python::json::canonical(&unknown),
        b"[]".to_vec(),
        b"not json".to_vec(),
    ] {
        let (_, resolver) = resolver(200, body);
        assert_eq!(resolver.resolve(&digest).await, Err(ResolveError::Protocol));
    }
}

#[tokio::test]
async fn statuses_and_transport_failures_map_to_closed_errors() {
    let digest = source_digest(&fixture());
    assert_eq!(
        resolver(404, Vec::new()).1.resolve(&digest).await,
        Err(ResolveError::NotInstallable)
    );
    assert_eq!(
        resolver(503, Vec::new()).1.resolve(&digest).await,
        Err(ResolveError::Unavailable)
    );
    for failure in [ReadError::Unavailable, ReadError::TooLarge] {
        let scripted = Arc::new(Scripted {
            answer: Err(failure),
            targets: Mutex::new(Vec::new()),
        });
        assert_eq!(
            DevelopersResolver::new(scripted).resolve(&digest).await,
            Err(ResolveError::Unavailable)
        );
    }
}

#[tokio::test]
async fn an_icon_must_hash_to_its_publication_digest() {
    let digest = source_digest(&fixture());
    let icon = b"\x89PNG icon".to_vec();
    let icon_digest = format!("sha256:{}", assistant_protocol::python::sha256_hex(&icon));
    assert_eq!(
        resolver(200, icon.clone())
            .1
            .icon(&digest, &icon_digest)
            .await,
        Ok(icon.clone())
    );
    let other = format!("sha256:{}", "8".repeat(64));
    assert_eq!(
        resolver(200, icon.clone()).1.icon(&digest, &other).await,
        Err(ResolveError::Protocol)
    );
    assert_eq!(
        resolver(404, Vec::new())
            .1
            .icon(&digest, &icon_digest)
            .await,
        Err(ResolveError::NotInstallable)
    );
    assert_eq!(
        resolver(200, icon).1.icon(&digest, "sha256:bad").await,
        Err(ResolveError::NotInstallable)
    );
}
