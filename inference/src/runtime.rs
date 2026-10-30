//! A chat turn's Brain operations as Team admits them: the turn context the Brain receives, the closed turn answer,
//! one Action request's purpose, and a thread's deletion.
//!
//! The context and every answer are Team's HTTP protocol (`teams/protocol/http/v1/turn.py`): the bounds here are the
//! protocol's own, which the Brain and Admin read too. Every answer is admitted whole or refused as
//! [`BrainError::Protocol`]; its reported usage is recorded first, even when the rest is refused.

use std::fmt::Write as _;
use std::time::Duration;

use sha2::{Digest, Sha256};
use strict_json::Value;
use team_core::stop::Stop;
use team_protocol::browser::canonical_objective;
use team_protocol::identifiers::{canonical_action_id, canonical_assistant_id};
use team_protocol::payload::{
    MAX_CONVERSATION_CHARS, MAX_CONVERSATION_ENTRIES, MAX_CONVERSATION_TEXT_CHARS,
    canonical_clarification, canonical_locale, canonical_memory_changes, render_clarification,
};
use team_protocol::purpose::canonical_purpose;
use team_protocol::python::json::{Options, canonical, dumps};
use team_protocol::python::text;
use team_protocol::python::value::{object_of, string};
use team_protocol::turn::{MAX_ACTION_REQUESTS, MAX_REPLY_CHARS, REPLY_CONTROL_RE, SAFE_ID_RE};
use tokio::time::Instant;

use crate::brain::{BrainClient, BrainError, BrainOperation, BrainRequest, ModelCredential};
use crate::usage::{UsageRecorder, parse as parse_usage};

/// The Brain refuses a request body above this many bytes.
pub const MAX_REQUEST_BYTES: usize = 4 * 1024 * 1024;
/// A resume keys each result by its interrupt id (at most 256 characters, quoted, a colon and a comma), and a recording
/// turn that selects a Routine wraps a traced call's result with its Team handle (ADR-0122).
pub const RESULT_KEY_BYTES: usize = 256 + 4 + r#"{"call":"c256","result":}"#.len();
/// Brain answers a turn only when it ends, and a turn's model may legitimately work for minutes; Stop still aborts at
/// once.
pub const RESPONSE_TIMEOUT: Duration = Duration::from_secs(300);
/// An optional purpose sentence may delay a person's prompt only this long in total (ADR-0090).
pub const PURPOSE_DEADLINE: Duration = Duration::from_secs(15);

/// The JSON a Brain request body is written as: compact, insertion order, UTF-8.
pub const BODY: Options = Options {
    ensure_ascii: false,
    sort_keys: false,
    compact: true,
};

/// One Action exactly as the private Brain request carries it.
#[derive(Clone, Debug, PartialEq)]
pub struct RuntimeAction {
    /// The Action id.
    pub id: String,
    /// Its reviewed summary.
    pub summary: String,
    /// Its reviewed input schema.
    pub input_schema: Value,
    /// Whether the Action declares an authorization capability: only such an Action may run while attachment content
    /// is in the turn (ADR-0093).
    pub authorization: bool,
    /// The input properties that carry one Team file id each (ADR-0093).
    pub input_files: Vec<String>,
    /// The reviewed output schema. The Brain reads none of it, yet it stays in the contract digest that a Routine's
    /// scope pin keeps, so a changed output schema reads as a changed Assistant.
    pub output_schema: Value,
}

impl RuntimeAction {
    fn wire(&self) -> Value {
        object_of(vec![
            ("id", Value::String(self.id.clone())),
            ("summary", Value::String(self.summary.clone())),
            ("input_schema", self.input_schema.clone()),
            ("output_schema", self.output_schema.clone()),
            ("authorization", Value::Bool(self.authorization)),
            (
                "input_files",
                Value::Array(
                    self.input_files
                        .iter()
                        .cloned()
                        .map(Value::String)
                        .collect(),
                ),
            ),
        ])
    }
}

