//! Bounded, Team-bound, one-use challenges with a time to live (`integrations/challenge_store.py`).
//!
//! Each Team holds at most one live challenge; the whole store holds at most its capacity. A challenge is answered
//! only by its own Team, consumed once, and expires on the monotonic clock; a restored challenge keeps the remaining
//! lifetime it was sealed with and is never extended. Expired payloads are dropped unless the store's owner drains
//! them to clean dependent state.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use team_core::entropy::{Entropy, token_hex};
use tokio::time::Instant;

/// The default and largest store capacity.
pub const MAX_PENDING_CHALLENGES: usize = 32;

/// One live challenge: its id, Team, expiry, public metadata, and private payload.
#[derive(Debug)]
pub struct Pending<M, P> {
    /// The 32-hex one-use id.
    pub id: String,
    /// The Team it belongs to.
    pub team_id: String,
    /// When it expires.
    pub expires_at: Instant,
    /// The public metadata it was admitted with.
    pub metadata: M,
    /// The private payload only its owner reads.
    pub payload: P,
}

/// Why a challenge operation was refused; never carries the payload.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChallengeError {
    /// The Team id is not canonical, or the metadata or restore arguments are not admitted.
    Invalid,
    /// The Team already holds a live challenge.
    AlreadyPending,
    /// The store is at capacity.
    Capacity,
    /// No live challenge of this Team has this id (expired, consumed, another Team's, or malformed).
    NotFound,
    /// The store drops expired payloads, so it has none to drain.
    NotRetained,
    /// No random id could be drawn.
    Unavailable,
}

impl std::fmt::Display for ChallengeError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Invalid => "the challenge is invalid",
            Self::AlreadyPending => "the Team already has a pending challenge",
            Self::Capacity => "the challenge capacity is reached",
            Self::NotFound => "the challenge is unavailable",
            Self::NotRetained => "the challenges do not retain expired payloads",
            Self::Unavailable => "a challenge id could not be drawn",
        })
    }
}

impl std::error::Error for ChallengeError {}

/// A store's fixed configuration.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Limits {
    /// The most live challenges (1 to 1024).
    pub capacity: usize,
    /// Each challenge's lifetime (30 to 900 seconds).
    pub ttl: Duration,
    /// Whether expired payloads are kept for their owner to drain.
    pub retain_expired: bool,
}

impl Limits {
    /// Whether the configuration is in range.
    #[must_use]
    pub fn valid(&self) -> bool {
        (1..=1024).contains(&self.capacity)
            && (30..=900).contains(&self.ttl.as_secs())
            && self.ttl.subsec_nanos() == 0
    }
}

type Shared<M, P> = Arc<Pending<M, P>>;

struct Inner<M, P> {
    pending: HashMap<String, Shared<M, P>>,
    by_team: HashMap<String, String>,
    expired: Vec<Shared<M, P>>,
}

/// The challenge store; `admit` judges each metadata value it is given.
pub struct ChallengeStore<M, P> {
    limits: Limits,
    admit: fn(&M) -> bool,
    entropy: Arc<dyn Entropy>,
    inner: Mutex<Inner<M, P>>,
}

impl<M, P> std::fmt::Debug for ChallengeStore<M, P> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ChallengeStore")
            .field("limits", &self.limits)
            .finish_non_exhaustive()
    }
}

/// Whether `text` is a canonical Team id.
#[must_use]
pub fn team_id(text: &str) -> bool {
    team_protocol::identifiers::TEAM_ID_RE.is_full(text)
}

