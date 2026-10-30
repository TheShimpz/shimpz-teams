//! Team's [`ActionExecutor`]: one Action attempt from its reviewed binding to its projected outcome.
//!
//! An attempt resolves exactly the private values the Action declares (Integration tokens refreshed through the
//! broker, held Stored Inputs), sends the workload only their ids, answers its provider calls through the provider
//! broker with those values, and projects the terminal frame. The direct route runs one fresh logical operation; a
//! batch journals every operation first ([`batch`]).

use std::collections::HashSet;
use std::sync::{Arc, Mutex};

use strict_json::Value;
use team_assistant::spec::{ActionSpec, Direction, RuntimeContract};
use team_core::audit::{AuditEvent, AuditSink};
use team_core::entropy::Entropy;
use team_core::exec::ExecEngine;
use team_core::future::BoxFuture;
use team_core::secret::Secret;
use team_core::stop::Stop;
use team_integrations::flow::{self, ActionView, AssistantView};
use team_integrations::providers;
use team_integrations::service::OAuthService;
use team_integrations::store::{Declaration, IntegrationStore};
use tokio::time::Instant;

use crate::executor::{
    ActionBatch, ActionExecutor, ActionFailure, ActionFile, ActionRequest, BatchScope,
};
use crate::files::{self, Admitted, FileSlot, FileSource};
use crate::human::{ActionTranscript, HumanRequest};
use crate::journal::Journal;
use crate::projection::{self, Projected, Refused, ResultPolicy};
use crate::provider::{
    self, Attempt, Broker, CallScope, Capacity, EgressRoute, Placement, Refusal, Transport,
};
use crate::rpc::{self, DockerSlots, RpcCall, RpcError};
use crate::stored_input::{StoredInputError, StoredInputStore};

mod batch;

/// One installed Assistant's running binding, as the controller resolves it for a Team.
#[derive(Clone, Debug)]
pub struct Binding {
    /// The reviewed runtime contract.
    pub contract: Arc<RuntimeContract>,
    /// The Assistant's id.
    pub assistant_id: String,
    /// Its public name.
    pub name: String,
    /// Its version.
    pub version: String,
    /// Its public summary.
    pub summary: String,
    /// The workload container's full id.
    pub container: String,
    /// The workload's image id.
    pub image: String,
}

/// The Assistant's egress policy could not be read.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EgressUnavailable;

/// What the executor needs from the controller: running bindings and egress policies.
pub trait Workloads: Send + Sync {
    /// The running binding of `assistant` in `team`.
    ///
    /// # Errors
    /// [`ActionFailure::ContextChanged`] when it is not installed or not running; [`ActionFailure::DockerUnavailable`]
    /// when Docker cannot answer.
    fn binding<'a>(
        &'a self,
        team: &'a str,
        assistant: &'a str,
    ) -> BoxFuture<'a, Result<Binding, ActionFailure>>;

    /// The Assistant's egress policy token and reviewed hosts, or `None` when it has no policy.
    ///
    /// # Errors
    /// [`EgressUnavailable`] when the policy cannot be read.
    fn egress(
        &self,
        team: &str,
        assistant: &str,
    ) -> Result<Option<(Secret, Vec<String>)>, EgressUnavailable>;
}

