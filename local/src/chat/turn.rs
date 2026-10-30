//! A chat turn from its admission to its terminal answer (`local/chat/api.py`): the request's closed body, its slot,
//! its segment, and how the segment's outcome commits, pauses, or fails.

use std::sync::Arc;

use strict_json::Value;
use team_action::human::{ActionTranscript, retain_unfinished};
use team_chat::orchestrator::{ChatOutcome, Outcome};
use team_chat::progress::Reporter;
use team_chat::turn::terminal_body;
use team_core::secret::Secret;
use team_inference::memory::Source;
use team_inference::usage::TurnUsage;
use team_protocol::payload::{
    CHAT_BODY_FIELDS, MAX_CHAT_MESSAGE_CHARS, canonical_locale, canonical_request_identity,
};
use team_protocol::python::text;
use team_protocol::python::value::string;

use super::inference::team_id;
use super::pending::PendingLocalChat;
use super::problem;
use super::segment::{SegmentRequest, SegmentResult};
use super::service::{Admitted, ChatService, stream};
use super::usage::RequestUsage;
use crate::audit::{Principal, PrincipalClass};
use crate::http::server::current_request;
use crate::http::service::{Answer, Body, Call, Principal as Caller, Problem};
use crate::routine::proposal::{Proposed, Turn, Write};

/// What one admitted request's work runs with: its Team, caller, credential, usage, and audit principal.
#[derive(Clone)]
pub(super) struct Request {
    /// The Team.
    pub team_id: String,
    /// The provider and key the request carried.
    pub credential: (String, Secret),
    /// The verified Supervisor, when a person sent it.
    pub supervisor: Option<String>,
    /// The request's Brain usage.
    pub usage: Arc<RequestUsage>,
    /// The request's audit principal.
    pub actor: Principal,
}

impl ChatService {
    /// The request's Team, credential, caller, and usage.
    pub(super) fn request(&self, call: &Call<'_>) -> Result<Request, Problem> {
        let team = team_id(call)?.to_owned();
        let model = call
            .model
            .as_ref()
            .ok_or(problem::invalid_model_credential())?;
        let supervisor = match &call.principal {
            Caller::Supervisor { supervisor_id, .. } => Some(supervisor_id.clone()),
            _ => None,
        };
        let context = current_request();
        let actor = Principal {
            id: context
                .as_ref()
                .and_then(|context| context.principal_id.clone()),
            class: if supervisor.is_some() {
                PrincipalClass::Human
            } else {
                PrincipalClass::Machine
            },
            credential_state: context.as_ref().map(|context| context.credential_state),
            trace_id: Some(call.trace_id.clone()),
        };
        Ok(Request {
            team_id: team,
            credential: (model.provider.clone(), model.api_key.clone()),
            supervisor,
            usage: Arc::new(RequestUsage::new(
                Arc::clone(&self.parts.usage),
                &call.trace_id,
            )),
            actor,
        })
    }

    pub(super) fn now_ms(&self) -> i64 {
        let now = self.parts.clock.now();
        now.duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |elapsed| {
                i64::try_from(elapsed.as_millis()).unwrap_or(i64::MAX)
            })
    }
}

/// One admitted chat body.
pub(super) struct ChatBody {
    message: String,
    files: Vec<String>,
    assistant_ids: Vec<String>,
    conversation: Vec<team_inference::runtime::ConversationEntry>,
    locale: Option<String>,
    nonce: String,
    issued_at: i64,
    timezone: Option<String>,
    refine: Option<String>,
}

fn invalid(message: &'static str, code: &'static str) -> Problem {
    Problem::new(422, message, code)
}

/// The Assistants a turn selects: at most the Team's bound, each canonical and distinct.
pub(super) fn assistant_ids(value: Option<&Value>) -> Result<Vec<String>, Problem> {
    let refused = invalid("assistant_ids contains an invalid id", "invalid-assistants");
    let Some(Value::Array(items)) = value else {
        return Err(invalid(
            "assistant_ids must contain at most 16 ids",
            "invalid-assistants",
        ));
    };
    if items.len() > team_protocol::payload::MAX_CHAT_ASSISTANTS {
        return Err(invalid(
            "assistant_ids must contain at most 16 ids",
            "invalid-assistants",
        ));
    }
    let ids = items
        .iter()
        .map(|item| team_protocol::identifiers::canonical_assistant_id(item).map(str::to_owned))
        .collect::<Option<Vec<_>>>()
        .ok_or(refused)?;
    let unique = ids
        .iter()
        .enumerate()
        .all(|(index, id)| !ids[..index].contains(id));
    if unique {
        Ok(ids)
    } else {
        Err(invalid(
            "assistant_ids must be unique",
            "invalid-assistants",
        ))
    }
}

