//! One Save of a checked-out Sign-in: its chunks sealed as they arrive, its record written only by [`Draft::commit`].

use std::fs::DirBuilder;
use std::os::unix::fs::DirBuilderExt;
use std::path::PathBuf;
use std::sync::Arc;

use aes_gcm::Aes256Gcm;
use aes_gcm::aead::{Aead, KeyInit, Nonce, Payload};
use strict_json::Value;
use team_protocol::sign_in::{MAX_SITES, canonical_site};
use team_storage::durable;
use team_storage::envelope::{Key, NONCE_BYTES};

use super::{CHUNK_BYTES, Checkout, Kind, MAX_CHUNKS, READY, SignInError, SignInStore, chunk_aad};

fn cipher(key: &Key) -> Aes256Gcm {
    Aes256Gcm::new_from_slice(key.expose()).unwrap_or_else(|_| unreachable!())
}

/// `nonce ‖ ciphertext ‖ tag` of one chunk.
fn seal_chunk(key: &Key, nonce: [u8; NONCE_BYTES], chunk: &[u8], aad: &[u8]) -> Vec<u8> {
    // AES-GCM refuses only a plaintext or associated data beyond 2^36 bytes; a chunk is at most 1 MiB.
    let ciphertext = cipher(key)
        .encrypt(
            &Nonce::<Aes256Gcm>::from(nonce),
            Payload { msg: chunk, aad },
        )
        .unwrap_or_else(|_| unreachable!());
    [&nonce[..], &ciphertext].concat()
}

/// One sealed chunk opened under `key` and `aad`, or `None` when it is not this capsule's.
pub(super) fn open_chunk(key: &Key, sealed: &[u8], aad: &[u8]) -> Option<Vec<u8>> {
    let nonce: [u8; NONCE_BYTES] = sealed.get(..NONCE_BYTES)?.try_into().ok()?;
    cipher(key)
        .decrypt(
            &Nonce::<Aes256Gcm>::from(nonce),
            Payload {
                msg: &sealed[NONCE_BYTES..],
                aad,
            },
        )
        .ok()
}

/// One Save's next generation.
pub struct Draft {
    store: Arc<SignInStore>,
    checkout: Checkout,
    fence: u64,
    generation: i64,
    chunks: usize,
    directory: PathBuf,
    key: Option<Key>,
    // Set when the commit's outcome is unknown: what it wrote stays for the next Team start to reconcile.
    uncertain: bool,
}

impl std::fmt::Debug for Draft {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Draft")
            .field("generation", &self.generation)
            .field("chunks", &self.chunks)
            .finish_non_exhaustive()
    }
}

impl Draft {
    pub(super) fn new(store: Arc<SignInStore>, checkout: Checkout, fence: u64) -> Self {
        let directory = store.drafts(&checkout.team_id).join(&checkout.id);
        Self {
            generation: checkout.generation + 1,
            store,
            checkout,
            fence,
            chunks: 0,
            directory,
            key: None,
            uncertain: false,
        }
    }

    /// The generation it writes.
    #[must_use]
    pub fn generation(&self) -> i64 {
        self.generation
    }

    /// Seals one more chunk of the capsule, under the store's lock and its Team's fence.
    ///
    /// # Errors
    /// [`SignInError`] for a chunk outside its bounds, a revoked Save, or a store failure.
    pub fn add(&mut self, chunk: &[u8]) -> Result<(), SignInError> {
        if chunk.is_empty() || chunk.len() > CHUNK_BYTES || self.chunks >= MAX_CHUNKS {
            return Err(SignInError::FAILED);
        }
        let store = Arc::clone(&self.store);
        let fences = store.lock();
        if fences
            .saves
            .get(&self.checkout.team_id)
            .copied()
            .unwrap_or(0)
            != self.fence
        {
            return Err(SignInError::FAILED);
        }
        if self.key.is_none() {
            let key = store.key(true)?;
            if let Some(parent) = self.directory.parent() {
                durable::create_private_directory(parent).map_err(|_| SignInError::FAILED)?;
            }
            DirBuilder::new()
                .mode(0o700)
                .create(&self.directory)
                .map_err(|_| SignInError::FAILED)?;
            self.key = Some(key);
        }
        let key = self.key.as_ref().ok_or(SignInError::FAILED)?;
        let mut nonce = [0_u8; NONCE_BYTES];
        store
            .entropy
            .fill(&mut nonce)
            .map_err(|_| SignInError::FAILED)?;
        let aad = chunk_aad(&self.checkout, self.generation, self.chunks);
        let sealed = seal_chunk(key, nonce, chunk, &aad);
        durable::replace(
            &self.directory.join(format!("{:05}.chunk", self.chunks)),
            &sealed,
            0o600,
            None,
        )
        .map_err(|_| SignInError::FAILED)?;
        drop(fences);
        self.chunks += 1;
        Ok(())
    }

