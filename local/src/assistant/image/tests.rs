use std::collections::HashMap;

use bollard::models::{ImageConfig, ImageInspect};

use super::{Resolved, resolve, staged, trusted};
use crate::assistant::{ContainerSpec, Provenance};
use crate::testing::FakeDocker;

const PUBLISHED: &str = "ghcr.io/theshimpz/shimpz-assistant@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const IMAGE_ID: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

fn code(result: Result<Resolved, crate::http::service::Problem>) -> Option<&'static str> {
    result.err().and_then(|problem| problem.code)
}

fn config(labels: &[(&str, &str)]) -> ImageConfig {
    ImageConfig {
        labels: Some(
            labels
                .iter()
                .map(|(name, value)| ((*name).to_owned(), (*value).to_owned()))
                .collect::<HashMap<_, _>>(),
        ),
        ..ImageConfig::default()
    }
}

fn published_spec() -> ContainerSpec {
    ContainerSpec {
        assistant_id: "notes".to_owned(),
        image: PUBLISHED.to_owned(),
        provenance: Provenance::Published,
        allowed_hosts: Vec::new(),
        required_image_labels: vec![("org.shimpz.assistant.id".to_owned(), "notes".to_owned())],
        platform: None,
    }
}

fn published_image() -> ImageInspect {
    ImageInspect {
        id: Some(IMAGE_ID.to_owned()),
        repo_digests: Some(vec![PUBLISHED.to_owned()]),
        config: Some(config(&[("org.shimpz.assistant.id", "notes")])),
        ..ImageInspect::default()
    }
}

#[tokio::test]
async fn assistant_image_must_equal_reviewed_digest() {
    let spec = published_spec();
    // Absent: pulled once by its exact digest, then admitted.
    let docker = FakeDocker::default();
    docker.add_remote_image(PUBLISHED, published_image());
    assert_eq!(
        trusted(&docker, &spec).await,
        Ok(Resolved {
            id: IMAGE_ID.to_owned()
        })
    );
    assert_eq!(docker.pulled(), [PUBLISHED]);
    // Present: never pulled again.
    assert!(trusted(&docker, &spec).await.is_ok());
    assert_eq!(docker.pulled().len(), 1);
}

#[tokio::test]
async fn a_published_image_without_its_reviewed_digest_or_labels_is_refused() {
    let spec = published_spec();
    let refused = [
        ImageInspect {
            repo_digests: Some(vec![format!(
                "ghcr.io/theshimpz/shimpz-assistant@sha256:{}",
                "c".repeat(64)
            )]),
            ..published_image()
        },
        ImageInspect {
            repo_digests: None,
            ..published_image()
        },
        ImageInspect {
            config: Some(config(&[("org.shimpz.assistant.id", "other")])),
            ..published_image()
        },
        // A staged Local snapshot never passes as a published image.
        ImageInspect {
            config: Some(config(&[
                ("org.shimpz.assistant.id", "notes"),
                ("org.shimpz.local.stage", "assistant-v3"),
            ])),
            ..published_image()
        },
    ];
    for image in refused {
        let docker = FakeDocker::default();
        docker.add_image(PUBLISHED, image);
        assert_eq!(
            code(trusted(&docker, &spec).await),
            Some("image-contract-mismatch")
        );
    }
}

#[tokio::test]
async fn a_published_image_that_cannot_be_pulled_or_read_is_refused() {
    let spec = published_spec();
    let docker = FakeDocker::default();
    assert_eq!(
        code(trusted(&docker, &spec).await),
        Some("image-pull-failed")
    );
    docker.set_unavailable(true);
    assert_eq!(
        code(trusted(&docker, &spec).await),
        Some("docker-unavailable")
    );
    let local = ContainerSpec {
        provenance: Provenance::Local,
        ..spec
    };
    assert_eq!(
        code(trusted(&docker, &local).await),
        Some("image-provenance-mismatch")
    );
}

