//! The controller's OAuth orchestration (`integrations/service.py`): the one-use PKCE challenge, the platform broker,
//! and the encrypted store composed. Team never holds an OAuth Client Secret; this owns no route, cookie, browser
//! state, Assistant call, or Brain-visible data.

use std::sync::Arc;

use team_core::future::BoxFuture;
use team_core::secret::Secret;
use tokio::time::Instant;

use crate::broker::{BrokerClient, CALLBACK_MODES};
use crate::challenge::{Pending, challenge_id, team_id};
use crate::pending::IntegrationRequirement;
use crate::pkce::{PkceStore, Request, ResourceBinding};
use crate::providers::identifier;
use crate::store::{
    Declaration, IntegrationStore, Revocation, Revoke, Status, StoreError, assistant_id, intent,
};
use crate::token::TokenSet;

/// The most requirements one pending challenge carries.
pub const MAX_REQUIREMENTS: usize = 32;
/// The most Integrations one requirement carries.
pub const MAX_INTEGRATIONS_PER_REQUIREMENT: usize = 16;

/// Why an OAuth operation was refused; it never carries a claim, state, verifier, token, or callback text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ServiceError {
    /// The callback mode is not one the broker serves; nothing was created.
    CallbackMode,
    /// The requested Integration is not part of the pending challenge.
    NotPending,
    /// The requested Integration is already configured or only needs a refresh.
    AlreadyConfigured,
    /// The operation could not be completed; nothing private is reported.
    Failed,
}

impl std::fmt::Display for ServiceError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::CallbackMode => "the OAuth callback mode is invalid",
            Self::NotPending => "the requested pending OAuth Integration is unavailable",
            Self::AlreadyConfigured => {
                "the requested pending OAuth Integration is already configured"
            }
            Self::Failed => "the OAuth Integration operation could not be completed",
        })
    }
}

impl std::error::Error for ServiceError {}

/// A completed consent: public identifiers only.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Completion {
    /// The Team.
    pub team_id: String,
    /// The Assistant.
    pub assistant_id: String,
    /// The Integration.
    pub integration_id: String,
    /// The provider.
    pub provider: String,
    /// The granted scopes.
    pub scopes: Vec<String>,
    /// The sealed grant's generation.
    pub generation: u64,
    /// The resource binding the consent was started with.
    pub resource_binding: Option<ResourceBinding>,
}

/// One Integration a pending challenge requires, admitted.
#[derive(Clone, Debug, PartialEq, Eq)]
struct Candidate {
    assistant_id: String,
    integration_id: String,
    provider: &'static str,
    scopes: Vec<String>,
}

/// Every Integration a live pending challenge requires, admitted and sorted by Assistant and Integration.
fn candidates<P>(
    pending: &Pending<Vec<IntegrationRequirement>, P>,
) -> Result<Vec<Candidate>, ServiceError> {
    let requirements = &pending.metadata;
    if !(1..=MAX_REQUIREMENTS).contains(&requirements.len())
        || !team_id(&pending.team_id)
        || !challenge_id(&pending.id)
        || pending.expires_at <= Instant::now()
    {
        return Err(ServiceError::Failed);
    }
    let mut admitted: Vec<Candidate> = Vec::new();
    for requirement in requirements {
        if !(1..=MAX_INTEGRATIONS_PER_REQUIREMENT).contains(&requirement.integrations.len())
            || !assistant_id(&requirement.assistant_id)
        {
            return Err(ServiceError::Failed);
        }
        for integration in &requirement.integrations {
            let (provider, scopes) = intent(&integration.provider, &integration.scopes)
                .map_err(|_| ServiceError::Failed)?;
            let duplicate = admitted.iter().any(|candidate| {
                candidate.assistant_id == requirement.assistant_id
                    && candidate.integration_id == integration.id
            });
            if !identifier(&integration.id) || duplicate {
                return Err(ServiceError::Failed);
            }
            admitted.push(Candidate {
                assistant_id: requirement.assistant_id.clone(),
                integration_id: integration.id.clone(),
                provider,
                scopes,
            });
        }
    }
    admitted.sort_by(|left, right| {
        (&left.assistant_id, &left.integration_id)
            .cmp(&(&right.assistant_id, &right.integration_id))
    });
    Ok(admitted)
}