fn chat_files(value: Option<&Value>) -> Result<Vec<String>, Problem> {
    let refused = invalid("files must contain at most 8 opaque ids", "invalid-files");
    let Some(Value::Array(items)) = value else {
        return Err(refused);
    };
    if items.len() > team_protocol::payload::MAX_CHAT_FILES {
        return Err(refused);
    }
    items
        .iter()
        .map(|item| string(item).map(str::to_owned).ok_or(refused.clone()))
        .collect()
}

fn chat_message(value: Option<&Value>) -> Result<String, Problem> {
    match value {
        Some(Value::String(message))
            if !text::strip(message).is_empty()
                && text::len(message) <= MAX_CHAT_MESSAGE_CHARS
                && !message.contains('\0') =>
        {
            Ok(message.clone())
        }
        _ => Err(invalid(
            "message must be non-empty and within its size limit",
            "invalid-message",
        )),
    }
}

/// The browser's IANA zone, which must load, or `None` when it named none.
fn timezone(value: &Value) -> Result<Option<String>, Problem> {
    if matches!(value, Value::Null) {
        return Ok(None);
    }
    let zone = team_routine::schedule::zone(value).ok().and(string(value));
    zone.map(|zone| Some(zone.to_owned()))
        .ok_or(invalid("timezone is invalid", "invalid-timezone"))
}

/// The Routine a new turn's person asked to refine, by id, or `None`.
fn refine(value: &Value) -> Result<Option<String>, Problem> {
    if matches!(value, Value::Null) {
        return Ok(None);
    }
    let routine_id = string(value)
        .filter(|_| team_protocol::routine::matches(value, &team_protocol::routine::ROUTINE_ID_RE));
    routine_id.map(|id| Some(id.to_owned())).ok_or(invalid(
        "refine must name one Routine or be null",
        "invalid-refine",
    ))
}

/// Admits exactly the chat body's members.
pub(super) fn chat_body(body: &Body) -> Result<ChatBody, Problem> {
    let shape = "Team chat requires only message, files, assistant_ids, conversation, locale, request, timezone, refine";
    let Body::Json(body @ Value::Object(members)) = body else {
        return Err(invalid(shape, "invalid-body"));
    };
    let exact = members.len() == CHAT_BODY_FIELDS.len()
        && CHAT_BODY_FIELDS.iter().all(|name| body.get(name).is_some());
    if !exact {
        return Err(invalid(shape, "invalid-body"));
    }
    let field = |name: &str| body.get(name).unwrap_or(&Value::Null);
    let identity = canonical_request_identity(field("request"))
        .ok_or(invalid("request identity is invalid", "invalid-request"))?;
    let (timezone, refine) = (timezone(field("timezone"))?, refine(field("refine"))?);
    let locale = match field("locale") {
        Value::Null => None,
        locale => Some(
            canonical_locale(locale)
                .ok_or(invalid(
                    "locale must be one interface language or null",
                    "invalid-locale",
                ))?
                .to_owned(),
        ),
    };
    let conversation =
        team_chat::contract::conversation_window(field("conversation")).ok_or(invalid(
            "conversation must be a bounded committed history window",
            "invalid-conversation",
        ))?;
    let files = chat_files(body.get("files"))?;
    let assistant_ids = assistant_ids(body.get("assistant_ids"))?;
    let message = chat_message(body.get("message"))?;
    Ok(ChatBody {
        message,
        files,
        assistant_ids,
        conversation,
        locale,
        nonce: identity
            .get("nonce")
            .and_then(string)
            .unwrap_or_default()
            .to_owned(),
        issued_at: identity
            .get("issued_at")
            .and_then(team_protocol::python::value::small_int)
            .unwrap_or_default(),
        timezone,
        refine,
    })
}

/// What a segment's outcome resolves with.
pub(super) struct Response {
    /// The admitted request.
    pub request: Request,
    /// The segment.
    pub segment: SegmentResult,
    /// The turn's selected Assistants and files.
    pub selection: (Vec<String>, Vec<String>),
    /// The replay transcripts the segment ran with, and the requests answered.
    pub transcripts: (Vec<ActionTranscript>, usize),
    /// What the turn consumed before this request.
    pub usage: TurnUsage,
    /// The turn's interface language.
    pub locale: Option<String>,
    /// The turn's author.
    pub source: Option<Source>,
    /// The turn's send in the Team's recording span.
    pub recording: Option<String>,
}

