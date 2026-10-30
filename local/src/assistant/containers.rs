//! The lifecycle of Assistant containers on their Team network.
//!
//! - **Create.** The egress policy is written before Docker can create the container (ADR-0106), so a container
//!   whose create answer is lost still holds its route and the next install adopts it. The created container is
//!   inspected (its image must be the one resolved, and its image's manifest the reviewed one) before it starts, and
//!   inspected against its complete profile after it starts, before it is ready for any input.
//! - **Failure.** A failed create compensates only what it proves: a container is removed (or, when its removal
//!   fails, fail-stopped and left with its policy for the next install); a policy it created is removed only once no
//!   container can hold it. A create whose answer Docker did not give leaves everything for adoption.
//! - **Stop.** Fail-stop proves a workload stopped, or blocks it until it is reinstalled.
//! - **Removal.** Only an admitted container is removed: one whose Assistant has no binding, whose labels, name, or
//!   profile drifted, is never treated as Team's own.

use std::collections::HashSet;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use bollard::models::{ContainerInspectResponse, ContainerStateStatusEnum, ContainerSummary};
use team_core::entropy::Entropy;
use team_core::exec::{ExecEngine, FailStop};
use team_core::future::BoxFuture;
use team_egress::hosts::canonical_allowed_hosts;
use team_egress::policy::{PolicyError, PolicyStore};

use super::image::{self, Resolved};
use super::isolation::{self, Admitted, KIND, Placement};
use super::problem::{
    ACTION_BLOCKED, ALLOWED_HOSTS_INVALID, EGRESS_POLICY_DRIFT, EGRESS_POLICY_UNAVAILABLE,
    IMAGE_RESOLUTION_MISMATCH, INSTALL_FAILED, INSTALL_ROLLBACK_INCOMPLETE, ISOLATION_DRIFT,
    NOT_FOUND, NOT_READY, REGISTRY_DRIFT, REMOVE_FAILED, START_FAILED, UPDATE_REQUIRED,
};
use super::{Bindings, ContainerSpec};
use crate::docker::labels::{ASSISTANT, Space, filter};
use crate::docker::problem::{DOCKER_UNAVAILABLE, INTERNAL};
use crate::docker::{DockerEngine, DockerError};
use crate::http::service::Problem;

/// How long a started container may take to run.
pub const READY_WAIT: Duration = Duration::from_secs(15);
/// How often readiness is polled.
const READY_POLL: Duration = Duration::from_millis(200);

/// What the install workstream proves of a container's image beyond its isolation: its manifest and its Genesis.
pub trait Admission: Send + Sync {
    /// The image's manifest declares exactly `spec`'s reviewed contract (hosts and Integrations) for a container of
    /// `team_id`.
    ///
    /// # Errors
    /// `assistant-manifest-invalid` for a contract mismatch, `assistant-manifest-unavailable` when it cannot be read.
    fn manifest<'a>(
        &'a self,
        team_id: &'a str,
        container: &'a str,
        spec: &'a ContainerSpec,
    ) -> BoxFuture<'a, Result<(), Problem>>;

    /// The ready container's Genesis is admitted.
    ///
    /// # Errors
    /// The Genesis refusal.
    fn genesis<'a>(
        &'a self,
        container: &'a str,
        spec: &'a ContainerSpec,
    ) -> BoxFuture<'a, Result<(), Problem>>;

    /// Forgets every review cached for `container`, which is being removed.
    fn forget(&self, container: &str);
}

/// One install's last authorization before a container starts (it re-resolves the publication).
pub trait AuthorizeStart: Send + Sync {
    /// Authorizes the start.
    ///
    /// # Errors
    /// The refusal; the container is then removed.
    fn authorize(&self) -> BoxFuture<'_, Result<(), Problem>>;
}

/// What [`Containers::ensure`] did.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Ensured {
    /// A new container was created.
    Created,
    /// An outdated or unready container was replaced.
    Replaced,
    /// The existing container was admitted (and started when it was not running).
    Existing,
}

