//! Bounded, one-use OAuth Authorization Code + PKCE continuations (`integrations/pkce.py`).
//!
//! The browser receives only `state` and the S256 challenge. The verifier stays in this process and is released once,
//! after the callback proves the same browser session that started the flow; the Team, Assistant, and Integration
//! binding comes only from the challenge, never from the callback.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use team_core::entropy::Entropy;
use team_core::secret::Secret;
use tokio::time::Instant;

use crate::challenge::team_id;
use crate::providers::{self, identifier};
use crate::token::is_token;

/// A challenge's lifetime.
pub const DEFAULT_TTL: Duration = Duration::from_secs(600);
/// The most live challenges.
pub const MAX_PENDING_CHALLENGES: usize = 128;
/// The most live challenges one browser session holds.
pub const MAX_PENDING_PER_SESSION: usize = 4;
/// The most live challenges one Team holds.
pub const MAX_PENDING_PER_TEAM: usize = 16;
const SESSION_LABEL: &[u8] = b"shimpz-oauth-session-v1\0";

/// Why a PKCE challenge could not be created, found, or claimed; never carries a binding or the verifier.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PkceError {
    /// An input binding is malformed, or the provider or scopes are not reviewed.
    Invalid,
    /// This exact binding already has a live challenge.
    AlreadyPending,
    /// The store, the session's share, or the Team's share is full.
    Capacity,
    /// The state is unknown, expired, consumed, or was started by another browser session.
    NotFound,
    /// No random state or verifier could be drawn.
    Unavailable,
}

impl std::fmt::Display for PkceError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Invalid => "the OAuth challenge binding is invalid",
            Self::AlreadyPending => "the OAuth Integration already has a pending challenge",
            Self::Capacity => "the OAuth challenge capacity is reached",
            Self::NotFound => "the OAuth challenge is unavailable",
            Self::Unavailable => "the OAuth challenge could not be created",
        })
    }
}

impl std::error::Error for PkceError {}

/// The values safe to place in the provider authorization request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AuthorizationChallenge {
    /// The one-use state: 43 base64url characters.
    pub state: String,
    /// The reviewed provider.
    pub provider_id: String,
    /// The sorted scopes.
    pub scopes: Vec<String>,
    /// The S256 challenge of the verifier.
    pub code_challenge: String,
}

/// An optional opaque pair the flow that started the challenge binds to it: two visible-ASCII values of 1 to 256
/// characters.
pub type ResourceBinding = (String, String);

/// What the challenge was started for: the Team, Assistant, Integration, and any resource binding.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CallbackBinding {
    /// The Team.
    pub team_id: String,
    /// The Assistant.
    pub assistant_id: String,
    /// The Integration.
    pub integration_id: String,
    /// The resource binding, when one was given.
    pub resource_binding: Option<ResourceBinding>,
}

/// The private exchange inputs one exact claim releases.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Exchange {
    /// The provider.
    pub provider_id: String,
    /// The sorted scopes.
    pub scopes: Vec<String>,
    /// The PKCE verifier.
    pub code_verifier: Secret,
    /// What the challenge was bound to.
    pub binding: CallbackBinding,
}

/// One challenge's request.
#[derive(Clone, Copy, Debug)]
pub struct Request<'a> {
    /// The browser session binding (visible ASCII, 16 to 2048 bytes); only its digest is kept.
    pub session_binding: &'a str,
    /// The Team.
    pub team_id: &'a str,
    /// The Assistant.
    pub assistant_id: &'a str,
    /// The Integration.
    pub integration_id: &'a str,
    /// The provider.
    pub provider_id: &'a str,
    /// The requested scopes.
    pub scopes: &'a [String],
    /// The optional resource binding.
    pub resource_binding: Option<&'a ResourceBinding>,
}

struct Live {
    public: AuthorizationChallenge,
    session: [u8; 32],
    binding: CallbackBinding,
    verifier: Secret,
    expires_at: Instant,
}

type Key = ([u8; 32], String, String, String);

struct Inner {
    pending: HashMap<String, Live>,
    by_binding: HashMap<Key, String>,
}

/// The configuration of a [`PkceStore`].
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PkceLimits {
    /// The most live challenges (1 to 1024).
    pub capacity: usize,
    /// The most per browser session (1 to `capacity`).
    pub per_session: usize,
    /// The most per Team (1 to `capacity`).
    pub per_team: usize,
    /// Each challenge's lifetime (30 to 900 whole seconds).
    pub ttl: Duration,
}

impl Default for PkceLimits {
    fn default() -> Self {
        Self {
            capacity: MAX_PENDING_CHALLENGES,
            per_session: MAX_PENDING_PER_SESSION,
            per_team: MAX_PENDING_PER_TEAM,
            ttl: DEFAULT_TTL,
        }
    }
}

