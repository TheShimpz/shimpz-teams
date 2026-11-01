//! A staged snapshot's preview survives a restart in Team's own store, is validated again on every load, and is read
//! out of the image only when no intact stored copy holds.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use team_assistant::manifest::MANIFEST_PATH;
use team_core::testing::MemoryAudit;

use super::staged::{IMAGE, reference, stage};
use super::{Harness, reviewing_harness};
use crate::install::local::LocalSnapshots;
use crate::install::snapshots::previews::PreviewStore;

fn previews(harness: &Harness) -> PathBuf {
    harness.directory.path().join("publications/previews")
}

fn entry(harness: &Harness) -> PathBuf {
    previews(harness).join(IMAGE.trim_start_matches("sha256:"))
}

/// The inventory a restarted controller process has: the same daemon and disk, nothing in memory.
fn restarted(harness: &Harness) -> LocalSnapshots {
    LocalSnapshots::new(
        harness.assistants.stage.clone(),
        Arc::new(MemoryAudit::default()),
        previews(harness),
    )
}

/// Waits for the background store to hold an intact entry of [`IMAGE`].
async fn stored(harness: &Harness) {
    let store = PreviewStore::new(previews(harness));
    for _ in 0..2_000 {
        if store.load(IMAGE).is_some() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    panic!("the preview files were never stored");
}

fn readers(harness: &Harness) -> Vec<String> {
    harness
        .docker
        .container_names()
        .into_iter()
        .filter(|name| name.contains("-snapshot-"))
        .collect()
}

#[tokio::test]
async fn a_restart_serves_the_stored_preview_without_reading_the_image() {
    let harness = reviewing_harness().await;
    stage(&harness, &reference("shimpz.toml"));
    let page = harness
        .assistants
        .local
        .details(IMAGE, "pt")
        .await
        .expect("read from the image");
    let icon = harness.assistants.local.icon(IMAGE).await.expect("an icon");
    stored(&harness).await;
    let reads = harness.docker.archive_reads();

    let local = restarted(&harness);
    assert_eq!(local.details(IMAGE, "pt").await, Ok(page));
    assert_eq!(local.icon(IMAGE).await, Ok(icon));
    assert_eq!(harness.docker.archive_reads(), reads);
    assert_eq!(readers(&harness), Vec::<String>::new());
}

#[tokio::test]
async fn the_listing_stores_the_preview_files_it_admitted() {
    let harness = reviewing_harness().await;
    stage(&harness, &reference("shimpz.toml"));
    let actor = crate::audit::Principal::team();
    harness.assistants.local.list(&actor).await.expect("listed");
    stored(&harness).await;
    let reads = harness.docker.archive_reads();
    restarted(&harness)
        .details(IMAGE, "en")
        .await
        .expect("a stored page");
    assert_eq!(harness.docker.archive_reads(), reads);
}

#[tokio::test]
async fn a_corrupt_or_disagreeing_stored_entry_is_read_from_the_image_again() {
    let harness = reviewing_harness().await;
    let manifest = reference("shimpz.toml");
    stage(&harness, &manifest);
    let expected = harness
        .assistants
        .local
        .details(IMAGE, "pt")
        .await
        .expect("read from the image");
    stored(&harness).await;
    let store = PreviewStore::new(previews(&harness));
    let mut files = store.load(IMAGE).expect("stored");

    // Garbage in place of the entry is a miss.
    std::fs::write(entry(&harness), b"garbage").expect("written");
    let reads = harness.docker.archive_reads();
    assert_eq!(
        restarted(&harness).details(IMAGE, "pt").await,
        Ok(expected.clone())
    );
    assert_eq!(harness.docker.archive_reads(), reads + 4);
    stored(&harness).await;

    // An intact entry whose files disagree with the image's labels fails validation again on load.
    let drifted = String::from_utf8(manifest)
        .expect("UTF-8")
        .replace(r#"version = "0.1.0""#, r#"version = "0.1.1""#);
    files.insert(MANIFEST_PATH.to_owned(), drifted.into_bytes());
    store.save(IMAGE, &files);
    let reads = harness.docker.archive_reads();
    assert_eq!(restarted(&harness).details(IMAGE, "pt").await, Ok(expected));
    assert_eq!(harness.docker.archive_reads(), reads + 4);
}