/// The executor's collaborators.
pub struct Runner {
    /// The Action journal.
    pub journal: Arc<Journal>,
    /// Stored Inputs.
    pub stored_inputs: Arc<StoredInputStore>,
    /// Integration credentials.
    pub integrations: Arc<IntegrationStore>,
    /// The OAuth broker service (refresh).
    pub oauth: Arc<OAuthService>,
    /// Workload exec.
    pub engine: Arc<dyn ExecEngine>,
    /// The Team-wide Docker call slots.
    pub slots: Arc<DockerSlots>,
    /// The Team-wide provider-call capacity.
    pub capacity: Capacity,
    /// The provider-call transport.
    pub transport: Arc<Transport>,
    /// The audit sink.
    pub audit: Arc<dyn AuditSink>,
    /// Running bindings and egress policies.
    pub workloads: Arc<dyn Workloads>,
    /// The random source of operation ids.
    pub entropy: Arc<dyn Entropy>,
    /// Workloads that could not be proven stopped; nothing more runs in them.
    pub blocked: Mutex<HashSet<String>>,
    /// The memory-only replay transcripts of paused Actions, by Team and interrupt.
    pub transcripts: Mutex<Vec<(String, ActionTranscript)>>,
    /// Team storage, which holds the bytes of the files turns select.
    pub files: Arc<dyn FileSource>,
    /// The one file-RPC slot every delivery of file bytes holds.
    pub file_slot: FileSlot,
}

/// One attempt's file: the selected file its Action names, and the slot a delivery of its bytes holds.
#[derive(Debug, Default)]
pub(crate) struct Delivery<'a> {
    /// The selected file, or `None` for an ordinary Action.
    pub file: Option<&'a ActionFile>,
    /// The held slot, only for an invocation that delivers the bytes.
    pub admitted: Option<Admitted<'a>>,
}

impl std::fmt::Debug for Runner {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.debug_struct("Runner").finish_non_exhaustive()
    }
}

/// How one attempt ended.
#[derive(Clone, Debug, PartialEq)]
pub(crate) enum Outcome {
    /// A validated result.
    Completed(Value),
    /// A human request; the operation returns to `prepared`.
    Request(HumanRequest),
}

/// How one attempt failed, with what the journal may conclude.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Failed {
    /// Nothing was dispatched.
    NotDispatched(ActionFailure),
    /// A handled failure the Action answered after it ran.
    Handled,
    /// It may have had an effect.
    Uncertain(ActionFailure),
}

/// The egress route of one Assistant, read through the controller.
struct Route {
    workloads: Arc<dyn Workloads>,
    team: String,
    assistant: String,
}

impl EgressRoute for Route {
    fn route(&self) -> Result<Option<(Secret, Vec<String>)>, Refusal> {
        self.workloads
            .egress(&self.team, &self.assistant)
            .map_err(|EgressUnavailable| Refusal::new("unavailable", "egress-policy"))
    }
}

/// The Integration flow's view of a binding.
pub(crate) fn assistant_view(binding: &Binding) -> AssistantView {
    AssistantView {
        assistant_id: binding.assistant_id.clone(),
        version: binding.version.clone(),
        name: binding.name.clone(),
        summary: binding.summary.clone(),
        actions: binding
            .contract
            .actions
            .iter()
            .map(|action| ActionView {
                id: action.id.clone(),
                summary: action.summary.clone(),
                integrations: action.integrations.clone(),
            })
            .collect(),
        integrations: binding
            .contract
            .integrations
            .iter()
            .map(|integration| {
                let declaration = Declaration {
                    provider: integration.provider.clone(),
                    scopes: integration.scopes.clone(),
                };
                (integration.id.clone(), declaration)
            })
            .collect(),
    }
}

/// The private values of one attempt.
pub(crate) struct Private {
    /// Integration access tokens by id.
    pub integrations: Vec<(String, Secret)>,
    /// Held Stored Input values by id.
    pub stored_inputs: Vec<(String, Secret)>,
}

impl Private {
    /// The ids of the held Stored Inputs.
    fn held(&self) -> Vec<String> {
        self.stored_inputs
            .iter()
            .map(|(id, _)| id.clone())
            .collect()
    }

    /// Every value Team injects, by `integration:` or `stored-input:` id.
    fn injected(&self) -> Vec<(String, Secret)> {
        let integrations = self
            .integrations
            .iter()
            .map(|(id, token)| (format!("integration:{id}"), token.clone()));
        let stored = self
            .stored_inputs
            .iter()
            .map(|(id, value)| (format!("stored-input:{id}"), value.clone()));
        integrations.chain(stored).collect()
    }
}

