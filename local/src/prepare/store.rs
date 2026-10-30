//! Team files (`team_storage::files::FileStore`) as attachment preparation and the Files service reach them: each
//! SQLite call on the blocking pool, and an acknowledgement that commits only once its audit event is recorded.
//!
//! The Team lifecycle answers whether a Team exists through [`TeamPresence`], which the composition root wires.

use std::sync::Arc;

use team_core::audit::{AuditEvent, AuditSink};
use team_core::future::BoxFuture;
use team_storage::files::{
    FileMetadata, FileStore, Listing, StorageError, StorageErrorKind, StoredFile, Upload,
};

use crate::http::service::{FileUpload, Problem};

/// Whether a Team exists, as the Team lifecycle answers it.
pub trait TeamPresence: Send + Sync {
    /// Succeeds when `team_id` names an existing Team of this Space.
    ///
    /// # Errors
    /// The Team lifecycle's [`Problem`] (an absent Team, or Docker unavailable).
    fn require<'a>(&'a self, team_id: &'a str) -> BoxFuture<'a, Result<(), Problem>>;
}

/// An acknowledgement that was not committed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Unacknowledged {
    /// The store refused it.
    Storage(StorageError),
    /// Its audit event was not recorded, so nothing was committed.
    Unrecorded,
}

impl From<StorageError> for Unacknowledged {
    fn from(error: StorageError) -> Self {
        Self::Storage(error)
    }
}

const POOL_FAILED: StorageError = StorageError {
    kind: StorageErrorKind::Unsafe,
    message: "Team storage transaction failed",
};

/// The Team files store, reached from async code.
#[derive(Clone)]
pub struct Store(pub Arc<FileStore>);

impl std::fmt::Debug for Store {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Store")
    }
}

impl Store {
    async fn blocking<T: Send + 'static>(
        &self,
        work: impl FnOnce(&FileStore) -> Result<T, StorageError> + Send + 'static,
    ) -> Result<T, StorageError> {
        let store = Arc::clone(&self.0);
        tokio::task::spawn_blocking(move || work(&store))
            .await
            .unwrap_or(Err(POOL_FAILED))
    }

    /// The Team's listing.
    ///
    /// # Errors
    /// [`StorageError`] when it cannot be read.
    pub async fn list(&self, team_id: &str) -> Result<Listing, StorageError> {
        let team = team_id.to_owned();
        self.blocking(move |store| store.list(&team)).await
    }

    /// Stores one upload (never from the web).
    ///
    /// # Errors
    /// [`StorageError`] when it is refused or cannot be stored.
    pub async fn put(&self, team_id: &str, upload: &FileUpload) -> Result<Upload, StorageError> {
        let (team, upload) = (team_id.to_owned(), upload.clone());
        self.blocking(move |store| {
            store.put(
                &team,
                &upload.filename,
                &upload.content,
                Some(&upload.media_type),
                false,
            )
        })
        .await
    }

    /// One listed file's metadata.
    ///
    /// # Errors
    /// [`StorageError`] when the listing no longer shows it.
    pub async fn listed(&self, team_id: &str, file_id: &str) -> Result<FileMetadata, StorageError> {
        let (team, file) = (team_id.to_owned(), file_id.to_owned());
        self.blocking(move |store| store.listed(&team, &file)).await
    }

    /// One file's metadata and verified bytes.
    ///
    /// # Errors
    /// [`StorageError`] when it is absent or fails its integrity check.
    pub async fn read(
        &self,
        team_id: &str,
        file_id: &str,
    ) -> Result<(StoredFile, Vec<u8>), StorageError> {
        let (team, file) = (team_id.to_owned(), file_id.to_owned());
        self.blocking(move |store| store.get(&team, &file)).await
    }

    /// Whether the original may leave Team for `principal`.
    ///
    /// # Errors
    /// [`StorageError`] when it is absent or cannot be read.
    pub async fn released(
        &self,
        team_id: &str,
        file_id: &str,
        principal: Option<&str>,
    ) -> Result<bool, StorageError> {
        let (team, file) = (team_id.to_owned(), file_id.to_owned());
        let principal = principal.map(str::to_owned);
        self.blocking(move |store| store.released(&team, &file, principal.as_deref()))
            .await
    }

    /// Records `principal`'s acknowledgement of a file from the web, committed only once `event` is recorded.
    ///
    /// # Errors
    /// [`Unacknowledged`] when it is refused or its event is not recorded.
    pub async fn acknowledge(
        &self,
        team_id: &str,
        file_id: &str,
        principal: &str,
        audit: Arc<dyn AuditSink>,
        event: AuditEvent,
    ) -> Result<(), Unacknowledged> {
        let store = Arc::clone(&self.0);
        let (team, file, who) = (team_id.to_owned(), file_id.to_owned(), principal.to_owned());
        let runtime = tokio::runtime::Handle::current();
        tokio::task::spawn_blocking(move || {
            store.acknowledge(&team, &file, &who, |_| {
                runtime
                    .block_on(audit.record(event))
                    .map_err(|_| Unacknowledged::Unrecorded)
            })
        })
        .await
        .unwrap_or(Err(Unacknowledged::Storage(POOL_FAILED)))
    }
}
