//! Eventual removal of superseded Local Assistant snapshots that no Team binds or runs (`local/install/collector.py`).
//!
//! An install admits only an Assistant's current (tagged) snapshot, so once an image has been seen superseded,
//! unbound, and unused for [`GRACE`], any install that admitted it has long since committed its binding or failed.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::Duration;

use bollard::models::ImageInspect;
use tokio::sync::Mutex;
use tokio::time::Instant;

use super::registry::Registry;
use super::snapshots::{ASSISTANT_LABEL, LOCAL_REPOSITORY, LOCAL_STAGE_LABEL, LOCAL_STAGE_VALUE};
use super::spec::AssistantSpec;
use crate::docker::{DockerEngine, DockerError};

/// How long a superseded snapshot stays unbound and unused before it is removed.
pub const GRACE: Duration = Duration::from_mins(15);

/// Removes stage-labelled images that lost their tag once no binding or container keeps them.
pub struct SnapshotCollector {
    docker: Arc<dyn DockerEngine>,
    registry: Arc<Registry>,
    grace: Duration,
    observed: Mutex<HashMap<String, Instant>>,
}

impl std::fmt::Debug for SnapshotCollector {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("SnapshotCollector")
    }
}

fn untagged(tags: &[String]) -> bool {
    tags.is_empty() || tags == ["<none>:<none>"]
}

/// A superseded snapshot was never pulled: no digest, or its own digest on a containerd image store.
fn local_digests(labels: &HashMap<String, String>, digests: &[String], image_id: &str) -> bool {
    digests.is_empty()
        || labels.get(ASSISTANT_LABEL).is_some_and(|assistant| {
            digests == [format!("{LOCAL_REPOSITORY}/{assistant}@{image_id}")]
        })
}

fn staged(labels: &HashMap<String, String>) -> bool {
    labels.get(LOCAL_STAGE_LABEL).map(String::as_str) == Some(LOCAL_STAGE_VALUE)
}

/// The image is still exactly a superseded snapshot: staged, untagged, never pulled.
fn still_superseded(image: &ImageInspect, image_id: &str) -> bool {
    let labels = image
        .config
        .as_ref()
        .and_then(|config| config.labels.as_ref());
    labels.is_some_and(|labels| {
        staged(labels)
            && image.repo_tags.as_deref() == Some(&[][..])
            && local_digests(
                labels,
                image.repo_digests.as_deref().unwrap_or(&[]),
                image_id,
            )
    })
}

impl SnapshotCollector {
    /// The collector of `docker`'s superseded snapshots, keeping every image `registry` binds.
    #[must_use]
    pub fn new(docker: Arc<dyn DockerEngine>, registry: Arc<Registry>) -> Self {
        Self {
            docker,
            registry,
            grace: GRACE,
            observed: Mutex::new(HashMap::new()),
        }
    }

    /// One collection pass; anything Docker or the registry cannot answer defers to the next.
    pub async fn collect(&self) {
        let Some(superseded) = self.superseded().await else {
            return;
        };
        let mut observed = self.observed.lock().await;
        observed.retain(|image_id, _| superseded.contains(image_id));
        if superseded.is_empty() {
            return;
        }
        let Some(retained) = self.retained().await else {
            return;
        };
        let now = Instant::now();
        let mut ordered: Vec<&String> = superseded.iter().collect();
        ordered.sort();
        for image_id in ordered {
            if retained.contains(image_id) {
                observed.remove(image_id);
                continue;
            }
            let first_seen = *observed.entry(image_id.clone()).or_insert(now);
            if now.duration_since(first_seen) >= self.grace && self.remove(image_id).await {
                observed.remove(image_id);
            }
        }
    }

    /// Every untagged, never-pulled staged image.
    async fn superseded(&self) -> Option<HashSet<String>> {
        let summaries = self
            .docker
            .list_images(&[(LOCAL_STAGE_LABEL, LOCAL_STAGE_VALUE)])
            .await
            .ok()?;
        Some(
            summaries
                .into_iter()
                .filter(|summary| {
                    staged(&summary.labels)
                        && untagged(&summary.repo_tags)
                        && (summary.repo_digests == ["<none>@<none>"]
                            || local_digests(&summary.labels, &summary.repo_digests, &summary.id))
                })
                .map(|summary| summary.id)
                .collect(),
        )
    }

    /// Every image a binding of any Team or any container in any state still references.
    async fn retained(&self) -> Option<HashSet<String>> {
        let mut retained = HashSet::new();
        for binding in self.registry.snapshot().await.ok()? {
            let spec = AssistantSpec::of(&binding)?;
            match self.docker.inspect_image(&spec.image).await {
                Ok(image) => retained.extend(image.id),
                Err(DockerError::NotFound) => {}
                Err(_) => return None,
            }
        }
        for container in self.docker.list_containers(&[]).await.ok()? {
            retained.insert(container.image_id?);
        }
        Some(retained)
    }

    /// Whether `image_id` is gone: removed now, absent, or no longer a superseded snapshot.
    async fn remove(&self, image_id: &str) -> bool {
        match self.docker.inspect_image(image_id).await {
            Err(DockerError::NotFound) => true,
            Err(_) => false,
            Ok(image) if !still_superseded(&image, image_id) => true,
            Ok(_) => matches!(
                self.docker.remove_image(image_id).await,
                Ok(()) | Err(DockerError::NotFound)
            ),
        }
    }
}

#[cfg(all(test, feature = "testing"))]
mod tests;
