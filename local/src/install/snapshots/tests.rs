//! Staged snapshot identity (`tests/test_local_snapshots.py`, the label and runtime half).

use std::collections::HashMap;

use bollard::models::{ImageConfig, ImageInspect};

use super::{
    ASSISTANT_LABEL, BUILD_LABEL, Candidate, LOCAL_STAGE_LABEL, LOCAL_STAGE_VALUE,
    RUNTIME_ENTRYPOINT, RUNTIME_USER, SOURCE_LABEL, SnapshotError, VERSION_LABEL, candidate,
    canonical_reference,
};

const ID: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const PLATFORM: &str = "linux/amd64";

pub(crate) fn staged_labels() -> HashMap<String, String> {
    [
        (LOCAL_STAGE_LABEL, LOCAL_STAGE_VALUE),
        (ASSISTANT_LABEL, "notes"),
        (VERSION_LABEL, "1.2.3"),
        (super::NAME_LABEL, "Notes"),
        (super::SUMMARY_LABEL, "Keep short notes."),
        (super::CREATORS_LABEL, "@creator"),
        (super::ACTIONS_LABEL, "read,write"),
        (super::INTEGRATIONS_LABEL, ""),
        (SOURCE_LABEL, &format!("sha256:{}", "b".repeat(64))),
        (BUILD_LABEL, &format!("sha256:{}", "c".repeat(64))),
    ]
    .into_iter()
    .map(|(name, value)| (name.to_owned(), value.to_owned()))
    .collect()
}

pub(crate) fn staged_image() -> ImageInspect {
    ImageInspect {
        id: Some(ID.to_owned()),
        created: Some("2026-10-30T12:00:00Z".to_owned()),
        architecture: Some("amd64".to_owned()),
        repo_tags: Some(vec![canonical_reference("notes")]),
        repo_digests: Some(Vec::new()),
        config: Some(ImageConfig {
            user: Some(RUNTIME_USER.to_owned()),
            entrypoint: Some(RUNTIME_ENTRYPOINT.map(str::to_owned).to_vec()),
            labels: Some(staged_labels()),
            ..ImageConfig::default()
        }),
        ..ImageInspect::default()
    }
}

fn with_label(name: &str, value: Option<&str>) -> ImageInspect {
    let mut image = staged_image();
    let labels = image
        .config
        .as_mut()
        .and_then(|config| config.labels.as_mut())
        .expect("labels");
    match value {
        Some(value) => labels.insert(name.to_owned(), value.to_owned()),
        None => labels.remove(name),
    };
    image
}

#[test]
fn a_staged_image_is_a_candidate_with_its_declared_identity() {
    let found: Candidate = candidate(&staged_image(), PLATFORM).expect("a candidate");
    assert_eq!(
        (
            found.assistant_id.as_str(),
            found.version.as_str(),
            found.image_id.as_str()
        ),
        ("notes", "1.2.3", ID)
    );
    assert_eq!(found.actions, ["read", "write"]);
    assert_eq!(found.integrations, Vec::<String>::new());
}

#[test]
fn any_label_tag_platform_or_runtime_drift_is_refused() {
    let refused = SnapshotError::Invalid(super::DECLARATION_INVALID);
    let mut pulled = staged_image();
    pulled.repo_digests = Some(vec![format!("ghcr.io/other/notes@{ID}")]);
    let mut retagged = staged_image();
    retagged.repo_tags = Some(vec!["shimpz-local/notes:latest".to_owned()]);
    let mut commanded = staged_image();
    if let Some(config) = commanded.config.as_mut() {
        config.cmd = Some(vec!["sh".to_owned()]);
    }
    let mut rooted = staged_image();
    if let Some(config) = rooted.config.as_mut() {
        config.user = Some("0:0".to_owned());
    }
    for image in [
        with_label(LOCAL_STAGE_LABEL, Some("assistant-v2")),
        with_label(SOURCE_LABEL, Some("sha256:short")),
        with_label(BUILD_LABEL, None),
        with_label(super::ACTIONS_LABEL, Some("write,read")),
        with_label(super::ACTIONS_LABEL, Some("")),
        with_label(VERSION_LABEL, Some("one")),
        pulled,
        retagged,
        commanded,
        rooted,
    ] {
        assert_eq!(candidate(&image, PLATFORM), Err(refused));
    }
    assert_eq!(candidate(&staged_image(), "linux/arm64"), Err(refused));
}