/// One attempt's fixed context: the Team, binding, Action, and logical operation.
#[derive(Clone, Copy)]
pub(crate) struct Target<'a> {
    /// The Team.
    pub team: &'a str,
    /// The running binding.
    pub binding: &'a Binding,
    /// The reviewed Action.
    pub action: &'a ActionSpec,
    /// The logical operation id.
    pub operation_id: &'a str,
}

/// The Stored Input placements an Action declares, from its binding.
fn placements(binding: &Binding, action: &ActionSpec) -> Vec<(String, Placement)> {
    let declared = |id: &String| {
        binding
            .contract
            .stored_inputs
            .iter()
            .find(|declared| declared.id == *id)
    };
    action
        .stored_inputs
        .iter()
        .filter_map(declared)
        .map(|declared| {
            let placement = Placement {
                host: declared.host.clone(),
                header: declared.header.clone(),
                query: declared.query.clone(),
                scheme: declared.scheme.clone(),
                hmac: declared.hmac.clone(),
                routes: declared.routes.clone(),
            };
            (declared.id.clone(), placement)
        })
        .collect()
}

/// The Integrations an Action declares with their reviewed providers and held tokens.
fn integration_tokens(
    binding: &Binding,
    action: &ActionSpec,
    private: &Private,
) -> Vec<(String, &'static providers::Provider, Option<Secret>)> {
    action
        .integrations
        .iter()
        .filter_map(|id| {
            let declared = binding
                .contract
                .integrations
                .iter()
                .find(|declared| declared.id == *id)?;
            let reviewed = providers::resolve(&declared.provider).ok()?;
            let token = private
                .integrations
                .iter()
                .find(|(held, _)| held == id)
                .map(|(_, token)| token.clone());
            Some((id.clone(), reviewed, token))
        })
        .collect()
}

impl Runner {
    /// Keeps the replay transcript a person's answer extended, for the batch that replays its Action.
    pub fn set_transcript(&self, team: &str, transcript: ActionTranscript) {
        if let Ok(mut transcripts) = self.transcripts.lock() {
            transcripts.retain(|(owner, kept)| {
                !(owner == team && kept.interrupt_id == transcript.interrupt_id)
            });
            transcripts.push((team.to_owned(), transcript));
        }
    }

    /// Drops a Team's transcripts of `interrupts` (completed), or all of them (`None`, the Team's turn ended).
    pub fn clear_transcripts(&self, team: &str, interrupts: Option<&[String]>) {
        if let Ok(mut transcripts) = self.transcripts.lock() {
            transcripts.retain(|(owner, kept)| {
                owner != team
                    || interrupts.is_some_and(|completed| !completed.contains(&kept.interrupt_id))
            });
        }
    }

    /// The transcript of one paused interrupt, empty when none was kept.
    #[must_use]
    pub fn transcript(&self, team: &str, interrupt_id: &str) -> ActionTranscript {
        self.transcripts
            .lock()
            .ok()
            .and_then(|transcripts| {
                transcripts
                    .iter()
                    .find(|(owner, kept)| owner == team && kept.interrupt_id == interrupt_id)
                    .map(|(_, kept)| kept.clone())
            })
            .unwrap_or_else(|| ActionTranscript::new(interrupt_id))
    }

    async fn record(
        &self,
        target: &Target<'_>,
        result: &str,
        detail: &str,
    ) -> Result<(), ActionFailure> {
        let detail = format!("{detail}:{}", target.action.id);
        self.record_detail(target, result, &detail).await
    }

    /// Records a delivered file by its opaque id and size only, never its name or content (ADR-0093).
    async fn record_file(
        &self,
        target: &Target<'_>,
        result: &str,
        kind: &str,
        sent: &(String, i64),
    ) {
        let detail = format!("{kind}:{}:{}:{}", target.action.id, sent.0, sent.1);
        let _ = self.record_detail(target, result, &detail).await;
    }

