//! One segment of a Routine run or of its verification on the chat turn engine (`local/chat/segment.py`'s Routine
//! path): in the run's own journal generation and thread, against exactly the Assistants the run names, with no model,
//! files, knowledge, or Browser.

use std::sync::{Arc, Mutex, PoisonError};

use strict_json::Value;
use team_action::challenges::{HumanRequirement, render_copy};
use team_action::executor::BatchScope;
use team_assistant::spec::Direction;
use team_chat::orchestrator::{
    ChatContinuation, ChatError, ChatHumanSuspension, Outcome, Refusal, Strategy,
    continue_after_pause, run_until_pause,
};
use team_chat::progress::Reporter;
use team_core::future::BoxFuture;
use team_core::secret::Secret;
use team_core::stop::Stop;
use team_inference::brain::ModelCredential;
use team_inference::config::InferenceConfig;
use team_inference::runtime::{ActionRequest, Runtime, RuntimeAction, RuntimeContext};
use team_integrations::flow::{Requested, requirements_for_batch};
use team_integrations::pending::IntegrationRequirement;
use team_routine::claim::network_of;

use crate::chat::answer::declared_help;
use crate::chat::pending::Identity;
use crate::chat::ports::ActiveAssistant;
use crate::chat::problem;
use crate::chat::segment::{Setup, admitted_request, paused_batch_id, routine_thread_id};
use crate::chat::service::{chat_problem, refused};
use crate::http::service::Problem;
use crate::routine::executor::DispatchHooks;
use crate::routine::service::RoutineService;

/// One segment's request.
pub struct Segment<'a> {
    /// The Team.
    pub team_id: &'a str,
    /// The run (or the held run a verification belongs to).
    pub run_id: &'a str,
    /// The journal generation it runs in, derived from the run id in the Team's current network.
    pub generation: &'a str,
    /// The Assistants it may call.
    pub assistant_ids: &'a [String],
    /// The execution's Stop.
    pub stop: &'a Stop,
    /// Its progress.
    pub progress: &'a Reporter,
    /// The interface language a frozen run's request is rendered in.
    pub locale: Option<String>,
    /// The frozen run it continues, and the Team identity it froze on.
    pub continuation: Option<(ChatContinuation, Identity)>,
    /// The responses a person gave exactly that frozen run's Actions; only this segment's batch replays them.
    pub transcripts: &'a [team_action::human::ActionTranscript],
    /// Its Action rounds: one per step of a run's plan, one for a verification.
    pub max_rounds: usize,
    /// Whether its batch is a run's own, held for recovery when an outcome is uncertain; a verification's is not.
    pub held: bool,
}

/// How a segment that reached an outcome ended.
pub struct Finished {
    /// The outcome.
    pub outcome: Outcome,
    /// The Team it ran against.
    pub identity: Identity,
    /// The Integrations a paused batch waits for.
    pub integrations: Vec<IntegrationRequirement>,
    /// The request an Action paused for, its copy rendered.
    pub human: Option<HumanRequirement>,
    /// The id of the batch a human request paused.
    pub paused_batch: Option<String>,
}

/// A segment that ended without an outcome: its stable code, and whether its batch was left uncertain.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Cut {
    /// The stable reason.
    pub code: &'static str,
    /// Whether the batch holds an outcome this segment left uncertain.
    pub held: bool,
}

fn code(problem: &Problem) -> &'static str {
    problem.code.unwrap_or("internal-error")
}

const fn cut(code: &'static str) -> Cut {
    Cut { code, held: false }
}

/// A Routine run's Team identity: its network incarnation and the exact release and container of each Assistant. It
/// never names the Team's name or model, so renaming the Team never ends a frozen run (ADR-0092).
#[must_use]
pub fn routine_identity(network_id: &str, assistants: &[ActiveAssistant]) -> Identity {
    Identity::Routine {
        network_id: network_id.to_owned(),
        assistants: assistants
            .iter()
            .map(|active| {
                (
                    active.id().to_owned(),
                    active.binding.image.clone(),
                    active.binding.container.clone(),
                )
            })
            .collect(),
    }
}

/// The controller adapters one Routine segment runs the turn engine with.
struct RunStrategy<'a> {
    service: &'a RoutineService,
    segment: &'a Segment<'a>,
    identity: Identity,
    assistants: Vec<ActiveAssistant>,
    integrations: Mutex<Vec<IntegrationRequirement>>,
}

fn invalid_input() -> ChatError {
    ChatError::Refused(Refusal {
        status: 422,
        message: "Action payload does not match its reviewed schema",
        code: "invalid-action-input",
    })
}

