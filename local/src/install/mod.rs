//! The Assistants service group (workstream W3): Team inventory and pages, publication and Local snapshot installs,
//! uninstall, and direct invocation (`local/install/*`, `local/assistant/api.py`).
//!
//! Every lifecycle change runs under the Team's execution slot (refused `chat-active` while a chat turn or Routine
//! segment holds it) and then its stripe, after the publication's resolution and trust are proved. Container
//! isolation, egress policies, and readiness are the Assistant containers' ([`crate::assistant::containers`]); this
//! module owns what an Assistant may be: its binding, its review, its icon, and its update transaction.

pub mod admission;
pub mod automatic;
pub mod collector;
pub mod local;
pub mod problem;
pub mod publication;
pub mod registry;
pub mod snapshots;
pub mod source_package;
pub mod spec;
pub mod teardown;

use std::sync::Arc;
use std::time::Duration;

use strict_json::Value;
use team_action::executor::{ActionExecutor, ActionFailure, ActionRequest};
use team_assistant::details::AssistantPage;
use team_core::audit::AuditSink;
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_install::bindings::Provenance;
use team_install::icons::IconIdentity;
use team_install::trust::{ArtifactVerifier, Resolver};
use team_protocol::payload::{canonical_assistant_id, canonical_locale, canonical_team_id};

use self::admission::ImageAdmission;
use self::local::LocalSnapshots;
use self::problem::{
    ACTION_FAILED, ACTION_FILE_BUSY, ACTION_FILE_UNAVAILABLE, ACTION_INPUT_INVALID,
    ACTION_NOT_DECLARED, ACTION_STATE_UNAVAILABLE, ACTION_STOPPED, ACTION_UNCERTAIN, CHAT_ACTIVE,
    CONTEXT_CHANGED, HUMAN_REQUEST_REQUIRED, ICON_READ_UNAVAILABLE, INSTALL_BODY,
    INTEGRATION_UNAVAILABLE, INVALID_ASSISTANT_ID, INVALID_LOCALE, INVALID_TEAM_ID,
    LOCAL_INSTALL_BODY, MANIFEST_INVALID, NOT_INSTALLED,
};
use self::registry::Registry;
use self::snapshots::Stage;
use self::spec::{AssistantSpec, version_triple};
use crate::assistant::containers::Containers;
use crate::assistant::problem::{ACTION_BLOCKED, EGRESS_POLICY_DRIFT};
use crate::coordination::Coordination;
use crate::coordination::executions::SlotGuard;
use crate::coordination::locks::TeamGuard;
use crate::docker::problem::{DOCKER_UNAVAILABLE, INTERNAL};
use crate::http::operation::Operation;
use crate::http::service::{Answer, Body, Call, Problem, Service};
use crate::lifecycle::network::{TeamNetwork, TeamNetworks};

/// The private state Team holds for an installed Assistant (Stored Inputs, Integration credentials, the paused chat
/// continuation), which installs, updates, and uninstalls keep consistent; the chat and Integration services implement
/// it at the composition root.
pub trait PrivateState: Send + Sync {
    /// Keeps only what the updated Assistant still declares.
    ///
    /// # Errors
    /// The store's problem.
    fn retain_declared<'a>(
        &'a self,
        team_id: &'a str,
        spec: &'a AssistantSpec,
    ) -> BoxFuture<'a, Result<(), Problem>>;

    /// Deletes everything held for one Assistant of the Team.
    ///
    /// # Errors
    /// The store's problem.
    fn delete_assistant<'a>(
        &'a self,
        team_id: &'a str,
        assistant_id: &'a str,
    ) -> BoxFuture<'a, Result<(), Problem>>;

    /// Ends the Team's paused chat continuation, whose Assistants are changing.
    ///
    /// # Errors
    /// The store's problem.
    fn end_continuation<'a>(&'a self, team_id: &'a str) -> BoxFuture<'a, Result<(), Problem>>;
}