    /// Commits the next generation, only if the checkout is still current, nothing fenced it, and `admitted()`.
    ///
    /// # Errors
    /// [`SignInError`] when the capsule or sites are not admissible, the Save was revoked, or the store failed.
    pub fn commit(
        &mut self,
        sites: &[String],
        truncated: bool,
        writer: &str,
        admitted: impl FnOnce() -> bool,
    ) -> Result<Value, SignInError> {
        let sites_admitted = sites
            .iter()
            .all(|site| canonical_site(&Value::String(site.clone())).is_some());
        if self.chunks == 0 || !sites_admitted {
            return Err(SignInError::FAILED);
        }
        let store = Arc::clone(&self.store);
        let mut fences = store.lock();
        let current = store.held(&self.checkout)?;
        let revoked = fences
            .saves
            .get(&self.checkout.team_id)
            .copied()
            .unwrap_or(0)
            != self.fence;
        let Some((teams, mut record)) = current.filter(|_| !revoked && admitted()) else {
            return Err(SignInError::FAILED);
        };
        self.publish_capsule()?;
        let now = store.now();
        let earlier = record.generation;
        let mut sorted = sites.to_vec();
        sorted.sort();
        sorted.truncate(MAX_SITES);
        record.generation = self.generation;
        READY.clone_into(&mut record.state);
        record.sites = sorted;
        record.sites_truncated = truncated;
        record.saved_at = now;
        record.last_used_at = now;
        record.chunks = i64::try_from(self.chunks).unwrap_or(i64::MAX);
        writer.clone_into(&mut record.writer);
        record.checkout = None;
        if store.put(teams, &record).is_err() && !self.published(&mut fences.quarantine, &record) {
            return Err(SignInError::FAILED);
        }
        if self.checkout.kind == Kind::Person {
            // A person's Save resolves whatever an earlier unknown publication left.
            fences.quarantine.remove(&self.checkout.team_id);
        }
        drop(fences);
        if earlier != 0 {
            // The generation this one replaced; one left behind is deleted at the next Team start.
            let _ = std::fs::remove_dir_all(store.capsule_dir(&self.checkout.sign_in_id, earlier));
        }
        Ok(record.public(false))
    }

    /// Moves the sealed chunks into place as the next generation's capsule, durably.
    fn publish_capsule(&mut self) -> Result<(), SignInError> {
        let failed = |_| SignInError::FAILED;
        durable::fsync_directory(&self.directory).map_err(failed)?;
        let capsule = self
            .store
            .capsule_dir(&self.checkout.sign_in_id, self.generation);
        let parent = capsule
            .parent()
            .map(std::path::Path::to_path_buf)
            .ok_or(SignInError::FAILED)?;
        durable::create_private_directory(&parent).map_err(failed)?;
        std::fs::rename(&self.directory, &capsule).map_err(failed)?;
        // From here a discard deletes the capsule where it now is.
        self.directory = capsule;
        durable::fsync_directory(&parent).map_err(failed)
    }

    /// After a failed write of the index: whether it names this generation anyway (its replacement happened). When
    /// that is unknowable, the capsule stays where it is, its generation needs a person, and the Save is refused.
    fn published(
        &mut self,
        quarantine: &mut std::collections::HashMap<String, i64>,
        record: &super::Record,
    ) -> bool {
        let kept = self
            .store
            .record(&self.checkout.team_id)
            .map(|(_teams, kept)| kept);
        match kept {
            Ok(Some(kept)) if kept == *record => {
                #[cfg(test)]
                if self.store.faults().index_unsynced {
                    self.uncertain = true;
                    quarantine.insert(self.checkout.team_id.clone(), self.generation);
                    return false;
                }
                let index = self.store.index.parent().map(durable::fsync_directory);
                if matches!(index, Some(Ok(()))) {
                    return true;
                }
            }
            Ok(_) => return false,
            Err(_) => {}
        }
        self.uncertain = true;
        quarantine.insert(self.checkout.team_id.clone(), self.generation);
        false
    }

    /// Deletes what the Save wrote; anything left is deleted when the Team process next starts.
    pub fn discard(&self) {
        if !self.uncertain {
            let _ = std::fs::remove_dir_all(&self.directory);
        }
    }
}
