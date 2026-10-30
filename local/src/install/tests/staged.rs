//! A staged Local snapshot built from the reference Assistant's manifest, machine contract, and language pack: its
//! canonical source package and every file the image carries, as the daemon's archive endpoint returns them
//! (`tests/local_snapshot_fixtures.py`). Installed end to end with the containers admitted by the real image review.

use std::collections::HashMap;
use std::path::PathBuf;

use bollard::models::SystemInfo;
use strict_json::{Number, Value};
use team_assistant::language::PACK_PATH;
use team_assistant::manifest::{CONTRACT_PATH, MANIFEST_PATH};

use super::{ASSISTANT_LABEL, Harness, SOURCE_LABEL, TEAM, reviewing_harness};
use crate::assistant::containers::Admission;
use crate::docker::Archive;
use crate::install::problem::MANIFEST_INVALID;
use crate::install::snapshots::tests::staged_image;
use crate::install::snapshots::{
    ACTIONS_LABEL, BUILD_LABEL, CREATORS_LABEL, ICON_PATH, INTEGRATIONS_LABEL, NAME_LABEL,
    SOURCE_PATH, SUMMARY_LABEL, VERSION_LABEL, canonical_reference,
};
use crate::install::source_package::tests::{canonical, icon};
use crate::install::spec::AssistantSpec;

const ASSISTANT: &str = "shimpz-cloudflare";
const IMAGE: &str = "sha256:5555555555555555555555555555555555555555555555555555555555555555";

fn reference(name: &str) -> Vec<u8> {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../assistant/tests/fixtures/reference")
        .join(name);
    std::fs::read(path).expect("a reference fixture")
}

/// One read-only regular file `name` as Docker's archive endpoint answers it: its stat and a one-member tar.
fn archive(name: &str, contents: &[u8]) -> Archive {
    let mut header = [0_u8; 512];
    header[..name.len()].copy_from_slice(name.as_bytes());
    header[100..108].copy_from_slice(b"0000444\0");
    header[108..116].copy_from_slice(b"0000000\0");
    header[116..124].copy_from_slice(b"0000000\0");
    header[124..136].copy_from_slice(format!("{:011o}\0", contents.len()).as_bytes());
    header[136..148].copy_from_slice(b"00000000000\0");
    header[148..156].copy_from_slice(b"        ");
    header[156] = b'0';
    header[257..265].copy_from_slice(b"ustar\x0000");
    let sum: u32 = header.iter().map(|byte| u32::from(*byte)).sum();
    header[148..156].copy_from_slice(format!("{sum:06o}\0 ").as_bytes());
    let mut tar = header.to_vec();
    tar.extend_from_slice(contents);
    tar.resize(tar.len().div_ceil(512) * 512 + 1024, 0);
    let metadata = Value::Object(vec![
        ("name".to_owned(), Value::String(name.to_owned())),
        (
            "size".to_owned(),
            Value::Number(Number::Int(i64::try_from(contents.len()).expect("small"))),
        ),
        ("mode".to_owned(), Value::Number(Number::Int(0o444))),
    ]);
    Archive { metadata, tar }
}

/// The stage labels of the reference Assistant built from source package `source`.
fn labels(source: &str) -> HashMap<String, String> {
    [
        (ASSISTANT_LABEL, ASSISTANT),
        (VERSION_LABEL, "0.1.0"),
        (NAME_LABEL, "Shimpz Cloudflare"),
        (
            SUMMARY_LABEL,
            "List Cloudflare zones and inspect their DNS records through OAuth.",
        ),
        (CREATORS_LABEL, "@roxygens"),
        (ACTIONS_LABEL, "list-dns-records,list-zones"),
        (INTEGRATIONS_LABEL, "cloudflare"),
        (SOURCE_LABEL, source),
        (BUILD_LABEL, &format!("sha256:{}", "c".repeat(64))),
        (
            crate::install::snapshots::LOCAL_STAGE_LABEL,
            crate::install::snapshots::LOCAL_STAGE_VALUE,
        ),
    ]
    .into_iter()
    .map(|(name, value)| (name.to_owned(), value.to_owned()))
    .collect()
}

