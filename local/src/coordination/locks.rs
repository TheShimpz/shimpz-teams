//! The Space-wide names lock and the 64 Team stripes.

use std::sync::Arc;

use sha2::{Digest, Sha256};
use tokio::sync::{Mutex, OwnedMutexGuard};

/// How many stripes the Teams share.
pub const STRIPES: usize = 64;

/// The names lock (ADR-0088: always taken before any stripe) and the stripes that serialize each Team's mutation.
#[derive(Debug)]
pub struct SpaceLocks {
    names: Arc<Mutex<()>>,
    stripes: Vec<Arc<Mutex<()>>>,
}

/// The names lock, held.
#[derive(Debug)]
pub struct NamesGuard {
    _held: OwnedMutexGuard<()>,
}

/// One Team's stripe, held.
#[derive(Debug)]
pub struct TeamGuard {
    _held: OwnedMutexGuard<()>,
}

/// Every stripe, held in index order (the Space reset).
#[derive(Debug)]
pub struct AllTeamsGuard {
    _held: Vec<OwnedMutexGuard<()>>,
}

impl SpaceLocks {
    /// Fresh locks.
    #[must_use]
    pub fn new() -> Self {
        Self {
            names: Arc::new(Mutex::new(())),
            stripes: (0..STRIPES).map(|_| Arc::new(Mutex::new(()))).collect(),
        }
    }

    /// The stripe of `team_id`: the first byte of its SHA-256, modulo [`STRIPES`].
    #[must_use]
    pub fn stripe(team_id: &str) -> usize {
        usize::from(Sha256::digest(team_id.as_bytes())[0]) % STRIPES
    }

    /// Waits for the names lock.
    pub async fn names(&self) -> NamesGuard {
        NamesGuard {
            _held: Arc::clone(&self.names).lock_owned().await,
        }
    }

    /// Waits for `team_id`'s stripe; two Teams may share one.
    pub async fn team(&self, team_id: &str) -> TeamGuard {
        TeamGuard {
            _held: Arc::clone(&self.stripes[Self::stripe(team_id)])
                .lock_owned()
                .await,
        }
    }

    /// Waits for every stripe, in index order.
    pub async fn all_teams(&self) -> AllTeamsGuard {
        let mut held = Vec::with_capacity(STRIPES);
        for stripe in &self.stripes {
            held.push(Arc::clone(stripe).lock_owned().await);
        }
        AllTeamsGuard { _held: held }
    }
}

impl Default for SpaceLocks {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use tokio::time::timeout;

    use super::{STRIPES, SpaceLocks};

    #[test]
    fn the_stripe_is_the_first_digest_byte_modulo_64() {
        // SHA-256("alpha") starts with 0x8e (142); 142 % 64 = 14.
        assert_eq!(SpaceLocks::stripe("alpha"), 14);
        assert!((0..1000).all(|index| SpaceLocks::stripe(&format!("team-{index}")) < STRIPES));
    }

    #[tokio::test]
    async fn reset_takes_all_stripes_in_index_order() {
        let locks = SpaceLocks::new();
        let all = locks.all_teams().await;
        assert!(
            timeout(Duration::from_millis(10), locks.team("alpha"))
                .await
                .is_err()
        );
        drop(all);
        let _team = locks.team("alpha").await;
        assert!(
            timeout(Duration::from_millis(10), locks.all_teams())
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn the_names_lock_is_independent_of_the_stripes() {
        let locks = SpaceLocks::new();
        let _names = locks.names().await;
        let _team = locks.team("alpha").await;
        assert!(
            timeout(Duration::from_millis(10), locks.names())
                .await
                .is_err()
        );
    }
}