    async fn record_detail(
        &self,
        target: &Target<'_>,
        result: &str,
        detail: &str,
    ) -> Result<(), ActionFailure> {
        let text = |value: &str| Value::String(value.to_owned());
        let event = AuditEvent::new()
            .with("operation", text("assistant-action"))
            .with("result", text(result))
            .with("team_id", text(target.team))
            .with("assistant", text(&target.binding.assistant_id))
            .with("detail", text(detail));
        self.audit
            .record(event)
            .await
            .map_err(|_| ActionFailure::StateUnavailable)
    }

    /// Exactly the private values the Action declares.
    pub(crate) async fn private(&self, target: &Target<'_>) -> Result<Private, ActionFailure> {
        let (team, binding, action) = (target.team, target.binding, target.action);
        let integrations = if action.integrations.is_empty() {
            Vec::new()
        } else {
            flow::resolve_action_integrations(
                team,
                &assistant_view(binding),
                &action.id,
                &self.integrations,
                &self.oauth,
            )
            .await
            .map_err(|_| ActionFailure::IntegrationUnavailable)?
        };
        let mut stored_inputs = Vec::new();
        for id in &action.stored_inputs {
            match self
                .stored_inputs
                .resolve((team, &binding.assistant_id, id), "password")
            {
                Ok(value) => stored_inputs.push((id.clone(), value.value)),
                Err(StoredInputError::Missing) => {}
                Err(_) => return Err(ActionFailure::StateUnavailable),
            }
        }
        Ok(Private {
            integrations,
            stored_inputs,
        })
    }

    fn broker(
        &self,
        target: &Target<'_>,
        private: &Private,
        transcript: &ActionTranscript,
        stop: &Stop,
    ) -> Broker {
        let (binding, action) = (target.binding, target.action);
        let integrations = integration_tokens(binding, action, private);
        let (credentials, missing) = provider::action_credentials(
            &placements(binding, action),
            &private.stored_inputs,
            &integrations,
        );
        let declares_authorization = action
            .human_requests
            .iter()
            .any(|kind| crate::human::authorizes(kind));
        Broker::new(CallScope {
            attempt: Attempt {
                team_id: target.team.to_owned(),
                assistant_id: binding.assistant_id.clone(),
                action_id: action.id.clone(),
                operation_id: target.operation_id.to_owned(),
            },
            credentials,
            missing,
            authorized: !declares_authorization || transcript.authorized(&action.human_requests),
            route: Box::new(Route {
                workloads: Arc::clone(&self.workloads),
                team: target.team.to_owned(),
                assistant: binding.assistant_id.clone(),
            }),
            audit: Arc::clone(&self.audit),
            stop: stop.clone(),
            capacity: self.capacity.clone(),
            transport: Arc::clone(&self.transport),
        })
    }

    /// Whether `container` could not be proven stopped after a failure (a poisoned lock counts as blocked).
    fn is_blocked(&self, container: &str) -> bool {
        self.blocked
            .lock()
            .map_or(true, |blocked| blocked.contains(container))
    }

    /// The invocation's `files`: the selected file, with its bytes only behind the Action's authorization.
    async fn invocation_files(
        &self,
        target: &Target<'_>,
        input: &Value,
        transcript: &ActionTranscript,
        delivery: &Delivery<'_>,
    ) -> Result<Value, ActionFailure> {
        let granted = match (delivery.file, &delivery.admitted) {
            (Some(file), Some(_)) => {
                let source = Arc::clone(&self.files);
                let (team, id) = (target.team.to_owned(), file.id.clone());
                let read = tokio::task::spawn_blocking(move || source.read(&team, &id)).await;
                let read = read.map_err(|_| ActionFailure::FileUnavailable)?;
                Some(read.map_err(|_| ActionFailure::FileUnavailable)?)
            }
            _ => None,
        };
        files::deliver(
            target.action,
            delivery.file,
            transcript,
            input,
            granted.as_ref(),
        )
        .map_err(|_| ActionFailure::FileUnavailable)
    }

