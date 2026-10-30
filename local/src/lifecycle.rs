//! Team lifecycle and the Space reset (the `Teams` service group): list, create, rename, and destroy Teams, reset the
//! whole Space, and the bootstrap reset of a Space whose Admin publishes no Supervisor key.
//!
//! Destruction and reset follow the coordination's fixed order (see [`crate::coordination`]): they prove every owned
//! Docker object the Team's or the Space's before anything changes, then run every [`TeamTeardown`] participant in
//! [`DESTROY_ORDER`] or [`RESET_ORDER`]. A participant that fails stops the teardown with its problem; an answer names
//! every one of the 17 residue kinds in `residue_absent`, or the teardown is `teardown-incomplete`.

pub mod network;
pub mod participants;
pub mod wipe;

use std::borrow::Cow;
use std::path::PathBuf;
use std::sync::Arc;

use strict_json::{Number, Value};
use team_core::future::BoxFuture;
use team_protocol::identifiers::TEAM_ID_RE;

use self::network::{TeamNetwork, TeamNetworks};
use self::participants::{AssistantTeardown, RuntimeState};
use crate::authority::{Absence, Verifier};
use crate::coordination::Coordination;
use crate::coordination::executions::DRAIN;
use crate::coordination::previews::PREVIEW_DRAIN;
use crate::coordination::residue::{
    DESTROY_ORDER, RESET_ORDER, ResidueKind, ResidueProof, TeamRef, TeamTeardown, TeardownFailed,
};
use crate::docker::helpers::Helpers;
use crate::docker::problem::{
    INTERNAL, SPACE_RESOURCE_OWNERSHIP_CONFLICT, TEAM_RESOURCES_OWNERSHIP_CONFLICT,
};
use crate::http::operation::Operation;
use crate::http::service::{Answer, Body, Call, Problem, Service};
use crate::names::{NAMES_UNAVAILABLE, TeamNames, canonical_name, docker_instant_ns, folded};

/// Docker could not remove a Team's resource.
pub const TEAM_DESTROY_FAILED: Problem = Problem::new(
    503,
    "Docker could not destroy the Team",
    "docker-remove-failed",
);
/// Docker could not reset the Space.
pub const SPACE_RESET_FAILED: Problem = Problem::new(
    503,
    "Docker could not reset the Space",
    "docker-reset-failed",
);
/// The Team's execution did not stop in time.
pub const CHAT_STOP_TIMEOUT: Problem =
    Problem::new(409, "active Team chat did not stop in time", "chat-active");
/// A file preview did not end in time.
pub const FILE_PREVIEW_ACTIVE: Problem = Problem::new(
    503,
    "a file preview did not end in time",
    "file-preview-active",
);
/// The destruction's name confirmation is not the Team's current name.
pub const TEAM_NAME_MISMATCH: Problem = Problem::new(
    409,
    "Team name confirmation does not match",
    "team-name-mismatch",
);
/// Another Team has the name, ignoring case.
pub const TEAM_NAME_TAKEN: Problem =
    Problem::new(409, "another Team already has this name", "team-name-taken");
/// The Team id exists under another name.
pub const TEAM_NAME_CONFLICT: Problem = Problem::new(
    409,
    "Team id already belongs to a different name",
    "team-name-conflict",
);
/// Docker reported a creation time that is not an instant.
pub const TEAM_METADATA_INVALID: Problem = Problem::new(
    503,
    "Docker returned an invalid Team creation time",
    "team-metadata-invalid",
);
/// The Team id is not one.
pub const INVALID_TEAM_ID: Problem = Problem::new(422, "invalid Team id", "invalid-team-id");
/// The body is not exactly `{"team_name": …}`.
pub const TEAM_NAME_BODY: Problem =
    Problem::new(422, "the request requires only team_name", "invalid-body");
/// A Supervisor is established, so the bootstrap reset is refused.
pub const SUPERVISOR_ESTABLISHED: Problem = Problem::new(
    409,
    "Supervisor authority is already established",
    "supervisor-established",
);
/// The Supervisor authority state could not be examined or changed.
pub const SUPERVISOR_UNAVAILABLE: Problem = Problem::new(
    503,
    "Supervisor authority state is unavailable",
    "supervisor-unavailable",
);