/// A new turn's first segment.
fn first_segment(
    admitted: &Admitted,
    request: &Request,
    body: &ChatBody,
    progress: Reporter,
    recording: Option<String>,
) -> SegmentRequest {
    SegmentRequest {
        team_id: request.team_id.clone(),
        token: admitted.execution.token().to_owned(),
        stop: admitted.execution.stop().clone(),
        file_ids: body.files.clone(),
        assistant_ids: body.assistant_ids.clone(),
        credential: request.credential.clone(),
        message: Some(body.message.clone()),
        conversation: body.conversation.clone(),
        locale: body.locale.clone(),
        continuation: None,
        expected: None,
        progress,
        model: None,
        usage: Arc::clone(&request.usage),
        actor: request.actor.clone(),
        recording,
        refine: body.refine.clone(),
    }
}

/// What a turn admitted at `admitted` either answers with (the Team's pending challenge) or runs under (its slot).
enum Admission {
    Pending(Value),
    Admitted(Admitted),
}

impl ChatService {
    /// Starts a turn, streaming its progress and its terminal answer.
    pub(super) fn start(&self, call: &Call<'_>) -> Result<Answer, Problem> {
        let request = self.request(call)?;
        let body = chat_body(&call.body)?;
        let service = self.clone();
        Ok(stream(move |reporter| async move {
            service.turn(request, body, reporter).await
        }))
    }

    /// The Team's pending challenge, read before and again after the turn holds the slot, or the slot.
    async fn admission(&self, team_id: &str, locale: Option<&str>) -> Result<Admission, Problem> {
        if let Some(pending) = self.pending_continuation(team_id, locale).await? {
            return Ok(Admission::Pending(pending));
        }
        let admitted = self.admit(team_id)?;
        match self.pending_continuation(team_id, locale).await? {
            Some(pending) => Ok(Admission::Pending(pending)),
            None => Ok(Admission::Admitted(admitted)),
        }
    }

    async fn turn(
        &self,
        request: Request,
        body: ChatBody,
        progress: Reporter,
    ) -> Result<Value, Problem> {
        let team_id = request.team_id.clone();
        let admitted = match self.admission(&team_id, body.locale.as_deref()).await? {
            Admission::Pending(pending) => return Ok(pending),
            Admission::Admitted(admitted) => admitted,
        };
        // The turn is admitted: its duration runs from here to its terminal, across every resume.
        let usage = TurnUsage::start(self.now_ms());
        let network = self.parts.team.network(&team_id).await?;
        let source = request.supervisor.clone().map(|actor_id| Source {
            nonce: body.nonce.clone(),
            actor_id,
            incarnation: network,
        });
        let recording = self.recording(&team_id, &body, source.as_ref());
        let progress = self.recorded_progress(&team_id, recording.as_deref(), progress);
        let added = self.turn_started(&team_id, &body.files).await?;
        let first = first_segment(&admitted, &request, &body, progress, recording.clone());
        let segment = match self.run_segment(&first).await {
            Ok(segment) => segment,
            Err(failure) => {
                // A failed turn leaves no continuation, so nothing can read the files only it referenced.
                self.turn_failed(&team_id, added).await;
                return Err(failure);
            }
        };
        let response = Response {
            request,
            segment,
            selection: (body.assistant_ids, body.files),
            transcripts: (Vec::new(), 0),
            usage,
            locale: body.locale,
            source,
            recording,
        };
        self.respond(&admitted, response).await
    }

    /// Opens the turn's send in the Team's recording span: only a verified person's fresh request without files.
    fn recording(&self, team_id: &str, body: &ChatBody, source: Option<&Source>) -> Option<String> {
        let started = crate::routine::recorder::Started {
            message: body.message.clone(),
            conversation: body.conversation.clone(),
            timezone: body.timezone.clone(),
            locale: body.locale.clone(),
        };
        let author = source
            .filter(|_| body.files.is_empty())
            .map(|source| (source.actor_id.as_str(), source.incarnation.as_str()));
        self.record_send(team_id, author, body.issued_at, &started)
    }

