//! The staged snapshot preview files Team already read and validated, kept on its private publications volume so a
//! restart does not read them out of the image again.
//!
//! An entry holds the exact bytes of the files a preview reads, keyed by the image id: a content digest, so the same
//! id always names the same bytes. The entry is sealed with a SHA-256 over the image id and its bytes, and every load
//! validates them again exactly as a fresh read is validated. An entry that is absent, outside its custody,
//! oversized, malformed, or unsealed is a miss, and the preview is read from the image again. The store keeps at most
//! [`MAX_ENTRIES`] entries and [`MAX_BYTES`] bytes, evicting the oldest first, and only entries of currently staged
//! images.

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};
use team_protocol::payload::SOURCE_DIGEST_RE;
use team_storage::private::{read_private, write_private};

use super::{Files, MAX_CANDIDATES, PREVIEW_FILES};

/// The most entries the store keeps.
pub const MAX_ENTRIES: usize = MAX_CANDIDATES;
/// The most bytes all entries together hold.
pub const MAX_BYTES: u64 = 32 * 1024 * 1024;
const SEAL_BYTES: usize = 32;
const LENGTH_BYTES: usize = 4;
/// The largest entry: every preview file at its bound, framed and sealed.
const MAX_ENTRY_BYTES: usize = {
    let mut total = SEAL_BYTES;
    let mut index = 0;
    while index < PREVIEW_FILES.len() {
        total += LENGTH_BYTES + PREVIEW_FILES[index].2;
        index += 1;
    }
    total
};

/// The preview store below one private directory.
#[derive(Clone, Debug)]
pub struct PreviewStore {
    root: PathBuf,
}

impl PreviewStore {
    /// The store below `root`, created owner-only when first written.
    #[must_use]
    pub fn new(root: PathBuf) -> Self {
        Self { root }
    }

    fn path(&self, image_id: &str) -> Option<PathBuf> {
        SOURCE_DIGEST_RE
            .is_full(image_id)
            .then(|| image_id.strip_prefix("sha256:"))
            .flatten()
            .map(|name| self.root.join(name))
    }

    /// The stored files of `image_id`, or `None` when it has no intact entry; a malformed entry is removed.
    #[must_use]
    pub fn load(&self, image_id: &str) -> Option<Files> {
        let path = self.path(image_id)?;
        let entry = read_private(&path, MAX_ENTRY_BYTES as u64).ok()??;
        let files = decode(image_id, &entry);
        if files.is_none() {
            let _ = fs::remove_file(&path);
        }
        files
    }

    /// Keeps `files` as the entry of `image_id`, then evicts the oldest entries beyond the bounds. Best effort: a
    /// failure leaves at worst a miss.
    pub fn save(&self, image_id: &str, files: &Files) {
        let (Some(path), Some(entry)) = (self.path(image_id), encode(image_id, files)) else {
            return;
        };
        if write_private(&path, &entry).is_ok() {
            self.evict();
        }
    }

    /// Removes every entry whose image is not one of `staged`.
    pub fn retain(&self, staged: &HashSet<String>) {
        for (_, name, _) in self.entries() {
            if !staged.contains(&format!("sha256:{name}")) {
                let _ = fs::remove_file(self.root.join(name));
            }
        }
    }

    /// Every entry with its modification time and size, oldest first; anything else in the directory is left alone.
    fn entries(&self) -> Vec<(std::time::SystemTime, String, u64)> {
        let Ok(listing) = fs::read_dir(&self.root) else {
            return Vec::new();
        };
        let mut entries: Vec<_> = listing
            .filter_map(|entry| {
                let entry = entry.ok()?;
                let name = entry.file_name().into_string().ok()?;
                // The entry's own metadata, never a link's target.
                let metadata = entry.metadata().ok()?;
                (SOURCE_DIGEST_RE.is_full(&format!("sha256:{name}")) && metadata.is_file())
                    .then_some(())?;
                Some((metadata.modified().ok()?, name, metadata.len()))
            })
            .collect();
        entries.sort();
        entries
    }

    fn evict(&self) {
        let entries = self.entries();
        let (mut count, mut total) = (
            entries.len(),
            entries.iter().map(|(_, _, size)| size).sum::<u64>(),
        );
        for (_, name, size) in entries {
            if count <= MAX_ENTRIES && total <= MAX_BYTES {
                break;
            }
            if fs::remove_file(self.root.join(&name)).is_ok() {
                count -= 1;
                total -= size;
            }
        }
    }

    /// The directory the store keeps its entries in.
    #[must_use]
    pub fn root(&self) -> &Path {
        &self.root
    }
}

fn seal(image_id: &str, body: &[u8]) -> [u8; SEAL_BYTES] {
    let mut digest = Sha256::new();
    digest.update(image_id.as_bytes());
    digest.update([0]);
    digest.update(body);
    digest.finalize().into()
}

/// Each preview file in [`PREVIEW_FILES`] order as a four-byte big-endian length and its bytes, then the seal.
fn encode(image_id: &str, files: &Files) -> Option<Vec<u8>> {
    let mut entry = Vec::new();
    for (path, _, maximum) in PREVIEW_FILES {
        let bytes = files.get(path)?;
        if bytes.is_empty() || bytes.len() > maximum {
            return None;
        }
        entry.extend_from_slice(&u32::try_from(bytes.len()).ok()?.to_be_bytes());
        entry.extend_from_slice(bytes);
    }
    let sealed = seal(image_id, &entry);
    entry.extend_from_slice(&sealed);
    Some(entry)
}

fn decode(image_id: &str, entry: &[u8]) -> Option<Files> {
    let (body, sealed) = entry.split_at_checked(entry.len().checked_sub(SEAL_BYTES)?)?;
    if seal(image_id, body) != sealed {
        return None;
    }
    let mut rest = body;
    let mut files = Files::new();
    for (path, _, maximum) in PREVIEW_FILES {
        let (length, tail) = rest.split_first_chunk::<LENGTH_BYTES>()?;
        let length = usize::try_from(u32::from_be_bytes(*length)).ok()?;
        if length == 0 || length > maximum {
            return None;
        }
        let (bytes, tail) = tail.split_at_checked(length)?;
        files.insert(path.to_owned(), bytes.to_vec());
        rest = tail;
    }
    rest.is_empty().then_some(files)
}

#[cfg(test)]
mod tests;