/// The kinds a new Team incarnation clears of whatever an earlier incarnation of its id left after a crash: it never
/// runs beside an earlier Browser, and starts with no files, settings, name, or Routine of the earlier one.
pub const CREATE_CLEARS: [ResidueKind; 5] = [
    ResidueKind::Browsers,
    ResidueKind::TeamStorage,
    ResidueKind::InferenceConfiguration,
    ResidueKind::TeamNames,
    ResidueKind::Routines,
];

/// Whether `team_id` is a Team id.
#[must_use]
pub fn team_id_valid(team_id: &str) -> bool {
    TEAM_ID_RE.is_full(team_id)
}

/// The Supervisor key pin the bootstrap reset proves absent and then forgets.
pub trait SupervisorPin: Send + Sync {
    /// Proves that Admin publishes no Supervisor key.
    ///
    /// # Errors
    /// [`Absence`] when a key is published or the state cannot be examined.
    fn require_absent(&self) -> Result<(), Absence>;

    /// Forgets Team's pin of an earlier key; absence is success.
    ///
    /// # Errors
    /// When it may remain.
    fn forget(&self) -> Result<(), PinRemains>;
}

/// The pin may remain.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PinRemains;

impl SupervisorPin for Verifier {
    fn require_absent(&self) -> Result<(), Absence> {
        self.require_supervisor_absent()
    }

    fn forget(&self) -> Result<(), PinRemains> {
        self.forget_pin().map_err(|_| PinRemains)
    }
}

/// The participants do not cover the 17 kinds exactly once, or one spans two steps of an order.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Coverage;

/// What the lifecycle is built from.
pub struct Parts {
    /// The coordination every service shares.
    pub coordination: Coordination,
    /// The Space's Team networks.
    pub networks: TeamNetworks,
    /// The Team display names.
    pub names: TeamNames,
    /// The Assistant containers' teardown.
    pub assistants: Arc<AssistantTeardown>,
    /// Preparation helpers (removed by a preview's close and by the teardown).
    pub helpers: Arc<Helpers>,
    /// Every other participant (this workstream's egress policies and the other owners' state).
    pub participants: Vec<Arc<dyn TeamTeardown>>,
    /// The Team files root (`storage/`), read only to report what a teardown removed.
    pub storage: PathBuf,
    /// The Supervisor key pin.
    pub pin: Arc<dyn SupervisorPin>,
}

/// Team lifecycle and the Space reset.
pub struct Lifecycle {
    coordination: Coordination,
    networks: TeamNetworks,
    names: TeamNames,
    assistants: Arc<AssistantTeardown>,
    helpers: Arc<Helpers>,
    participants: Vec<Arc<dyn TeamTeardown>>,
    storage: PathBuf,
    pin: Arc<dyn SupervisorPin>,
}

impl std::fmt::Debug for Lifecycle {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.debug_struct("Lifecycle").finish_non_exhaustive()
    }
}

/// Whether `participants` cover every kind exactly once, each within one step of both orders.
fn covers(participants: &[Arc<dyn TeamTeardown>]) -> bool {
    let mut seen: Vec<ResidueKind> = participants
        .iter()
        .flat_map(|participant| participant.kinds().iter().copied())
        .collect();
    let listed = seen.len();
    seen.sort_unstable();
    seen.dedup();
    let within_one_step = |kinds: &[ResidueKind], order: &[&[ResidueKind]]| {
        !kinds.is_empty()
            && order
                .iter()
                .any(|step| kinds.iter().all(|kind| step.contains(kind)))
    };
    listed == ResidueKind::ALL.len()
        && seen.len() == ResidueKind::ALL.len()
        && participants.iter().all(|participant| {
            within_one_step(participant.kinds(), &DESTROY_ORDER)
                && within_one_step(participant.kinds(), &RESET_ORDER)
        })
}