/// The collaborators of [`Assistants`].
pub struct Parts {
    /// The shared coordination.
    pub coordination: Coordination,
    /// The Team networks.
    pub networks: Arc<TeamNetworks>,
    /// The Assistant containers.
    pub containers: Arc<Containers>,
    /// The publication registry.
    pub registry: Arc<Registry>,
    /// Manifest, Genesis, and pack admission (also wired into the containers).
    pub admission: Arc<ImageAdmission>,
    /// Publication resolution.
    pub resolver: Arc<dyn Resolver>,
    /// Artifact verification.
    pub verifier: Arc<dyn ArtifactVerifier>,
    /// Direct Action invocation.
    pub executor: Arc<dyn ActionExecutor>,
    /// Private state the lifecycle keeps consistent.
    pub private: Arc<dyn PrivateState>,
    /// The daemon and Space staged snapshots are read from.
    pub stage: Stage,
    /// The audit log.
    pub audit: Arc<dyn AuditSink>,
}

/// The Assistants service group.
pub struct Assistants {
    coordination: Coordination,
    networks: Arc<TeamNetworks>,
    containers: Arc<Containers>,
    registry: Arc<Registry>,
    admission: Arc<ImageAdmission>,
    resolver: Arc<dyn Resolver>,
    verifier: Arc<dyn ArtifactVerifier>,
    executor: Arc<dyn ActionExecutor>,
    private: Arc<dyn PrivateState>,
    stage: Stage,
    local: LocalSnapshots,
}

impl std::fmt::Debug for Assistants {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Assistants")
    }
}

/// One Assistant of the Space's catalog and its newest binding's order.
struct CatalogEntry {
    id: String,
    order: ((u64, u64, u64), String),
    name: String,
}

/// A held Team: its execution slot (no chat turn or Routine segment runs) and then its stripe.
pub(crate) struct Held {
    _slot: SlotGuard,
    _stripe: TeamGuard,
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

/// An object of `members`, in order.
pub(crate) fn object(members: Vec<(&str, Value)>) -> Value {
    Value::Object(
        members
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    )
}

fn team_id<'a>(call: &'a Call<'_>) -> Result<&'a str, Problem> {
    call.route
        .param("team_id")
        .filter(|team| canonical_team_id(&text(team)).is_some())
        .ok_or(INVALID_TEAM_ID)
}

fn assistant_id<'a>(call: &'a Call<'_>) -> Result<&'a str, Problem> {
    call.route
        .param("assistant_id")
        .filter(|id| canonical_assistant_id(&text(id)).is_some())
        .ok_or(INVALID_ASSISTANT_ID)
}

fn locale<'a>(call: &'a Call<'_>) -> Result<&'a str, Problem> {
    call.route
        .param("locale")
        .filter(|locale| canonical_locale(&text(locale)).is_some())
        .ok_or(INVALID_LOCALE)
}

fn json(body: Value) -> Answer {
    Answer::Json { status: 200, body }
}

fn png(body: Vec<u8>) -> Answer {
    Answer::Binary {
        media_type: "image/png",
        body,
        headers: Vec::new(),
    }
}

/// The members of an exact JSON body of string fields `names`.
fn string_body<'a>(
    body: &'a Body,
    names: &[&str],
    problem: Problem,
) -> Result<Vec<&'a str>, Problem> {
    let Body::Json(Value::Object(members)) = body else {
        return Err(problem);
    };
    if members.len() != names.len() {
        return Err(problem);
    }
    names
        .iter()
        .map(|name| match body_member(members, name) {
            Some(Value::String(value)) => Ok(value.as_str()),
            _ => Err(problem.clone()),
        })
        .collect()
}

fn body_member<'a>(members: &'a [(String, Value)], name: &str) -> Option<&'a Value> {
    members
        .iter()
        .find(|(key, _)| key == name)
        .map(|(_, value)| value)
}