/// The private inputs of one broker claim.
struct Claimed {
    claim: String,
    state: String,
    verifier: Secret,
    provider: &'static str,
    scopes: Vec<String>,
}

/// The brokered OAuth service.
pub struct OAuthService {
    pkce: Arc<PkceStore>,
    store: Arc<IntegrationStore>,
    broker: Arc<BrokerClient>,
}

impl std::fmt::Debug for OAuthService {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("OAuthService(shimpz.com)")
    }
}

/// One authorization start.
#[derive(Clone, Copy, Debug)]
pub struct Start<'a> {
    /// The browser session binding.
    pub session_binding: &'a str,
    /// The pending Assistant.
    pub assistant_id: &'a str,
    /// The pending Integration.
    pub integration_id: &'a str,
    /// The broker callback mode.
    pub callback_mode: &'a str,
    /// An optional resource binding.
    pub resource_binding: Option<&'a ResourceBinding>,
}

impl OAuthService {
    /// The service over its three parts.
    #[must_use]
    pub fn new(
        pkce: Arc<PkceStore>,
        store: Arc<IntegrationStore>,
        broker: Arc<BrokerClient>,
    ) -> Self {
        Self {
            pkce,
            store,
            broker,
        }
    }

    /// The broker start URL for one exact Integration the pending challenge requires and that is not configured.
    ///
    /// # Errors
    /// [`ServiceError::CallbackMode`] before anything is created, [`ServiceError::NotPending`],
    /// [`ServiceError::AlreadyConfigured`], or [`ServiceError::Failed`].
    pub fn authorization_url<P>(
        &self,
        pending: &Pending<Vec<IntegrationRequirement>, P>,
        start: &Start<'_>,
    ) -> Result<String, ServiceError> {
        if !CALLBACK_MODES.contains(&start.callback_mode) {
            return Err(ServiceError::CallbackMode);
        }
        let selected = self.unconfigured(pending, start)?;
        let public = self
            .pkce
            .create(&Request {
                session_binding: start.session_binding,
                team_id: &pending.team_id,
                assistant_id: &selected.assistant_id,
                integration_id: &selected.integration_id,
                provider_id: selected.provider,
                scopes: &selected.scopes,
                resource_binding: start.resource_binding,
            })
            .map_err(|_| ServiceError::Failed)?;
        self.broker
            .authorization_url(
                &public.provider_id,
                &public.state,
                &public.code_challenge,
                &public.scopes,
                start.callback_mode,
            )
            .map_err(|_| ServiceError::Failed)
    }

    /// The pending candidate `start` names, provided it is still missing or needs reauthorization.
    fn unconfigured<P>(
        &self,
        pending: &Pending<Vec<IntegrationRequirement>, P>,
        start: &Start<'_>,
    ) -> Result<Candidate, ServiceError> {
        let candidates = candidates(pending)?;
        if !assistant_id(start.assistant_id) || !identifier(start.integration_id) {
            return Err(ServiceError::Failed);
        }
        let selected = candidates
            .into_iter()
            .find(|candidate| {
                candidate.assistant_id == start.assistant_id
                    && candidate.integration_id == start.integration_id
            })
            .ok_or(ServiceError::NotPending)?;
        let declared = [(
            selected.integration_id.clone(),
            Declaration {
                provider: selected.provider.to_owned(),
                scopes: selected.scopes.clone(),
            },
        )];
        let rows = self
            .store
            .metadata(&pending.team_id, &selected.assistant_id, &declared)
            .map_err(|_| ServiceError::Failed)?;
        match rows.first().map(|row| row.status) {
            Some(Status::Missing | Status::ReauthorizationRequired) => Ok(selected),
            _ => Err(ServiceError::AlreadyConfigured),
        }
    }

