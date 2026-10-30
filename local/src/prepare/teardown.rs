//! The preparation helpers' part in a Team's destruction and the Space reset, and the removal a preview close uses.
//!
//! Every helper carrying the Team's (or, for the reset, the Space's) exact `prepare` labels is removed; an already
//! absent one counts as removed, and one that may remain fails the teardown.

use std::sync::Arc;

use team_core::future::BoxFuture;

use super::helper;
use crate::coordination::previews::{HelperRemover, RemoveFailed};
use crate::coordination::residue::{ResidueKind, TeamRef, TeamTeardown, TeardownFailed};
use crate::docker::DockerEngine;

/// The helpers' teardown participant.
#[derive(Clone)]
pub struct HelperTeardown {
    /// The Docker daemon.
    pub docker: Arc<dyn DockerEngine>,
    /// The Space whose helpers these are.
    pub space_id: String,
}

impl std::fmt::Debug for HelperTeardown {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("HelperTeardown")
            .field("space_id", &self.space_id)
            .finish_non_exhaustive()
    }
}

impl TeamTeardown for HelperTeardown {
    fn kinds(&self) -> &'static [ResidueKind] {
        &[ResidueKind::PreparationHelpers]
    }

    fn remove_team<'a>(&'a self, team: &'a TeamRef) -> BoxFuture<'a, Result<(), TeardownFailed>> {
        Box::pin(async move {
            helper::remove_all(self.docker.as_ref(), &self.space_id, Some(&team.team_id))
                .await
                .map_err(|_| TeardownFailed::Failed)
        })
    }

    fn remove_space(&self) -> BoxFuture<'_, Result<(), TeardownFailed>> {
        Box::pin(async move {
            helper::remove_all(self.docker.as_ref(), &self.space_id, None)
                .await
                .map_err(|_| TeardownFailed::Failed)
        })
    }
}

impl HelperRemover for HelperTeardown {
    fn remove<'a>(&'a self, container: &'a str) -> BoxFuture<'a, Result<(), RemoveFailed>> {
        Box::pin(async move {
            helper::remove(self.docker.as_ref(), container)
                .await
                .map_err(|_| RemoveFailed)
        })
    }
}

#[cfg(test)]
mod tests;