impl Assistants {
    /// The group from its collaborators.
    #[must_use]
    pub fn new(parts: Parts) -> Self {
        let local = LocalSnapshots::new(parts.stage.clone(), Arc::clone(&parts.audit));
        Self {
            coordination: parts.coordination,
            networks: parts.networks,
            containers: parts.containers,
            registry: parts.registry,
            admission: parts.admission,
            resolver: parts.resolver,
            verifier: parts.verifier,
            executor: parts.executor,
            private: parts.private,
            stage: parts.stage,
            local,
        }
    }

    /// Holds `team_id` for a lifecycle change.
    ///
    /// # Errors
    /// `chat-active` while a chat turn or Routine segment holds the Team.
    pub(crate) async fn hold(&self, team_id: &str) -> Result<Held, Problem> {
        let slot = self
            .coordination
            .executions
            .exclusive(team_id, Duration::ZERO)
            .await
            .map_err(|_| CHAT_ACTIVE)?;
        Ok(Held {
            _slot: slot,
            _stripe: self.coordination.locks.team(team_id).await,
        })
    }

    async fn network(&self, team_id: &str) -> Result<TeamNetwork, Problem> {
        self.networks.require(team_id).await
    }

    async fn installed(
        &self,
        team_id: &str,
        assistant_id: &str,
    ) -> Result<(team_install::bindings::Binding, AssistantSpec), Problem> {
        let binding = self
            .registry
            .binding(team_id, assistant_id)
            .await?
            .ok_or(NOT_INSTALLED)?;
        let spec = Registry::spec(&binding)?;
        Ok((binding, spec))
    }

    /// Every installed Assistant of the Space by name only.
    ///
    /// # Errors
    /// `internal-error` when the registry cannot be read.
    pub async fn list_registry(&self) -> Result<Value, Problem> {
        // Each Assistant once, as its newest binding names it: by version, then binding digest.
        let mut catalog: Vec<CatalogEntry> = Vec::new();
        for binding in self.registry.snapshot().await? {
            let spec = Registry::spec(&binding)?;
            let order = (
                version_triple(&spec.version).ok_or(INTERNAL)?,
                binding.binding_digest.clone(),
            );
            let entry = CatalogEntry {
                id: spec.assistant_id.clone(),
                order,
                name: spec.name.clone(),
            };
            match catalog.iter_mut().find(|item| item.id == entry.id) {
                Some(current) if current.order < entry.order => *current = entry,
                Some(_) => {}
                None => catalog.push(entry),
            }
        }
        catalog.sort_by(|left, right| left.id.cmp(&right.id));
        let assistants = catalog
            .into_iter()
            .map(|entry| object(vec![("id", text(&entry.id)), ("title", text(&entry.name))]))
            .collect();
        Ok(object(vec![("assistants", Value::Array(assistants))]))
    }

    /// One Team's Assistants as its own bindings describe them.
    ///
    /// # Errors
    /// `team-not-found` and the registry's problems.
    pub async fn team_registry(&self, team_id: &str) -> Result<Value, Problem> {
        let _stripe = self.coordination.locks.team(team_id).await;
        self.network(team_id).await?;
        let team = team_id.to_owned();
        let bindings = self
            .registry
            .store(move |store| store.list(&team))
            .await?
            .map_err(|_| INTERNAL)?;
        let assistants = bindings
            .iter()
            .map(|binding| {
                let spec = Registry::spec(binding)?;
                // The canonical machine contract lists its Actions sorted by id.
                let actions: Vec<Value> = spec
                    .runtime
                    .actions
                    .iter()
                    .map(|action| text(&action.id))
                    .collect();
                Ok(object(vec![
                    ("id", text(&spec.assistant_id)),
                    ("title", text(&spec.name)),
                    ("summary", text(&spec.summary)),
                    ("actions", Value::Array(actions)),
                ]))
            })
            .collect::<Result<Vec<_>, Problem>>()?;
        Ok(object(vec![("assistants", Value::Array(assistants))]))
    }

