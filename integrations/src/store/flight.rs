//! The single-flight operations of one Integration: refresh on use, and replacement on reconsent.
//!
//! Each holds that Integration's own lock across its broker calls, so a second resolve waits for the first refresh
//! and reuses its result, while every other Integration proceeds. Every state read and write runs on the blocking
//! pool.

use std::sync::Arc;

use team_core::future::BoxFuture;
use team_core::secret::Secret;
use team_storage::private::StateError;
use team_storage::record_store::Outcome;

use super::record::Grant;
use super::{
    Identity, IntegrationStore, Metadata, REFRESH_WINDOW_SECONDS, Reference, Revocation, Revoke,
    StoreError, Stored, intent, set_record, stored,
};
use crate::token::TokenSet;

/// Holds one Integration's flight; the last holder removes its entry.
struct Flight<'a> {
    store: &'a IntegrationStore,
    reference: Reference,
    guard: Option<tokio::sync::OwnedMutexGuard<()>>,
}

impl Drop for Flight<'_> {
    fn drop(&mut self) {
        let Ok(mut flights) = self.store.flights.lock() else {
            return;
        };
        self.guard.take();
        if flights
            .get(&self.reference)
            .is_some_and(|entry| Arc::strong_count(entry) == 1)
        {
            flights.remove(&self.reference);
        }
    }
}