/// Whether `text` is a challenge id: 32 lowercase hexadecimal characters.
#[must_use]
pub fn challenge_id(text: &str) -> bool {
    text.len() == 32
        && text
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

impl<M, P> ChallengeStore<M, P> {
    /// A store with `limits`, admitting metadata with `admit`.
    ///
    /// # Errors
    /// [`ChallengeError::Invalid`] for limits out of range.
    pub fn new(
        limits: Limits,
        admit: fn(&M) -> bool,
        entropy: Arc<dyn Entropy>,
    ) -> Result<Self, ChallengeError> {
        if !limits.valid() {
            return Err(ChallengeError::Invalid);
        }
        Ok(Self {
            limits,
            admit,
            entropy,
            inner: Mutex::new(Inner {
                pending: HashMap::new(),
                by_team: HashMap::new(),
                expired: Vec::new(),
            }),
        })
    }

    /// The store's lifetime per challenge.
    #[must_use]
    pub fn ttl(&self) -> Duration {
        self.limits.ttl
    }

    fn lock(&self) -> Result<std::sync::MutexGuard<'_, Inner<M, P>>, ChallengeError> {
        let mut inner = self.inner.lock().map_err(|_| ChallengeError::Unavailable)?;
        self.expire(&mut inner, Instant::now());
        Ok(inner)
    }

    fn expire(&self, inner: &mut Inner<M, P>, now: Instant) {
        let expired: Vec<String> = inner
            .pending
            .iter()
            .filter(|(_, challenge)| challenge.expires_at <= now)
            .map(|(id, _)| id.clone())
            .collect();
        for id in expired {
            if let Some(challenge) = inner.pending.remove(&id) {
                if inner.by_team.get(&challenge.team_id) == Some(&id) {
                    inner.by_team.remove(&challenge.team_id);
                }
                if self.limits.retain_expired {
                    inner.expired.push(challenge);
                }
            }
        }
    }

    fn fresh_id(&self, inner: &Inner<M, P>) -> Result<String, ChallengeError> {
        loop {
            let id =
                token_hex(self.entropy.as_ref(), 16).map_err(|_| ChallengeError::Unavailable)?;
            if !inner.pending.contains_key(&id) {
                return Ok(id);
            }
        }
    }

    fn insert(inner: &mut Inner<M, P>, challenge: Pending<M, P>) -> Shared<M, P> {
        let challenge = Arc::new(challenge);
        inner
            .by_team
            .insert(challenge.team_id.clone(), challenge.id.clone());
        inner
            .pending
            .insert(challenge.id.clone(), Arc::clone(&challenge));
        challenge
    }

    fn room(&self, inner: &Inner<M, P>, team: &str) -> Result<(), ChallengeError> {
        if inner.by_team.contains_key(team) {
            return Err(ChallengeError::AlreadyPending);
        }
        if inner.pending.len() >= self.limits.capacity {
            return Err(ChallengeError::Capacity);
        }
        Ok(())
    }

    /// Creates the Team's one live challenge.
    ///
    /// # Errors
    /// [`ChallengeError`] for a malformed Team id or metadata, a Team that already has one, or a full store.
    pub fn create(
        &self,
        team: &str,
        metadata: M,
        payload: P,
    ) -> Result<Shared<M, P>, ChallengeError> {
        if !team_id(team) || !(self.admit)(&metadata) {
            return Err(ChallengeError::Invalid);
        }
        let mut inner = self.lock()?;
        self.room(&inner, team)?;
        let id = self.fresh_id(&inner)?;
        let challenge = Pending {
            id,
            team_id: team.to_owned(),
            expires_at: Instant::now() + self.limits.ttl,
            metadata,
            payload,
        };
        Ok(Self::insert(&mut inner, challenge))
    }

    /// The Team's live challenge with this id.
    ///
    /// # Errors
    /// [`ChallengeError::Invalid`] for a malformed Team id; [`ChallengeError::NotFound`] otherwise.
    pub fn get(&self, team: &str, id: &str) -> Result<Shared<M, P>, ChallengeError> {
        let inner = self.checked(team, id)?;
        Self::available(&inner, team, id)
    }

    fn checked(
        &self,
        team: &str,
        id: &str,
    ) -> Result<std::sync::MutexGuard<'_, Inner<M, P>>, ChallengeError> {
        if !team_id(team) {
            return Err(ChallengeError::Invalid);
        }
        if !challenge_id(id) {
            return Err(ChallengeError::NotFound);
        }
        self.lock()
    }

    fn available(
        inner: &Inner<M, P>,
        team: &str,
        id: &str,
    ) -> Result<Shared<M, P>, ChallengeError> {
        match inner.pending.get(id) {
            Some(challenge) if challenge.team_id == team => Ok(Arc::clone(challenge)),
            _ => Err(ChallengeError::NotFound),
        }
    }

    /// Rehydrates one authenticated durable challenge with the `remaining` lifetime it was sealed with, never more.
    ///
    /// # Errors
    /// [`ChallengeError::Invalid`] for a remaining lifetime outside one second to the TTL or refused metadata, and as
    /// [`ChallengeStore::create`].
    pub fn restore(
        &self,
        team: &str,
        id: &str,
        remaining: Duration,
        metadata: M,
        payload: P,
    ) -> Result<Shared<M, P>, ChallengeError> {
        if !team_id(team) {
            return Err(ChallengeError::Invalid);
        }
        if !challenge_id(id) {
            return Err(ChallengeError::NotFound);
        }
        if !(Duration::from_secs(1)..=self.limits.ttl).contains(&remaining)
            || !(self.admit)(&metadata)
        {
            return Err(ChallengeError::Invalid);
        }
        let mut inner = self.lock()?;
        if inner.pending.contains_key(id) {
            return Err(ChallengeError::AlreadyPending);
        }
        self.room(&inner, team)?;
        let challenge = Pending {
            id: id.to_owned(),
            team_id: team.to_owned(),
            expires_at: Instant::now() + remaining,
            metadata,
            payload,
        };
        Ok(Self::insert(&mut inner, challenge))
    }

    /// Replaces one live challenge with a fresh id for new metadata, keeping its payload and expiry; the earlier id
    /// stops answering at once.
    ///
    /// # Errors
    /// [`ChallengeError::Invalid`] for refused metadata; [`ChallengeError::NotFound`] for no live challenge.
    pub fn reissue(&self, team: &str, id: &str, metadata: M) -> Result<Shared<M, P>, ChallengeError>
    where
        P: Clone,
    {
        if !(self.admit)(&metadata) {
            return Err(ChallengeError::Invalid);
        }
        let mut inner = self.checked(team, id)?;
        let previous = Self::available(&inner, team, id)?;
        let replacement = self.fresh_id(&inner)?;
        inner.pending.remove(id);
        let challenge = Pending {
            id: replacement,
            team_id: team.to_owned(),
            expires_at: previous.expires_at,
            metadata,
            payload: previous.payload.clone(),
        };
        Ok(Self::insert(&mut inner, challenge))
    }

    /// The Team's live challenge, if any.
    ///
    /// # Errors
    /// [`ChallengeError::Invalid`] for a malformed Team id.
    pub fn current(&self, team: &str) -> Result<Option<Shared<M, P>>, ChallengeError> {
        if !team_id(team) {
            return Err(ChallengeError::Invalid);
        }
        let inner = self.lock()?;
        Ok(inner
            .by_team
            .get(team)
            .and_then(|id| inner.pending.get(id))
            .map(Arc::clone))
    }

    /// Consumes the Team's live challenge with this id.
    ///
    /// # Errors
    /// As [`ChallengeStore::get`].
    pub fn claim(&self, team: &str, id: &str) -> Result<Shared<M, P>, ChallengeError> {
        self.claim_after(team, id, |_| Ok::<(), ChallengeError>(()))
            .map_err(|error| match error {
                Claim::Store(error) | Claim::Commit(error) => error,
            })
    }

    /// Consumes one challenge only after `commit` succeeds on it, under the store's lock; a failed commit leaves it
    /// live.
    ///
    /// # Errors
    /// [`Claim::Store`] as [`ChallengeStore::get`]; [`Claim::Commit`] with `commit`'s error.
    pub fn claim_after<E>(
        &self,
        team: &str,
        id: &str,
        commit: impl FnOnce(&Pending<M, P>) -> Result<(), E>,
    ) -> Result<Shared<M, P>, Claim<E>> {
        let mut inner = self.checked(team, id).map_err(Claim::Store)?;
        let challenge = Self::available(&inner, team, id).map_err(Claim::Store)?;
        commit(&challenge).map_err(Claim::Commit)?;
        inner.pending.remove(id);
        if inner.by_team.get(team).map(String::as_str) == Some(id) {
            inner.by_team.remove(team);
        }
        Ok(challenge)
    }

    /// Cancels the Team's live challenge and returns it, so its owner ends exactly the state it held.
    ///
    /// # Errors
    /// [`ChallengeError::Invalid`] for a malformed Team id.
    pub fn withdraw_team(&self, team: &str) -> Result<Option<Shared<M, P>>, ChallengeError> {
        if !team_id(team) {
            return Err(ChallengeError::Invalid);
        }
        let mut inner = self.inner.lock().map_err(|_| ChallengeError::Unavailable)?;
        Ok(inner
            .by_team
            .remove(team)
            .and_then(|id| inner.pending.remove(&id)))
    }

    /// Whether the Team had a live challenge, which is now cancelled.
    ///
    /// # Errors
    /// As [`ChallengeStore::withdraw_team`].
    pub fn cancel_team(&self, team: &str) -> Result<bool, ChallengeError> {
        Ok(self.withdraw_team(team)?.is_some())
    }

    /// Cancels every challenge and drops every retained expiry; how many were live.
    ///
    /// # Errors
    /// [`ChallengeError::Unavailable`] when the store's lock is poisoned.
    pub fn cancel_all(&self) -> Result<usize, ChallengeError> {
        let mut inner = self.inner.lock().map_err(|_| ChallengeError::Unavailable)?;
        let removed = inner.pending.len();
        inner.pending.clear();
        inner.by_team.clear();
        inner.expired.clear();
        Ok(removed)
    }

    /// Returns expired payloads once, every Team's or one Team's, so their owner cleans dependent state.
    ///
    /// # Errors
    /// [`ChallengeError::NotRetained`] for a store that drops them; [`ChallengeError::Invalid`] for a malformed Team.
    pub fn drain_expired(&self, team: Option<&str>) -> Result<Vec<Shared<M, P>>, ChallengeError> {
        if !self.limits.retain_expired {
            return Err(ChallengeError::NotRetained);
        }
        if team.is_some_and(|team| !team_id(team)) {
            return Err(ChallengeError::Invalid);
        }
        let mut inner = self.lock()?;
        let (drained, kept) = std::mem::take(&mut inner.expired)
            .into_iter()
            .partition(|item| team.is_none_or(|team| item.team_id == team));
        inner.expired = kept;
        Ok(drained)
    }
}

/// A [`ChallengeStore::claim_after`] refusal.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Claim<E> {
    /// The store refused before the commit ran.
    Store(ChallengeError),
    /// The commit failed; the challenge stays live.
    Commit(E),
}

#[cfg(test)]
mod tests;