    /// Every bound Assistant container of the Team with its status: its Docker state, `outdated` for an older
    /// artifact, or `invalid` for egress or manifest drift.
    ///
    /// # Errors
    /// `team-not-found`, Docker's, and every isolation refusal.
    pub async fn list_assistants(&self, team_id: &str) -> Result<Value, Problem> {
        let _stripe = self.coordination.locks.team(team_id).await;
        let network = self.network(team_id).await?;
        let mut output = Vec::new();
        for (inspection, container_spec) in self
            .containers
            .bound(team_id, self.registry.as_ref())
            .await?
        {
            let (binding, spec) = self
                .installed(team_id, &container_spec.assistant_id)
                .await?;
            let status = self
                .status(team_id, &network.name, &inspection, &container_spec)
                .await?;
            let provenance = binding.provenance.name();
            output.push(object(vec![
                ("assistant", text(&spec.assistant_id)),
                ("assistant_version", text(&spec.version)),
                ("status", text(&status)),
                ("provenance", text(provenance)),
            ]));
        }
        Ok(object(vec![("assistants", Value::Array(output))]))
    }

    /// One bound container's status: its Docker state, `outdated`, or `invalid`.
    async fn status(
        &self,
        team_id: &str,
        network: &str,
        inspection: &bollard::models::ContainerInspectResponse,
        container_spec: &crate::assistant::ContainerSpec,
    ) -> Result<String, Problem> {
        let admitted =
            self.containers
                .admit_profile(inspection, team_id, network, container_spec)?;
        match self
            .containers
            .admit_egress(&admitted, team_id, container_spec)
        {
            Err(problem) if problem == EGRESS_POLICY_DRIFT => return Ok("invalid".to_owned()),
            Err(problem) => return Err(problem),
            Ok(()) if admitted.image != container_spec.image => return Ok("outdated".to_owned()),
            Ok(()) => {}
        }
        let admission = crate::assistant::containers::Admission::manifest(
            self.admission.as_ref(),
            team_id,
            &admitted.id,
            container_spec,
        );
        match admission.await {
            Err(problem) if problem == MANIFEST_INVALID => Ok("invalid".to_owned()),
            Err(problem) => Err(problem),
            Ok(()) => Ok(inspection
                .state
                .as_ref()
                .and_then(|state| state.status)
                .map_or_else(String::new, |status| status.to_string())),
        }
    }

    /// The verified icon bound to one installed Assistant.
    ///
    /// # Errors
    /// `assistant-not-installed`, `assistant-icon-unavailable`.
    pub async fn icon(&self, team_id: &str, assistant_id: &str) -> Result<Vec<u8>, Problem> {
        let _stripe = self.coordination.locks.team(team_id).await;
        let (binding, _) = self.installed(team_id, assistant_id).await?;
        let identity = IconIdentity::binding(&binding).map_err(|_| ICON_READ_UNAVAILABLE)?;
        self.registry
            .icons()
            .read(&identity)
            .map_err(|_| ICON_READ_UNAVAILABLE)
    }

    /// The pack of a non-English page: read from the binding's running container and verified against its digest.
    async fn pack(
        &self,
        team_id: &str,
        spec: &AssistantSpec,
        locale: &str,
    ) -> Result<Option<team_assistant::language::LanguagePack>, Problem> {
        if locale == team_assistant::language::ENGLISH {
            return Ok(None);
        }
        let container = self
            .containers
            .find(team_id, &spec.assistant_id)
            .await?
            .ok_or(crate::assistant::problem::NOT_FOUND)?;
        let id = container.id.ok_or(DOCKER_UNAVAILABLE)?;
        self.admission.pack(&id, spec).await.map(Some)
    }

    /// One installed Assistant's summary in one interface language.
    ///
    /// # Errors
    /// `assistant-not-installed`, and the pack's problems.
    pub async fn summary(
        &self,
        team_id: &str,
        assistant_id: &str,
        locale: &str,
    ) -> Result<Value, Problem> {
        let _stripe = self.coordination.locks.team(team_id).await;
        let (_, spec) = self.installed(team_id, assistant_id).await?;
        let summary = match self.pack(team_id, &spec, locale).await? {
            None => spec.summary.clone(),
            Some(pack) => pack
                .template(
                    &assistant_protocol::catalog::message_id(&spec.summary),
                    locale,
                )
                .ok_or(MANIFEST_INVALID)?,
        };
        Ok(object(vec![
            ("locale", text(locale)),
            ("summary", text(&summary)),
        ]))
    }