    /// Commits a completed turn, or pauses it on exactly one challenge.
    pub(super) async fn respond(
        &self,
        admitted: &Admitted,
        response: Response,
    ) -> Result<Value, Problem> {
        let team_id = response.request.team_id.clone();
        let outcome = response.segment.outcome.clone();
        match outcome {
            Outcome::Completed(terminal) => self.complete(admitted, &response, &terminal).await,
            Outcome::Suspended(suspension) => {
                if response.segment.integrations.is_empty() || response.segment.human.is_some() {
                    return Err(problem::internal_error());
                }
                let completed = suspension.continuation.seen_interrupts.clone();
                let pending = Self::pending(&response, suspension.continuation.clone(), &completed);
                let requirements = response.segment.integrations.clone();
                self.pause_integrations(&team_id, admitted, &suspension, requirements, pending)
                    .await
            }
            Outcome::Human(suspension) => {
                let requirement = response
                    .segment
                    .human
                    .clone()
                    .ok_or(problem::internal_error())?;
                if !response.segment.integrations.is_empty()
                    || response.segment.paused_batch.is_none()
                {
                    return Err(problem::internal_error());
                }
                let mut completed = suspension.continuation.seen_interrupts.clone();
                completed.extend(suspension.completed_interrupts.iter().cloned());
                let pending = Self::pending(&response, suspension.continuation.clone(), &completed);
                self.pause_human(&team_id, admitted, &suspension, requirement, pending)
                    .await
            }
        }
    }

    /// The paused turn's secret-free state, its transcripts of completed Actions dropped.
    fn pending(
        response: &Response,
        continuation: team_chat::orchestrator::ChatContinuation,
        completed: &[String],
    ) -> PendingLocalChat {
        let segment = &response.segment;
        PendingLocalChat {
            continuation,
            assistant_ids: response.selection.0.clone(),
            file_ids: response.selection.1.clone(),
            provider: Some(response.request.credential.0.clone()),
            identity: segment.identity.clone(),
            transcripts: retain_unfinished(&response.transcripts.0, completed),
            requests_used: response.transcripts.1,
            locale: response.locale.clone(),
            usage: Some(response.usage.joined(&response.request.usage.tokens())),
            paused_batch: segment.paused_batch.clone(),
            recording: response.recording.clone(),
            model: Some(segment.model.clone()),
            source: response.source.clone(),
        }
    }

    async fn complete(
        &self,
        admitted: &Admitted,
        response: &Response,
        terminal: &ChatOutcome,
    ) -> Result<Value, Problem> {
        let team_id = &response.request.team_id;
        self.delete_continuation(team_id, None)?;
        // Measured once, so the reply and the recording span count the same usage.
        let spent = response
            .usage
            .spent(&response.request.usage.tokens(), self.now_ms());
        let mut body = terminal_body(team_id, &response.segment.team_name, terminal, Some(&spent));
        // A turn that consumed selected files or read the web learns nothing, nor does one no verified person sent.
        let learns = response.selection.1.is_empty()
            && terminal.browser.is_none()
            && !terminal.memory.is_empty();
        let source = response.source.as_ref().filter(|_| learns);
        let proposed = match (&response.segment.answered, &terminal.routine) {
            (Some(intent), _) => Some(Proposed::Answered(intent.clone())),
            (None, Some(routine)) => Some(Proposed::Call(routine)),
            (None, None) => None,
        };
        {
            // A recorded Routine's card is kept with the reply, under the Team's stripe and the Stop guard: when Stop
            // wins, no card exists (ADR-0101).
            let _team = match proposed {
                Some(_) => Some(self.parts.coordination.locks.team(team_id).await),
                None => None,
            };
            let write = match proposed {
                Some(proposed) => Some(
                    self.routine_write(response, proposed, &mut body, &spent)
                        .await?,
                ),
                None => None,
            };
            let also = self.committed(team_id, response.recording.clone(), write, spent);
            self.commit_turn(admitted, team_id, &terminal.memory, source, also)
                .await?;
        }
        self.parts.transcripts.clear(team_id, None);
        let mut referenced = response.selection.1.clone();
        if let Some(Value::Array(files)) = terminal
            .browser
            .as_ref()
            .and_then(|browser| browser.get("files"))
        {
            referenced.extend(
                files
                    .iter()
                    .filter_map(|file| file.get("id").and_then(string).map(str::to_owned)),
            );
        }
        self.turn_completed(team_id, referenced).await;
        Ok(body)
    }

    /// What a committing turn also does: its Routine outcome's write, and its usage joining its send.
    fn committed(
        &self,
        team_id: &str,
        recording: Option<String>,
        write: Option<Write>,
        spent: team_inference::usage::Spent,
    ) -> impl FnOnce() + Send + use<> {
        let (routine, team) = (self.parts.routine.clone(), team_id.to_owned());
        move || {
            let Some(recording) = recording else { return };
            if let Some(write) = write {
                routine.apply(&team, &recording, write);
            }
            // A committed reply's usage joins its send, for a Routine the span may still create.
            routine.recordings.committed(&team, &recording, spent);
        }
    }

