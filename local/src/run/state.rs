//! The private state the composed controller keeps consistent with each Team's Assistants (`local/chat/state.py`),
//! and the teardown participants of the residue kinds no service owns: a Team generation's Action checkpoints, its
//! Integration credentials and Stored Inputs, and its stored files.

use std::sync::Arc;

use team_action::journal::Journal;
use team_action::stored_input::StoredInputStore;
use team_core::future::BoxFuture;
use team_integrations::store::IntegrationStore;
use team_storage::files::FileStore;

use crate::chat::continuation_store::ContinuationStore;
use crate::chat::pause::continuation_unavailable;
use crate::chat::ports::TeamView;
use crate::chat::problem::stored_input_unavailable;
use crate::chat::service::IntegrationChallenges;
use crate::coordination::residue::{ResidueKind, TeamRef, TeamTeardown, TeardownFailed};
use crate::http::service::Problem;
use crate::install::PrivateState;
use crate::install::spec::AssistantSpec;

/// The Integration token store could not be read or written.
const INTEGRATION_STATE_UNAVAILABLE: Problem = Problem::new(
    503,
    "Assistant integration state is unavailable",
    "assistant-integration-state-unavailable",
);

/// The Integration credentials, Stored Inputs, and paused chat an Assistant's install and removal keep consistent.
pub struct Private {
    /// Integration credentials.
    pub integrations: Arc<IntegrationStore>,
    /// Stored Inputs.
    pub stored_inputs: Arc<StoredInputStore>,
    /// Paused chat turns.
    pub continuations: Arc<ContinuationStore>,
    /// The Integration challenges a pruned credential ends.
    pub challenges: Arc<IntegrationChallenges>,
}

impl std::fmt::Debug for Private {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Private")
    }
}

impl Private {
    fn end_continuation_now(&self, team_id: &str) -> Result<(), Problem> {
        self.continuations
            .delete(team_id, None)
            .map(|_| ())
            .map_err(|_| continuation_unavailable())
    }
}

impl PrivateState for Private {
    fn retain_declared<'a>(
        &'a self,
        team_id: &'a str,
        spec: &'a AssistantSpec,
    ) -> BoxFuture<'a, Result<(), Problem>> {
        Box::pin(async move {
            let mut integrations: Vec<&str> = spec
                .runtime
                .integrations
                .iter()
                .map(|declared| declared.id.as_str())
                .collect();
            integrations.sort_unstable();
            let pruned = self
                .integrations
                .retain_declared(team_id, &spec.assistant_id, &integrations)
                .map_err(|_| INTEGRATION_STATE_UNAVAILABLE)?;
            if pruned {
                // A pruned token ends the Team's Integration challenge and the paused chat that may wait on it.
                let _ = self.challenges.cancel_team(team_id);
                self.end_continuation_now(team_id)?;
            }
            let mut stored: Vec<&str> = spec
                .runtime
                .stored_inputs
                .iter()
                .map(|declared| declared.id.as_str())
                .collect();
            stored.sort_unstable();
            self.stored_inputs
                .retain_declared(team_id, &spec.assistant_id, &stored)
                .map(|_| ())
                .map_err(|_| stored_input_unavailable())
        })
    }

    fn delete_assistant<'a>(
        &'a self,
        team_id: &'a str,
        assistant_id: &'a str,
    ) -> BoxFuture<'a, Result<(), Problem>> {
        Box::pin(async move {
            self.integrations
                .delete_assistant(team_id, assistant_id)
                .map_err(|_| INTEGRATION_STATE_UNAVAILABLE)?;
            self.stored_inputs
                .delete_assistant(team_id, assistant_id)
                .map(|_| ())
                .map_err(|_| stored_input_unavailable())
        })
    }

    fn end_continuation<'a>(&'a self, team_id: &'a str) -> BoxFuture<'a, Result<(), Problem>> {
        Box::pin(async move { self.end_continuation_now(team_id) })
    }
}

fn unavailable<E>(_: E) -> TeardownFailed {
    TeardownFailed::StateUnavailable
}