/// The problem of a participant's failure, by its first kind.
fn failure_problem(kind: ResidueKind, failure: TeardownFailed, reset: bool) -> Problem {
    match (failure, reset) {
        (TeardownFailed::OwnershipConflict, false) => TEAM_RESOURCES_OWNERSHIP_CONFLICT,
        (TeardownFailed::OwnershipConflict, true) => SPACE_RESOURCE_OWNERSHIP_CONFLICT,
        (TeardownFailed::Failed, false) => TEAM_DESTROY_FAILED,
        (TeardownFailed::Failed, true) => SPACE_RESET_FAILED,
        (TeardownFailed::StateUnavailable, _) => state_problem(kind, reset),
    }
}

/// The problem of each store whose owner reports it unavailable.
const STATE_PROBLEMS: [(ResidueKind, Problem); 8] = [
    (
        ResidueKind::BrainCheckpoints,
        Problem::new(
            503,
            "Team conversation state could not be deleted",
            "brain-runtime-failed",
        ),
    ),
    (
        ResidueKind::ActionCheckpoints,
        Problem::new(
            503,
            "Team Action execution state is unavailable",
            "action-state-unavailable",
        ),
    ),
    (
        ResidueKind::Routines,
        Problem::new(
            503,
            "Team Routine state could not be deleted",
            "routine-state-unavailable",
        ),
    ),
    (
        ResidueKind::TeamStorage,
        Problem::new(
            500,
            "Team storage failed its safety checks",
            "storage-safety-failed",
        ),
    ),
    (
        ResidueKind::InferenceConfiguration,
        Problem::new(
            503,
            "Team model provider metadata is unavailable",
            "inference-store-failed",
        ),
    ),
    (
        ResidueKind::IntegrationCredentials,
        Problem::new(
            503,
            "Assistant integration state is unavailable",
            "assistant-integration-state-unavailable",
        ),
    ),
    (
        ResidueKind::StoredInputs,
        Problem::new(
            503,
            "Assistant Stored Input state is unavailable",
            "assistant-stored-input-state-unavailable",
        ),
    ),
    (
        ResidueKind::PublicationBindings,
        Problem::new(
            503,
            "Assistant icon storage is unavailable",
            "assistant-icon-unavailable",
        ),
    ),
];

/// The problem of a store that could not be read or written during a teardown.
fn state_problem(kind: ResidueKind, reset: bool) -> Problem {
    use ResidueKind as K;
    match kind {
        K::TeamNames => NAMES_UNAVAILABLE,
        K::EgressPolicies => crate::assistant::problem::EGRESS_POLICY_UNAVAILABLE,
        K::AssistantContainers
        | K::PreparationHelpers
        | K::Browsers
        | K::SignIns
        | K::TeamNetworks => failure_problem(kind, TeardownFailed::Failed, reset),
        _ => STATE_PROBLEMS
            .iter()
            .find(|(owner, _)| *owner == kind)
            .map_or(INTERNAL, |(_, problem)| problem.clone()),
    }
}

fn teardown_incomplete(reset: bool) -> Problem {
    Problem {
        status: 500,
        message: Cow::Borrowed(if reset {
            "Space reset proof is incomplete"
        } else {
            "Team teardown proof is incomplete"
        }),
        code: Some("teardown-incomplete"),
        retry_after_ms: None,
    }
}

