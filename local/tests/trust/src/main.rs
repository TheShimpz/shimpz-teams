//! `shimpz-team-trust-probe`: the live artifact-trust lane's probe (ADR-0123, plan phase R7, workstream W9).
//!
//! It runs inside the Team fixture image on a network whose only outbound member is the real Assistant-release proxy,
//! with the production resolver, verifier, and the shipping cosign; nothing is substituted. Each subcommand prints one
//! verdict line the lane checks, or fails.
//!
//! - `resolve [SOURCE_DIGEST]`: an unknown publication is refused as not installable through the proxy; a published
//!   one, when named, resolves to itself.
//! - `unavailable`: with the proxy gone, resolution and verification both fail closed as unavailable.
//! - `route`: the probe's container has no direct route to the internet.

use std::net::{SocketAddr, TcpStream};
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Duration;

use strict_json::Value;
use team_install::cosign::{Cosign, SIGNER_IDENTITY};
use team_install::developers::DevelopersResolver;
use team_install::release::{RELEASE_PROXY_HOST, RELEASE_PROXY_PORT, ReleaseProxy};
use team_install::trust::{ArtifactVerifier, Resolution, ResolveError, Resolver, TrustError};
use team_install::verifier::{SigstoreVerifier, TRUST_NAME};

/// A syntactically valid digest that names no publication.
const UNKNOWN: &str = "sha256:0000000000000000000000000000000000000000000000000000000000000000";
/// cosign's trust root inside the image.
const TUF_ROOT: &str = "/var/lib/shimpz-local/cosign";

fn resolver() -> DevelopersResolver {
    DevelopersResolver::new(Arc::new(ReleaseProxy::team()))
}

fn verifier() -> SigstoreVerifier {
    let proxy = format!("http://{RELEASE_PROXY_HOST}:{RELEASE_PROXY_PORT}");
    SigstoreVerifier::new(
        Arc::new(ReleaseProxy::team()),
        Cosign::new(std::path::Path::new(TUF_ROOT), &proxy),
    )
}

async fn resolve(published: Option<&str>) -> Result<&'static str, String> {
    let resolver = resolver();
    match resolver.resolve(UNKNOWN).await {
        Err(ResolveError::NotInstallable) => {}
        other => return Err(format!("an unknown publication answered {other:?}")),
    }
    let Some(digest) = published else {
        return Ok("resolution-refused");
    };
    let resolution = resolver
        .resolve(digest)
        .await
        .map_err(|error| format!("the published {digest} did not resolve: {error:?}"))?;
    match resolution.document().get("source_digest") {
        Some(Value::String(echoed)) if echoed == digest => Ok("resolution-published"),
        echoed => Err(format!("the resolution echoed {echoed:?}")),
    }
}

/// A well-formed resolution whose recorded bundles live in the trust repository, so verification must reach it.
fn reaching() -> Resolution {
    let digest = |fill: char| format!("sha256:{}", fill.to_string().repeat(64));
    let reference = |fill: char| Value::String(format!("ghcr.io/{TRUST_NAME}@{}", digest(fill)));
    Resolution::admitted(Value::Object(vec![
        ("oci_digest".into(), Value::String(digest('a'))),
        (
            "trust".into(),
            Value::Object(vec![
                (
                    "signer_identity".into(),
                    Value::String(SIGNER_IDENTITY.into()),
                ),
                ("signature_reference".into(), reference('b')),
                ("provenance_reference".into(), reference('c')),
            ]),
        ),
    ]))
}

async fn unavailable() -> Result<&'static str, String> {
    match resolver().resolve(UNKNOWN).await {
        Err(ResolveError::Unavailable) => {}
        other => return Err(format!("resolution without the proxy answered {other:?}")),
    }
    match verifier().verify(&reaching()).await {
        Err(TrustError::Unavailable) => Ok("unavailable"),
        other => Err(format!("verification without the proxy answered {other:?}")),
    }
}

fn route() -> Result<&'static str, String> {
    let public = SocketAddr::from(([1, 1, 1, 1], 443));
    match TcpStream::connect_timeout(&public, Duration::from_secs(1)) {
        Ok(_) => Err("the probe reached the internet directly".into()),
        Err(_) => Ok("no-route"),
    }
}

fn run(arguments: &[String]) -> Result<&'static str, String> {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|error| format!("no runtime: {error}"))?;
    match arguments {
        [command] if command == "resolve" => runtime.block_on(resolve(None)),
        [command, digest] if command == "resolve" => runtime.block_on(resolve(Some(digest))),
        [command] if command == "unavailable" => runtime.block_on(unavailable()),
        [command] if command == "route" => route(),
        _ => Err(
            "usage: shimpz-team-trust-probe resolve [SOURCE_DIGEST] | unavailable | route".into(),
        ),
    }
}

fn main() -> ExitCode {
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    match run(&arguments) {
        Ok(verdict) => {
            println!("{verdict}");
            ExitCode::SUCCESS
        }
        Err(failure) => {
            eprintln!("shimpz-team-trust-probe: {failure}");
            ExitCode::FAILURE
        }
    }
}