/// One installed Assistant as a turn sees it.
#[derive(Clone, Debug, PartialEq)]
pub struct RuntimeAssistant {
    /// The Assistant id.
    pub id: String,
    /// Its Genesis text.
    pub genesis: String,
    /// Its Actions, sorted by id.
    pub actions: Vec<RuntimeAction>,
}

impl RuntimeAssistant {
    fn wire(&self) -> Value {
        object_of(vec![
            ("id", Value::String(self.id.clone())),
            ("genesis", Value::String(self.genesis.clone())),
            (
                "actions",
                Value::Array(self.actions.iter().map(RuntimeAction::wire).collect()),
            ),
        ])
    }
}

/// The `sha256:` fingerprint of one Assistant contract as Brain receives it; a changed contract changes it.
#[must_use]
pub fn contract_digest(assistant: &RuntimeAssistant) -> String {
    let digest = Sha256::digest(canonical(&assistant.wire()));
    let mut text = String::from("sha256:");
    for byte in digest {
        let _ = write!(text, "{byte:02x}");
    }
    text
}

/// One entry of the committed presentation history a new turn carries.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConversationEntry {
    /// `user` or `assistant`.
    pub role: String,
    /// Its text.
    pub text: String,
    /// Whether Admin cut it.
    pub truncated: bool,
}

/// Everything a turn's Brain sees.
#[derive(Clone, Debug, PartialEq)]
pub struct RuntimeContext {
    /// The Brain thread.
    pub thread_id: String,
    /// The Team's display name.
    pub team_name: String,
    /// The installed Assistants the turn may call.
    pub assistants: Vec<RuntimeAssistant>,
    /// The request-only model credential, its model filled by Team.
    pub credential: ModelCredential,
    /// The Team's learned memory as `{topic, preference}` entries; `None` withholds the Brain's memory tool.
    pub memories: Option<Vec<Value>>,
    /// The Team's Routines as data (ADR-0086); `None` withholds the Brain's Routine tool.
    pub routines: Option<Vec<Value>>,
    /// The question Team asked about the recording this chat continues (ADR-0101), `{code, options, value}`.
    pub routine_question: Option<Value>,
    /// Advisory: whether the chat is about a Routine (ADR-0101).
    pub routine_mode: bool,
    /// The work a pending rerun question asks to run again, in order, bounded and redacted.
    pub routine_rerun: Option<Vec<Value>>,
    /// The one schedule the recording span's person states.
    pub routine_schedule: Option<Value>,
    /// The one output mode the recording span's person states.
    pub routine_output: Option<String>,
    /// Whether the latest send is exactly the person's pick of one fixed settings answer (ADR-0101).
    pub routine_settings_answer: bool,
    /// The one Routine the recording send selects (ADR-0122).
    pub routine_selected: Option<Value>,
    /// The interface language a new turn is written in (ADR-0090); only a start sends it.
    pub locale: Option<String>,
    /// The date the person's send started on in the Routine's zone, and that zone (ADR-0101); only a start sends it.
    pub turn_clock: Option<(String, String)>,
    /// The selected files prepared for this message (ADR-0093), each its complete Brain wire attachment.
    pub attachments: Vec<Value>,
    /// Whether the chat agent may call the Browser this turn (ADR-0117).
    pub browser: bool,
}

fn optional_list(value: Option<&Vec<Value>>) -> Value {
    value.map_or(Value::Null, |items| Value::Array(items.clone()))
}

fn optional(value: Option<&Value>) -> Value {
    value.cloned().unwrap_or(Value::Null)
}

