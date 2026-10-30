//! A chat turn's part in recording a Routine (`local/chat/api.py`, `local/chat/segment.py`, ADR-0101, ADR-0122): the
//! send it opens in the Team's span, what its Brain sees of the Team's Routines and the span, the runtime that records
//! a bound answer with no model, and the card, question, or refusal its reply commits with.

use std::sync::Arc;

use strict_json::Value;
use team_core::stop::Stop;
use team_inference::brain::BrainError;
use team_inference::runtime::{ActionRequest, Runtime, RuntimeContext, RuntimeTurn, TurnStatus};
use team_inference::usage::Spent;
use team_protocol::payload::{authored_segments, request_identity_fresh};
use team_protocol::progress::MAX_LINE_BYTES;
use team_protocol::python::json::{Options, dumps};
use team_protocol::python::value::{int, object_of};
use team_protocol::routine_context::canonical_routine_listings;
use team_routine::definition::daily_steps;
use team_routine::json::{as_str, at, elements, members};
use team_routine::provenance::date_at;
use team_routine::record::TeamRoutines;
use team_routine::recording::zone;
use team_routine::reference::referred;
use team_storage::private::epoch_seconds;

use super::problem;
use super::segment::{ROUTINE_MODELS, SegmentRequest};
use super::service::ChatService;
use crate::http::service::Problem;
use crate::routine::proposal::{Admitted, Proposed, Turn};
use crate::routine::recorder::{
    Intent, Span, Started, answered, rerun_work, routine_mode, settings_answer, stated_output,
    stated_schedule,
};
use crate::routine::state::load;

/// The runtime of a send Team records itself: it never asks the Brain, and its reply is empty; the card, question, or
/// refusal the recording ends with says everything.
pub(super) struct AnsweredRuntime;

fn answered_turn() -> RuntimeTurn {
    RuntimeTurn {
        status: TurnStatus::Completed,
        reply: String::new(),
        actions: Vec::new(),
        clarification: None,
        memory: Vec::new(),
        routine: None,
        browser: None,
    }
}

impl Runtime for AnsweredRuntime {
    fn start<'a>(
        &'a self,
        _context: &'a RuntimeContext,
        _message: &'a str,
        _conversation: &'a [team_inference::runtime::ConversationEntry],
        _stop: &'a Stop,
    ) -> team_core::future::BoxFuture<'a, Result<RuntimeTurn, BrainError>> {
        Box::pin(async { Ok(answered_turn()) })
    }

    fn resume<'a>(
        &'a self,
        _context: &'a RuntimeContext,
        _results: Value,
        _stop: &'a Stop,
    ) -> team_core::future::BoxFuture<'a, Result<RuntimeTurn, BrainError>> {
        Box::pin(async { Ok(answered_turn()) })
    }

    fn purpose<'a>(
        &'a self,
        _context: &'a RuntimeContext,
        _request: &'a ActionRequest,
        _assistant_name: &'a str,
        _summary: &'a str,
        _stop: &'a Stop,
    ) -> team_core::future::BoxFuture<'a, Option<String>> {
        Box::pin(async { None })
    }
}

/// What a segment's Brain sees of the Team's Routines and the recording span.
#[derive(Default)]
pub(super) struct RoutineView {
    pub routines: Option<Vec<Value>>,
    pub question: Option<Value>,
    pub mode: bool,
    pub rerun: Option<Vec<Value>>,
    pub schedule: Option<Value>,
    pub output: Option<String>,
    pub settings_answer: bool,
    pub selected: Option<Value>,
    pub turn_clock: Option<(String, String)>,
    /// Whether the span reads as about a Routine, which routes the model and withholds the Browser.
    pub span_mode: bool,
}