/// A removed container.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Removed {
    /// The image id it ran (`sha256:…`), for the retired-image cleanup, when Docker reported one.
    pub image_id: Option<String>,
}

/// Why a create step failed: a refusal already public, or a Docker failure the create maps itself.
enum Failure {
    Refused(Problem),
    Docker,
}

impl From<Problem> for Failure {
    fn from(problem: Problem) -> Self {
        Self::Refused(problem)
    }
}

impl From<DockerError> for Failure {
    fn from(_: DockerError) -> Self {
        Self::Docker
    }
}

fn policy_problem(error: PolicyError) -> Problem {
    match error {
        PolicyError::Drift => EGRESS_POLICY_DRIFT,
        PolicyError::Unavailable => EGRESS_POLICY_UNAVAILABLE,
    }
}

/// The Space's Assistant containers.
pub struct Containers {
    docker: Arc<dyn DockerEngine>,
    exec: Arc<dyn ExecEngine>,
    admission: Arc<dyn Admission>,
    policies: PolicyStore,
    entropy: Arc<dyn Entropy>,
    space: Space,
    cpuset: String,
    ready_wait: Duration,
    blocked: Mutex<HashSet<String>>,
}

impl std::fmt::Debug for Containers {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.debug_struct("Containers").finish_non_exhaustive()
    }
}

/// The collaborators of [`Containers`].
pub struct Parts {
    /// The daemon.
    pub docker: Arc<dyn DockerEngine>,
    /// Fail-stop.
    pub exec: Arc<dyn ExecEngine>,
    /// Manifest and Genesis admission.
    pub admission: Arc<dyn Admission>,
    /// The egress policy directory.
    pub policies: PolicyStore,
    /// Fresh policy tokens.
    pub entropy: Arc<dyn Entropy>,
    /// The Space.
    pub space: Space,
    /// The CPUs every workload shares.
    pub cpuset: String,
}

impl Containers {
    /// The Space's Assistant containers.
    #[must_use]
    pub fn new(parts: Parts) -> Self {
        Self {
            docker: parts.docker,
            exec: parts.exec,
            admission: parts.admission,
            policies: parts.policies,
            entropy: parts.entropy,
            space: parts.space,
            cpuset: parts.cpuset,
            ready_wait: READY_WAIT,
            blocked: Mutex::default(),
        }
    }

    /// The same, waiting at most `wait` for readiness (tests).
    #[must_use]
    pub fn with_ready_wait(mut self, wait: Duration) -> Self {
        self.ready_wait = wait;
        self
    }