/// PKCE verifiers kept in memory only, bounded, short-lived, and one-use.
pub struct PkceStore {
    limits: PkceLimits,
    entropy: Arc<dyn Entropy>,
    inner: Mutex<Inner>,
}

impl std::fmt::Debug for PkceStore {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("PkceStore")
            .field("limits", &self.limits)
            .finish_non_exhaustive()
    }
}

/// The digest a browser session binding is kept as.
///
/// # Errors
/// [`PkceError::Invalid`] for anything but visible ASCII of 16 to 2048 bytes.
fn session_digest(binding: &str) -> Result<[u8; 32], PkceError> {
    if !is_token(binding, 16, 2048) {
        return Err(PkceError::Invalid);
    }
    let mut hasher = Sha256::new();
    hasher.update(SESSION_LABEL);
    hasher.update(binding.as_bytes());
    Ok(hasher.finalize().into())
}

fn state_text(state: &str) -> bool {
    state.len() == 43
        && state
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

fn resource_part(text: &str) -> bool {
    (1..=256).contains(&text.len()) && text.bytes().all(|byte| (0x21..=0x7e).contains(&byte))
}

fn assistant_id(text: &str) -> bool {
    team_protocol::identifiers::canonical_assistant_id(&strict_json::Value::String(text.to_owned()))
        .is_some()
}

/// The binding key and reviewed intent of one well-formed request.
fn admit(request: &Request<'_>) -> Result<(Key, providers::Intent), PkceError> {
    let session = session_digest(request.session_binding)?;
    if !team_id(request.team_id)
        || !assistant_id(request.assistant_id)
        || !identifier(request.integration_id)
    {
        return Err(PkceError::Invalid);
    }
    if let Some((first, second)) = request.resource_binding
        && !(resource_part(first) && resource_part(second))
    {
        return Err(PkceError::Invalid);
    }
    let intent =
        providers::intent(request.provider_id, request.scopes).map_err(|_| PkceError::Invalid)?;
    let key = (
        session,
        request.team_id.to_owned(),
        request.assistant_id.to_owned(),
        request.integration_id.to_owned(),
    );
    Ok((key, intent))
}

impl PkceStore {
    /// A store with `limits`.
    ///
    /// # Errors
    /// [`PkceError::Invalid`] for limits out of range.
    pub fn new(limits: PkceLimits, entropy: Arc<dyn Entropy>) -> Result<Self, PkceError> {
        let valid = (1..=1024).contains(&limits.capacity)
            && (1..=limits.capacity).contains(&limits.per_session)
            && (1..=limits.capacity).contains(&limits.per_team)
            && (30..=900).contains(&limits.ttl.as_secs())
            && limits.ttl.subsec_nanos() == 0;
        if !valid {
            return Err(PkceError::Invalid);
        }
        Ok(Self {
            limits,
            entropy,
            inner: Mutex::new(Inner {
                pending: HashMap::new(),
                by_binding: HashMap::new(),
            }),
        })
    }

    fn lock(&self) -> Result<std::sync::MutexGuard<'_, Inner>, PkceError> {
        let mut inner = self.inner.lock().map_err(|_| PkceError::Unavailable)?;
        let now = Instant::now();
        let expired: Vec<String> = inner
            .pending
            .iter()
            .filter(|(_, live)| live.expires_at <= now)
            .map(|(state, _)| state.clone())
            .collect();
        for state in expired {
            Self::remove(&mut inner, &state);
        }
        Ok(inner)
    }

    fn remove(inner: &mut Inner, state: &str) -> Option<Live> {
        let live = inner.pending.remove(state)?;
        let key = (
            live.session,
            live.binding.team_id.clone(),
            live.binding.assistant_id.clone(),
            live.binding.integration_id.clone(),
        );
        if inner.by_binding.get(&key).map(String::as_str) == Some(state) {
            inner.by_binding.remove(&key);
        }
        Some(live)
    }

    fn token(&self, bytes: usize) -> Result<String, PkceError> {
        let mut raw = zeroize::Zeroizing::new(vec![0_u8; bytes]);
        self.entropy
            .fill(&mut raw)
            .map_err(|_| PkceError::Unavailable)?;
        Ok(URL_SAFE_NO_PAD.encode(raw.as_slice()))
    }

    /// Starts one challenge: a fresh state and verifier bound to the session, Team, Assistant, and Integration.
    ///
    /// # Errors
    /// [`PkceError`] for a malformed binding, unreviewed provider or scopes, a binding already pending, or capacity.
    pub fn create(&self, request: &Request<'_>) -> Result<AuthorizationChallenge, PkceError> {
        let (key, intent) = admit(request)?;
        let mut inner = self.lock()?;
        self.room(&inner, &key)?;
        let state = loop {
            let state = self.token(32)?;
            if !inner.pending.contains_key(&state) {
                break state;
            }
        };
        let verifier = Secret::new(self.token(64)?);
        let code_challenge = URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.expose().as_bytes()));
        let public = AuthorizationChallenge {
            state: state.clone(),
            provider_id: intent.provider.id.to_owned(),
            scopes: intent.scopes,
            code_challenge,
        };
        let live = Live {
            public: public.clone(),
            session: key.0,
            binding: CallbackBinding {
                team_id: key.1.clone(),
                assistant_id: key.2.clone(),
                integration_id: key.3.clone(),
                resource_binding: request.resource_binding.cloned(),
            },
            verifier,
            expires_at: Instant::now() + self.limits.ttl,
        };
        inner.pending.insert(state.clone(), live);
        inner.by_binding.insert(key, state);
        Ok(public)
    }

    /// Refuses a binding already pending, a full store, and a full session or Team share.
    fn room(&self, inner: &Inner, key: &Key) -> Result<(), PkceError> {
        if inner.by_binding.contains_key(key) {
            return Err(PkceError::AlreadyPending);
        }
        let sessions = inner
            .pending
            .values()
            .filter(|live| bool::from(live.session.ct_eq(&key.0)))
            .count();
        let teams = inner
            .pending
            .values()
            .filter(|live| live.binding.team_id == key.1)
            .count();
        if inner.pending.len() >= self.limits.capacity
            || sessions >= self.limits.per_session
            || teams >= self.limits.per_team
        {
            return Err(PkceError::Capacity);
        }
        Ok(())
    }

    fn bound<'a>(inner: &'a Inner, state: &str, session: &[u8; 32]) -> Result<&'a Live, PkceError> {
        match inner.pending.get(state) {
            Some(live) if bool::from(live.session.ct_eq(session)) => Ok(live),
            _ => Err(PkceError::NotFound),
        }
    }

    fn checked(state: &str, session_binding: &str) -> Result<[u8; 32], PkceError> {
        if !state_text(state) {
            return Err(PkceError::NotFound);
        }
        session_digest(session_binding)
    }

    /// Consumes the challenge `state` names, only for the browser session that started it, releasing its verifier.
    /// A callback from another session consumes nothing.
    ///
    /// # Errors
    /// [`PkceError::NotFound`] for an unknown, expired, consumed, or foreign challenge; [`PkceError::Invalid`] for a
    /// malformed session binding.
    pub fn claim_callback(
        &self,
        state: &str,
        session_binding: &str,
    ) -> Result<Exchange, PkceError> {
        let session = Self::checked(state, session_binding)?;
        let mut inner = self.lock()?;
        Self::bound(&inner, state, &session)?;
        let live = Self::remove(&mut inner, state).ok_or(PkceError::NotFound)?;
        Ok(Exchange {
            provider_id: live.public.provider_id,
            scopes: live.public.scopes,
            code_verifier: live.verifier,
            binding: live.binding,
        })
    }

    /// The identifiers the challenge was started for, without consuming it or exposing its verifier.
    ///
    /// # Errors
    /// As [`PkceStore::claim_callback`].
    pub fn inspect_callback(
        &self,
        state: &str,
        session_binding: &str,
    ) -> Result<CallbackBinding, PkceError> {
        let session = Self::checked(state, session_binding)?;
        let inner = self.lock()?;
        Ok(Self::bound(&inner, state, &session)?.binding.clone())
    }

    fn remove_where(&self, selected: impl Fn(&Live) -> bool) -> Result<usize, PkceError> {
        let mut inner = self.inner.lock().map_err(|_| PkceError::Unavailable)?;
        let states: Vec<String> = inner
            .pending
            .iter()
            .filter(|(_, live)| selected(live))
            .map(|(state, _)| state.clone())
            .collect();
        for state in &states {
            Self::remove(&mut inner, state);
        }
        Ok(states.len())
    }

    /// Cancels every challenge one browser session started; how many.
    ///
    /// # Errors
    /// [`PkceError::Invalid`] for a malformed session binding.
    pub fn cancel_session(&self, session_binding: &str) -> Result<usize, PkceError> {
        let session = session_digest(session_binding)?;
        self.remove_where(|live| bool::from(live.session.ct_eq(&session)))
    }

    /// Cancels every challenge of one Team; how many.
    ///
    /// # Errors
    /// [`PkceError::Invalid`] for a malformed Team id.
    pub fn cancel_team(&self, team: &str) -> Result<usize, PkceError> {
        if !team_id(team) {
            return Err(PkceError::Invalid);
        }
        self.remove_where(|live| live.binding.team_id == team)
    }

    /// Cancels every challenge; how many.
    ///
    /// # Errors
    /// [`PkceError::Unavailable`] when the store's lock is poisoned.
    pub fn cancel_all(&self) -> Result<usize, PkceError> {
        let mut inner = self.inner.lock().map_err(|_| PkceError::Unavailable)?;
        let removed = inner.pending.len();
        inner.pending.clear();
        inner.by_binding.clear();
        Ok(removed)
    }
}

#[cfg(test)]
mod tests;
