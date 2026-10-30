//! What chat removes when a Team is destroyed or the Space is reset: its paused turns, its Brain conversation, and
//! its inference settings and learned memory.

use std::sync::Arc;

use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_inference::brain::BrainClient;
use team_inference::config::InferenceConfigStore;
use team_inference::runtime::Brain;
use team_inference::usage::Unmetered;

use super::limit::ChatLimitStore;
use super::ports::TeamView;
use super::segment::brain_thread_id;
use super::service::ChatService;
use crate::coordination::residue::{ResidueKind, TeamRef, TeamTeardown, TeardownFailed};

impl ChatService {
    /// Ends one Team's paused turns, or every Team's, with their challenges, continuations, and OAuth state. The
    /// caller drained the Team's slot and holds its stripe, so nothing recreates them.
    ///
    /// # Errors
    /// [`TeardownFailed::StateUnavailable`] when a continuation cannot be deleted.
    pub fn end_paused(&self, team_id: Option<&str>) -> Result<(), TeardownFailed> {
        let unavailable = |_| TeardownFailed::StateUnavailable;
        if let Some(team) = team_id {
            let _ = self.human.cancel_team(team);
            let _ = self.parts.integration_challenges.cancel_team(team);
            let _ = self.parts.pkce.cancel_team(team);
            self.parts
                .continuations
                .delete(team, None)
                .map_err(unavailable)?;
            self.parts.transcripts.clear(team, None);
        } else {
            let _ = self.human.cancel_all();
            let _ = self.parts.integration_challenges.cancel_all();
            let _ = self.parts.pkce.cancel_all();
            self.parts.continuations.clear().map_err(unavailable)?;
        }
        Ok(())
    }
}

/// The paused turns of a destroyed Team, or of every Team on a Space reset.
#[derive(Debug)]
pub struct ContinuationsTeardown(pub ChatService);

impl TeamTeardown for ContinuationsTeardown {
    fn kinds(&self) -> &'static [ResidueKind] {
        &[ResidueKind::ChatContinuations]
    }

    fn remove_team<'a>(&'a self, team: &'a TeamRef) -> BoxFuture<'a, Result<(), TeardownFailed>> {
        Box::pin(async move { self.0.end_paused(Some(&team.team_id)) })
    }

    fn remove_space(&self) -> BoxFuture<'_, Result<(), TeardownFailed>> {
        Box::pin(async move { self.0.end_paused(None) })
    }
}

/// A Team's Brain conversation, deleted by its thread in the Team's network generation.
pub struct ConversationTeardown {
    /// The Space's id.
    pub space_id: String,
    /// The Brain runtime.
    pub brain: Arc<dyn BrainClient>,
    /// The Teams the Space holds.
    pub team: Arc<dyn TeamView>,
}

impl std::fmt::Debug for ConversationTeardown {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("ConversationTeardown")
    }
}

impl ConversationTeardown {
    async fn delete(&self, team_id: &str, network_id: &str) -> Result<(), TeardownFailed> {
        let thread = brain_thread_id(&self.space_id, team_id, network_id)
            .map_err(|_| TeardownFailed::OwnershipConflict)?;
        let brain = Brain {
            client: self.brain.as_ref(),
            usage: &Unmetered,
        };
        brain
            .delete_thread(&thread, &Stop::new())
            .await
            .map_err(|_| TeardownFailed::StateUnavailable)
    }
}

impl TeamTeardown for ConversationTeardown {
    fn kinds(&self) -> &'static [ResidueKind] {
        &[ResidueKind::BrainCheckpoints]
    }

    fn remove_team<'a>(&'a self, team: &'a TeamRef) -> BoxFuture<'a, Result<(), TeardownFailed>> {
        Box::pin(async move {
            match &team.generation {
                Some(network) => self.delete(&team.team_id, network).await,
                None => Ok(()),
            }
        })
    }

    fn remove_space(&self) -> BoxFuture<'_, Result<(), TeardownFailed>> {
        Box::pin(async move {
            let teams = self
                .team
                .teams()
                .await
                .map_err(|_| TeardownFailed::Failed)?;
            for (team_id, network_id) in teams {
                self.delete(&team_id, &network_id).await?;
            }
            Ok(())
        })
    }
}

/// A Team's inference settings and learned memory; the Space reset also forgets the chat limit.
#[derive(Debug)]
pub struct InferenceTeardown {
    /// The settings store.
    pub store: Arc<InferenceConfigStore>,
    /// The Space chat limit.
    pub limit: Arc<ChatLimitStore>,
}

impl TeamTeardown for InferenceTeardown {
    fn kinds(&self) -> &'static [ResidueKind] {
        &[ResidueKind::InferenceConfiguration]
    }

    fn remove_team<'a>(&'a self, team: &'a TeamRef) -> BoxFuture<'a, Result<(), TeardownFailed>> {
        Box::pin(async move {
            let store = Arc::clone(&self.store);
            let team = team.team_id.clone();
            tokio::task::spawn_blocking(move || store.delete(&team))
                .await
                .map_err(|_| TeardownFailed::StateUnavailable)?
                .map_err(|_| TeardownFailed::StateUnavailable)
        })
    }

    fn remove_space(&self) -> BoxFuture<'_, Result<(), TeardownFailed>> {
        Box::pin(async move {
            let store = Arc::clone(&self.store);
            tokio::task::spawn_blocking(move || store.delete_all())
                .await
                .map_err(|_| TeardownFailed::StateUnavailable)?
                .map_err(|_| TeardownFailed::StateUnavailable)?;
            self.limit
                .delete()
                .map_err(|_| TeardownFailed::StateUnavailable)
        })
    }
}
