//! Superseded snapshot collection (`tests/test_local_snapshot_collector.py`), on the paused clock.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use bollard::models::{ContainerCreateBody, ImageConfig, ImageInspect};

use super::{GRACE, SnapshotCollector, still_superseded};
use crate::docker::recorded::{Events, Fault, Recorded};
use crate::docker::{DockerEngine, DockerError};
use crate::install::registry::Registry;
use crate::install::snapshots::{ASSISTANT_LABEL, LOCAL_STAGE_LABEL, LOCAL_STAGE_VALUE};
use crate::install::tests::{TEAM, digest, publication, reference};
use crate::testing::FakeDocker;

const SECOND: Duration = Duration::from_secs(1);
/// Just short of the grace.
const ALMOST: Duration = Duration::from_secs(15 * 60 - 1);

fn superseded(id: &str) -> ImageInspect {
    ImageInspect {
        id: Some(id.to_owned()),
        repo_tags: Some(Vec::new()),
        repo_digests: Some(Vec::new()),
        config: Some(ImageConfig {
            labels: Some(HashMap::from([(
                LOCAL_STAGE_LABEL.to_owned(),
                LOCAL_STAGE_VALUE.to_owned(),
            )])),
            ..ImageConfig::default()
        }),
        ..ImageInspect::default()
    }
}

struct Harness {
    _directory: tempfile::TempDir,
    docker: Arc<Recorded<FakeDocker>>,
    registry: Arc<Registry>,
    collector: SnapshotCollector,
}

impl Harness {
    fn new(images: &[ImageInspect]) -> Self {
        let directory = tempfile::tempdir().expect("a temporary directory");
        let docker = Arc::new(Recorded::new(FakeDocker::default(), Events::default()));
        for image in images {
            let id = image.id.clone().expect("an id");
            docker.inner().add_image(&id, image.clone());
        }
        let registry = Arc::new(Registry::new(directory.path()));
        let collector = SnapshotCollector::new(docker.clone(), registry.clone());
        Self {
            _directory: directory,
            docker,
            registry,
            collector,
        }
    }

    fn removed(&self) -> Vec<String> {
        self.docker.inner().removed_images()
    }

    /// Binds the publication whose image is `image` (by reference) to `id`.
    async fn bind(&self, image: char, id: &str) {
        let document = publication("0.1.0", 'a', image);
        self.registry
            .store(move |store| store.put(TEAM, document))
            .await
            .expect("ran")
            .expect("bound");
        self.docker
            .inner()
            .add_image(&reference(image), superseded(id));
        self.docker.inner().add_image(id, superseded(id));
    }

    async fn run(&self, image: &str) -> String {
        let body = ContainerCreateBody {
            image: Some(image.to_owned()),
            ..ContainerCreateBody::default()
        };
        self.docker
            .create_container("runner", body)
            .await
            .expect("created")
    }

    /// Collects, waits `wait`, and collects again.
    async fn collect_across(&self, wait: Duration) {
        self.collector.collect().await;
        tokio::time::advance(wait).await;
        self.collector.collect().await;
    }
}

#[tokio::test(start_paused = true)]
async fn an_unbound_unused_superseded_snapshot_is_removed_only_after_the_grace() {
    let harness = Harness::new(&[superseded(&digest('a'))]);
    harness.collect_across(ALMOST).await;
    assert_eq!(harness.removed(), Vec::<String>::new());
    tokio::time::advance(SECOND).await;
    harness.collector.collect().await;
    assert_eq!(harness.removed(), [digest('a')]);
}

#[tokio::test(start_paused = true)]
async fn a_bound_or_used_image_is_kept_and_waits_a_full_grace_once_released() {
    let (bound, running) = (digest('b'), digest('c'));
    let harness = Harness::new(&[superseded(&digest('a')), superseded(&running)]);
    harness.bind('d', &bound).await;
    let container = harness.run(&running).await;
    harness.collect_across(GRACE).await;
    assert_eq!(harness.removed(), [digest('a')]);

    harness
        .registry
        .store(|store| store.delete(TEAM, "hello-world"))
        .await
        .expect("ran")
        .expect("deleted");
    harness
        .docker
        .remove_container(&container)
        .await
        .expect("removed");
    harness.collect_across(ALMOST).await;
    assert_eq!(harness.removed().len(), 1);
    tokio::time::advance(SECOND).await;
    harness.collector.collect().await;
    assert_eq!(harness.removed(), [digest('a'), bound, running]);
}

