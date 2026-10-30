//! The Team-scoped publication registry as the controller uses it: the bindings, icons, update transactions, and
//! residues, read off the async workers, and the [`Bindings`] seam the Assistant containers see.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use team_core::future::BoxFuture;
use team_install::bindings::{Binding, BindingError, BindingStore, LocalRecordValidator};
use team_install::icons::{IconError, IconStore};
use team_install::update::{ResidueStore, UpdateStore};

use super::problem::{BINDING_CONFLICT, LIMIT_REACHED};
use super::snapshots::record_valid;
use super::spec::AssistantSpec;
use crate::assistant::{Bindings, BindingsUnavailable, ContainerSpec};
use crate::docker::problem::INTERNAL;
use crate::http::service::Problem;

/// The registry and its companion stores, under `publications/`.
#[derive(Debug)]
pub struct Registry {
    store: Arc<BindingStore>,
    icons: Arc<IconStore>,
    updates: PathBuf,
    residues: Arc<ResidueStore>,
}

/// A registry failure as the install routes report it.
#[must_use]
pub fn binding_problem(error: BindingError) -> Problem {
    match error {
        BindingError::LimitReached => LIMIT_REACHED,
        BindingError::Conflict | BindingError::Invalid => BINDING_CONFLICT,
        BindingError::Unavailable => INTERNAL,
    }
}

impl Registry {
    /// The registry under `root` (`publications/`).
    #[must_use]
    pub fn new(root: &Path) -> Self {
        Self::with_store(root, |store| store)
    }

    /// The registry under `root` admitting resolutions whose images live outside the production repository, for the
    /// controller suite's fixture publications (feature `testing`). Never compiled into a shipped binary.
    #[cfg(feature = "testing")]
    #[must_use]
    pub fn admitting_any_resolution(root: &Path) -> Self {
        Self::with_store(root, BindingStore::admitting_any_resolution)
    }

    fn with_store(root: &Path, admission: impl FnOnce(BindingStore) -> BindingStore) -> Self {
        let validator: LocalRecordValidator = Arc::new(record_valid);
        let store = admission(BindingStore::new(&root.join("bindings.json"), validator));
        Self {
            store: Arc::new(store),
            icons: IconStore::new(&root.join("icons")),
            updates: root.join("updates"),
            residues: Arc::new(ResidueStore::new(&root.join("residues"))),
        }
    }

    /// The icon store.
    #[must_use]
    pub fn icons(&self) -> &IconStore {
        &self.icons
    }

    /// Runs `operation` on the binding store off the async workers.
    ///
    /// # Errors
    /// `internal-error` when the blocking task did not complete.
    pub async fn store<T: Send + 'static>(
        &self,
        operation: impl FnOnce(&BindingStore) -> T + Send + 'static,
    ) -> Result<T, Problem> {
        let store = Arc::clone(&self.store);
        tokio::task::spawn_blocking(move || operation(&store))
            .await
            .map_err(|_| INTERNAL)
    }

    /// Runs `operation` on the update transactions off the async workers.
    ///
    /// # Errors
    /// `internal-error` when the blocking task did not complete.
    pub async fn updates<T: Send + 'static>(
        &self,
        operation: impl FnOnce(&UpdateStore<'_>) -> T + Send + 'static,
    ) -> Result<T, Problem> {
        let (store, root) = (Arc::clone(&self.store), self.updates.clone());
        tokio::task::spawn_blocking(move || operation(&UpdateStore::new(&root, &store)))
            .await
            .map_err(|_| INTERNAL)
    }

    /// Every active update transaction.
    ///
    /// # Errors
    /// `internal-error` when the transactions cannot be read.
    pub async fn list_updates(
        &self,
    ) -> Result<Vec<team_install::update::AssistantUpdate>, Problem> {
        self.updates(list_updates).await?.map_err(|_| INTERNAL)
    }

    /// Runs `operation` on the residue queue off the async workers.
    ///
    /// # Errors
    /// `internal-error` when the blocking task did not complete.
    pub async fn residues<T: Send + 'static>(
        &self,
        operation: impl FnOnce(&ResidueStore) -> T + Send + 'static,
    ) -> Result<T, Problem> {
        let residues = Arc::clone(&self.residues);
        tokio::task::spawn_blocking(move || operation(&residues))
            .await
            .map_err(|_| INTERNAL)
    }

    /// One Team's binding of one Assistant.
    ///
    /// # Errors
    /// `internal-error` when the registry cannot be read.
    pub async fn binding(
        &self,
        team_id: &str,
        assistant_id: &str,
    ) -> Result<Option<Binding>, Problem> {
        let (team, assistant) = (team_id.to_owned(), assistant_id.to_owned());
        self.store(move |store| store.get(&team, &assistant))
            .await?
            .map_err(|_| INTERNAL)
    }

    /// Every binding of the Space.
    ///
    /// # Errors
    /// `internal-error` when the registry cannot be read.
    pub async fn snapshot(&self) -> Result<Vec<Binding>, Problem> {
        self.store(BindingStore::snapshot)
            .await?
            .map_err(|_| INTERNAL)
    }

    /// A reader of every current binding, for the icon store's reference checks.
    pub fn references(
        &self,
    ) -> impl Fn() -> Result<Vec<Binding>, BindingError> + Send + Sync + use<> {
        let store = Arc::clone(&self.store);
        move || store.snapshot()
    }

    /// Deletes `binding` and its icon in one custody transaction, keeping an icon another binding or install holds
    /// (blocking).
    ///
    /// # Errors
    /// [`IconError`] for the icon or the registry; the binding then stays as the retry anchor.
    pub fn retire(&self, binding: &Binding) -> Result<(), IconError> {
        let references = || self.store.snapshot();
        self.icons.retire(binding, &references, || {
            self.store
                .delete(&binding.team_id, binding.assistant_id())
                .map(|_| ())
        })
    }

    /// The spec of a binding.
    ///
    /// # Errors
    /// `internal-error` for a binding whose document holds no valid runtime contract.
    pub fn spec(binding: &Binding) -> Result<AssistantSpec, Problem> {
        AssistantSpec::of(binding).ok_or(INTERNAL)
    }
}

impl Bindings for Registry {
    fn spec<'a>(
        &'a self,
        team_id: &'a str,
        assistant_id: &'a str,
    ) -> BoxFuture<'a, Result<Option<ContainerSpec>, BindingsUnavailable>> {
        Box::pin(async move {
            let binding = self
                .binding(team_id, assistant_id)
                .await
                .map_err(|_| BindingsUnavailable)?;
            binding
                .map(|binding| {
                    AssistantSpec::of(&binding)
                        .map(|spec| spec.container_spec())
                        .ok_or(BindingsUnavailable)
                })
                .transpose()
        })
    }

    fn identities(&self) -> BoxFuture<'_, Result<Vec<(String, String)>, BindingsUnavailable>> {
        Box::pin(async move {
            let bindings = self.snapshot().await.map_err(|_| BindingsUnavailable)?;
            Ok(bindings
                .iter()
                .map(|binding| (binding.team_id.clone(), binding.assistant_id().to_owned()))
                .collect())
        })
    }
}

fn list_updates(
    updates: &UpdateStore<'_>,
) -> Result<Vec<team_install::update::AssistantUpdate>, BindingError> {
    updates.list()
}