    /// Runs one attempt of `target`'s Action with `input`; a delivery's slot is released when it ends.
    pub(crate) async fn attempt(
        &self,
        target: &Target<'_>,
        input: &Value,
        transcript: &ActionTranscript,
        delivery: Delivery<'_>,
        stop: &Stop,
    ) -> Result<Outcome, Failed> {
        let binding = target.binding;
        if self.is_blocked(&binding.container) {
            return Err(Failed::NotDispatched(ActionFailure::Blocked));
        }
        let private = self.private(target).await.map_err(Failed::NotDispatched)?;
        let names = private.held();
        let files = self
            .invocation_files(target, input, transcript, &delivery)
            .await
            .map_err(Failed::NotDispatched)?;
        let encoded = rpc::encode_invocation(
            input,
            &names,
            &files,
            target.operation_id,
            &transcript.payloads(),
        )
        .map_err(|_| Failed::NotDispatched(ActionFailure::InvalidInput))?;
        let mut broker = self.broker(target, &private, transcript, stop);
        self.record(target, "ok", "started")
            .await
            .map_err(Failed::NotDispatched)?;
        let deadline = delivery.admitted.as_ref().map_or_else(
            || Instant::now() + rpc::RPC_TIMEOUT,
            |admitted| admitted.deadline,
        );
        let call = RpcCall {
            container: &binding.container,
            action: &target.action.id,
            encoded: &encoded,
            deadline,
            stop,
            broker: Some(&mut broker),
        };
        let exchanged = rpc::exchange(self.engine.as_ref(), &self.slots, call).await;
        let withheld =
            delivery.file.is_some() && !transcript.authorized(&target.action.human_requests);
        self.exchanged(
            target,
            exchanged,
            files::delivered(&files).as_ref(),
            (&private, transcript, withheld),
            stop,
        )
        .await
    }

    async fn exchanged(
        &self,
        target: &Target<'_>,
        exchanged: Result<Value, RpcError>,
        sent: Option<&(String, i64)>,
        context: (&Private, &ActionTranscript, bool),
        stop: &Stop,
    ) -> Result<Outcome, Failed> {
        match exchanged {
            Ok(raw) => {
                if let Some(sent) = sent {
                    self.record_file(target, "ok", "file-delivered", sent).await;
                }
                self.projected(target, &raw, context).await
            }
            Err(error) => {
                let failed = self.rpc_failed(target, error, stop).await;
                if let Some(sent) = sent {
                    // The exchange failed, so whether the workload received the bytes is unknown.
                    self.record_file(target, "error", "file-delivery-unconfirmed", sent)
                        .await;
                }
                Err(failed)
            }
        }
    }

    async fn rpc_failed(&self, target: &Target<'_>, error: RpcError, stop: &Stop) -> Failed {
        let _ = self.record(target, "error", "failed").await;
        match error {
            RpcError::NotDispatched if stop.is_stopped() => {
                Failed::NotDispatched(ActionFailure::Stopped)
            }
            RpcError::NotDispatched => Failed::NotDispatched(ActionFailure::DockerUnavailable),
            RpcError::Blocked => {
                if let Ok(mut blocked) = self.blocked.lock() {
                    blocked.insert(target.binding.container.clone());
                }
                Failed::Uncertain(ActionFailure::Blocked)
            }
            RpcError::Failed { .. } => Failed::Uncertain(ActionFailure::Uncertain),
        }
    }

    async fn projected(
        &self,
        target: &Target<'_>,
        raw: &Value,
        (private, transcript, file_withheld): (&Private, &ActionTranscript, bool),
    ) -> Result<Outcome, Failed> {
        let action = target.action;
        let injected = private.injected();
        let held = private.held();
        let empty = Value::Array(Vec::new());
        let messages = target
            .binding
            .contract
            .machine_contract
            .get("messages")
            .unwrap_or(&empty);
        let policy = ResultPolicy {
            human_requests: &action.human_requests,
            authorization_requested: transcript
                .responses
                .iter()
                .any(|response| crate::human::authorizes(&response.kind)),
            injected: &injected,
            held_stored_inputs: &held,
            declared_stored_inputs: &action.stored_inputs,
            messages,
            file_withheld,
        };
        let projected = projection::project(raw, &policy, |result| {
            action.validate(Direction::Output, result).is_ok()
        });
        self.settle_projection(target, projected).await
    }