/// One Routine as the Brain lists it: enough to name it and see its steps, never an input value.
fn listing(state: &TeamRoutines) -> Vec<Value> {
    let text = |value: &str| Value::String(value.to_owned());
    state
        .routines
        .iter()
        .filter(|item| !item.deleting)
        .map(|item| {
            let steps = elements(at(&item.plan, "steps"))
                .iter()
                .map(|step| {
                    let mut inputs: Vec<&str> = members(at(step, "input"))
                        .iter()
                        .map(|(name, _)| name.as_str())
                        .collect();
                    inputs.sort_unstable();
                    object_of(vec![
                        ("id", at(step, "id").clone()),
                        ("assistant", at(step, "assistant").clone()),
                        ("action", at(step, "action").clone()),
                        (
                            "inputs",
                            Value::Array(inputs.into_iter().map(text).collect()),
                        ),
                    ])
                })
                .collect();
            object_of(vec![
                ("routine_id", text(&item.routine_id)),
                ("name", text(&item.name)),
                ("schedule", item.schedule.clone()),
                ("timezone", text(item.timezone())),
                ("timezone_source", text(&item.timezone_source)),
                ("revision", int(item.revision)),
                ("daily_steps", int(daily_steps(item))),
                (
                    "output",
                    object_of(vec![("mode", at(at(&item.plan, "output"), "mode").clone())]),
                ),
                ("steps", Value::Array(steps)),
            ])
        })
        .collect()
}

/// The encoded bytes of one terminal NDJSON line, its newline included.
fn line_bytes(body: &Value) -> usize {
    let line = object_of(vec![
        ("type", Value::String("terminal".to_owned())),
        ("status", int(200)),
        ("body", body.clone()),
    ]);
    dumps(
        &line,
        Options {
            ensure_ascii: false,
            sort_keys: false,
            compact: true,
        },
    )
    .len()
        + 1
}

impl ChatService {
    /// Opens the send of a new turn in the Team's recording span, or `None`, which ends any span: only a verified
    /// person's fresh request without files records, in the Team incarnation it starts in.
    pub(super) fn record_send(
        &self,
        team_id: &str,
        author: Option<(&str, &str)>,
        issued_at: i64,
        started: &Started,
    ) -> Option<String> {
        let recordings = &self.parts.routine.recordings;
        let now = epoch_seconds(self.parts.clock.as_ref());
        let recording = author
            .filter(|_| request_identity_fresh(issued_at, now))
            .and_then(|binding| recordings.start(team_id, binding, started, now).ok());
        if recording.is_none() {
            recordings.drop_team(team_id);
        }
        recording
    }

    /// A recording turn's progress, which also keeps each finished Team step in its send.
    pub(super) fn recorded_progress(
        &self,
        team_id: &str,
        recording: Option<&str>,
        progress: team_chat::progress::Reporter,
    ) -> team_chat::progress::Reporter {
        let Some(recording) = recording else {
            return progress;
        };
        let book = Arc::clone(&self.parts.routine.recordings);
        let (team, recording) = (team_id.to_owned(), recording.to_owned());
        progress.teed(Arc::new(move |event: Value| {
            book.stepped(&team, &recording, &event);
        }))
    }

    /// The intent Team records itself when a new send is Admin's composed answer that binds the span's question.
    pub(super) fn answered_intent(&self, request: &SegmentRequest) -> Option<Intent> {
        let recording = request
            .recording
            .as_deref()
            .filter(|_| request.continuation.is_none())?;
        answered(
            self.parts
                .routine
                .recordings
                .live(&request.team_id, Some(recording))
                .as_ref(),
        )
    }

    /// The Team's Routines for a recording turn's Brain, only in the protocol's admitted listing.
    async fn chat_routines(&self, team_id: &str) -> Result<Vec<Value>, Problem> {
        let store = Arc::clone(&self.parts.routine.routines);
        let team = team_id.to_owned();
        let state = tokio::task::spawn_blocking(move || load(&store, &team))
            .await
            .map_err(|_| problem::internal_error())?
            .map_err(|_| problem::routine_state_unavailable())?;
        let listed = listing(&state);
        canonical_routine_listings(&Value::Array(listed.clone()))
            .ok_or(problem::internal_error())?;
        Ok(listed)
    }

    /// The Team's Routines, kept with the recording as the only ones it may replace, with the one the send selects.
    async fn listed(
        &self,
        request: &SegmentRequest,
        recording: &str,
    ) -> Result<Vec<Value>, Problem> {
        let routines = self.chat_routines(&request.team_id).await?;
        let field =
            |item: &Value, name: &str| as_str(at(item, name)).unwrap_or_default().to_owned();
        let named: Vec<(String, String)> = routines
            .iter()
            .map(|item| (field(item, "routine_id"), field(item, "name")))
            .collect();
        let revisions = routines
            .iter()
            .map(|item| (field(item, "routine_id"), at(item, "revision").clone()))
            .filter_map(|(id, revision)| {
                Some((id, team_protocol::python::value::small_int(&revision)?))
            })
            .collect();
        let segments = authored_segments(request.message.as_deref().unwrap_or_default());
        let reference = referred(&named, &segments, request.refine.as_deref());
        self.parts
            .routine
            .recordings
            .listed(&request.team_id, recording, revisions, &reference);
        Ok(routines)
    }