    /// One installed Assistant's page in one interface language, from its exact current binding.
    ///
    /// # Errors
    /// `assistant-not-installed`, `assistant-manifest-invalid`, and the pack's problems.
    pub async fn details(
        &self,
        team_id: &str,
        assistant_id: &str,
        locale: &str,
    ) -> Result<Value, Problem> {
        let _stripe = self.coordination.locks.team(team_id).await;
        let (binding, spec) = self.installed(team_id, assistant_id).await?;
        let pack = self.pack(team_id, &spec, locale).await?;
        let creators_key = if binding.provenance == Provenance::Local {
            "declared_creators"
        } else {
            "creators"
        };
        let creators = match binding.document.get(creators_key) {
            Some(Value::Array(items)) => items
                .iter()
                .filter_map(|item| match item {
                    Value::String(creator) => Some(creator.clone()),
                    _ => None,
                })
                .collect(),
            _ => Vec::new(),
        };
        let page = AssistantPage {
            assistant_id: spec.assistant_id.clone(),
            version: spec.version.clone(),
            name: spec.name.clone(),
            creators,
            summary: spec.summary.clone(),
            description: spec.runtime.presentation.description.clone(),
            links: binding
                .document
                .get("links")
                .cloned()
                .unwrap_or(Value::Null),
            machine_contract: spec.runtime.machine_contract.clone(),
            integrations: spec
                .runtime
                .integrations
                .iter()
                .map(|item| (item.id.clone(), item.provider.clone()))
                .collect(),
            stored_inputs: spec
                .runtime
                .stored_inputs
                .iter()
                .map(snapshots::copy)
                .collect(),
        };
        page.localized(locale, pack.as_ref())
            .map_err(|_| MANIFEST_INVALID)
    }

    /// Runs one Action outside any turn; an Action that asks a person is refused, since no chat can answer it.
    ///
    /// # Errors
    /// The executor's failure as its problem.
    pub async fn invoke(
        &self,
        team_id: &str,
        assistant_id: &str,
        action: &str,
        input: Value,
    ) -> Result<Value, Problem> {
        let request = ActionRequest {
            interrupt_id: String::new(),
            assistant_id: assistant_id.to_owned(),
            action: action.to_owned(),
            input,
            // The direct route belongs to no turn, so it selected no file (ADR-0093).
            files: Vec::new(),
        };
        let result = self
            .executor
            .invoke(team_id, &request, &Stop::new())
            .await
            .map_err(action_problem)?;
        Ok(object(vec![
            ("assistant", text(assistant_id)),
            ("action", text(action)),
            ("result", result),
        ]))
    }

    async fn answer(&self, call: &Call<'_>) -> Result<Answer, Problem> {
        match call.operation() {
            Operation::LocalAssistantList
            | Operation::LocalAssistantIcon
            | Operation::LocalAssistantSummary
            | Operation::LocalAssistantDetails
            | Operation::LocalAssistantInstall
            | Operation::LocalAssistantFreshInstall => self.answer_local(call).await,
            Operation::AssistantInstall => {
                let team = team_id(call)?;
                let fields =
                    string_body(&call.body, &["assistant_id", "source_digest"], INSTALL_BODY)?;
                self.install_publication(team, fields[0], fields[1], None)
                    .await
                    .map(json)
            }
            Operation::AssistantInvoke => {
                let (team, assistant) = (team_id(call)?, assistant_id(call)?);
                let action = call.route.param("action_id").ok_or(ACTION_NOT_DECLARED)?;
                let Body::Json(input @ Value::Object(_)) = &call.body else {
                    return Err(ACTION_INPUT_INVALID);
                };
                self.invoke(team, assistant, action, input.clone())
                    .await
                    .map(json)
            }
            _ => self.answer_inventory(call).await,
        }
    }