impl Strategy for RunStrategy<'_> {
    fn stop(&self) -> &Stop {
        self.segment.stop
    }

    fn validate_context(&self) -> BoxFuture<'_, Result<(), ChatError>> {
        Box::pin(async move {
            let segment = self.segment;
            let (_name, network, assistants) = self
                .service
                .routine_setup(segment.team_id, segment.assistant_ids)
                .await
                .map_err(|problem| refused(&problem))?;
            if routine_identity(&network, &assistants) == self.identity {
                Ok(())
            } else {
                Err(ChatError::ContextChanged)
            }
        })
    }

    fn validate_action(
        &self,
        assistant_id: &str,
        action: &RuntimeAction,
        input: &Value,
    ) -> Result<Value, ChatError> {
        let active = self
            .assistants
            .iter()
            .find(|active| active.id() == assistant_id)
            .ok_or_else(|| refused(&problem::assistant_unavailable()))?;
        let spec = active.action(&action.id).ok_or_else(invalid_input)?;
        spec.validate(Direction::Input, input)
            .map_err(|_| invalid_input())?;
        Ok(input.clone())
    }

    fn pause_before_batch<'a>(
        &'a self,
        batch: &'a [ActionRequest],
    ) -> BoxFuture<'a, Result<bool, ChatError>> {
        Box::pin(async move {
            let views: Vec<_> = self.assistants.iter().map(ActiveAssistant::view).collect();
            let requested: Vec<Requested<'_>> = batch
                .iter()
                .map(|request| Requested {
                    assistant_id: &request.assistant_id,
                    action: &request.action,
                })
                .collect();
            let integrations = &self.service.parts.chat.parts().integrations;
            let missing =
                requirements_for_batch(self.segment.team_id, &views, &requested, integrations)
                    .map_err(|_| refused(&problem::integration_contract_unavailable()))?;
            let pause = !missing.is_empty();
            *self
                .integrations
                .lock()
                .unwrap_or_else(PoisonError::into_inner) = missing;
            Ok(pause)
        })
    }

    fn progress(&self) -> &Reporter {
        self.segment.progress
    }

    fn max_rounds(&self) -> usize {
        self.segment.max_rounds
    }

    fn record_invoked(&self) -> bool {
        false
    }
}

/// What a prepared segment runs against.
struct Prepared {
    identity: Identity,
    assistants: Vec<ActiveAssistant>,
    context: RuntimeContext,
    scope: BatchScope,
}

fn context(
    thread_id: &str,
    team_name: String,
    assistants: &[ActiveAssistant],
    locale: Option<String>,
) -> RuntimeContext {
    RuntimeContext {
        thread_id: thread_id.to_owned(),
        team_name,
        assistants: assistants.iter().map(ActiveAssistant::runtime).collect(),
        // A Routine run asks no model and carries no model-provider key.
        credential: ModelCredential {
            provider: String::new(),
            model: String::new(),
            api_key: Secret::new(String::new()),
        },
        memories: None,
        routines: None,
        routine_question: None,
        routine_mode: false,
        routine_rerun: None,
        routine_schedule: None,
        routine_output: None,
        routine_settings_answer: false,
        routine_selected: None,
        locale,
        turn_clock: None,
        attachments: Vec::new(),
        browser: false,
    }
}

impl RoutineService {
    /// The Team's name, network, and exactly the running Assistants a Routine run names; one not running refuses.
    ///
    /// # Errors
    /// The Team read's problem, or `assistant-unavailable`.
    pub(crate) async fn routine_setup(
        &self,
        team_id: &str,
        assistant_ids: &[String],
    ) -> Result<(String, String, Vec<ActiveAssistant>), Problem> {
        let _team = self.parts.coordination.locks.team(team_id).await;
        self.routine_scope(team_id, assistant_ids).await
    }

    /// [`RoutineService::routine_setup`] for a caller that already holds the Team's stripe.
    ///
    /// # Errors
    /// The Team read's problem, or `assistant-unavailable`.
    pub(crate) async fn routine_scope(
        &self,
        team_id: &str,
        assistant_ids: &[String],
    ) -> Result<(String, String, Vec<ActiveAssistant>), Problem> {
        let scope = self.parts.team.scope(team_id, true).await?;
        let assistants = assistant_ids
            .iter()
            .map(|id| scope.assistant(id).cloned())
            .collect::<Option<Vec<_>>>()
            .ok_or(problem::assistant_unavailable())?;
        Ok((scope.team_name, scope.network_id, assistants))
    }

