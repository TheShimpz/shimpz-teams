//! The seams a replayed Routine run reaches the rest of the controller through (ADR-0092): the Action batch it
//! dispatches with and the hooks that seal each dispatch in its cursor before the RPC, the Action journal its
//! incidents read and archive, the workloads its recovery proves stopped, and the routine key it is claimed under.
//!
//! The Action runner implements [`RoutineActions`] (and [`RoutineJournal`] over its journal); the Docker controller
//! implements [`Workloads`]. Tests run over the fakes in [`fakes`].

use std::sync::Arc;

use team_action::executor::{ActionBatch, ActionRequest, BatchScope};
use team_action::failure::ActionFailure;
use team_action::journal::{Journal, OperationRecord};
use team_core::future::BoxFuture;

use crate::authority::Verifier;

/// Team's own classification of one failed attempt, never a model's or a message's (`replay.fault_of`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FaultClass {
    /// The Action answered a Team-admitted handled failure.
    Handled,
    /// The exchange failed after the workload was proven fail-stopped.
    Transport,
    /// Team could not prove the workload stopped after an ambiguous outcome (`assistant-action-blocked`).
    Unquiesced,
    /// A Team-detected policy fault: a secret echo, an invalid result or frame, or an undeclared human request.
    Policy,
    /// Anything else, such as a refusal outside the RPC.
    Other,
}

impl FaultClass {
    /// Its name as a cursor seals it.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Handled => "handled",
            Self::Transport => "transport",
            Self::Unquiesced => "unquiesced",
            Self::Policy => "policy",
            Self::Other => "other",
        }
    }
}

/// One failed attempt as the Action runner knows it, before the segment unwinds.
#[derive(Clone, Debug)]
pub struct Failed {
    /// The attempt's logical operation.
    pub operation_id: String,
    /// Team's classification of the failure.
    pub fault: FaultClass,
    /// The Team-admitted handled failure document, for [`FaultClass::Handled`].
    pub failure: Option<ActionFailure>,
    /// The closed transport condition, for [`FaultClass::Transport`] or [`FaultClass::Policy`] frame faults.
    pub condition: Option<String>,
    /// Every value Team injected into the attempt: its Stored Input values and resolved Integration strings.
    pub protected: Vec<String>,
}

/// A dispatch the run refused to seal: nothing may be sent, and the invoke fails with
/// [`team_action::executor::ActionFailure::StateUnavailable`].
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Refused;

/// What a Routine run's batch tells the run around each dispatch (`replay.ReplayRuntime`'s executor hooks).
pub trait DispatchHooks: Send + Sync {
    /// The logical operation a permitted retry repeats; `None` lets the journal mint the request's own.
    fn logical_operation(&self, request: &ActionRequest) -> Option<String>;

    /// Called after the operation is journaled `executing` and before its RPC, with the workload it is sent to and
    /// every value Team injects into it. The run seals the dispatch in its cursor here.
    ///
    /// # Errors
    /// [`Refused`] when the dispatch cannot be sealed; the RPC must then not be sent.
    fn dispatching(
        &self,
        request: &ActionRequest,
        operation_id: &str,
        workload: &str,
        protected: &[String],
    ) -> Result<(), Refused>;

    /// The dispatched Action returned a result or a human request, before Team projects anything of it.
    fn returned(&self, request: &ActionRequest);

    /// The dispatched Action failed; called once, before the invoke returns its failure.
    fn failed(&self, request: &ActionRequest, failed: &Failed);
}

/// Runs a Routine run's Actions: a durable batch bound to `scope` (`scope.routine` is `true`) that calls `hooks`
/// around every dispatch.
pub trait RoutineActions: Send + Sync {
    /// A new batch.
    fn batch(&self, scope: BatchScope, hooks: Arc<dyn DispatchHooks>) -> Box<dyn ActionBatch>;
}

/// The Action journal could not be read or written.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct JournalUnavailable;

/// What a Routine's incidents and cleanup read and change of the Action journal.
pub trait RoutineJournal: Send + Sync {
    /// The fingerprint and state (`open`, `ended`, or `archived`) of a generation's batch, if it holds one.
    ///
    /// # Errors
    /// [`JournalUnavailable`].
    fn current_batch(
        &self,
        generation: &str,
    ) -> Result<Option<(String, String)>, JournalUnavailable>;

