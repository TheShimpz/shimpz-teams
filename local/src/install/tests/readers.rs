//! Every staged snapshot reader is removed: after its reads, when its read is abandoned, and at the next start when a
//! previous controller process left it.

use std::task::{Context, Waker};
use std::time::Duration;

use bollard::models::ContainerCreateBody;

use super::staged::{IMAGE, archive, reference, stage};
use super::{Harness, reviewing_harness};
use crate::docker::DockerEngine;
use crate::docker::labels::label_map;
use crate::install::problem::PREVIEW_INVALID;
use crate::install::snapshots::{self, ICON_PATH};

fn readers(harness: &Harness) -> Vec<String> {
    harness
        .docker
        .container_names()
        .into_iter()
        .filter(|name| name.contains("-snapshot-"))
        .collect()
}

#[tokio::test]
async fn a_file_beyond_its_bound_is_refused_and_its_reader_removed() {
    let harness = reviewing_harness().await;
    stage(&harness, &reference("shimpz.toml"));
    harness.docker.add_image_file(
        IMAGE,
        ICON_PATH,
        archive(
            "icon.png",
            &vec![0; team_install::icons::MAX_ICON_BYTES + 1],
        ),
    );
    assert_eq!(
        harness.assistants.local.details(IMAGE, "pt").await,
        Err(PREVIEW_INVALID)
    );
    assert_eq!(readers(&harness), Vec::<String>::new());
}

#[tokio::test]
async fn a_start_removes_only_the_readers_another_process_left() {
    let harness = reviewing_harness().await;
    let stage = &harness.assistants.stage;
    let prefix = stage.space.prefix().to_owned();
    for (name, kind) in [
        (
            format!("shimpz-local-{prefix}-snapshot-0000dead-01"),
            "snapshot-reader",
        ),
        (
            format!("shimpz-local-{prefix}-snapshot-{}-02", stage.run),
            "snapshot-reader",
        ),
        (format!("shimpz-local-{prefix}-prepare-03"), "prepare"),
    ] {
        let body = ContainerCreateBody {
            labels: Some(label_map(&stage.space.labels(kind, None))),
            ..ContainerCreateBody::default()
        };
        harness
            .docker
            .create_container(&name, body)
            .await
            .expect("created");
    }
    harness.assistants.remove_abandoned_readers().await;
    let names = harness.docker.container_names();
    assert!(!names.iter().any(|name| name.contains("0000dead")));
    assert!(names.iter().any(|name| name.ends_with("-02")));
    assert!(names.iter().any(|name| name.ends_with("prepare-03")));
}

#[tokio::test]
async fn a_read_abandoned_before_its_create_still_reads_and_removes_its_reader() {
    let harness = reviewing_harness().await;
    stage(&harness, &reference("shimpz.toml"));
    let stage = harness.assistants.stage.clone();
    let mut extract = Box::pin(snapshots::extract(&stage, IMAGE, &snapshots::PREVIEW_FILES));
    let mut context = Context::from_waker(Waker::noop());
    assert!(extract.as_mut().poll(&mut context).is_pending());
    drop(extract);
    assert_eq!(readers(&harness), Vec::<String>::new());
    for _ in 0..2_000 {
        if harness.docker.archive_reads() == snapshots::PREVIEW_FILES.len()
            && readers(&harness).is_empty()
        {
            return;
        }
        tokio::time::sleep(Duration::from_millis(1)).await;
    }
    panic!("the abandoned read never finished and removed its reader");
}