#[tokio::test(start_paused = true)]
async fn only_untagged_never_pulled_current_stage_images_are_considered() {
    let mut tagged = superseded(&digest('a'));
    tagged.repo_tags = Some(vec!["shimpz-local/notes:staged".to_owned()]);
    let mut pulled = superseded(&digest('b'));
    pulled.repo_digests = Some(vec![format!("registry/image@{}", digest('b'))]);
    let mut older = superseded(&digest('c'));
    if let Some(labels) = older
        .config
        .as_mut()
        .and_then(|config| config.labels.as_mut())
    {
        labels.insert(LOCAL_STAGE_LABEL.to_owned(), "assistant-v2".to_owned());
    }
    let harness = Harness::new(&[tagged, pulled, older]);
    harness.collect_across(GRACE).await;
    assert_eq!(harness.removed(), Vec::<String>::new());
}

#[tokio::test(start_paused = true)]
async fn older_markers_and_a_containerd_digest_still_count_as_superseded() {
    let mut marked = superseded(&digest('a'));
    marked.repo_tags = Some(vec!["<none>:<none>".to_owned()]);
    marked.repo_digests = Some(vec!["<none>@<none>".to_owned()]);
    let mut containerd = superseded(&digest('b'));
    containerd.repo_digests = Some(vec![format!("shimpz-local/proof@{}", digest('b'))]);
    if let Some(labels) = containerd
        .config
        .as_mut()
        .and_then(|config| config.labels.as_mut())
    {
        labels.insert(ASSISTANT_LABEL.to_owned(), "proof".to_owned());
    }
    let harness = Harness::new(&[marked, containerd]);
    harness.collect_across(GRACE).await;
    // The marked image's own inspection is not the exact superseded shape, so it is never deleted on that answer.
    assert_eq!(harness.removed(), [digest('b')]);
}

#[test]
fn removal_re_inspects_and_spares_an_image_that_gained_a_tag_or_lost_its_stage() {
    let id = digest('a');
    assert!(still_superseded(&superseded(&id), &id));
    let mut tagged = superseded(&id);
    tagged.repo_tags = Some(vec!["shimpz-local/notes:staged".to_owned()]);
    let mut unlabelled = superseded(&id);
    unlabelled.config = None;
    for image in [tagged, unlabelled] {
        assert!(!still_superseded(&image, &id));
    }
}

#[tokio::test(start_paused = true)]
async fn docker_failures_defer_without_losing_the_observation() {
    for operation in [
        "list_images",
        "list_containers",
        "inspect_image",
        "remove_image",
    ] {
        let harness = Harness::new(&[superseded(&digest('a'))]);
        harness.collector.collect().await;
        tokio::time::advance(GRACE).await;
        harness
            .docker
            .fail(operation, Fault::Before(DockerError::Unavailable));
        harness.collector.collect().await;
        assert_eq!(harness.removed(), Vec::<String>::new(), "{operation}");
        harness.collector.collect().await;
        assert_eq!(harness.removed(), [digest('a')], "{operation}");
    }
}

#[tokio::test(start_paused = true)]
async fn an_observation_is_forgotten_once_the_image_is_no_longer_superseded() {
    let id = digest('a');
    let harness = Harness::new(&[superseded(&id)]);
    harness.collector.collect().await;
    let mut tagged = superseded(&id);
    tagged.repo_tags = Some(vec!["shimpz-local/notes:staged".to_owned()]);
    harness.docker.inner().add_image(&id, tagged);
    harness.collector.collect().await;
    harness.docker.inner().add_image(&id, superseded(&id));
    tokio::time::advance(GRACE).await;
    harness.collector.collect().await;
    assert_eq!(harness.removed(), Vec::<String>::new());
}

#[tokio::test(start_paused = true)]
async fn a_bound_image_missing_from_the_daemon_does_not_block_collection() {
    let harness = Harness::new(&[superseded(&digest('a'))]);
    let document = publication("0.1.0", 'a', 'd');
    harness
        .registry
        .store(move |store| store.put(TEAM, document))
        .await
        .expect("ran")
        .expect("bound");
    harness.collect_across(GRACE).await;
    assert_eq!(harness.removed(), [digest('a')]);
}