    async fn settle_projection(
        &self,
        target: &Target<'_>,
        projected: Result<Projected, Refused>,
    ) -> Result<Outcome, Failed> {
        match projected {
            Ok(Projected::Result(result)) => {
                let _ = self.record(target, "ok", "completed").await;
                Ok(Outcome::Completed(result))
            }
            Ok(Projected::Request(request)) => Ok(Outcome::Request(request)),
            Err(Refused::StoredInputRejected(id)) => {
                let _ = self.record(target, "error", "stored-input-rejected").await;
                let _ = self
                    .stored_inputs
                    .delete((target.team, &target.binding.assistant_id, &id));
                Err(Failed::Uncertain(ActionFailure::Failed))
            }
            Err(Refused::Failure(_)) => {
                let _ = self.record(target, "error", "action-failed").await;
                Err(Failed::Handled)
            }
            Err(Refused::SecretExposure | Refused::Invalid) => {
                let _ = self.record(target, "error", "invalid-output").await;
                Err(Failed::Uncertain(ActionFailure::Uncertain))
            }
        }
    }

    /// The binding and declared Action of `request`, its input validated.
    pub(crate) async fn admitted(
        &self,
        team: &str,
        request: &ActionRequest,
    ) -> Result<(Binding, ActionSpec), ActionFailure> {
        let binding = self.workloads.binding(team, &request.assistant_id).await?;
        let action = binding
            .contract
            .action(&request.action)
            .cloned()
            .ok_or(ActionFailure::NotDeclared)?;
        action
            .validate(Direction::Input, &request.input)
            .map_err(|_| ActionFailure::InvalidInput)?;
        Ok((binding, action))
    }
}

/// Wraps a [`Runner`] as the frozen seam.
#[derive(Clone, Debug)]
pub struct Executor(pub Arc<Runner>);

impl ActionExecutor for Executor {
    fn batch(&self, scope: BatchScope) -> Box<dyn ActionBatch> {
        Box::new(batch::Batch::new(Arc::clone(&self.0), scope))
    }

    fn invoke<'a>(
        &'a self,
        team_id: &'a str,
        request: &'a ActionRequest,
        stop: &'a Stop,
    ) -> BoxFuture<'a, Result<Value, ActionFailure>> {
        Box::pin(async move {
            let runner = &self.0;
            let (binding, action) = runner.admitted(team_id, request).await?;
            let operation_id = crate::journal::new_operation_id(runner.entropy.as_ref())
                .map_err(|_| ActionFailure::StateUnavailable)?;
            let target = Target {
                team: team_id,
                binding: &binding,
                action: &action,
                operation_id: &operation_id,
            };
            let transcript = ActionTranscript::new(&request.interrupt_id);
            // The direct route has no replay, so it never delivers bytes and holds no slot.
            let file = files::action_file(&action.input_files, &request.input, &request.files)
                .map_err(|_| ActionFailure::FileUnavailable)?;
            let delivery = Delivery {
                file,
                admitted: None,
            };
            match runner
                .attempt(&target, &request.input, &transcript, delivery, stop)
                .await
            {
                Ok(Outcome::Completed(result)) => Ok(result),
                Ok(Outcome::Request(_)) => Err(ActionFailure::HumanRequestRequired),
                Err(Failed::NotDispatched(failure) | Failed::Uncertain(failure)) => Err(failure),
                Err(Failed::Handled) => Err(ActionFailure::Failed),
            }
        })
    }
}

#[cfg(test)]
mod tests;