impl IntegrationStore {
    async fn flight(&self, reference: &Reference) -> Result<Flight<'_>, StoreError> {
        let lock = {
            let mut flights = self
                .flights
                .lock()
                .map_err(|_| StoreError::State(StateError::Unavailable))?;
            Arc::clone(flights.entry(reference.clone()).or_default())
        };
        let guard = lock.lock_owned().await;
        Ok(Flight {
            store: self,
            reference: reference.clone(),
            guard: Some(guard),
        })
    }

    /// Whether no flight is held or awaited (every entry is removed by its last holder).
    #[must_use]
    pub fn idle(&self) -> bool {
        self.flights.lock().is_ok_and(|flights| flights.is_empty())
    }

    async fn blocking<T: Send + 'static>(
        self: &Arc<Self>,
        work: impl FnOnce(&Self) -> Result<T, StoreError> + Send + 'static,
    ) -> Result<T, StoreError> {
        let store = Arc::clone(self);
        tokio::task::spawn_blocking(move || work(&store))
            .await
            .map_err(|_| StoreError::State(StateError::Unavailable))?
    }

    /// One declared Integration's access token, refreshed once through `refresh` when it expires within the refresh
    /// window. Concurrent resolves of the same Integration share one refresh.
    ///
    /// # Errors
    /// [`StoreError::Missing`], [`StoreError::Reauthorization`] (demoted, drifted, no refresh token, or changed during
    /// the refresh), [`StoreError::Exchange`] from `refresh`, or a state failure.
    pub async fn resolve<F>(
        self: &Arc<Self>,
        reference: (&str, &str, &str),
        declaration: (&str, &[String]),
        refresh: F,
    ) -> Result<Secret, StoreError>
    where
        F: FnOnce(Secret, Option<Secret>) -> BoxFuture<'static, Result<TokenSet, StoreError>>,
    {
        let reference = Reference::admit(reference.0, reference.1, reference.2)?;
        let (provider, scopes) = intent(declaration.0, declaration.1)?;
        let _flight = self.flight(&reference).await?;
        let read = (reference.clone(), scopes.clone());
        let (generation, grant) = self
            .blocking(move |store| store.declared(&read.0, provider, &read.1))
            .await?;
        if grant.expires_at > self.now() + REFRESH_WINDOW_SECONDS {
            return Ok(grant.access_token);
        }
        let refresh_token = grant
            .refresh_token
            .clone()
            .ok_or(StoreError::Reauthorization)?;
        let tokens = refresh(refresh_token, grant.broker_lease.clone()).await?;
        let refreshed =
            Grant::from_tokens(&tokens, &scopes, self.now(), grant.integration.as_ref())?;
        let access = refreshed.access_token.clone();
        self.blocking(move |store| {
            store.seal_if_current(&reference, provider, &refreshed, generation)
        })
        .await?;
        Ok(access)
    }

    /// The current usable grant of one declared Integration and its generation.
    fn declared(
        &self,
        reference: &Reference,
        provider: &str,
        scopes: &[String],
    ) -> Result<(u64, Grant), StoreError> {
        self.records.read(|state| {
            let record = stored(state, reference).ok_or(StoreError::Missing)?;
            let (header, grant) = self.open(reference, record)?;
            if header.provider != provider
                || header.scopes != scopes
                || header.status == Stored::ReauthorizationRequired
            {
                return Err(StoreError::Reauthorization);
            }
            Ok((header.generation, grant))
        })
    }

    /// Seals a refreshed grant only while the record is still the usable declared one of `generation`.
    fn seal_if_current(
        &self,
        reference: &Reference,
        provider: &'static str,
        grant: &Grant,
        generation: u64,
    ) -> Result<Metadata, StoreError> {
        let plaintext = grant.plaintext()?;
        self.records.update(|state| {
            let record = stored(state, reference).ok_or(StoreError::Missing)?;
            let (header, _) = self.open(reference, record)?;
            if header.provider != provider
                || header.scopes != grant.scopes
                || header.status == Stored::ReauthorizationRequired
                || header.generation != generation
            {
                return Err(StoreError::Reauthorization);
            }
            let key = self.records.key(false)?;
            let next = super::record::Header {
                expires_at: grant.expires_at,
                generation: state.advance_generation(),
                ..header
            };
            let sealed = self.seal(reference, &next, &plaintext, &key)?;
            set_record(state, reference, sealed);
            Ok(Outcome::Changed(Metadata {
                id: reference.integration.clone(),
                provider: provider.to_owned(),
                scopes: grant.scopes.clone(),
                status: super::Status::Connected,
                integration: grant.integration.clone(),
                expires_at: Some(grant.expires_at),
                generation: next.generation,
            }))
        })
    }

    /// Replaces one Integration's grant under its flight: any prior grant is first demoted and revoked, and only then
    /// is `exchange` run and its tokens sealed. A failed revocation stops before the exchange and leaves the demoted
    /// record; a seal that fails revokes the unsealed replacement before it reports the failure.
    ///
    /// # Errors
    /// [`StoreError::Revocation`] from the prior grant's revocation, [`StoreError::Exchange`] from `exchange`, or as
    /// [`IntegrationStore::put`].
    pub async fn replace<F>(
        self: &Arc<Self>,
        reference: (&str, &str, &str),
        declaration: (&str, &[String]),
        identity: Option<Identity>,
        exchange: F,
        revoke: &dyn Revoke,
    ) -> Result<Metadata, StoreError>
    where
        F: FnOnce() -> BoxFuture<'static, Result<TokenSet, StoreError>>,
    {
        let reference = Reference::admit(reference.0, reference.1, reference.2)?;
        let (provider, scopes) = intent(declaration.0, declaration.1)?;
        let _flight = self.flight(&reference).await?;
        let demoted = reference.clone();
        if let Some((previous, grant)) = self.blocking(move |store| store.demote(&demoted)).await? {
            revoke.revoke(revocation(&previous, &grant)).await?;
        }
        let tokens = exchange().await?;
        let admitted = Grant::from_tokens(&tokens, &scopes, self.now(), identity.as_ref())?;
        let sealing = (reference.clone(), admitted.clone());
        let sealed = self
            .blocking(move |store| store.seal_new(&sealing.0, provider, &sealing.1))
            .await;
        if sealed.is_err() {
            // The compensation's own failure is not the error to report: the write failure is.
            let _ = revoke.revoke(revocation(provider, &admitted)).await;
        }
        sealed
    }
}

fn revocation<'a>(provider: &'a str, grant: &'a Grant) -> Revocation<'a> {
    Revocation {
        provider,
        access_token: &grant.access_token,
        refresh_token: grant.refresh_token.as_ref(),
        broker_lease: grant.broker_lease.as_ref(),
    }
}
