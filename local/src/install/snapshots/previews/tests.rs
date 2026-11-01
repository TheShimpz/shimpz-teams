//! The stored preview files: intact entries round-trip, anything else is a miss, and the bounds hold.

use std::collections::HashSet;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::time::{Duration, SystemTime};

use super::{MAX_BYTES, MAX_ENTRIES, PreviewStore};
use crate::install::snapshots::Files;
use crate::install::snapshots::PREVIEW_FILES;

fn id(index: usize) -> String {
    format!("sha256:{index:064x}")
}

/// One file of `size` bytes (within each file's bound) at every preview path.
fn files(size: usize) -> Files {
    PREVIEW_FILES
        .iter()
        .map(|(path, _, maximum)| ((*path).to_owned(), vec![b'x'; size.min(*maximum)]))
        .collect()
}

fn entry_count(store: &PreviewStore) -> usize {
    store.entries().len()
}

/// Gives the entry of `image_id` the modification time `seconds` after the epoch.
fn age(store: &PreviewStore, image_id: &str, seconds: u64) {
    let file = fs::File::options()
        .write(true)
        .open(store.path(image_id).expect("a path"))
        .expect("opened");
    file.set_modified(SystemTime::UNIX_EPOCH + Duration::from_secs(seconds))
        .expect("aged");
}

#[test]
fn an_intact_entry_round_trips_owner_only() {
    let root = tempfile::tempdir().expect("a temporary directory");
    let store = PreviewStore::new(root.path().join("previews"));
    store.save(&id(1), &files(10));
    assert_eq!(store.load(&id(1)), Some(files(10)));
    let path = store.path(&id(1)).expect("a path");
    assert_eq!(
        fs::metadata(&path).expect("stat").permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(
        fs::metadata(store.root())
            .expect("stat")
            .permissions()
            .mode()
            & 0o777,
        0o700
    );
    assert_eq!(store.load(&id(2)), None);
    assert_eq!(store.load("sha256:short"), None);
}

#[test]
fn a_corrupt_moved_or_exposed_entry_is_a_miss() {
    let root = tempfile::tempdir().expect("a temporary directory");
    let store = PreviewStore::new(root.path().join("previews"));
    store.save(&id(1), &files(10));
    let path = store.path(&id(1)).expect("a path");

    // One flipped byte breaks the seal; the malformed entry is removed.
    let mut bytes = fs::read(&path).expect("read");
    bytes[6] ^= 1;
    fs::write(&path, &bytes).expect("written");
    assert_eq!(store.load(&id(1)), None);
    assert!(!path.exists());

    // Another image's intact entry is sealed to that image.
    store.save(&id(2), &files(10));
    fs::copy(store.path(&id(2)).expect("a path"), &path).expect("copied");
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).expect("owner-only");
    assert_eq!(store.load(&id(1)), None);

    // An entry outside its custody is never read.
    store.save(&id(3), &files(10));
    let exposed = store.path(&id(3)).expect("a path");
    fs::set_permissions(&exposed, fs::Permissions::from_mode(0o644)).expect("exposed");
    assert_eq!(store.load(&id(3)), None);

    // A truncated entry is a miss.
    store.save(&id(4), &files(10));
    let truncated = store.path(&id(4)).expect("a path");
    let bytes = fs::read(&truncated).expect("read");
    fs::write(&truncated, &bytes[..bytes.len() / 2]).expect("written");
    assert_eq!(store.load(&id(4)), None);
}

#[test]
fn files_outside_their_bounds_are_never_stored() {
    let root = tempfile::tempdir().expect("a temporary directory");
    let store = PreviewStore::new(root.path().join("previews"));
    let mut oversized = files(10);
    let (icon, _, maximum) = PREVIEW_FILES[3];
    oversized.insert(icon.to_owned(), vec![0; maximum + 1]);
    store.save(&id(1), &oversized);
    let mut missing = files(10);
    missing.remove(icon);
    store.save(&id(2), &missing);
    let mut empty = files(10);
    empty.insert(icon.to_owned(), Vec::new());
    store.save(&id(3), &empty);
    assert_eq!(entry_count(&store), 0);
}

#[test]
fn the_oldest_entries_go_first_beyond_the_entry_bound() {
    let root = tempfile::tempdir().expect("a temporary directory");
    let store = PreviewStore::new(root.path().join("previews"));
    for index in 0..MAX_ENTRIES {
        store.save(&id(index), &files(10));
        age(&store, &id(index), 1_000 + index as u64);
    }
    // Something else in the directory is neither counted nor removed.
    fs::write(store.root().join("notes"), b"kept").expect("written");
    assert_eq!(entry_count(&store), MAX_ENTRIES);
    store.save(&id(MAX_ENTRIES), &files(10));
    assert_eq!(entry_count(&store), MAX_ENTRIES);
    assert_eq!(store.load(&id(0)), None);
    assert!(store.load(&id(1)).is_some());
    assert!(store.load(&id(MAX_ENTRIES)).is_some());
    assert!(store.root().join("notes").exists());
}

#[test]
fn the_oldest_entries_go_first_beyond_the_byte_bound() {
    let root = tempfile::tempdir().expect("a temporary directory");
    let store = PreviewStore::new(root.path().join("previews"));
    // Every file at its bound: about 3.8 MiB an entry, so nine exceed the byte bound.
    let largest = files(usize::MAX);
    let count = 9;
    for index in 0..count {
        store.save(&id(index), &largest);
        age(&store, &id(index), 1_000 + index as u64);
    }
    let total: u64 = store.entries().iter().map(|(_, _, size)| size).sum();
    assert!(total <= MAX_BYTES);
    assert!(entry_count(&store) < count);
    assert_eq!(store.load(&id(0)), None);
    assert!(store.load(&id(count - 1)).is_some());
}

#[test]
fn only_staged_images_keep_their_entries() {
    let root = tempfile::tempdir().expect("a temporary directory");
    let store = PreviewStore::new(root.path().join("previews"));
    for index in 0..3 {
        store.save(&id(index), &files(10));
    }
    store.retain(&HashSet::from([id(1)]));
    assert_eq!(store.load(&id(0)), None);
    assert!(store.load(&id(1)).is_some());
    assert_eq!(store.load(&id(2)), None);
    assert_eq!(entry_count(&store), 1);
}
