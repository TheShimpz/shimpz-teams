//! The publication bindings' part of a Team's destruction and the Space reset: every binding of the Team (or the
//! Space) retired with its unreferenced icon, and the images its removed containers ran queued and swept
//! (`local/lifecycle.py`). The containers and egress policies were removed in the same step before it.

use std::sync::Arc;

use team_core::future::BoxFuture;

use super::Assistants;
use crate::coordination::residue::{ResidueKind, TeamRef, TeamTeardown, TeardownFailed};
use crate::lifecycle::participants::Removals;

/// The `PublicationBindings` teardown participant.
pub struct PublicationTeardown {
    assistants: Arc<Assistants>,
    removals: Removals,
}

impl std::fmt::Debug for PublicationTeardown {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("PublicationTeardown")
    }
}

impl PublicationTeardown {
    /// The participant, reading the retired images from `removals`.
    #[must_use]
    pub fn new(assistants: Arc<Assistants>, removals: Removals) -> Self {
        Self {
            assistants,
            removals,
        }
    }

    async fn retire(&self, team_id: Option<&str>) -> Result<(), TeardownFailed> {
        let assistants = &self.assistants;
        for removed in self.removals.take() {
            let published = removed
                .provenance
                .is_none_or(|provenance| provenance == crate::assistant::Provenance::Published);
            if let Some(image) = removed.removed.image_id.filter(|_| published) {
                assistants.queue_residue(&image).await;
            }
        }
        let bindings = assistants
            .registry
            .snapshot()
            .await
            .map_err(|_| TeardownFailed::StateUnavailable)?;
        for binding in bindings
            .iter()
            .filter(|binding| team_id.is_none_or(|team| binding.team_id == team))
        {
            assistants
                .retire(&binding.team_id, binding.assistant_id(), Some(binding))
                .await
                .map_err(|_| TeardownFailed::StateUnavailable)?;
        }
        assistants.sweep_residues().await;
        Ok(())
    }
}

impl TeamTeardown for PublicationTeardown {
    fn kinds(&self) -> &'static [ResidueKind] {
        &[ResidueKind::PublicationBindings]
    }

    fn remove_team<'a>(&'a self, team: &'a TeamRef) -> BoxFuture<'a, Result<(), TeardownFailed>> {
        Box::pin(self.retire(Some(&team.team_id)))
    }

    fn remove_space(&self) -> BoxFuture<'_, Result<(), TeardownFailed>> {
        Box::pin(self.retire(None))
    }
}
