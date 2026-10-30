//! The teardown contract: the 17 kinds of owned Team state a destruction or the Space reset must prove absent, the
//! order in which their owners remove them, and the participant seam every owner implements.
//!
//! A successful answer names every kind in `residue_absent`; a teardown that proves fewer is `teardown-incomplete`.

use std::collections::BTreeSet;

use team_core::future::BoxFuture;

/// One kind of owned Team state.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum ResidueKind {
    /// Assistant containers.
    AssistantContainers,
    /// The Brain's conversation checkpoints.
    BrainCheckpoints,
    /// Browser containers.
    Browsers,
    /// Encrypted chat continuations.
    ChatContinuations,
    /// Assistant egress tokens and policies.
    EgressPolicies,
    /// Inference settings and learned memory.
    InferenceConfiguration,
    /// Integration credentials.
    IntegrationCredentials,
    /// Stored Inputs.
    StoredInputs,
    /// The Action journal's generation.
    ActionCheckpoints,
    /// Preparation helpers.
    PreparationHelpers,
    /// Publication bindings.
    PublicationBindings,
    /// Routines, their runs, and diagnostics.
    Routines,
    /// Runtime state.
    RuntimeState,
    /// Sign-ins.
    SignIns,
    /// The Team network.
    TeamNetworks,
    /// The Team's display name.
    TeamNames,
    /// Team files.
    TeamStorage,
}

impl ResidueKind {
    /// Every kind.
    pub const ALL: [Self; 17] = [
        Self::AssistantContainers,
        Self::BrainCheckpoints,
        Self::Browsers,
        Self::ChatContinuations,
        Self::EgressPolicies,
        Self::InferenceConfiguration,
        Self::IntegrationCredentials,
        Self::StoredInputs,
        Self::ActionCheckpoints,
        Self::PreparationHelpers,
        Self::PublicationBindings,
        Self::Routines,
        Self::RuntimeState,
        Self::SignIns,
        Self::TeamNetworks,
        Self::TeamNames,
        Self::TeamStorage,
    ];

    /// The kind's name in `residue_absent`.
    #[must_use]
    pub const fn name(self) -> &'static str {
        NAMES[self as usize]
    }
}

const NAMES: [&str; 17] = [
    "assistant_containers",
    "brain_checkpoints",
    "browsers",
    "chat_continuations",
    "egress_policies",
    "inference_configuration",
    "integration_credentials",
    "stored_inputs",
    "action_checkpoints",
    "preparation_helpers",
    "publication_bindings",
    "routines",
    "runtime_state",
    "sign_ins",
    "team_networks",
    "team_names",
    "team_storage",
];

use ResidueKind as K;

/// A Team's destruction, step by step: each step's owner removes its kinds before the next step starts. The Team's
/// chat continuations end first (its paused turns, with the slot drained and the stripe held); its network goes only
/// after everything inside it, and its name only after its network.
pub const DESTROY_ORDER: [&[ResidueKind]; 12] = [
    &[K::ChatContinuations],
    &[K::BrainCheckpoints, K::ActionCheckpoints],
    &[K::PreparationHelpers],
    &[K::Browsers],
    &[K::SignIns],
    &[K::Routines],
    &[
        K::AssistantContainers,
        K::EgressPolicies,
        K::PublicationBindings,
    ],
    &[K::InferenceConfiguration, K::TeamStorage],
    &[K::TeamNetworks],
    &[K::TeamNames],
    &[K::IntegrationCredentials, K::StoredInputs],
    &[K::RuntimeState],
];

/// The Space reset, step by step, once every execution is drained, every preview closed, and every stripe held.
pub const RESET_ORDER: [&[ResidueKind]; 13] = [
    &[K::IntegrationCredentials, K::StoredInputs],
    &[K::BrainCheckpoints, K::ActionCheckpoints],
    &[K::Routines],
    &[K::AssistantContainers],
    &[K::EgressPolicies, K::PublicationBindings],
    &[K::TeamStorage],
    &[K::InferenceConfiguration],
    &[K::TeamNetworks],
    &[K::TeamNames],
    &[K::RuntimeState, K::ChatContinuations],
    &[K::PreparationHelpers],
    &[K::Browsers],
    &[K::SignIns],
];

/// The kinds a teardown proved absent so far.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ResidueProof(BTreeSet<ResidueKind>);

impl ResidueProof {
    /// Nothing proved yet.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Records `kinds` as proved absent.
    pub fn prove(&mut self, kinds: &[ResidueKind]) {
        self.0.extend(kinds);
    }

    /// Whether every kind is proved absent.
    #[must_use]
    pub fn complete(&self) -> bool {
        self.0.len() == ResidueKind::ALL.len()
    }

    /// The proved kinds' names, sorted, for `residue_absent`.
    #[must_use]
    pub fn names(&self) -> Vec<&'static str> {
        let mut names: Vec<&'static str> = self.0.iter().map(|kind| kind.name()).collect();
        names.sort_unstable();
        names
    }
}

/// The Team (and, once its network exists, its generation) a teardown removes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TeamRef {
    /// The Team's id.
    pub team_id: String,
    /// Its network's id, when the network still exists.
    pub generation: Option<String>,
}

/// A closed teardown failure.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TeardownFailed {
    /// A resource is not exactly owned; nothing of it was removed.
    OwnershipConflict,
    /// A store could not be read or written.
    StateUnavailable,
    /// Docker or another external operation failed.
    Failed,
}

/// One owner of Team state taking part in a teardown. Removal is idempotent: state already absent is a success.
pub trait TeamTeardown: Send + Sync {
    /// The kinds this owner removes and proves absent; together, the owners' kinds are every kind exactly once.
    fn kinds(&self) -> &'static [ResidueKind];

    /// Removes `team`'s state of these kinds.
    ///
    /// # Errors
    /// [`TeardownFailed`] when it may remain.
    fn remove_team<'a>(&'a self, team: &'a TeamRef) -> BoxFuture<'a, Result<(), TeardownFailed>>;

    /// Removes every Team's state of these kinds (the Space reset).
    ///
    /// # Errors
    /// [`TeardownFailed`] when some may remain.
    fn remove_space(&self) -> BoxFuture<'_, Result<(), TeardownFailed>>;
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use super::{DESTROY_ORDER, RESET_ORDER, ResidueKind, ResidueProof};

    fn partitions_every_kind(order: &[&[ResidueKind]]) -> bool {
        let listed: Vec<ResidueKind> = order.iter().flat_map(|step| step.iter().copied()).collect();
        let unique: BTreeSet<ResidueKind> = listed.iter().copied().collect();
        listed.len() == ResidueKind::ALL.len() && unique.len() == ResidueKind::ALL.len()
    }

    #[test]
    fn destroy_and_reset_each_prove_all_17_residue_kinds_once() {
        assert!(partitions_every_kind(&DESTROY_ORDER));
        assert!(partitions_every_kind(&RESET_ORDER));
        for (index, kind) in ResidueKind::ALL.iter().enumerate() {
            assert_eq!(*kind as usize, index);
        }
    }

    #[test]
    fn a_partial_proof_is_incomplete() {
        let mut proof = ResidueProof::new();
        for step in &DESTROY_ORDER[..11] {
            proof.prove(step);
        }
        assert!(!proof.complete());
        proof.prove(DESTROY_ORDER[11]);
        assert!(proof.complete());
        let names = proof.names();
        assert_eq!(names.len(), 17);
        assert_eq!(names.first(), Some(&"action_checkpoints"));
        assert_eq!(names.last(), Some(&"team_storage"));
    }
}