    async fn answer_inventory(&self, call: &Call<'_>) -> Result<Answer, Problem> {
        match call.operation() {
            Operation::RegistryList => self.list_registry().await.map(json),
            Operation::AssistantList => self.list_assistants(team_id(call)?).await.map(json),
            Operation::AssistantRegistry => self.team_registry(team_id(call)?).await.map(json),
            Operation::AssistantIcon => self
                .icon(team_id(call)?, assistant_id(call)?)
                .await
                .map(png),
            Operation::AssistantSummary => self
                .summary(team_id(call)?, assistant_id(call)?, locale(call)?)
                .await
                .map(json),
            Operation::AssistantDetails => self
                .details(team_id(call)?, assistant_id(call)?, locale(call)?)
                .await
                .map(json),
            Operation::AssistantUninstall => self
                .uninstall(team_id(call)?, assistant_id(call)?)
                .await
                .map(json),
            _ => Err(INTERNAL),
        }
    }

    async fn answer_local(&self, call: &Call<'_>) -> Result<Answer, Problem> {
        match call.operation() {
            Operation::LocalAssistantList => {
                let actor = crate::browser::audit::request_actor(&call.principal, &call.trace_id);
                self.local.list(&actor).await.map(json)
            }
            Operation::LocalAssistantIcon => self.local.icon(&image_id(call)?).await.map(png),
            Operation::LocalAssistantSummary => self
                .local
                .summary(&image_id(call)?, locale(call)?)
                .await
                .map(json),
            Operation::LocalAssistantDetails => self
                .local
                .details(&image_id(call)?, locale(call)?)
                .await
                .map(json),
            Operation::LocalAssistantInstall | Operation::LocalAssistantFreshInstall => {
                let team = team_id(call)?;
                let fields = string_body(&call.body, &["image_id"], LOCAL_INSTALL_BODY)?;
                let fresh = call.operation() == Operation::LocalAssistantFreshInstall;
                self.install_local(team, fields[0], fresh).await.map(json)
            }
            _ => Err(INTERNAL),
        }
    }
}

fn image_id(call: &Call<'_>) -> Result<String, Problem> {
    let hash = call
        .route
        .param("image_hash")
        .ok_or(problem::PREVIEW_INVALID)?;
    Ok(format!("sha256:{hash}"))
}

/// An Action failure of the direct route as its public problem.
#[must_use]
pub const fn action_problem(failure: ActionFailure) -> Problem {
    match failure {
        ActionFailure::NotDeclared => ACTION_NOT_DECLARED,
        ActionFailure::InvalidInput => ACTION_INPUT_INVALID,
        ActionFailure::ContextChanged => CONTEXT_CHANGED,
        ActionFailure::Blocked => ACTION_BLOCKED,
        ActionFailure::HumanRequestRequired => HUMAN_REQUEST_REQUIRED,
        ActionFailure::Stopped => ACTION_STOPPED,
        ActionFailure::Uncertain => ACTION_UNCERTAIN,
        ActionFailure::Failed => ACTION_FAILED,
        ActionFailure::StateUnavailable => ACTION_STATE_UNAVAILABLE,
        ActionFailure::DockerUnavailable => DOCKER_UNAVAILABLE,
        ActionFailure::IntegrationUnavailable => INTEGRATION_UNAVAILABLE,
        ActionFailure::FileUnavailable => ACTION_FILE_UNAVAILABLE,
        ActionFailure::FileBusy => ACTION_FILE_BUSY,
    }
}

impl Service for Assistants {
    fn call<'a>(&'a self, call: Call<'a>) -> BoxFuture<'a, Result<Answer, Problem>> {
        Box::pin(async move { self.answer(&call).await })
    }
}

#[cfg(all(test, feature = "testing"))]
mod tests;