fn local_spec() -> ContainerSpec {
    ContainerSpec {
        assistant_id: "fixture-assistant".to_owned(),
        image: IMAGE_ID.to_owned(),
        provenance: Provenance::Local,
        allowed_hosts: Vec::new(),
        required_image_labels: vec![(
            "org.shimpz.assistant.id".to_owned(),
            "fixture-assistant".to_owned(),
        )],
        platform: Some("linux/amd64".to_owned()),
    }
}

fn staged_image() -> ImageInspect {
    ImageInspect {
        id: Some(IMAGE_ID.to_owned()),
        architecture: Some("amd64".to_owned()),
        repo_digests: Some(Vec::new()),
        repo_tags: Some(Vec::new()),
        config: Some(config(&[("org.shimpz.assistant.id", "fixture-assistant")])),
        ..ImageInspect::default()
    }
}

#[tokio::test]
async fn a_staged_image_is_never_pulled_and_must_be_exactly_bound() {
    let spec = local_spec();
    let admitted = [
        staged_image(),
        ImageInspect {
            repo_digests: Some(vec![format!("shimpz-local/fixture-assistant@{IMAGE_ID}")]),
            repo_tags: Some(vec!["shimpz-local/fixture-assistant:staged".to_owned()]),
            ..staged_image()
        },
    ];
    for image in admitted {
        let docker = FakeDocker::default();
        docker.add_image(IMAGE_ID, image);
        assert_eq!(
            staged(&docker, &spec).await.map(|image| image.id),
            Ok(IMAGE_ID.to_owned())
        );
    }
}

#[tokio::test]
async fn a_staged_image_of_another_identity_is_refused() {
    let spec = local_spec();
    let refused = [
        ImageInspect {
            id: Some(format!("sha256:{}", "e".repeat(64))),
            ..staged_image()
        },
        ImageInspect {
            architecture: Some("arm64".to_owned()),
            ..staged_image()
        },
        ImageInspect {
            repo_digests: Some(vec![PUBLISHED.to_owned()]),
            ..staged_image()
        },
        ImageInspect {
            repo_tags: Some(vec!["attacker/latest".to_owned()]),
            ..staged_image()
        },
        ImageInspect {
            repo_tags: Some(vec!["shimpz-local/other-assistant:staged".to_owned()]),
            ..staged_image()
        },
        ImageInspect {
            repo_tags: Some(vec![
                "shimpz-local/fixture-assistant:staged".to_owned(),
                "shimpz-local/fixture-assistant:other".to_owned(),
            ]),
            ..staged_image()
        },
        ImageInspect {
            config: Some(config(&[("org.shimpz.assistant.id", "other")])),
            ..staged_image()
        },
    ];
    for image in refused {
        let docker = FakeDocker::default();
        docker.add_image(IMAGE_ID, image);
        assert_eq!(
            code(staged(&docker, &spec).await),
            Some("local-image-contract-mismatch")
        );
    }
}

#[tokio::test]
async fn a_staged_image_that_is_gone_is_missing_and_never_pulled() {
    let spec = local_spec();
    // Gone is missing, never pulled.
    let docker = FakeDocker::default();
    docker.add_remote_image(IMAGE_ID, staged_image());
    assert_eq!(
        code(staged(&docker, &spec).await),
        Some("local-image-missing")
    );
    assert_eq!(docker.pulled(), [] as [std::string::String; 0]);
    docker.set_unavailable(true);
    assert_eq!(
        code(staged(&docker, &spec).await),
        Some("docker-unavailable")
    );
    assert_eq!(
        code(staged(&docker, &published_spec()).await),
        Some("image-provenance-mismatch")
    );
    // Resolution follows the provenance.
    let docker = FakeDocker::default();
    docker.add_image(IMAGE_ID, staged_image());
    docker.add_image(PUBLISHED, published_image());
    assert!(resolve(&docker, &spec).await.is_ok());
    assert!(resolve(&docker, &published_spec()).await.is_ok());
}
