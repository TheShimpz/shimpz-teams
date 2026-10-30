//! The image an Assistant container may be created from. A published image is the exact reviewed digest reference,
//! pulled when absent, carrying its reviewed labels and never the Local stage label. A staged image is the exact bound
//! image id on this daemon, of the bound platform, never pulled, and never reachable through another repository or
//! tag than its own snapshot's.

use bollard::models::ImageInspect;

use super::problem::{
    IMAGE_CONTRACT_MISMATCH, IMAGE_PULL_FAILED, LOCAL_IMAGE_CONTRACT_MISMATCH, LOCAL_IMAGE_MISSING,
    LOCAL_PROVENANCE_REQUIRED, PUBLISHED_PROVENANCE_REQUIRED,
};
use super::{ContainerSpec, Provenance};
use crate::docker::problem::DOCKER_UNAVAILABLE;
use crate::docker::{DockerEngine, DockerError};
use crate::http::service::Problem;

/// The label a staged Local snapshot carries, which no published image may.
pub const LOCAL_STAGE_LABEL: &str = "org.shimpz.local.stage";
/// The Local stage label's value.
pub const LOCAL_STAGE_VALUE: &str = "assistant-v3";
/// The repository of staged Local snapshots.
pub const LOCAL_SNAPSHOT_REPOSITORY: &str = "shimpz-local";
/// The one tag that makes a staged image its Assistant's current snapshot.
pub const LOCAL_SNAPSHOT_TAG: &str = "staged";

/// The image a container is created from: its id as Docker resolves it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Resolved {
    /// The image id (`sha256:…`).
    pub id: String,
}

fn label<'a>(image: &'a ImageInspect, name: &str) -> Option<&'a str> {
    image
        .config
        .as_ref()
        .and_then(|config| config.labels.as_ref())
        .and_then(|labels| labels.get(name))
        .map(String::as_str)
}

fn labels_valid(image: &ImageInspect, spec: &ContainerSpec) -> bool {
    spec.required_image_labels
        .iter()
        .all(|(name, value)| label(image, name) == Some(value.as_str()))
}

/// The daemon's architecture of a platform (`linux/amd64` is `amd64`).
fn architecture(platform: Option<&str>) -> Option<&str> {
    platform
        .and_then(|platform| platform.rsplit_once('/'))
        .map(|(_, architecture)| architecture)
}

/// The published image of `spec`, pulled when the daemon does not hold it.
///
/// # Errors
/// `image-provenance-mismatch` for a staged Assistant; `image-pull-failed` when it cannot be pulled;
/// `image-contract-mismatch` when the image does not carry the reviewed digest and labels, or is a staged snapshot;
/// `docker-unavailable` otherwise.
pub async fn trusted(docker: &dyn DockerEngine, spec: &ContainerSpec) -> Result<Resolved, Problem> {
    if spec.provenance != Provenance::Published {
        return Err(PUBLISHED_PROVENANCE_REQUIRED);
    }
    let image = match docker.inspect_image(&spec.image).await {
        Ok(image) => image,
        Err(DockerError::NotFound) => {
            docker
                .pull_image(&spec.image)
                .await
                .map_err(|_| IMAGE_PULL_FAILED)?;
            docker
                .inspect_image(&spec.image)
                .await
                .map_err(|_| IMAGE_PULL_FAILED)?
        }
        Err(_) => return Err(DOCKER_UNAVAILABLE),
    };
    let digested = image
        .repo_digests
        .as_ref()
        .is_some_and(|digests| digests.contains(&spec.image));
    if !digested
        || !labels_valid(&image, spec)
        || label(&image, LOCAL_STAGE_LABEL) == Some(LOCAL_STAGE_VALUE)
    {
        return Err(IMAGE_CONTRACT_MISMATCH);
    }
    Ok(Resolved {
        id: image.id.ok_or(IMAGE_CONTRACT_MISMATCH)?,
    })
}

/// The staged image of `spec`, exactly as bound; never pulled.
///
/// # Errors
/// `image-provenance-mismatch` for a published Assistant; `local-image-missing` when the daemon no longer holds it;
/// `local-image-contract-mismatch` for any other id, platform, repository digest, tag, or label; `docker-unavailable`
/// otherwise.
pub async fn staged(docker: &dyn DockerEngine, spec: &ContainerSpec) -> Result<Resolved, Problem> {
    if spec.provenance != Provenance::Local {
        return Err(LOCAL_PROVENANCE_REQUIRED);
    }
    let image = match docker.inspect_image(&spec.image).await {
        Ok(image) => image,
        Err(DockerError::NotFound) => return Err(LOCAL_IMAGE_MISSING),
        Err(_) => return Err(DOCKER_UNAVAILABLE),
    };
    let snapshot = format!("{LOCAL_SNAPSHOT_REPOSITORY}/{}", spec.assistant_id);
    let digests_valid = image.repo_digests.as_ref().is_some_and(|digests| {
        digests.is_empty() || *digests == [format!("{snapshot}@{}", spec.image)]
    });
    // A bound snapshot keeps running after a newer stage supersedes it and takes the Assistant's tag.
    let tags_valid = image.repo_tags.as_ref().is_some_and(|tags| {
        tags.is_empty() || *tags == [format!("{snapshot}:{LOCAL_SNAPSHOT_TAG}")]
    });
    let exact = image.id.as_deref() == Some(spec.image.as_str())
        && image.architecture.is_some()
        && image.architecture.as_deref() == architecture(spec.platform.as_deref())
        && digests_valid
        && tags_valid
        && labels_valid(&image, spec);
    if !exact {
        return Err(LOCAL_IMAGE_CONTRACT_MISMATCH);
    }
    Ok(Resolved {
        id: spec.image.clone(),
    })
}

/// The image of `spec` by its provenance.
///
/// # Errors
/// As [`trusted`] or [`staged`].
pub async fn resolve(docker: &dyn DockerEngine, spec: &ContainerSpec) -> Result<Resolved, Problem> {
    match spec.provenance {
        Provenance::Published => trusted(docker, spec).await,
        Provenance::Local => staged(docker, spec).await,
    }
}

#[cfg(all(test, feature = "testing"))]
mod tests;