    /// Completes one consent: consumes the challenge for this browser session, requires the Assistant's current
    /// declaration to equal the consented one, and then replaces the grant (revoking any prior one) with the broker's
    /// claim. A wrong session consumes nothing; drift consumes the challenge before any exchange.
    ///
    /// # Errors
    /// [`ServiceError::Failed`], whatever failed.
    pub async fn complete<F>(
        &self,
        state: &str,
        claim: &str,
        session_binding: &str,
        current_declaration: F,
    ) -> Result<Completion, ServiceError>
    where
        F: for<'b> FnOnce(
            &'b crate::pkce::CallbackBinding,
        ) -> BoxFuture<'b, Result<Declaration, ()>>,
    {
        let exchange = self
            .pkce
            .claim_callback(state, session_binding)
            .map_err(|_| ServiceError::Failed)?;
        let binding = exchange.binding.clone();
        let current = current_declaration(&binding)
            .await
            .map_err(|()| ServiceError::Failed)?;
        let (provider, scopes) =
            intent(&current.provider, &current.scopes).map_err(|_| ServiceError::Failed)?;
        if provider != exchange.provider_id || scopes != exchange.scopes {
            return Err(ServiceError::Failed);
        }
        let claimed = Claimed {
            claim: claim.to_owned(),
            state: state.to_owned(),
            verifier: exchange.code_verifier.clone(),
            provider,
            scopes,
        };
        let sealed = self.seal_claim(&binding, claimed).await?;
        Ok(Completion {
            team_id: binding.team_id,
            assistant_id: binding.assistant_id,
            integration_id: binding.integration_id,
            provider: sealed.provider,
            scopes: sealed.scopes,
            generation: sealed.generation,
            resource_binding: binding.resource_binding,
        })
    }

    /// Replaces the bound Integration's grant with the broker's claim of `claimed`.
    async fn seal_claim(
        &self,
        binding: &crate::pkce::CallbackBinding,
        claimed: Claimed,
    ) -> Result<crate::store::Metadata, ServiceError> {
        let broker = Arc::clone(&self.broker);
        let reference = (
            binding.team_id.as_str(),
            binding.assistant_id.as_str(),
            binding.integration_id.as_str(),
        );
        let (provider, scopes) = (claimed.provider, claimed.scopes.clone());
        let exchange = move || -> BoxFuture<'static, Result<TokenSet, StoreError>> {
            Box::pin(async move {
                broker
                    .claim(
                        provider,
                        &claimed.claim,
                        &claimed.state,
                        &claimed.verifier,
                        &claimed.scopes,
                    )
                    .await
                    .map_err(|_| StoreError::Exchange)
            })
        };
        self.store
            .replace(reference, (provider, &scopes), None, exchange, self)
            .await
            .map_err(|_| ServiceError::Failed)
    }

    /// Removes only the PKCE continuations one browser session holds; whether any existed.
    ///
    /// # Errors
    /// [`ServiceError::Failed`] for a malformed session binding.
    pub fn cancel(&self, session_binding: &str) -> Result<bool, ServiceError> {
        self.pkce
            .cancel_session(session_binding)
            .map(|removed| removed > 0)
            .map_err(|_| ServiceError::Failed)
    }

    /// Refreshes one grant through the broker; a grant without a lease cannot be refreshed.
    ///
    /// # Errors
    /// [`ServiceError::Failed`].
    pub async fn refresh(
        &self,
        provider: &str,
        scopes: &[String],
        refresh_token: &Secret,
        broker_lease: Option<&Secret>,
    ) -> Result<TokenSet, ServiceError> {
        let lease = broker_lease.ok_or(ServiceError::Failed)?;
        self.broker
            .refresh(provider, refresh_token, lease, scopes)
            .await
            .map_err(|_| ServiceError::Failed)
    }

    /// The broker client.
    #[must_use]
    pub fn broker(&self) -> &Arc<BrokerClient> {
        &self.broker
    }
}

impl Revoke for OAuthService {
    /// Revokes the grant a replacement supersedes: its refresh token when it has one, else its access token.
    fn revoke<'a>(&'a self, grant: Revocation<'a>) -> BoxFuture<'a, Result<(), StoreError>> {
        Box::pin(async move {
            let lease = grant.broker_lease.ok_or(StoreError::Revocation)?;
            let token = grant.refresh_token.unwrap_or(grant.access_token);
            self.broker
                .revoke(grant.provider, token, lease)
                .await
                .map_err(|_| StoreError::Revocation)
        })
    }
}

#[cfg(test)]
mod tests;