impl RuntimeContext {
    /// The context's body without the credential, which the Brain client places as `provider`.
    #[must_use]
    pub fn body(&self) -> Vec<(String, Value)> {
        let members = vec![
            ("thread_id", Value::String(self.thread_id.clone())),
            ("team_name", Value::String(self.team_name.clone())),
            (
                "assistants",
                Value::Array(self.assistants.iter().map(RuntimeAssistant::wire).collect()),
            ),
            ("memories", optional_list(self.memories.as_ref())),
            ("routines", optional_list(self.routines.as_ref())),
            ("routine_question", optional(self.routine_question.as_ref())),
            ("routine_mode", Value::Bool(self.routine_mode)),
            ("routine_rerun", optional_list(self.routine_rerun.as_ref())),
            ("routine_schedule", optional(self.routine_schedule.as_ref())),
            (
                "routine_output",
                self.routine_output
                    .clone()
                    .map_or(Value::Null, Value::String),
            ),
            (
                "routine_settings_answer",
                Value::Bool(self.routine_settings_answer),
            ),
            ("routine_selected", optional(self.routine_selected.as_ref())),
            ("attachments", Value::Array(self.attachments.clone())),
            ("browser_available", Value::Bool(self.browser)),
        ];
        members
            .into_iter()
            .map(|(name, value)| (name.to_owned(), value))
            .collect()
    }
}

/// The provider credential as a Brain request carries it under `provider`.
#[must_use]
pub fn credential_wire(credential: &ModelCredential) -> Value {
    object_of(vec![
        ("provider", Value::String(credential.provider.clone())),
        ("model", Value::String(credential.model.clone())),
        (
            "api_key",
            Value::String(credential.api_key.expose().to_owned()),
        ),
    ])
}

/// `body` with the credential placed first as `provider`, exactly as the Brain client sends it.
#[must_use]
pub fn with_credential(body: Value, credential: &ModelCredential) -> Value {
    match body {
        Value::Object(members) => {
            let mut placed = Vec::with_capacity(members.len() + 1);
            placed.push(("provider".to_owned(), credential_wire(credential)));
            placed.extend(members.into_iter().filter(|(name, _)| name != "provider"));
            Value::Object(placed)
        }
        other => other,
    }
}

/// How many Action results of at most `result_bytes` each one resume of this turn can carry to the Brain.
///
/// The fixed part is the exact serialized turn context, attachments and credential included; each result reserves its
/// worst case.
#[must_use]
pub fn resume_capacity(context: &RuntimeContext, result_bytes: usize) -> usize {
    let mut members = context.body();
    members.push(("results".to_owned(), Value::Object(Vec::new())));
    let fixed = dumps(
        &with_credential(Value::Object(members), &context.credential),
        BODY,
    )
    .len();
    MAX_REQUEST_BYTES.saturating_sub(fixed) / (result_bytes + RESULT_KEY_BYTES)
}

/// One Action request of a Brain suspension.
#[derive(Clone, Debug, PartialEq)]
pub struct ActionRequest {
    /// The Brain's interrupt id.
    pub interrupt_id: String,
    /// The Assistant.
    pub assistant_id: String,
    /// The Action.
    pub action: String,
    /// Its input object.
    pub input: Value,
}

/// The chat agent's one `browser` call: its interrupt and its self-contained objective.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BrowserRequest {
    /// The interrupt id.
    pub interrupt_id: String,
    /// The objective.
    pub objective: String,
}

/// How a turn answered.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TurnStatus {
    /// The turn ended with its reply.
    Completed,
    /// The turn suspended on Action requests.
    ActionRequired,
    /// The turn suspended on its one Browser call.
    BrowserRequired,
}

impl TurnStatus {
    /// The wire name.
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Completed => "completed",
            Self::ActionRequired => "action-required",
            Self::BrowserRequired => "browser-required",
        }
    }

    /// The status of a wire name.
    #[must_use]
    pub fn parse(name: &str) -> Option<Self> {
        match name {
            "completed" => Some(Self::Completed),
            "action-required" => Some(Self::ActionRequired),
            "browser-required" => Some(Self::BrowserRequired),
            _ => None,
        }
    }
}

/// One admitted turn answer.
#[derive(Clone, Debug, PartialEq)]
pub struct RuntimeTurn {
    /// How the turn answered.
    pub status: TurnStatus,
    /// The reply of a completed turn.
    pub reply: String,
    /// The Action requests of a suspension.
    pub actions: Vec<ActionRequest>,
    /// One closed multiple-choice question that ended a completed turn (ADR-0081).
    pub clarification: Option<Value>,
    /// The canonical memory changes a completed turn proposed (ADR-0084).
    pub memory: Vec<Value>,
    /// The chat agent's one Routine `record` that ended a completed turn (ADR-0101).
    pub routine: Option<Value>,
    /// The one `browser` call a `browser-required` turn suspended on (ADR-0117).
    pub browser: Option<BrowserRequest>,
}

