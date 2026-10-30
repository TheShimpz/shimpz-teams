//! A restart restores every live paused turn from its encrypted continuation, with the lifetime it was sealed with,
//! and ends what expired meanwhile (`restore_all_chat_continuations`).

use std::time::Duration;

use team_storage::private::epoch_seconds;

use super::StoredContinuation;
use crate::chat::continuation::decode;
use crate::chat::pause::continuation_unavailable;
use crate::chat::pending::Requirements;
use crate::chat::service::ChatService;
use crate::http::service::Problem;

impl ChatService {
    /// Restores every live paused turn and ends the expired ones' Action batches.
    ///
    /// # Errors
    /// `chat-state-unavailable` when the continuations cannot be read, opened, or restored: Team does not start
    /// with a paused turn it cannot keep.
    pub async fn restore(&self) -> Result<(), Problem> {
        let store = &self.parts.continuations;
        let expired = store
            .drain_expired()
            .map_err(|_| continuation_unavailable())?;
        let active = store.active().map_err(|_| continuation_unavailable())?;
        for stored in expired.iter().filter(|stored| stored.kind == "human") {
            let decoded = decode(&stored.kind, &stored.payload, &stored.bindings)
                .map_err(|_| continuation_unavailable())?;
            self.purge(&stored.team_id, &decoded.pending).await?;
        }
        for stored in &active {
            self.restore_one(stored)?;
        }
        Ok(())
    }

    fn restore_one(&self, stored: &StoredContinuation) -> Result<(), Problem> {
        let remaining = stored.expires_at - epoch_seconds(self.parts.clock.as_ref());
        let Ok(remaining) = u64::try_from(remaining) else {
            return Ok(());
        };
        if remaining == 0 {
            return Ok(());
        }
        let decoded = decode(&stored.kind, &stored.payload, &stored.bindings)
            .map_err(|_| continuation_unavailable())?;
        let lifetime = Duration::from_secs(remaining);
        let (team, id) = (&stored.team_id, &stored.challenge_id);
        let restored = match decoded.requirements {
            Requirements::Integrations(requirements) => self
                .parts
                .integration_challenges
                .restore(team, id, lifetime, requirements, decoded.pending)
                .map(|_| ()),
            Requirements::Human(requirement) => self
                .human
                .restore(team, id, lifetime, *requirement, decoded.pending)
                .map(|_| ()),
        };
        restored.map_err(|_| continuation_unavailable())
    }
}