    fn blocked(&self) -> MutexGuard<'_, HashSet<String>> {
        self.blocked.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn placement<'a>(&'a self, team_id: &'a str, network: &'a str) -> Placement<'a> {
        Placement {
            space: &self.space,
            team_id,
            network,
            cpuset: &self.cpuset,
        }
    }

    fn identity(&self, team_id: &str, assistant_id: &str) -> String {
        format!("{}\0{team_id}\0{assistant_id}", self.space.id())
    }

    /// The container of `assistant_id` in `team_id`, or `None` when it is proven absent.
    ///
    /// # Errors
    /// `docker-unavailable`: a Docker failure proves nothing about absence.
    pub async fn find(
        &self,
        team_id: &str,
        assistant_id: &str,
    ) -> Result<Option<ContainerInspectResponse>, Problem> {
        let name = self.space.container_name(team_id, assistant_id);
        match self.docker.inspect_container(&name).await {
            Ok(inspection) => Ok(Some(inspection)),
            Err(DockerError::NotFound) => Ok(None),
            Err(_) => Err(DOCKER_UNAVAILABLE),
        }
    }

    /// Admits `inspection` as `spec`'s container against its isolation profile alone (no egress or manifest), as a
    /// removal needs.
    ///
    /// # Errors
    /// `assistant-isolation-drift`.
    pub fn admit_profile(
        &self,
        inspection: &ContainerInspectResponse,
        team_id: &str,
        network: &str,
        spec: &ContainerSpec,
    ) -> Result<Admitted, Problem> {
        isolation::admit(inspection, self.placement(team_id, network), spec)
            .map_err(|isolation::Drift| ISOLATION_DRIFT)
    }

    /// Requires `admitted`'s environment to hold no proxy and, for an Assistant that calls providers, its egress
    /// policy to admit exactly the reviewed hosts.
    ///
    /// # Errors
    /// `assistant-registry-drift` for non-canonical reviewed hosts; `assistant-isolation-drift` for a proxy variable;
    /// `egress-policy-drift` or `egress-policy-unavailable` for the policy.
    pub fn admit_egress(
        &self,
        admitted: &Admitted,
        team_id: &str,
        spec: &ContainerSpec,
    ) -> Result<(), Problem> {
        if canonical_allowed_hosts(&spec.allowed_hosts).as_ref() != Some(&spec.allowed_hosts) {
            return Err(ALLOWED_HOSTS_INVALID);
        }
        if !isolation::egress_environment_valid(&admitted.environment) {
            return Err(ISOLATION_DRIFT);
        }
        if !spec.allowed_hosts.is_empty() {
            let identity = self.identity(team_id, &spec.assistant_id);
            self.policies
                .validate(&identity, &spec.allowed_hosts, self.entropy.as_ref())
                .map_err(policy_problem)?;
        }
        Ok(())
    }

    /// Admits `inspection` completely: profile, egress, and the image's manifest; with `current`, also the binding's
    /// exact image.
    ///
    /// # Errors
    /// As [`Containers::admit_profile`] and [`Containers::admit_egress`], the manifest refusal, and
    /// `assistant-update-required` for an older artifact.
    pub async fn admit(
        &self,
        inspection: &ContainerInspectResponse,
        team_id: &str,
        network: &str,
        spec: &ContainerSpec,
        current: bool,
    ) -> Result<Admitted, Problem> {
        let admitted = self.admit_profile(inspection, team_id, network, spec)?;
        self.admit_egress(&admitted, team_id, spec)?;
        self.admission.manifest(team_id, &admitted.id, spec).await?;
        if current && admitted.image != spec.image {
            return Err(UPDATE_REQUIRED);
        }
        Ok(admitted)
    }

    /// The Team's Assistant containers with the binding each one's label names, sorted by Assistant, each with its
    /// name, ownership labels, and uniqueness proved; no deeper admission.
    ///
    /// # Errors
    /// `docker-unavailable`; `assistant-registry-drift` for a container whose Assistant has no binding;
    /// `assistant-isolation-drift` for a misnamed, mislabelled, or repeated one.
    pub async fn bound(
        &self,
        team_id: &str,
        bindings: &dyn Bindings,
    ) -> Result<Vec<(ContainerInspectResponse, ContainerSpec)>, Problem> {
        let labels = self.space.labels(KIND, Some(team_id));
        let listed = self
            .docker
            .list_containers(&filter(&labels))
            .await
            .map_err(|_| DOCKER_UNAVAILABLE)?;
        let mut bound: Vec<(ContainerInspectResponse, ContainerSpec)> = Vec::new();
        for summary in listed {
            let (inspection, spec) = self.bound_one(team_id, summary, bindings).await?;
            if bound
                .iter()
                .any(|(_, other)| other.assistant_id == spec.assistant_id)
            {
                return Err(ISOLATION_DRIFT);
            }
            bound.push((inspection, spec));
        }
        bound.sort_by(|(_, left), (_, right)| left.assistant_id.cmp(&right.assistant_id));
        Ok(bound)
    }

    /// One listed container with the binding its Assistant label names, its name and labels proved.
    async fn bound_one(
        &self,
        team_id: &str,
        summary: ContainerSummary,
        bindings: &dyn Bindings,
    ) -> Result<(ContainerInspectResponse, ContainerSpec), Problem> {
        let assistant_id = summary
            .labels
            .as_ref()
            .and_then(|labels| labels.get(ASSISTANT))
            .ok_or(REGISTRY_DRIFT)?;
        let spec = bindings
            .spec(team_id, assistant_id)
            .await
            .map_err(|_| INTERNAL)?
            .ok_or(REGISTRY_DRIFT)?;
        let id = summary.id.ok_or(DOCKER_UNAVAILABLE)?;
        let inspection = self
            .docker
            .inspect_container(&id)
            .await
            .map_err(|_| DOCKER_UNAVAILABLE)?;
        let named = inspection
            .name
            .as_deref()
            .and_then(|name| name.strip_prefix('/'))
            == Some(
                self.space
                    .container_name(team_id, &spec.assistant_id)
                    .as_str(),
            );
        let labelled = crate::docker::labels::include(
            inspection
                .config
                .as_ref()
                .and_then(|config| config.labels.as_ref()),
            &isolation::ownership(&self.space, team_id, &spec.assistant_id),
        );
        if named && labelled {
            Ok((inspection, spec))
        } else {
            Err(ISOLATION_DRIFT)
        }
    }

    /// Writes the policy of `spec` before any container of it exists. A failed write that created the token removes
    /// it (no container can hold it yet); one that met an existing token keeps it, since a container not yet proven
    /// absent may hold it.
    fn prepare_policy(&self, team_id: &str, spec: &ContainerSpec) -> Result<(), Problem> {
        let identity = self.identity(team_id, &spec.assistant_id);
        let entropy = self.entropy.as_ref();
        let existed = self
            .policies
            .token(&identity, false, entropy)
            .map_err(policy_problem)?
            .is_some();
        let written = self
            .policies
            .token(&identity, true, entropy)
            .and_then(|token| token.ok_or(PolicyError::Unavailable))
            .and_then(|token| self.policies.write(&token, &spec.allowed_hosts));
        match written {
            Ok(()) => Ok(()),
            Err(error) if existed => Err(policy_problem(error)),
            Err(error) => match self.policies.remove(&identity) {
                Ok(()) => Err(policy_problem(error)),
                Err(_) => Err(INSTALL_ROLLBACK_INCOMPLETE),
            },
        }
    }

    /// Removes `assistant_id`'s policy and token; absence is success.
    ///
    /// # Errors
    /// `egress-policy-drift` or `egress-policy-unavailable`.
    pub fn remove_policy(&self, team_id: &str, assistant_id: &str) -> Result<(), Problem> {
        self.policies
            .remove(&self.identity(team_id, assistant_id))
            .map_err(policy_problem)
    }

    /// Whether `assistant_id` has an egress token.
    ///
    /// # Errors
    /// `egress-policy-drift` or `egress-policy-unavailable`.
    pub fn has_policy(&self, team_id: &str, assistant_id: &str) -> Result<bool, Problem> {
        self.policies
            .token(
                &self.identity(team_id, assistant_id),
                false,
                self.entropy.as_ref(),
            )
            .map(|token| token.is_some())
            .map_err(policy_problem)
    }

    /// The egress token an Action's provider calls carry: `None` when the Assistant has no policy; otherwise the
    /// token of a policy that admits exactly the reviewed `hosts`, read again at every use.
    ///
    /// # Errors
    /// `egress-policy-drift` for a policy outside its custody or admitting other hosts; `egress-policy-unavailable`.
    pub fn egress_token(
        &self,
        team_id: &str,
        assistant_id: &str,
        hosts: &[String],
    ) -> Result<Option<team_core::secret::Secret>, Problem> {
        let identity = self.identity(team_id, assistant_id);
        let admitted = self
            .policies
            .admitted(&identity, self.entropy.as_ref())
            .map_err(policy_problem)?;
        if admitted.is_none() {
            return Ok(None);
        }
        PolicyStore::validate_admitted(admitted, hosts)
            .map(Some)
            .map_err(policy_problem)
    }

    /// Creates, admits, starts, and readies `spec`'s container from the resolved `image` on `network`.
    ///
    /// # Errors
    /// The step's refusal, `docker-install-failed` for a Docker refusal whose container is proven absent or removed,
    /// or `assistant-install-rollback-incomplete` when what the create made could not be proven gone.
    pub async fn create(
        &self,
        team_id: &str,
        network: &str,
        spec: &ContainerSpec,
        image: &Resolved,
        authorize: Option<&dyn AuthorizeStart>,
    ) -> Result<Admitted, Problem> {
        let egress = !spec.allowed_hosts.is_empty();
        if egress {
            self.prepare_policy(team_id, spec)?;
        }
        let name = self.space.container_name(team_id, &spec.assistant_id);
        let body = isolation::create_body(self.placement(team_id, network), spec);
        let created = match self.docker.create_container(&name, body).await {
            Ok(id) => id,
            Err(error) => return Err(self.refused_create(error, team_id, spec, egress).await),
        };
        let admitted = self
            .admit_created(&created, team_id, network, spec, image, authorize)
            .await;
        match admitted {
            Ok(admitted) => Ok(admitted),
            Err(failure) => {
                self.rollback(team_id, spec, Some(&created), egress).await?;
                Err(match failure {
                    Failure::Refused(problem) => problem,
                    Failure::Docker => INSTALL_FAILED,
                })
            }
        }
    }

    /// The problem of a create Docker did not answer with an id. Docker may still be creating the container after a
    /// lost answer: only a refusal it answered, followed by a proven absence, lets the create roll back.
    async fn refused_create(
        &self,
        error: DockerError,
        team_id: &str,
        spec: &ContainerSpec,
        egress: bool,
    ) -> Problem {
        let answered = matches!(error, DockerError::Conflict | DockerError::NotFound);
        if !answered || !matches!(self.find(team_id, &spec.assistant_id).await, Ok(None)) {
            return INSTALL_ROLLBACK_INCOMPLETE;
        }
        match self.rollback(team_id, spec, None, egress).await {
            Ok(()) => INSTALL_FAILED,
            Err(problem) => problem,
        }
    }

    async fn admit_created(
        &self,
        created: &str,
        team_id: &str,
        network: &str,
        spec: &ContainerSpec,
        image: &Resolved,
        authorize: Option<&dyn AuthorizeStart>,
    ) -> Result<Admitted, Failure> {
        let inspection = self.docker.inspect_container(created).await?;
        if inspection.image.as_deref() != Some(image.id.as_str()) {
            return Err(IMAGE_RESOLUTION_MISMATCH.into());
        }
        // The image's manifest must declare exactly the reviewed hosts its policy already admits.
        self.admission.manifest(team_id, created, spec).await?;
        if let Some(authorize) = authorize {
            authorize.authorize().await?;
        }
        self.docker.start_container(created).await?;
        let inspection = self.docker.inspect_container(created).await?;
        let admitted = self
            .admit(&inspection, team_id, network, spec, true)
            .await?;
        self.wait_ready(created).await?;
        self.admission.genesis(created, spec).await?;
        Ok(admitted)
    }

    /// Removes what a failed create made. A container whose removal fails is fail-stopped and keeps its policy for
    /// the next install; a policy is removed only once its container is gone.
    async fn rollback(
        &self,
        team_id: &str,
        spec: &ContainerSpec,
        container: Option<&str>,
        egress: bool,
    ) -> Result<(), Problem> {
        let mut incomplete = false;
        if let Some(container) = container {
            self.admission.forget(container);
            match self.docker.remove_container(container).await {
                Ok(()) | Err(DockerError::NotFound) => {}
                Err(_) => {
                    incomplete = true;
                    let _stopped = self.fail_stop(container).await;
                }
            }
        }
        if egress && !incomplete && self.remove_policy(team_id, &spec.assistant_id).is_err() {
            incomplete = true;
        }
        if incomplete {
            Err(INSTALL_ROLLBACK_INCOMPLETE)
        } else {
            Ok(())
        }
    }

    /// Waits until `container` runs: created is still starting; anything else is not ready.
    async fn wait_ready(&self, container: &str) -> Result<(), Failure> {
        let deadline = tokio::time::Instant::now() + self.ready_wait;
        loop {
            let inspection = self.docker.inspect_container(container).await?;
            let status = inspection.state.and_then(|state| state.status);
            match status {
                Some(ContainerStateStatusEnum::RUNNING) => return Ok(()),
                Some(ContainerStateStatusEnum::CREATED)
                    if tokio::time::Instant::now() < deadline =>
                {
                    tokio::time::sleep(READY_POLL).await;
                }
                _ => return Err(NOT_READY.into()),
            }
        }
    }

    /// Removes an admitted container and forgets every review of it.
    ///
    /// # Errors
    /// [`DockerError`] when it may remain (an absent container is removed).
    pub async fn remove(&self, admitted: &Admitted) -> Result<Removed, DockerError> {
        self.remove_container(&admitted.id, admitted.image_id.clone())
            .await
    }

    /// Removes the container `id`, which ran image `image_id`, once its owner proved it the Team's.
    ///
    /// # Errors
    /// [`DockerError`] when it may remain (an absent container is removed).
    pub async fn remove_container(
        &self,
        id: &str,
        image_id: Option<String>,
    ) -> Result<Removed, DockerError> {
        self.admission.forget(id);
        match self.docker.remove_container(id).await {
            Ok(()) | Err(DockerError::NotFound) => {}
            Err(error) => return Err(error),
        }
        self.blocked().remove(id);
        Ok(Removed { image_id })
    }

    /// Fail-stops `container`: proven stopped, or blocked until it is reinstalled.
    ///
    /// # Errors
    /// `assistant-action-blocked` when it could not be proven stopped.
    pub async fn fail_stop(&self, container: &str) -> Result<(), Problem> {
        match self.exec.fail_stop(container).await {
            Ok(FailStop::Stopped) => Ok(()),
            Ok(FailStop::Blocked) | Err(_) => {
                self.blocked().insert(container.to_owned());
                Err(ACTION_BLOCKED)
            }
        }
    }

    /// Whether `container` is blocked.
    #[must_use]
    pub fn is_blocked(&self, container: &str) -> bool {
        self.blocked().contains(container)
    }

    /// Makes `spec`'s container of `team_id` exist, admitted, running, and ready: an absent one is created from its
    /// resolved image, an outdated one replaced, a stopped one started, and one that never became ready replaced.
    /// Isolation drift is never repaired: it refuses.
    ///
    /// # Errors
    /// Every admission refusal, `docker-start-failed`, `docker-remove-failed`, and the create's failures.
    pub async fn ensure(
        &self,
        team_id: &str,
        network: &str,
        spec: &ContainerSpec,
        authorize: Option<&dyn AuthorizeStart>,
    ) -> Result<Ensured, Problem> {
        let Some(existing) = self.find(team_id, &spec.assistant_id).await? else {
            let resolved = image::resolve(self.docker.as_ref(), spec).await?;
            Box::pin(self.create(team_id, network, spec, &resolved, authorize)).await?;
            return Ok(Ensured::Created);
        };
        let admitted = self.admit_profile(&existing, team_id, network, spec)?;
        self.admit_egress(&admitted, team_id, spec)?;
        if admitted.image == spec.image {
            return Box::pin(self.start_existing(team_id, network, spec, &admitted, authorize))
                .await;
        }
        // An outdated artifact is replaced from the trusted image, resolved before anything is removed; its manifest
        // is never admitted, since it is only removed.
        let resolved = image::resolve(self.docker.as_ref(), spec).await?;
        Box::pin(self.replace(team_id, network, spec, &admitted, &resolved, authorize)).await?;
        Ok(Ensured::Replaced)
    }

    /// Starts the current, admitted container when it is stopped and waits for it; one that never became ready is
    /// replaced.
    async fn start_existing(
        &self,
        team_id: &str,
        network: &str,
        spec: &ContainerSpec,
        admitted: &Admitted,
        authorize: Option<&dyn AuthorizeStart>,
    ) -> Result<Ensured, Problem> {
        self.admission.manifest(team_id, &admitted.id, spec).await?;
        if !admitted.running {
            self.start(&admitted.id, authorize).await?;
        }
        match self.wait_ready(&admitted.id).await {
            Ok(()) => {
                self.admission.genesis(&admitted.id, spec).await?;
                Ok(Ensured::Existing)
            }
            Err(Failure::Refused(problem)) if problem == NOT_READY => {
                Box::pin(self.replace_unready(team_id, network, spec, authorize)).await?;
                Ok(Ensured::Replaced)
            }
            Err(Failure::Refused(problem)) => Err(problem),
            Err(Failure::Docker) => Err(DOCKER_UNAVAILABLE),
        }
    }

    /// Starts an existing container once its install authorizes it.
    async fn start(
        &self,
        container: &str,
        authorize: Option<&dyn AuthorizeStart>,
    ) -> Result<(), Problem> {
        if let Some(authorize) = authorize {
            authorize.authorize().await?;
        }
        self.docker
            .start_container(container)
            .await
            .map_err(|_| START_FAILED)
    }

    /// Replaces a container that never became ready: its trusted image is resolved first, then its ownership is proved
    /// again, closing the pull/remove race.
    async fn replace_unready(
        &self,
        team_id: &str,
        network: &str,
        spec: &ContainerSpec,
        authorize: Option<&dyn AuthorizeStart>,
    ) -> Result<(), Problem> {
        let resolved = image::resolve(self.docker.as_ref(), spec).await?;
        let again = self
            .find(team_id, &spec.assistant_id)
            .await?
            .ok_or(NOT_FOUND)?;
        let admitted = self.admit(&again, team_id, network, spec, true).await?;
        self.replace(team_id, network, spec, &admitted, &resolved, authorize)
            .await
    }

    async fn replace(
        &self,
        team_id: &str,
        network: &str,
        spec: &ContainerSpec,
        existing: &Admitted,
        resolved: &Resolved,
        authorize: Option<&dyn AuthorizeStart>,
    ) -> Result<(), Problem> {
        self.remove(existing).await.map_err(|_| REMOVE_FAILED)?;
        if !spec.allowed_hosts.is_empty() {
            self.remove_policy(team_id, &spec.assistant_id)?;
        }
        Box::pin(self.create(team_id, network, spec, resolved, authorize))
            .await
            .map(|_| ())
    }

    /// Removes `assistant_id`'s container and policy from `team_id`; an Assistant already absent is success. A
    /// container is removed only when `spec` (its binding) admits it: without a binding it is registry drift and
    /// stays.
    ///
    /// # Errors
    /// `assistant-registry-drift`, `assistant-isolation-drift`, `docker-remove-failed`, and the policy's problems.
    pub async fn uninstall(
        &self,
        team_id: &str,
        network: &str,
        assistant_id: &str,
        spec: Option<&ContainerSpec>,
    ) -> Result<Option<Removed>, Problem> {
        let Some(existing) = self.find(team_id, assistant_id).await? else {
            if self.has_policy(team_id, assistant_id)? {
                self.remove_policy(team_id, assistant_id)?;
            }
            return Ok(None);
        };
        let spec = spec.ok_or(REGISTRY_DRIFT)?;
        let admitted = self.admit_profile(&existing, team_id, network, spec)?;
        let removed = self.remove(&admitted).await.map_err(|_| REMOVE_FAILED)?;
        if !spec.allowed_hosts.is_empty() {
            self.remove_policy(team_id, assistant_id)?;
        }
        Ok(Some(removed))
    }

    /// The admitted, running container of `spec` that may now receive an Action's input.
    ///
    /// # Errors
    /// `assistant-not-found`, every admission refusal, `assistant-action-blocked`, and `assistant-not-running`.
    pub async fn ready_for_input(
        &self,
        team_id: &str,
        network: &str,
        spec: &ContainerSpec,
    ) -> Result<Admitted, Problem> {
        let inspection = self
            .find(team_id, &spec.assistant_id)
            .await?
            .ok_or(NOT_FOUND)?;
        let admitted = self
            .admit(&inspection, team_id, network, spec, true)
            .await?;
        if self.is_blocked(&admitted.id) {
            return Err(ACTION_BLOCKED);
        }
        if !admitted.running {
            return Err(Problem::new(
                409,
                "Assistant is not running",
                "assistant-not-running",
            ));
        }
        Ok(admitted)
    }
}

#[cfg(all(test, feature = "testing"))]
mod tests;