    /// The compact state of one live batch's operations.
    ///
    /// # Errors
    /// [`JournalUnavailable`], also when the batch is not the generation's current one.
    fn snapshot(
        &self,
        generation: &str,
        fingerprint: &str,
    ) -> Result<Vec<OperationRecord>, JournalUnavailable>;

    /// Archives a held batch, which nothing may reopen.
    ///
    /// # Errors
    /// [`JournalUnavailable`].
    fn archive(&self, generation: &str, fingerprint: &str) -> Result<(), JournalUnavailable>;

    /// Releases an archived batch once its incident no longer needs it.
    ///
    /// # Errors
    /// [`JournalUnavailable`].
    fn release_archive(
        &self,
        generation: &str,
        fingerprint: &str,
    ) -> Result<(), JournalUnavailable>;

    /// Removes a generation's batch unless it is archived.
    ///
    /// # Errors
    /// [`JournalUnavailable`].
    fn discard(&self, generation: &str) -> Result<(), JournalUnavailable>;

    /// Removes a generation whole, archive marker included.
    ///
    /// # Errors
    /// [`JournalUnavailable`].
    fn purge(&self, generation: &str) -> Result<(), JournalUnavailable>;

    /// The fingerprint of a generation's batch when one of its operations may have acted.
    ///
    /// # Errors
    /// [`JournalUnavailable`].
    fn uncertain_fingerprint(&self, generation: &str)
    -> Result<Option<String>, JournalUnavailable>;

    /// Ends a generation's settled open batch before a fresh segment.
    ///
    /// # Errors
    /// [`JournalUnavailable`].
    fn end_settled(&self, generation: &str) -> Result<(), JournalUnavailable>;
}

impl RoutineJournal for Journal {
    fn current_batch(
        &self,
        generation: &str,
    ) -> Result<Option<(String, String)>, JournalUnavailable> {
        Self::current_batch(self, generation).map_err(|_| JournalUnavailable)
    }

    fn snapshot(
        &self,
        generation: &str,
        fingerprint: &str,
    ) -> Result<Vec<OperationRecord>, JournalUnavailable> {
        Self::snapshot(self, generation, fingerprint).map_err(|_| JournalUnavailable)
    }

    fn archive(&self, generation: &str, fingerprint: &str) -> Result<(), JournalUnavailable> {
        Self::archive(self, generation, fingerprint).map_err(|_| JournalUnavailable)
    }

    fn release_archive(
        &self,
        generation: &str,
        fingerprint: &str,
    ) -> Result<(), JournalUnavailable> {
        Self::release_archive(self, generation, fingerprint).map_err(|_| JournalUnavailable)
    }

    fn discard(&self, generation: &str) -> Result<(), JournalUnavailable> {
        Self::discard(self, generation).map_err(|_| JournalUnavailable)
    }

    fn purge(&self, generation: &str) -> Result<(), JournalUnavailable> {
        Self::purge(self, generation).map_err(|_| JournalUnavailable)
    }

    fn uncertain_fingerprint(
        &self,
        generation: &str,
    ) -> Result<Option<String>, JournalUnavailable> {
        Self::uncertain_fingerprint(self, generation).map_err(|_| JournalUnavailable)
    }

    fn end_settled(&self, generation: &str) -> Result<(), JournalUnavailable> {
        Self::end_settled(self, generation).map_err(|_| JournalUnavailable)
    }
}

/// An attempt's workload as Docker reports it now.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct WorkloadState {
    /// Whether it runs; `false` also when that container is gone or was replaced by another.
    pub running: bool,
    /// When it last started, in whole epoch seconds, if Docker says.
    pub started_at: Option<i64>,
}

/// The containers a held attempt was dispatched to (`recovery._workload_state`).
pub trait Workloads: Send + Sync {
    /// The state of `workload`, the container an attempt of `assistant_id` was dispatched to; `None` when it cannot
    /// be read, which is never proof of anything.
    fn state<'a>(
        &'a self,
        team_id: &'a str,
        assistant_id: &'a str,
        workload: &'a str,
    ) -> BoxFuture<'a, Option<WorkloadState>>;
}

/// The routine key fingerprint Admin's routine identity signs with now.
pub trait RoutineKey: Send + Sync {
    /// The current fingerprint; `None` when no Supervisor key is installed.
    fn fingerprint(&self) -> Option<String>;
}

impl RoutineKey for Verifier {
    fn fingerprint(&self) -> Option<String> {
        self.routine_key_fingerprint().ok()
    }
}

#[cfg(test)]
pub mod fakes;