/// Stages the reference Assistant as the exact image [`IMAGE`] carrying `manifest`.
fn stage(harness: &Harness, manifest: &[u8]) {
    let docker = harness.docker.as_ref();
    docker.set_info(SystemInfo {
        architecture: Some("x86_64".to_owned()),
        ..SystemInfo::default()
    });
    let icon = icon(1024);
    let package = canonical(manifest, &icon);
    let source = format!(
        "sha256:{}",
        assistant_protocol::python::sha256_hex(&package)
    );
    let labels = labels(&source);
    let mut image = staged_image();
    image.id = Some(IMAGE.to_owned());
    image.repo_tags = Some(vec![canonical_reference(ASSISTANT)]);
    if let Some(config) = image.config.as_mut() {
        config.labels = Some(labels);
    }
    docker.add_image(IMAGE, image.clone());
    docker.add_image(&canonical_reference(ASSISTANT), image);
    for (path, name, contents) in [
        (SOURCE_PATH, "source.package", package),
        (ICON_PATH, "icon.png", icon),
        (MANIFEST_PATH, "shimpz.toml", manifest.to_vec()),
        (
            CONTRACT_PATH,
            "shimpz.contract.json",
            reference("shimpz.contract.json"),
        ),
        (PACK_PATH, "shimpz.pack.json", reference("shimpz.pack.json")),
    ] {
        docker.add_image_file(IMAGE, path, archive(name, &contents));
    }
}

#[tokio::test]
async fn a_staged_snapshot_is_listed_admissible_and_installs_under_the_image_review() {
    let harness = reviewing_harness().await;
    stage(&harness, &reference("shimpz.toml"));
    let actor = crate::audit::Principal::team();
    let listed = harness.assistants.local.list(&actor).await.expect("listed");
    let Some(Value::Array(entries)) = listed.get("assistants") else {
        panic!("a list")
    };
    assert_eq!(entries.len(), 1);
    assert_eq!(
        entries[0].get("admission"),
        Some(&Value::String("admissible".to_owned()))
    );

    let installed = harness
        .assistants
        .install_local(TEAM, IMAGE, true)
        .await
        .expect("installed");
    assert_eq!(installed.get("installed"), Some(&Value::Bool(true)));
    let binding = harness
        .registry
        .binding(TEAM, ASSISTANT)
        .await
        .expect("readable")
        .expect("bound");
    assert_eq!(
        binding.provenance,
        team_install::bindings::Provenance::Local
    );
    assert!(
        harness
            .docker
            .container_names()
            .iter()
            .any(|name| name.contains(ASSISTANT))
    );
    let page = harness
        .assistants
        .details(TEAM, ASSISTANT, "pt")
        .await
        .expect("a localized page from the image's pack");
    assert_eq!(
        page.get("assistant_id"),
        Some(&Value::String(ASSISTANT.to_owned()))
    );
}

#[tokio::test]
async fn the_image_review_refuses_a_manifest_that_differs_from_the_binding() {
    let harness = reviewing_harness().await;
    let manifest = reference("shimpz.toml");
    stage(&harness, &manifest);
    harness
        .assistants
        .install_local(TEAM, IMAGE, true)
        .await
        .expect("installed");
    let binding = harness
        .registry
        .binding(TEAM, ASSISTANT)
        .await
        .expect("readable")
        .expect("bound");
    let spec = AssistantSpec::of(&binding)
        .expect("a spec")
        .container_spec();
    let container = harness
        .assistants
        .containers
        .find(TEAM, ASSISTANT)
        .await
        .expect("listed")
        .and_then(|container| container.id)
        .expect("running");
    assert_eq!(harness.admission.manifest(&container, &spec).await, Ok(()));

    let widened = String::from_utf8(manifest).expect("UTF-8").replace(
        r#"allowed_hosts = ["api.cloudflare.com"]"#,
        r#"allowed_hosts = ["api.cloudflare.com", "upload.example.org"]"#,
    );
    harness.docker.add_image_file(
        IMAGE,
        MANIFEST_PATH,
        archive("shimpz.toml", widened.as_bytes()),
    );
    harness.admission.forget(&container);
    assert_eq!(
        harness.admission.manifest(&container, &spec).await,
        Err(MANIFEST_INVALID)
    );
}