    /// A completed turn's Routine outcome: its write, with its terminal member added to `body`.
    async fn routine_write(
        &self,
        response: &Response,
        proposed: Proposed<'_>,
        body: &mut Value,
        spent: &team_inference::usage::Spent,
    ) -> Result<Write, Problem> {
        let request = &response.request;
        let (assistants, files) = (&response.selection.0, &response.selection.1);
        let current = self
            .setup_unlocked(&request.team_id, files, &request.credential.0, assistants)
            .await?;
        let unchanged = current.identity() == response.segment.identity;
        let turn = Turn {
            team_id: &request.team_id,
            recording: response.recording.as_deref().unwrap_or_default(),
            principal: request.supervisor.as_deref(),
            current: unchanged
                .then_some((current.network_id.as_str(), current.assistants.as_slice())),
            scopes: &response.segment.contracts,
        };
        let admitted = self.routine_outcome(&turn, proposed, body, Some(spent))?;
        if let Value::Object(members) = body {
            members.push((admitted.field.0.to_owned(), admitted.field.1));
        }
        Ok(admitted.write)
    }

    /// The Team's pending challenge, a human one opened in the chat's interface language; a drifted one ends its turn.
    pub(super) async fn pending_continuation(
        &self,
        team_id: &str,
        locale: Option<&str>,
    ) -> Result<Option<Value>, Problem> {
        self.expire_human(None).await?;
        let _team = self.parts.coordination.locks.team(team_id).await;
        if let Ok(Some(human)) = self.human.current(team_id) {
            let locale = locale.map_or_else(|| human.metadata.copy.locale.clone(), str::to_owned);
            let opened = self.relocalized(team_id, &human, &locale).await?;
            return Self::human_response(&opened).map(Some);
        }
        let Ok(Some(integration)) = self.parts.integration_challenges.current(team_id) else {
            return Ok(None);
        };
        let pending = &integration.payload;
        if self.paused_identity(team_id, pending).await? != pending.identity {
            return Err(self
                .end_drifted(team_id, super::pause::Kind::Integrations, &integration.id)
                .await);
        }
        self.integration_response(&integration).await.map(Some)
    }

    /// Records that a new turn references its files before its Brain start can; the files it newly referenced.
    async fn turn_started(
        &self,
        team_id: &str,
        file_ids: &[String],
    ) -> Result<Vec<String>, Problem> {
        if file_ids.is_empty() {
            return Ok(Vec::new());
        }
        let files = Arc::clone(&self.parts.files);
        let (team, ids) = (team_id.to_owned(), file_ids.to_vec());
        tokio::task::spawn_blocking(move || {
            let ids: Vec<&str> = ids.iter().map(String::as_str).collect();
            files.reference(&team, &ids)
        })
        .await
        .map_err(|_| problem::internal_error())?
        .map_err(|error| match error.kind {
            team_storage::files::StorageErrorKind::NotFound => problem::selected_file_not_found(),
            _ => crate::prepare::files::storage_problem(&error),
        })
    }

    /// A turn that ended without an outcome releases what it newly referenced; a failed release keeps them.
    async fn turn_failed(&self, team_id: &str, added: Vec<String>) {
        if added.is_empty() {
            return;
        }
        let files = Arc::clone(&self.parts.files);
        let team = team_id.to_owned();
        let _ = tokio::task::spawn_blocking(move || {
            let ids: Vec<&str> = added.iter().map(String::as_str).collect();
            files.release(&team, &ids)
        })
        .await;
    }

    /// A completed turn leaves its Brain thread referencing only this turn's files.
    async fn turn_completed(&self, team_id: &str, referenced: Vec<String>) {
        let files = Arc::clone(&self.parts.files);
        let team = team_id.to_owned();
        let _ = tokio::task::spawn_blocking(move || {
            let ids: Vec<&str> = referenced.iter().map(String::as_str).collect();
            files.settle(&team, &ids)
        })
        .await;
    }

    /// The identity the Team presents now to a paused turn, with the paused turn's selection.
    pub(super) async fn paused_identity(
        &self,
        team_id: &str,
        pending: &PendingLocalChat,
    ) -> Result<super::pending::Identity, Problem> {
        let provider = pending.provider.clone().unwrap_or_default();
        let setup = self
            .setup_unlocked(
                team_id,
                &pending.file_ids,
                &provider,
                &pending.assistant_ids,
            )
            .await?;
        Ok(setup.identity())
    }
}