const TURN_FIELDS: [&str; 7] = [
    "status",
    "reply",
    "actions",
    "clarification",
    "memory",
    "routine",
    "browser",
];

fn exactly<'v>(value: &'v Value, fields: &[&str]) -> Option<&'v [(String, Value)]> {
    let Value::Object(members) = value else {
        return None;
    };
    (members.len() == fields.len()
        && members
            .iter()
            .all(|(name, _)| fields.contains(&name.as_str())))
    .then_some(members.as_slice())
}

fn member<'v>(value: &'v Value, name: &str) -> &'v Value {
    value.get(name).unwrap_or(&Value::Null)
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(strict_json::Number::Int(number)) => *number != 0,
        Value::Number(_) => true,
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(members) => !members.is_empty(),
    }
}

/// A `browser-required` turn's one Browser call, which nothing accompanies; every other turn carries none.
/// What a turn answer's `browser` member holds.
enum BrowserCall {
    /// No call, on a turn that is not `browser-required`.
    None,
    /// The one call of a `browser-required` turn.
    Call(BrowserRequest),
}

fn parse_browser_call(value: &Value) -> Option<BrowserCall> {
    let browser = member(value, "browser");
    if string(member(value, "status")) != Some("browser-required") {
        return matches!(browser, Value::Null).then_some(BrowserCall::None);
    }
    let call = exactly(browser, &["interrupt_id", "objective"])?;
    let interrupt_id = string(browser.get("interrupt_id")?).filter(|id| SAFE_ID_RE.is_full(id))?;
    let objective = browser.get("objective")?;
    let admitted = canonical_objective(objective)
        .is_some_and(|canonical| string(objective) == Some(canonical));
    let alone = !truthy(member(value, "reply"))
        && !truthy(member(value, "actions"))
        && matches!(member(value, "clarification"), Value::Null)
        && !truthy(member(value, "memory"))
        && matches!(member(value, "routine"), Value::Null);
    let _ = call;
    (admitted && alone).then(|| {
        BrowserCall::Call(BrowserRequest {
            interrupt_id: interrupt_id.to_owned(),
            objective: string(objective).unwrap_or_default().to_owned(),
        })
    })
}

fn parse_action(raw: &Value) -> Option<ActionRequest> {
    exactly(raw, &["interrupt_id", "assistant_id", "action", "input"])?;
    let interrupt_id = string(raw.get("interrupt_id")?).filter(|id| SAFE_ID_RE.is_full(id))?;
    let assistant_id = canonical_assistant_id(raw.get("assistant_id")?)?;
    let action = canonical_action_id(raw.get("action")?)?;
    let input = raw.get("input")?;
    matches!(input, Value::Object(_)).then(|| ActionRequest {
        interrupt_id: interrupt_id.to_owned(),
        assistant_id: assistant_id.to_owned(),
        action: action.to_owned(),
        input: input.clone(),
    })
}

/// A completed turn's Routine record and closed question, which never come together.
fn parse_ending(value: &Value, completed: bool) -> Option<(Option<Value>, Option<Value>)> {
    let routine = match member(value, "routine") {
        Value::Null => None,
        routine @ Value::Object(_)
            if completed && matches!(member(value, "clarification"), Value::Null) =>
        {
            Some(routine.clone())
        }
        _ => return None,
    };
    let clarification = match member(value, "clarification") {
        Value::Null => None,
        raw => {
            let clarification = canonical_clarification(raw)?;
            let rendered = render_clarification(&clarification);
            if !completed || string(member(value, "reply")) != Some(rendered.as_str()) {
                return None;
            }
            Some(clarification)
        }
    };
    Some((routine, clarification))
}