    async fn prepare_segment(&self, segment: &Segment<'_>) -> Result<Prepared, &'static str> {
        let (team, ids) = (segment.team_id, segment.assistant_ids);
        let (team_name, network, assistants) = self
            .routine_setup(team, ids)
            .await
            .map_err(|problem| code(&problem))?;
        let identity = routine_identity(&network, &assistants);
        let expected = segment.continuation.as_ref().map(|(_, identity)| identity);
        let foreign = network_of(segment.generation, segment.run_id).as_deref() != Some(&network);
        if foreign || expected.is_some_and(|expected| *expected != identity) {
            // Nothing may run in another network, or against Assistants other than the ones the run froze on.
            return Err("team-context-changed");
        }
        let thread_id = routine_thread_id(&self.parts.space_id, team, &network, segment.run_id)
            .map_err(|problem| code(&problem))?;
        if segment.continuation.is_none() {
            self.parts
                .journal
                .end_settled(segment.generation)
                .map_err(|_| "action-state-unavailable")?;
        }
        let context = context(&thread_id, team_name, &assistants, segment.locale.clone());
        let scope = BatchScope {
            team_id: team.to_owned(),
            generation: segment.generation.to_owned(),
            thread_id,
            routine: segment.held,
            transcripts: segment.transcripts.to_vec(),
        };
        Ok(Prepared {
            identity,
            assistants,
            context,
            scope,
        })
    }

    /// Runs or resumes one Routine segment with `runtime` answering in place of the Brain.
    ///
    /// # Errors
    /// [`Cut`] for a segment that did not complete or pause; its batch is ended unless an outcome is uncertain.
    pub(crate) async fn run_segment(
        &self,
        segment: &Segment<'_>,
        runtime: &dyn Runtime,
        hooks: Arc<dyn DispatchHooks>,
    ) -> Result<Finished, Cut> {
        let prepared = self.prepare_segment(segment).await.map_err(cut)?;
        let mut batch = self.parts.actions.batch(prepared.scope.clone(), hooks);
        let strategy = RunStrategy {
            service: self,
            segment,
            identity: prepared.identity.clone(),
            assistants: prepared.assistants.clone(),
            integrations: Mutex::new(Vec::new()),
        };
        let context = &prepared.context;
        let driven = match &segment.continuation {
            None => run_until_pause(runtime, context, "", &[], &strategy, batch.as_mut()).await,
            Some((continuation, _)) => {
                let continuation = continuation.clone();
                continue_after_pause(runtime, context, continuation, &strategy, batch.as_mut())
                    .await
            }
        };
        let outcome = match driven {
            Ok(outcome) => outcome,
            Err(error) => {
                let held = !matches!(batch.terminate().await, Ok(true));
                let problem = chat_problem(error);
                return Err(Cut {
                    code: code(&problem),
                    held,
                });
            }
        };
        let integrations = std::mem::take(
            &mut *strategy
                .integrations
                .lock()
                .unwrap_or_else(PoisonError::into_inner),
        );
        Self::finished(&prepared, segment, outcome, integrations)
    }

    fn finished(
        prepared: &Prepared,
        segment: &Segment<'_>,
        outcome: Outcome,
        integrations: Vec<IntegrationRequirement>,
    ) -> Result<Finished, Cut> {
        let (human, paused_batch) = match &outcome {
            Outcome::Human(paused) => {
                let requirement = requirement(prepared, segment, paused)
                    .map_err(|error| cut(code(&chat_problem(error))))?;
                let scope = (
                    prepared.scope.generation.clone(),
                    prepared.scope.thread_id.clone(),
                );
                let paused_batch = paused_batch_id(&scope, &paused.continuation.turn.actions);
                (Some(requirement), Some(paused_batch))
            }
            _ => (None, None),
        };
        Ok(Finished {
            outcome,
            identity: prepared.identity.clone(),
            integrations,
            human,
            paused_batch,
        })
    }
}

/// The paused request rendered in the run's language; a Routine run's card shows no argument and no purpose, since
/// its values may be protected (ADR-0101).
fn requirement(
    prepared: &Prepared,
    segment: &Segment<'_>,
    paused: &ChatHumanSuspension,
) -> Result<HumanRequirement, ChatError> {
    let setup = Setup {
        team_name: String::new(),
        network_id: String::new(),
        assistants: prepared.assistants.clone(),
        files: Vec::new(),
        config: InferenceConfig {
            provider: String::new(),
            model: String::new(),
        },
    };
    let (active, spec, request) = admitted_request(&setup, paused)?;
    let help = declared_help(&request, &active.binding.contract.stored_inputs);
    let locale = segment.locale.clone().unwrap_or_else(|| "en".to_owned());
    let help_text = help.as_ref().map(|(text, _)| text.as_str());
    let copy = render_copy(&request, &active.pack, &locale, help_text)
        .map_err(|_| ChatError::Orchestration("Action human request copy is unavailable"))?;
    Ok(HumanRequirement {
        assistant_id: active.id().to_owned(),
        assistant_name: active.binding.name.clone(),
        action_id: paused.action.action.clone(),
        action_summary: spec.summary.clone(),
        interrupt_id: paused.action.interrupt_id.clone(),
        request,
        assistant_version: active.binding.version.clone(),
        copy,
        help_url: help.as_ref().map(|(_, url)| url.clone()),
        help_text: help.map(|(text, _)| text),
        purpose: None,
        purpose_locale: None,
        file: None,
        input: None,
    })
}