/// Runs a blocking store change off the runtime.
async fn blocking<F>(change: F) -> Result<(), TeardownFailed>
where
    F: FnOnce() -> Result<(), TeardownFailed> + Send + 'static,
{
    tokio::task::spawn_blocking(change)
        .await
        .map_err(unavailable)?
}

/// A Team's Integration credentials, then its Stored Inputs; every Team's on a Space reset.
pub struct PrivateTeardown {
    /// Integration credentials.
    pub integrations: Arc<IntegrationStore>,
    /// Stored Inputs.
    pub stored_inputs: Arc<StoredInputStore>,
}

impl std::fmt::Debug for PrivateTeardown {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("PrivateTeardown")
    }
}

impl TeamTeardown for PrivateTeardown {
    fn kinds(&self) -> &'static [ResidueKind] {
        &[
            ResidueKind::IntegrationCredentials,
            ResidueKind::StoredInputs,
        ]
    }

    fn remove_team<'a>(&'a self, team: &'a TeamRef) -> BoxFuture<'a, Result<(), TeardownFailed>> {
        let (integrations, stored) = (
            Arc::clone(&self.integrations),
            Arc::clone(&self.stored_inputs),
        );
        let team = team.team_id.clone();
        Box::pin(blocking(move || {
            integrations.delete_team(&team).map_err(unavailable)?;
            stored.delete_team(&team).map(|_| ()).map_err(unavailable)
        }))
    }

    fn remove_space(&self) -> BoxFuture<'_, Result<(), TeardownFailed>> {
        let (integrations, stored) = (
            Arc::clone(&self.integrations),
            Arc::clone(&self.stored_inputs),
        );
        Box::pin(blocking(move || {
            integrations.delete_all().map_err(unavailable)?;
            stored.delete_all().map(|_| ()).map_err(unavailable)
        }))
    }
}

/// A Team generation's Action checkpoints in the journal, purged with its Brain conversation.
pub struct ActionCheckpoints {
    /// The Action journal.
    pub journal: Arc<Journal>,
    /// The Teams the Space holds.
    pub team: Arc<dyn TeamView>,
}

impl std::fmt::Debug for ActionCheckpoints {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("ActionCheckpoints")
    }
}

impl ActionCheckpoints {
    async fn purge(&self, generation: String) -> Result<(), TeardownFailed> {
        let journal = Arc::clone(&self.journal);
        blocking(move || journal.purge(&generation).map_err(unavailable)).await
    }
}

impl TeamTeardown for ActionCheckpoints {
    fn kinds(&self) -> &'static [ResidueKind] {
        &[ResidueKind::ActionCheckpoints]
    }

    fn remove_team<'a>(&'a self, team: &'a TeamRef) -> BoxFuture<'a, Result<(), TeardownFailed>> {
        Box::pin(async move {
            match &team.generation {
                Some(generation) => self.purge(generation.clone()).await,
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
            for (_, generation) in teams {
                self.purge(generation).await?;
            }
            Ok(())
        })
    }
}

/// A Team's stored files; every Team's on a Space reset.
pub struct Storage(pub Arc<FileStore>);

impl std::fmt::Debug for Storage {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Storage")
    }
}

impl TeamTeardown for Storage {
    fn kinds(&self) -> &'static [ResidueKind] {
        &[ResidueKind::TeamStorage]
    }

    fn remove_team<'a>(&'a self, team: &'a TeamRef) -> BoxFuture<'a, Result<(), TeardownFailed>> {
        let (files, team) = (Arc::clone(&self.0), team.team_id.clone());
        Box::pin(blocking(move || {
            files.destroy(&team).map(|_| ()).map_err(unavailable)
        }))
    }

    fn remove_space(&self) -> BoxFuture<'_, Result<(), TeardownFailed>> {
        let files = Arc::clone(&self.0);
        Box::pin(blocking(move || {
            files.destroy_all().map(|_| ()).map_err(unavailable)
        }))
    }
}