    /// What this segment's Brain sees of the Team's Routines and the recording span.
    pub(super) async fn routine_view(
        &self,
        request: &SegmentRequest,
    ) -> Result<RoutineView, Problem> {
        let Some(recording) = request.recording.as_deref() else {
            return Ok(RoutineView::default());
        };
        let mutable = request.continuation.is_none();
        let routines = if mutable {
            Some(self.listed(request, recording).await?)
        } else {
            None
        };
        // The span, read once after the listing kept the Routines this send's Brain is shown.
        let span = self
            .parts
            .routine
            .recordings
            .live(&request.team_id, Some(recording));
        let span_mode = routine_mode(span.as_ref());
        let Some(recorded) = span.filter(|_| mutable) else {
            return Ok(RoutineView {
                routines,
                span_mode,
                ..RoutineView::default()
            });
        };
        let selected = self.selected(&request.team_id, &recorded).await?;
        let zone = selected
            .as_ref()
            .map_or_else(|| zone(&recorded.sends).0, |(_, zone)| zone.clone());
        let turn_clock = date_at(recorded.started_at(), &zone).map(|date| (date.isoformat(), zone));
        Ok(RoutineView {
            routines,
            question: recorded.asked.as_ref().and_then(|asked| asked.wire.clone()),
            mode: span_mode,
            rerun: rerun_work(Some(&recorded)),
            schedule: stated_schedule(Some(&recorded)),
            output: stated_output(Some(&recorded)),
            settings_answer: settings_answer(Some(&recorded)),
            selected: selected.map(|(view, _)| view),
            turn_clock,
            span_mode,
        })
    }

    /// The Routine the span selects, as its Brain sees it, and its zone.
    async fn selected(
        &self,
        team_id: &str,
        span: &Span,
    ) -> Result<Option<(Value, String)>, Problem> {
        if span.selected.is_none() {
            return Ok(None);
        }
        let (routine, team, span) = (self.parts.routine.clone(), team_id.to_owned(), span.clone());
        tokio::task::spawn_blocking(move || routine.chat_selected(&team, &span))
            .await
            .map_err(|_| problem::internal_error())
    }

    /// The model a segment runs on: the one its start pinned, or for a start the Team's, except a chat turn about a
    /// Routine on a provider with a Routine model.
    pub(super) fn turn_model(
        request: &SegmentRequest,
        config_model: &str,
        span_mode: bool,
    ) -> String {
        if let Some(model) = &request.model {
            return model.clone();
        }
        let routed = ROUTINE_MODELS
            .iter()
            .find(|(provider, _)| *provider == request.credential.0)
            .filter(|_| span_mode);
        routed.map_or_else(|| config_model.to_owned(), |(_, model)| (*model).to_owned())
    }

    /// A completed recording turn's `record` or `refine`: the write that keeps its card, and the terminal member its
    /// reply carries; a card is refused whole when the terminal line would outgrow its bound.
    pub(super) fn routine_outcome(
        &self,
        turn: &Turn<'_>,
        proposed: Proposed<'_>,
        body: &Value,
        spent: Option<&Spent>,
    ) -> Result<Admitted, Problem> {
        let routine = &self.parts.routine;
        let admitted = match proposed {
            // A refinement of the selected Routine names its edits; any other outcome is a record (ADR-0122).
            Proposed::Call(value) if value.get("edits").is_some() => {
                routine.admit_refine(turn, value)?
            }
            proposed => routine.admit(turn, proposed, spent)?,
        };
        let carded = matches!(admitted.field.0, "routine_proposal" | "routine_refinement");
        let mut line = body.clone();
        if let Value::Object(members) = &mut line {
            members.push((admitted.field.0.to_owned(), admitted.field.1.clone()));
        }
        if carded && line_bytes(&line) > MAX_LINE_BYTES {
            return Ok(Admitted::refusal("routine-proposal-too-large"));
        }
        Ok(admitted)
    }
}