fn parse_actions(value: &Value) -> Option<Vec<ActionRequest>> {
    let Value::Array(raw_actions) = member(value, "actions") else {
        return None;
    };
    if raw_actions.len() > MAX_ACTION_REQUESTS {
        return None;
    }
    raw_actions.iter().map(parse_action).collect()
}

/// Admits one turn answer (its usage already removed), or `None`.
#[must_use]
pub fn parse_turn(value: &Value) -> Option<RuntimeTurn> {
    exactly(value, &TURN_FIELDS)?;
    let browser = match parse_browser_call(value)? {
        BrowserCall::None => None,
        BrowserCall::Call(request) => Some(request),
    };
    let status_name = string(member(value, "status"));
    let completed = status_name == Some("completed");
    let Value::Array(memory) = canonical_memory_changes(member(value, "memory"))? else {
        return None;
    };
    if !memory.is_empty() && !completed {
        return None;
    }
    let (routine, clarification) = parse_ending(value, completed)?;
    let status = TurnStatus::parse(status_name?)?;
    let reply = string(member(value, "reply"))?;
    let actions = parse_actions(value)?;
    if text::len(reply) > MAX_REPLY_CHARS || !REPLY_CONTROL_RE.is_absent(reply) {
        return None;
    }
    // A completed turn says something, except one that ended on a Routine record: the card, question, or refusal Team
    // answers it with says everything, and its reply may be empty.
    let silent = text::strip(reply).is_empty() && (routine.is_none() || !reply.is_empty());
    let completed_invalid = status == TurnStatus::Completed && (!actions.is_empty() || silent);
    let suspended_invalid =
        status == TurnStatus::ActionRequired && (!reply.is_empty() || actions.is_empty());
    (!completed_invalid && !suspended_invalid).then(|| RuntimeTurn {
        status,
        reply: reply.to_owned(),
        actions,
        clarification,
        memory,
        routine,
        browser,
    })
}

/// A turn answer [`parse_turn`] admits as `turn` again: a turn kept durably crosses back exactly.
#[must_use]
pub fn turn_value(turn: &RuntimeTurn) -> Value {
    let text = |value: &str| Value::String(value.to_owned());
    let actions = turn
        .actions
        .iter()
        .map(|request| {
            Value::Object(vec![
                ("interrupt_id".to_owned(), text(&request.interrupt_id)),
                ("assistant_id".to_owned(), text(&request.assistant_id)),
                ("action".to_owned(), text(&request.action)),
                ("input".to_owned(), request.input.clone()),
            ])
        })
        .collect();
    let browser = turn.browser.as_ref().map_or(Value::Null, |browser| {
        Value::Object(vec![
            ("interrupt_id".to_owned(), text(&browser.interrupt_id)),
            ("objective".to_owned(), text(&browser.objective)),
        ])
    });
    Value::Object(vec![
        ("status".to_owned(), text(turn.status.name())),
        ("reply".to_owned(), text(&turn.reply)),
        ("actions".to_owned(), Value::Array(actions)),
        (
            "clarification".to_owned(),
            turn.clarification.clone().unwrap_or(Value::Null),
        ),
        ("memory".to_owned(), Value::Array(turn.memory.clone())),
        (
            "routine".to_owned(),
            turn.routine.clone().unwrap_or(Value::Null),
        ),
        ("browser".to_owned(), browser),
    ])
}

/// Records an answer's reported usage, even when the rest is invalid, and returns the rest; `None` without a usage.
#[must_use]
pub fn metered(
    value: Value,
    operation: &'static str,
    credential: &ModelCredential,
    usage: &dyn UsageRecorder,
) -> Option<Value> {
    let Value::Object(members) = value else {
        return None;
    };
    let mut rest = Vec::with_capacity(members.len());
    let mut reported = None;
    for (name, member) in members {
        if name == "usage" {
            reported = Some(member);
        } else {
            rest.push((name, member));
        }
    }
    let counts = parse_usage(&reported?)?;
    usage.record(operation, &credential.provider, &credential.model, &counts);
    Some(Value::Object(rest))
}