fn object(members: Vec<(&str, Value)>) -> Value {
    Value::Object(
        members
            .into_iter()
            .map(|(name, value)| (name.to_owned(), value))
            .collect(),
    )
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn count(value: usize) -> Value {
    Value::Number(Number::Int(i64::try_from(value).unwrap_or(i64::MAX)))
}

fn residue(proof: &ResidueProof) -> Value {
    Value::Array(proof.names().into_iter().map(text).collect())
}

fn json(body: Value) -> Answer {
    Answer::Json { status: 200, body }
}

/// The one `team_name` of a create, rename, or destroy body.
fn team_name_body(body: &Body) -> Result<&Value, Problem> {
    match body {
        Body::Json(Value::Object(members)) if members.len() == 1 && members[0].0 == "team_name" => {
            Ok(&members[0].1)
        }
        _ => Err(TEAM_NAME_BODY),
    }
}

impl Lifecycle {
    /// The lifecycle of `parts`; this workstream's participants (Assistant containers, preparation helpers, Team
    /// networks, names, and runtime state) join `parts.participants`.
    ///
    /// # Errors
    /// [`Coverage`] unless the participants cover every residue kind exactly once, each within one step.
    pub fn new(parts: Parts) -> Result<Self, Coverage> {
        let mut participants: Vec<Arc<dyn TeamTeardown>> = vec![
            parts.assistants.clone(),
            parts.helpers.clone(),
            Arc::new(parts.networks.clone()),
            Arc::new(parts.names.clone()),
            Arc::new(RuntimeState),
        ];
        participants.extend(parts.participants);
        if !covers(&participants) {
            return Err(Coverage);
        }
        Ok(Self {
            coordination: parts.coordination,
            networks: parts.networks,
            names: parts.names,
            assistants: parts.assistants,
            helpers: parts.helpers,
            participants,
            storage: parts.storage,
            pin: parts.pin,
        })
    }

    /// The Team's current name: its record for this incarnation, or its creation label.
    fn display_name(&self, network: &TeamNetwork) -> Result<String, Problem> {
        Ok(self
            .names
            .load(&network.team_id, &network.id)?
            .unwrap_or_else(|| network.label_name.clone()))
    }

    /// Every Team with its current name, the names lock held.
    async fn named(&self) -> Result<Vec<(TeamNetwork, String)>, Problem> {
        let mut named = Vec::new();
        for network in self.networks.list().await? {
            let name = self.display_name(&network)?;
            named.push((network, name));
        }
        Ok(named)
    }

    async fn require_free(&self, team_name: &str, team_id: &str) -> Result<(), Problem> {
        let wanted = folded(team_name);
        let taken = self
            .named()
            .await?
            .iter()
            .any(|(network, name)| network.team_id != team_id && folded(name) == wanted);
        if taken { Err(TEAM_NAME_TAKEN) } else { Ok(()) }
    }

    /// Every Team, newest first by its network's creation instant; the id orders only equal instants.
    ///
    /// # Errors
    /// `docker-unavailable`, `ownership-conflict`, `team-names-unavailable`, `team-metadata-invalid`.
    pub async fn list(&self) -> Result<Value, Problem> {
        let named = {
            let _names = self.coordination.locks.names().await;
            self.named().await?
        };
        let mut ordered = Vec::with_capacity(named.len());
        for (network, name) in named {
            let created = network
                .created
                .as_deref()
                .and_then(docker_instant_ns)
                .ok_or(TEAM_METADATA_INVALID)?;
            ordered.push((-created, network.team_id, name));
        }
        ordered.sort();
        let teams = ordered
            .into_iter()
            .map(|(_, team_id, name)| {
                object(vec![
                    ("team_id", text(&team_id)),
                    ("team_name", text(&name)),
                    ("status", text("running")),
                ])
            })
            .collect();
        Ok(object(vec![("teams", Value::Array(teams))]))
    }

    fn existing(&self, network: &TeamNetwork, team_name: &str) -> Result<Value, Problem> {
        if self.display_name(network)? != team_name {
            return Err(TEAM_NAME_CONFLICT);
        }
        Ok(object(vec![
            ("team_id", text(&network.team_id)),
            ("team_name", text(team_name)),
            ("status", text("running")),
            ("created", Value::Bool(false)),
        ]))
    }

    /// Creates the Team, or confirms it under the same name.
    ///
    /// # Errors
    /// `team-name-conflict`, `team-name-taken`, a cleanup's problem, `docker-create-failed`, and the network's.
    pub async fn create(&self, team_id: &str, team_name: &str) -> Result<Value, Problem> {
        let _names = self.coordination.locks.names().await;
        let _team = self.coordination.locks.team(team_id).await;
        if let Some(network) = self.networks.find(team_id).await? {
            return self.existing(&network, team_name);
        }
        self.require_free(team_name, team_id).await?;
        let team = TeamRef {
            team_id: team_id.to_owned(),
            generation: None,
        };
        for participant in &self.participants {
            let kinds = participant.kinds();
            if kinds.iter().any(|kind| CREATE_CLEARS.contains(kind)) {
                participant
                    .remove_team(&team)
                    .await
                    .map_err(|failure| failure_problem(kinds[0], failure, false))?;
            }
        }
        let (network, created) = self.networks.create(team_id, team_name).await?;
        if !created {
            return self.existing(&network, team_name);
        }
        Ok(object(vec![
            ("team_id", text(team_id)),
            ("team_name", text(team_name)),
            ("status", text("running")),
            ("created", Value::Bool(true)),
        ]))
    }

    /// Renames the Team; returning to its creation label drops the record.
    ///
    /// # Errors
    /// `team-not-found`, `team-name-taken`, `team-names-unavailable`, and the network's.
    pub async fn rename(&self, team_id: &str, team_name: &str) -> Result<Value, Problem> {
        let _names = self.coordination.locks.names().await;
        let _team = self.coordination.locks.team(team_id).await;
        let network = self.networks.require(team_id).await?;
        if self.display_name(&network)? != team_name {
            self.require_free(team_name, team_id).await?;
            if team_name == network.label_name {
                self.names.delete(team_id)?;
            } else {
                self.names.save(team_id, &network.id, team_name)?;
            }
        }
        Ok(object(vec![
            ("team_id", text(team_id)),
            ("team_name", text(team_name)),
        ]))
    }

    async fn run(
        &self,
        order: &[&[ResidueKind]],
        team: Option<&TeamRef>,
    ) -> Result<ResidueProof, Problem> {
        let mut proof = ResidueProof::new();
        for step in order {
            for participant in &self.participants {
                let kinds = participant.kinds();
                if !kinds.iter().all(|kind| step.contains(kind)) {
                    continue;
                }
                let removed = match team {
                    Some(team) => participant.remove_team(team).await,
                    None => participant.remove_space().await,
                };
                removed.map_err(|failure| failure_problem(kinds[0], failure, team.is_none()))?;
                proof.prove(kinds);
            }
        }
        if proof.complete() {
            Ok(proof)
        } else {
            Err(teardown_incomplete(team.is_none()))
        }
    }

    /// Destroys the Team once `expected_name` confirms its current name.
    ///
    /// # Errors
    /// `team-name-mismatch`, `file-preview-active`, `chat-active`, `ownership-conflict`, a participant's problem,
    /// and `teardown-incomplete`.
    pub async fn destroy(&self, team_id: &str, expected_name: &str) -> Result<Value, Problem> {
        let coordination = &self.coordination;
        // The names lock keeps the confirmed name current for the whole teardown; chat never takes it (ADR-0088).
        let _names = coordination.locks.names().await;
        {
            let _team = coordination.locks.team(team_id).await;
            if let Some(network) = self.networks.find(team_id).await?
                && self.display_name(&network)? != expected_name
            {
                return Err(TEAM_NAME_MISMATCH);
            }
        }
        let _closed = coordination
            .previews
            .close(Some(team_id), self.helpers.as_ref(), PREVIEW_DRAIN)
            .await
            .map_err(|_| FILE_PREVIEW_ACTIVE)?;
        let _cancelled = coordination.executions.cancel(team_id, false);
        let _slot = coordination
            .executions
            .exclusive(team_id, DRAIN)
            .await
            .map_err(|_| CHAT_STOP_TIMEOUT)?;
        let _team = coordination.locks.team(team_id).await;
        let network = self.networks.find(team_id).await?;
        let assistants = self
            .assistants
            .inventory_team(team_id, network.as_ref())
            .await?;
        let storage = std::fs::symlink_metadata(self.storage.join(team_id)).is_ok();
        let team = TeamRef {
            team_id: team_id.to_owned(),
            generation: network.as_ref().map(|network| network.id.clone()),
        };
        let proof = self.run(&DESTROY_ORDER, Some(&team)).await?;
        Ok(object(vec![
            ("team_id", text(team_id)),
            ("destroyed", Value::Bool(network.is_some())),
            ("assistants_removed", count(assistants)),
            ("storage_removed", Value::Bool(storage)),
            ("residue_absent", residue(&proof)),
        ]))
    }

    fn stored_teams(&self) -> usize {
        std::fs::read_dir(&self.storage).map_or(0, |entries| {
            entries
                .filter_map(Result::ok)
                .filter(|entry| entry.file_name().to_str().is_some_and(team_id_valid))
                .count()
        })
    }

    /// Removes every exactly owned workload, network, and Team state of the Space.
    ///
    /// # Errors
    /// `chat-active`, `file-preview-active`, `ownership-conflict`, `docker-reset-failed`, a participant's problem,
    /// and `teardown-incomplete`.
    pub async fn reset(&self) -> Result<Value, Problem> {
        let coordination = &self.coordination;
        let _names = coordination.locks.names().await;
        // Every running execution stops and drains first, and none is admitted until the reset ends.
        let _drained = coordination
            .executions
            .drain(DRAIN)
            .await
            .map_err(|_| CHAT_STOP_TIMEOUT)?;
        // No file preview runs or starts a helper until the reset ends.
        let _closed = coordination
            .previews
            .close(None, self.helpers.as_ref(), PREVIEW_DRAIN)
            .await
            .map_err(|_| FILE_PREVIEW_ACTIVE)?;
        let _teams = coordination.locks.all_teams().await;
        let assistants = self.assistants.inventory_space().await?;
        let teams = self.networks.list().await.map_err(|problem| {
            if problem.code == Some("ownership-conflict") {
                SPACE_RESOURCE_OWNERSHIP_CONFLICT
            } else {
                SPACE_RESET_FAILED
            }
        })?;
        let storage = self.stored_teams();
        let proof = self.run(&RESET_ORDER, None).await?;
        Ok(object(vec![
            ("reset", Value::Bool(true)),
            ("assistants_removed", count(assistants)),
            ("teams_removed", count(teams.len())),
            ("storage_removed", count(storage)),
            ("residue_absent", residue(&proof)),
        ]))
    }

    /// Resets a Space whose Admin publishes no Supervisor key, then forgets Team's pin of an earlier key.
    ///
    /// # Errors
    /// `supervisor-established`, `supervisor-unavailable`, and the reset's problems.
    pub async fn bootstrap_reset(&self) -> Result<Value, Problem> {
        self.pin.require_absent().map_err(|absence| match absence {
            Absence::Established => SUPERVISOR_ESTABLISHED,
            Absence::Unavailable => SUPERVISOR_UNAVAILABLE,
        })?;
        let reset = self.reset().await?;
        self.pin
            .forget()
            .map_err(|PinRemains| SUPERVISOR_UNAVAILABLE)?;
        Ok(reset)
    }

    async fn answer(&self, call: &Call<'_>) -> Result<Value, Problem> {
        let team_id = || {
            call.route
                .param("team_id")
                .filter(|team_id| team_id_valid(team_id))
                .ok_or(INVALID_TEAM_ID)
        };
        match call.operation() {
            Operation::TeamList => self.list().await,
            Operation::TeamCreate => {
                let team_id = team_id()?;
                let name = canonical_name(team_name_body(&call.body)?)?;
                self.create(team_id, &name).await
            }
            Operation::TeamRename => {
                let team_id = team_id()?;
                let name = canonical_name(team_name_body(&call.body)?)?;
                self.rename(team_id, &name).await
            }
            Operation::TeamDestroy => {
                let team_id = team_id()?;
                let name = canonical_name(team_name_body(&call.body)?)?;
                self.destroy(team_id, &name).await
            }
            Operation::SpaceReset => self.reset().await,
            Operation::SpaceBootstrapReset => self.bootstrap_reset().await,
            _ => Err(INTERNAL),
        }
    }
}

impl Service for Lifecycle {
    fn call<'a>(&'a self, call: Call<'a>) -> BoxFuture<'a, Result<Answer, Problem>> {
        Box::pin(async move { self.answer(&call).await.map(json) })
    }
}

#[cfg(all(test, feature = "testing"))]
mod tests;
