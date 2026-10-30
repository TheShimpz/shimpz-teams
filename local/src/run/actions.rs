//! The Action executor's controller ports (`local/app.py` `invoke`, `local/chat/segment.py`): the running binding an
//! attempt is dispatched to, the egress route its provider calls take, a Routine run's observed batches, and the
//! workload state a held attempt's recovery reads.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, PoisonError};

use team_action::executor::{
    ActionBatch, ActionFailure, ActionRequest, BatchScope, DispatchObserver, DispatchRefused, Fault,
};
use team_action::runner::{Binding, EgressUnavailable, Executor, Workloads};
use team_core::future::BoxFuture;
use team_core::secret::Secret;

use super::team::{Team, binding};
use crate::assistant::problem::{ACTION_BLOCKED, NOT_FOUND};
use crate::docker::problem::DOCKER_UNAVAILABLE;
use crate::names::docker_instant_ns;
use crate::routine::executor::{
    DispatchHooks, Failed, FaultClass, RoutineActions, WorkloadState, Workloads as RoutineWorkloads,
};

/// The running bindings and egress routes of the Space's Assistants.
pub struct Running {
    /// The Team view.
    pub team: Arc<Team>,
    /// The reviewed hosts of each binding an attempt was admitted with, which its provider calls are checked against.
    hosts: Mutex<HashMap<(String, String), Vec<String>>>,
}

impl Running {
    /// The ports over `team`.
    #[must_use]
    pub fn new(team: Arc<Team>) -> Self {
        Self {
            team,
            hosts: Mutex::new(HashMap::new()),
        }
    }
}

impl std::fmt::Debug for Running {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Running")
    }
}

fn binding_failure(problem: &crate::http::service::Problem) -> ActionFailure {
    if *problem == DOCKER_UNAVAILABLE {
        ActionFailure::DockerUnavailable
    } else if *problem == ACTION_BLOCKED {
        ActionFailure::Blocked
    } else {
        ActionFailure::ContextChanged
    }
}

impl Workloads for Running {
    fn binding<'a>(
        &'a self,
        team: &'a str,
        assistant: &'a str,
    ) -> BoxFuture<'a, Result<Binding, ActionFailure>> {
        Box::pin(async move {
            let view = &self.team;
            let network = view
                .networks
                .require(team)
                .await
                .map_err(|problem| binding_failure(&problem))?;
            let spec = view
                .spec(team, assistant)
                .await
                .map_err(|problem| binding_failure(&problem))?;
            let admitted = view
                .containers
                .ready_for_input(team, &network.name, &spec.container_spec())
                .await
                .map_err(|problem| binding_failure(&problem))?;
            self.hosts
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .insert(
                    (team.to_owned(), assistant.to_owned()),
                    spec.runtime.allowed_hosts.clone(),
                );
            Ok(binding(&spec, &admitted.id))
        })
    }

    fn egress(
        &self,
        team: &str,
        assistant: &str,
    ) -> Result<Option<(Secret, Vec<String>)>, EgressUnavailable> {
        let hosts = self
            .hosts
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .get(&(team.to_owned(), assistant.to_owned()))
            .cloned()
            .ok_or(EgressUnavailable)?;
        let token = self
            .team
            .containers
            .egress_token(team, assistant, &hosts)
            .map_err(|_| EgressUnavailable)?;
        Ok(token.map(|token| (token, hosts)))
    }
}

impl RoutineWorkloads for Running {
    fn state<'a>(
        &'a self,
        team_id: &'a str,
        assistant_id: &'a str,
        workload: &'a str,
    ) -> BoxFuture<'a, Option<WorkloadState>> {
        Box::pin(async move {
            let stopped = WorkloadState {
                running: false,
                started_at: None,
            };
            let found = match self.team.containers.find(team_id, assistant_id).await {
                Ok(found) => found,
                Err(problem) if problem == NOT_FOUND => None,
                Err(_) => return None,
            };
            let Some(inspection) = found else {
                return Some(stopped);
            };
            if inspection.id.as_deref() != Some(workload) {
                return Some(stopped);
            }
            let state = inspection.state?;
            let started_at = state
                .started_at
                .as_deref()
                .and_then(docker_instant_ns)
                .and_then(|nanos| i64::try_from(nanos.div_euclid(1_000_000_000)).ok());
            Some(WorkloadState {
                running: state.running?,
                started_at,
            })
        })
    }
}

/// A Routine run's batches: the executor's observed batches, telling the run's hooks around each dispatch.
#[derive(Clone, Debug)]
pub struct RoutineBatches(pub Executor);

impl RoutineActions for RoutineBatches {
    fn batch(&self, scope: BatchScope, hooks: Arc<dyn DispatchHooks>) -> Box<dyn ActionBatch> {
        self.0.observed(scope, Arc::new(Observer(hooks)))
    }
}

/// The run's hooks as the executor's observer.
struct Observer(Arc<dyn DispatchHooks>);

fn exposed(values: &[Secret]) -> Vec<String> {
    values
        .iter()
        .map(|value| value.expose().to_owned())
        .collect()
}

impl DispatchObserver for Observer {
    fn logical_operation(&self, request: &ActionRequest) -> Option<String> {
        self.0.logical_operation(request)
    }

    fn dispatching(
        &self,
        request: &ActionRequest,
        operation_id: &str,
        workload: &str,
        injected: &[Secret],
    ) -> Result<(), DispatchRefused> {
        self.0
            .dispatching(request, operation_id, workload, &exposed(injected))
            .map_err(|_| DispatchRefused)
    }

    fn returned(&self, request: &ActionRequest) {
        self.0.returned(request);
    }

    fn failed(
        &self,
        request: &ActionRequest,
        operation_id: &str,
        fault: Fault,
        injected: &[Secret],
    ) {
        let (fault, failure, condition) = match fault {
            Fault::Handled(failure) => (FaultClass::Handled, Some(*failure), None),
            Fault::Transport(condition) => (FaultClass::Transport, None, Some(condition)),
            Fault::Unquiesced => (FaultClass::Unquiesced, None, None),
            Fault::Policy(condition) => (FaultClass::Policy, None, condition),
            Fault::Other => (FaultClass::Other, None, None),
        };
        let failed = Failed {
            operation_id: operation_id.to_owned(),
            fault,
            failure,
            condition,
            protected: exposed(injected),
        };
        self.0.failed(request, &failed);
    }
}