/// Whether `value` is admissible bounded text: NFC, unpadded, 1 to `maximum` code points, and no control or other
/// `C` category character, except format characters and line layout when `layout` allows them.
#[must_use]
pub fn bounded_text(value: &str, maximum: usize, layout: bool) -> bool {
    text::is_nfc(value)
        && text::strip(value) == value
        && (1..=maximum).contains(&text::len(value))
        && value.chars().all(|character| {
            let category = text::category(character);
            !category.starts_with('C')
                || (layout && (category == "Cf" || matches!(character, '\n' | '\r' | '\t')))
        })
}

/// Admits one bounded window of untrusted committed presentation history, or `None`.
#[must_use]
pub fn validate_conversation(entries: &[ConversationEntry]) -> Option<Vec<ConversationEntry>> {
    if entries.len() > MAX_CONVERSATION_ENTRIES {
        return None;
    }
    let admitted = entries.iter().all(|entry| {
        (entry.role == "user" || entry.role == "assistant")
            && bounded_text(&entry.text, MAX_CONVERSATION_TEXT_CHARS, true)
    });
    let total: usize = entries.iter().map(|entry| text::len(&entry.text)).sum();
    (admitted && total <= MAX_CONVERSATION_CHARS).then(|| entries.to_vec())
}

/// The conversation as a Brain request carries it.
#[must_use]
pub fn conversation_wire(entries: &[ConversationEntry]) -> Value {
    Value::Array(
        entries
            .iter()
            .map(|entry| {
                object_of(vec![
                    ("role", Value::String(entry.role.clone())),
                    ("text", Value::String(entry.text.clone())),
                    ("truncated", Value::Bool(entry.truncated)),
                ])
            })
            .collect(),
    )
}

/// A chat turn's Brain, as the turn engine calls it: the Brain runtime, or a Routine run's runtime that answers from
/// its plan.
pub trait Runtime: Send + Sync {
    /// Starts a turn with `message`; the Brain uses `conversation` only when it retains no completed exchange.
    ///
    /// # Errors
    /// [`BrainError`] for every unsuccessful outcome.
    fn start<'a>(
        &'a self,
        context: &'a RuntimeContext,
        message: &'a str,
        conversation: &'a [ConversationEntry],
        stop: &'a Stop,
    ) -> team_core::future::BoxFuture<'a, Result<RuntimeTurn, BrainError>>;

    /// Resumes the turn with each interrupt's result.
    ///
    /// # Errors
    /// [`BrainError`] for every unsuccessful outcome.
    fn resume<'a>(
        &'a self,
        context: &'a RuntimeContext,
        results: Value,
        stop: &'a Stop,
    ) -> team_core::future::BoxFuture<'a, Result<RuntimeTurn, BrainError>>;

    /// Asks once why the user's task needs this paused Action, in the turn's language; any failure is `None`.
    fn purpose<'a>(
        &'a self,
        context: &'a RuntimeContext,
        request: &'a ActionRequest,
        assistant_name: &'a str,
        summary: &'a str,
        stop: &'a Stop,
    ) -> team_core::future::BoxFuture<'a, Option<String>>;
}

/// The Brain runtime as a chat turn calls it, recording each answer's usage.
pub struct Brain<'a> {
    /// The Brain client.
    pub client: &'a dyn BrainClient,
    /// Where each answer's usage is recorded.
    pub usage: &'a dyn UsageRecorder,
}

impl std::fmt::Debug for Brain<'_> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Brain")
    }
}

impl Brain<'_> {
    async fn turn(
        &self,
        operation: BrainOperation,
        metered_as: &'static str,
        context: &RuntimeContext,
        body: Vec<(String, Value)>,
        stop: &Stop,
    ) -> Result<RuntimeTurn, BrainError> {
        let answer = self
            .client
            .call(BrainRequest {
                operation,
                body: Value::Object(body),
                credential: Some(&context.credential),
                deadline: Instant::now() + RESPONSE_TIMEOUT,
                stop,
            })
            .await?;
        let rest = metered(answer, metered_as, &context.credential, self.usage)
            .ok_or(BrainError::Protocol)?;
        parse_turn(&rest).ok_or(BrainError::Protocol)
    }

    /// Deletes one Brain thread.
    ///
    /// # Errors
    /// [`BrainError`] for every unsuccessful outcome, [`BrainError::Protocol`] for an invalid thread id.
    pub async fn delete_thread(&self, thread_id: &str, stop: &Stop) -> Result<(), BrainError> {
        if !SAFE_ID_RE.is_full(thread_id) {
            return Err(BrainError::Protocol);
        }
        let answer = self
            .client
            .call(BrainRequest {
                operation: BrainOperation::DeleteThread,
                body: object_of(vec![("thread_id", Value::String(thread_id.to_owned()))]),
                credential: None,
                deadline: Instant::now() + RESPONSE_TIMEOUT,
                stop,
            })
            .await?;
        let deleted = object_of(vec![("status", Value::String("deleted".to_owned()))]);
        if answer == deleted {
            Ok(())
        } else {
            Err(BrainError::Protocol)
        }
    }
}

impl Runtime for Brain<'_> {
    fn start<'a>(
        &'a self,
        context: &'a RuntimeContext,
        message: &'a str,
        conversation: &'a [ConversationEntry],
        stop: &'a Stop,
    ) -> team_core::future::BoxFuture<'a, Result<RuntimeTurn, BrainError>> {
        Box::pin(async move {
            let locale = match &context.locale {
                None => Value::Null,
                Some(locale) => {
                    let value = Value::String(locale.clone());
                    if canonical_locale(&value).is_none() {
                        return Err(BrainError::Protocol);
                    }
                    value
                }
            };
            let conversation = validate_conversation(conversation).ok_or(BrainError::Protocol)?;
            let mut body = context.body();
            body.push(("message".to_owned(), Value::String(message.to_owned())));
            body.push(("locale".to_owned(), locale));
            if let Some((date, timezone)) = &context.turn_clock {
                body.push((
                    "turn_clock".to_owned(),
                    object_of(vec![
                        ("date", Value::String(date.clone())),
                        ("timezone", Value::String(timezone.clone())),
                    ]),
                ));
            }
            body.push(("conversation".to_owned(), conversation_wire(&conversation)));
            self.turn(BrainOperation::Turn, "turn", context, body, stop)
                .await
        })
    }

    fn resume<'a>(
        &'a self,
        context: &'a RuntimeContext,
        results: Value,
        stop: &'a Stop,
    ) -> team_core::future::BoxFuture<'a, Result<RuntimeTurn, BrainError>> {
        Box::pin(async move {
            let mut body = context.body();
            body.push(("results".to_owned(), results));
            self.turn(BrainOperation::Resume, "turn-resume", context, body, stop)
                .await
        })
    }

    fn purpose<'a>(
        &'a self,
        context: &'a RuntimeContext,
        request: &'a ActionRequest,
        assistant_name: &'a str,
        summary: &'a str,
        stop: &'a Stop,
    ) -> team_core::future::BoxFuture<'a, Option<String>> {
        Box::pin(async move {
            let body = object_of(vec![
                ("thread_id", Value::String(context.thread_id.clone())),
                ("interrupt_id", Value::String(request.interrupt_id.clone())),
                ("assistant_id", Value::String(request.assistant_id.clone())),
                ("assistant_name", Value::String(assistant_name.to_owned())),
                ("action_id", Value::String(request.action.clone())),
                ("action_summary", Value::String(summary.to_owned())),
            ]);
            let answer = self
                .client
                .call(BrainRequest {
                    operation: BrainOperation::Purpose,
                    body,
                    credential: Some(&context.credential),
                    deadline: Instant::now() + PURPOSE_DEADLINE,
                    stop,
                })
                .await
                .ok()?;
            let rest = metered(answer, "purpose", &context.credential, self.usage)?;
            let purpose = exactly(&rest, &["purpose"]).and_then(|_| rest.get("purpose"))?;
            canonical_purpose(purpose).map(str::to_owned)
        })
    }
}

#[cfg(test)]
mod tests;
