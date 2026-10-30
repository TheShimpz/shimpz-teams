//! The recording span of a Team's Local chat, in Team memory only (`local/routine/recorder.py`, ADR-0101).
//!
//! A person's fresh request without files opens a send in the Team's span, or a new span when the last one is another
//! person's, another Team incarnation's, or older than 15 minutes. A span keeps each consecutive send of the person,
//! every successful Action call it made, and one protection set across them. When the chat agent calls `record`
//! within 15 minutes of the span's latest send, Team builds the plan from the span. A card or a refusal ends the span;
//! a question keeps it. The span is bounded: its oldest sends go whole to fit a new one, and a send that alone
//! outgrows a bound refuses the recording. Nothing here persists.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use strict_json::Value;
use team_action::executor::ActionRequest;
use team_assistant::effect::READ_ONLY;
use team_core::clock::Clock;
use team_core::entropy::{Entropy, token_hex};
use team_inference::runtime::ConversationEntry;
use team_inference::usage::Spent;
use team_protocol::payload::authored_segments;
use team_protocol::phrase;
use team_protocol::python::value::{int, object_of};
use team_protocol::routine_context::{MAX_RERUN_LITERAL_CHARS, canonical_rerun};
use team_protocol::routine_notice::MAX_CREATION_STEPS;
use team_protocol::routine_proposal::settings_answers;
use team_routine::json::json_text;
use team_routine::pin::action_pins;
use team_routine::recording::{
    Asked, Question, RERUN, Send, Slot, clarified_answer, latest_work,
    stated_output as span_output, stated_schedules,
};
use team_routine::reference::Referred;
use team_routine::trace::{self, Occurrence, Protection};
use team_storage::private::epoch_seconds;

use crate::chat::ports::ActiveAssistant;

/// How long a span stays open after its latest send.
pub const SPAN_SECONDS: i64 = 15 * 60;
/// The most sends a span keeps.
pub const MAX_SENDS: usize = 16;
/// The span's retained text: each send's message, its person-authored lines, its window lines, and its context.
pub const MAX_TEXT_BYTES: usize = 64 * 1024;
const SEND_CONTEXT_BYTES: usize = 128;

/// What the span's latest `record` call asked for: the Routine's name, what it replaces at the revision it was shown,
/// its description, and the send whose calls are its work.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Intent {
    /// The Routine's name.
    pub name: String,
    /// The Routine it replaces.
    pub replaces: Option<String>,
    /// The revision the replaced Routine was shown at.
    pub revision: Option<i64>,
    /// The description of the work.
    pub description: String,
    /// The send whose calls are its work, by id; `None` when a replacement keeps its steps.
    pub work: Option<String>,
}

/// What one send's turn spent: the finished Team steps it measured, and its usage once its reply committed.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Receipt {
    /// The finished Team steps, without their sequence numbers.
    pub steps: Vec<Value>,
    /// The usage, once its reply committed.
    pub spent: Option<Spent>,
}

/// One Team's recording span.
#[derive(Clone, Debug)]
pub struct Span {
    /// Its Team.
    pub team_id: String,
    /// The person whose sends it keeps.
    pub principal: String,
    /// The Team incarnation it began in.
    pub incarnation: String,
    /// The ids of its sends, oldest first.
    pub send_ids: Vec<String>,
    /// Its sends.
    pub sends: Vec<Send>,
    /// Every value it protects.
    pub protection: Protection,
    /// Why nothing recorded may define a Routine any more; `None` while it may.
    pub refused: Option<String>,
    /// The revision of every Routine the latest send's Brain was shown, by id.
    pub revisions: Vec<(String, i64)>,
    /// The question it last asked.
    pub asked: Option<Asked>,
    /// What the latest record call asked for.
    pub intent: Option<Intent>,
    /// What each send's turn spent, by send id.
    pub receipts: Vec<(String, Receipt)>,
    /// The one Routine the person's sends selected (ADR-0122).
    pub selected: Option<String>,
}

impl Span {
    fn new(team_id: &str, principal: &str, incarnation: &str) -> Self {
        Self {
            team_id: team_id.to_owned(),
            principal: principal.to_owned(),
            incarnation: incarnation.to_owned(),
            send_ids: Vec::new(),
            sends: Vec::new(),
            protection: Protection::default(),
            refused: None,
            revisions: Vec::new(),
            asked: None,
            intent: None,
            receipts: Vec::new(),
            selected: None,
        }
    }

    /// The id of its latest send.
    #[must_use]
    pub fn recording_id(&self) -> &str {
        self.send_ids.last().map_or("", String::as_str)
    }

    /// When its latest send started.
    #[must_use]
    pub fn started_at(&self) -> i64 {
        self.sends.last().map_or(0, |send| send.started_at)
    }
}

/// A new send: its message, the conversation window it carried, its browser timezone, and its interface language.
#[derive(Clone, Debug)]
pub struct Started {
    /// The message.
    pub message: String,
    /// The committed history window.
    pub conversation: Vec<ConversationEntry>,
    /// The browser's zone.
    pub timezone: Option<String>,
    /// The chat's interface language.
    pub locale: Option<String>,
}

fn text_bytes(send: &Send) -> usize {
    let texts = std::iter::once(&send.message)
        .chain(&send.person)
        .chain(&send.window);
    texts.map(String::len).sum::<usize>() + SEND_CONTEXT_BYTES
}

fn fits(sends: &[Send]) -> bool {
    let occurrences = sends.iter().flat_map(|send| &send.occurrences);
    let (count, size) =
        occurrences.fold((0, 0), |(count, size), item| (count + 1, size + item.size));
    sends.len() <= MAX_SENDS
        && sends.iter().map(text_bytes).sum::<usize>() <= MAX_TEXT_BYTES
        && count <= trace::MAX_OCCURRENCES
        && size <= trace::MAX_TRACE_BYTES
}

/// The span with its oldest sends gone until it fits; one whose latest send alone does not fit is refused.
fn bounded(mut span: Span) -> Span {
    let mut gone = 0;
    while span.sends.len() > 1 && !fits(&span.sends) {
        span.sends.remove(0);
        span.send_ids.remove(0);
        gone += 1;
    }
    let kept = span.send_ids.clone();
    span.receipts.retain(|(id, _)| kept.contains(id));
    if !fits(&span.sends) {
        span.refused = Some("routine-recording-too-large".to_owned());
        return span;
    }
    // The question counted the sends before it; those that went leave the count too.
    if gone > 0
        && let Some(asked) = &mut span.asked
    {
        asked.after = asked.after.saturating_sub(gone);
        asked.repair_from = asked.repair_from.map(|from| from.saturating_sub(gone));
    }
    span
}

/// The person's own earlier sends in a conversation window: never the assistant's, and never a truncated one.
#[must_use]
pub fn earlier_sends(conversation: &[ConversationEntry]) -> Vec<String> {
    conversation
        .iter()
        .filter(|entry| entry.role == "user" && !entry.truncated)
        .map(|entry| entry.text.clone())
        .collect()
}

fn send_of(started: &Started, now: i64) -> Send {
    let window = earlier_sends(&started.conversation)
        .iter()
        .flat_map(|text| authored_segments(text))
        .flat_map(|segment| segment.split('\n').map(str::to_owned).collect::<Vec<_>>())
        .collect();
    Send {
        message: started.message.clone(),
        person: authored_segments(&started.message),
        window,
        timezone: started.timezone.clone(),
        started_at: now,
        locale: started.locale.clone(),
        occurrences: Vec::new(),
    }
}

/// Every Team's recording span, at most one each.
pub struct RecordingBook {
    clock: Arc<dyn Clock>,
    entropy: Arc<dyn Entropy>,
    spans: Mutex<HashMap<String, Span>>,
}

impl std::fmt::Debug for RecordingBook {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("RecordingBook")
    }
}

/// No send id could be drawn.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Undrawn;

impl RecordingBook {
    /// An empty book on `clock`.
    #[must_use]
    pub fn new(clock: Arc<dyn Clock>, entropy: Arc<dyn Entropy>) -> Self {
        Self {
            clock,
            entropy,
            spans: Mutex::new(HashMap::new()),
        }
    }

    fn spans(&self) -> MutexGuard<'_, HashMap<String, Span>> {
        self.spans.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Opens a send in the Team's span, or in a new span when the last one is not this person's or is stale.
    ///
    /// # Errors
    /// [`Undrawn`] when no send id could be drawn.
    pub fn start(
        &self,
        team_id: &str,
        binding: (&str, &str),
        started: &Started,
        now: i64,
    ) -> Result<String, Undrawn> {
        let (principal, incarnation) = binding;
        let send_id = token_hex(self.entropy.as_ref(), 16).map_err(|_| Undrawn)?;
        let send = send_of(started, now);
        let mut spans = self.spans();
        let found = spans.remove(team_id).filter(|found| {
            found.refused.is_none()
                && (found.principal.as_str(), found.incarnation.as_str())
                    == (principal, incarnation)
                && now - found.started_at() <= SPAN_SECONDS
        });
        let mut span = found.unwrap_or_else(|| Span::new(team_id, principal, incarnation));
        // A send whose reply never committed spends nothing a creation shows, so its steps go as the next opens.
        for (_, receipt) in &mut span.receipts {
            if receipt.spent.is_none() {
                *receipt = Receipt::default();
            }
        }
        span.send_ids.push(send_id.clone());
        span.sends.push(send);
        span.revisions.clear();
        span.receipts.push((send_id.clone(), Receipt::default()));
        spans.insert(team_id.to_owned(), bounded(span));
        Ok(send_id)
    }

    /// The Team's span whose latest send has exactly this id.
    #[must_use]
    pub fn get(&self, team_id: &str, recording_id: Option<&str>) -> Option<Span> {
        let spans = self.spans();
        let found = spans.get(team_id)?;
        (Some(found.recording_id()) == recording_id).then(|| found.clone())
    }

    /// The span to record from: its latest send has exactly this id and started at most 15 minutes ago.
    #[must_use]
    pub fn live(&self, team_id: &str, recording_id: Option<&str>) -> Option<Span> {
        let now = epoch_seconds(self.clock.as_ref());
        self.get(team_id, recording_id)
            .filter(|found| now - found.started_at() <= SPAN_SECONDS)
    }

    fn change(&self, team_id: &str, recording_id: &str, change: impl FnOnce(Span) -> Span) {
        let mut spans = self.spans();
        if spans
            .get(team_id)
            .is_some_and(|found| found.recording_id() == recording_id)
            && let Some(found) = spans.remove(team_id)
        {
            spans.insert(team_id.to_owned(), change(found));
        }
    }

    /// Protects more of the span's values; a protection past its bound is lost, and so is the recording.
    pub fn protect(&self, team_id: &str, recording_id: &str, values: &[String]) {
        self.change(team_id, recording_id, |mut found| {
            found.protection = found.protection.grow(values.iter().map(String::as_str));
            found
        });
    }

    /// Keeps the Routines, by id and revision, the send's Brain was shown, and the one it selects: a send that names
    /// none keeps what an earlier send of the span selected, while that Routine is still listed.
    pub fn listed(
        &self,
        team_id: &str,
        recording_id: &str,
        shown: Vec<(String, i64)>,
        reference: &Referred,
    ) {
        self.change(team_id, recording_id, |mut found| {
            let selected = match reference {
                Referred::Selected(routine_id) => Some(routine_id.clone()),
                Referred::Unresolved => None,
                Referred::Nothing => found.selected.clone(),
            };
            let listed = selected
                .as_ref()
                .is_some_and(|id| shown.iter().any(|(routine, _)| routine == id));
            found.selected = selected.filter(|_| listed);
            found.revisions = shown;
            found
        });
    }

    /// Keeps one successful Action call in the send in progress; a send past its bound refuses the recording.
    pub fn occurred(&self, team_id: &str, recording_id: &str, occurrence: Occurrence) {
        self.change(team_id, recording_id, |mut found| {
            if found.refused.is_some() {
                return found;
            }
            if let Some(latest) = found.sends.last_mut() {
                latest.occurrences.push(occurrence);
            }
            bounded(found)
        });
    }

    /// Keeps one finished Team step of the send in progress, while the span holds fewer than it may show.
    pub fn stepped(&self, team_id: &str, recording_id: &str, event: &Value) {
        if event.get("state") != Some(&Value::String("finished".to_owned())) {
            return;
        }
        let Value::Object(members) = event else {
            return;
        };
        let step = Value::Object(
            members
                .iter()
                .filter(|(name, _)| name != "seq")
                .cloned()
                .collect(),
        );
        self.change(team_id, recording_id, |mut found| {
            if creation_steps(&found).len() < MAX_CREATION_STEPS
                && let Some((_, latest)) = found.receipts.last_mut()
            {
                latest.steps.push(step);
            }
            found
        });
    }

    /// Keeps the usage of the send whose reply just committed.
    pub fn committed(&self, team_id: &str, recording_id: &str, spent: Spent) {
        self.change(team_id, recording_id, |mut found| {
            if let Some((_, latest)) = found.receipts.last_mut() {
                latest.spent = Some(spent);
            }
            found
        });
    }

    /// Keeps what the span asked, what it binds and settled, and the intent the person's answer records again.
    pub fn asked(&self, team_id: &str, recording_id: &str, question: &Question, intent: Intent) {
        self.change(team_id, recording_id, |mut found| {
            found.asked = Some(Asked {
                code: question.code.clone(),
                after: found.sends.len(),
                pending: question.pending.clone(),
                manifest: question.manifest.clone(),
                wire: Some(question.wire()),
                chosen: question.chosen.clone(),
                repair_from: question.repair_from,
            });
            found.intent = Some(intent);
            found
        });
    }

    /// Refuses the span's recording for good.
    pub fn refuse(&self, team_id: &str, recording_id: &str, code: &str) {
        self.change(team_id, recording_id, |mut found| {
            found.refused.get_or_insert_with(|| code.to_owned());
            found
        });
    }

    /// Ends the Team's span after the card or refusal its latest send made.
    pub fn finish(&self, team_id: &str, recording_id: &str) {
        let mut spans = self.spans();
        if spans
            .get(team_id)
            .is_some_and(|found| found.recording_id() == recording_id)
        {
            spans.remove(team_id);
        }
    }

    /// Drops the Team's span.
    pub fn drop_team(&self, team_id: &str) {
        self.spans().remove(team_id);
    }

    /// Drops every span.
    pub fn clear(&self) {
        self.spans().clear();
    }
}

/// The receipts a creation from the span shows: every committed earlier send's, then the send in progress.
fn shown(span: &Span) -> Vec<&Receipt> {
    let Some(((_, latest), earlier)) = span.receipts.split_last() else {
        return Vec::new();
    };
    let mut receipts: Vec<&Receipt> = earlier
        .iter()
        .map(|(_, receipt)| receipt)
        .filter(|receipt| receipt.spent.is_some())
        .collect();
    receipts.push(latest);
    receipts
}

/// The Team steps a Routine created now from the span lists, numbered from 1, within their bound.
#[must_use]
pub fn creation_steps(span: &Span) -> Vec<Value> {
    shown(span)
        .iter()
        .flat_map(|receipt| receipt.steps.iter())
        .take(MAX_CREATION_STEPS)
        .enumerate()
        .map(|(index, step)| {
            let mut members = vec![(
                "seq".to_owned(),
                int(i64::try_from(index + 1).unwrap_or(i64::MAX)),
            )];
            if let Value::Object(fields) = step {
                members.extend(fields.iter().cloned());
            }
            Value::Object(members)
        })
        .collect()
}

/// What a Routine created from the span shows it spent: its sends' summed usage, and their Team steps; `current` is
/// the usage of the send in progress, whose reply carries the card.
#[must_use]
pub fn creation(span: &Span, current: Option<&Spent>) -> (Option<Value>, Vec<Value>) {
    let receipts = shown(span);
    let mut spent: Vec<Spent> = receipts[..receipts.len().saturating_sub(1)]
        .iter()
        .filter_map(|receipt| receipt.spent.clone())
        .collect();
    spent.extend(current.cloned());
    (Spent::total(&spent).wire(), creation_steps(span))
}

fn stated(text: &str) -> Vec<Value> {
    phrase::stated(text, None).unwrap_or_default()
}

/// The intent to record again when the span's latest send is Admin's composed answer that binds its target question.
#[must_use]
pub fn answered(span: Option<&Span>) -> Option<Intent> {
    let span = span?;
    let (intent, asked) = (span.intent.as_ref()?, span.asked.as_ref()?);
    if span.refused.is_some() {
        return None;
    }
    let (_original, _question, answer) = clarified_answer(&span.sends.last()?.message)?;
    if !stated(&answer).is_empty() {
        return None;
    }
    let pending = asked.pending.as_ref()?;
    let bound = asked.code == "routine-binding-ambiguous"
        && pending
            .targets
            .iter()
            .any(|target| answer == json_text(&target.value));
    bound.then(|| intent.clone())
}

/// The stored intent a record call continues: one whose send ran no Action and replaces exactly what it replaces.
#[must_use]
pub fn continued(span: &Span, replaces: Option<&str>) -> Option<Intent> {
    let stored = span.intent.as_ref()?;
    let ran = span
        .sends
        .last()
        .is_some_and(|send| !send.occurrences.is_empty());
    (!ran && stored.replaces.as_deref() == replaces).then(|| stored.clone())
}

/// The send whose calls a new intent's work is, by id.
#[must_use]
pub fn selected_work(span: &Span, replaces: Option<&str>) -> Option<String> {
    if span
        .sends
        .last()
        .is_some_and(|send| !send.occurrences.is_empty())
    {
        return Some(span.recording_id().to_owned());
    }
    let index = if replaces.is_some() {
        None
    } else {
        latest_work(&span.sends)
    };
    index.map(|index| span.send_ids[index].clone())
}

/// Whether the span's latest send is exactly the person's pick of one fixed answer to a settings question.
#[must_use]
pub fn settings_answer(span: Option<&Span>) -> bool {
    let Some(asked) = span.and_then(|span| span.asked.as_ref()) else {
        return false;
    };
    let Some(offered) = settings_answers(&asked.code) else {
        return false;
    };
    let Some((_, _, answer)) = span
        .and_then(|span| span.sends.last())
        .and_then(|send| clarified_answer(&send.message))
    else {
        return false;
    };
    let seconds = match asked.wire.as_ref().and_then(|wire| wire.get("value")) {
        Some(value) => json_text(value),
        None => "None".to_owned(),
    };
    offered
        .iter()
        .any(|option| option.replace("{seconds}", &seconds) == answer)
}

/// The one schedule the span's person states.
#[must_use]
pub fn stated_schedule(span: Option<&Span>) -> Option<Value> {
    let found = span
        .map(|span| stated_schedules(&span.sends))
        .unwrap_or_default();
    (found.len() == 1).then(|| found[0].clone())
}

/// The one output mode the span's person states.
#[must_use]
pub fn stated_output(span: Option<&Span>) -> Option<String> {
    span.and_then(|span| span_output(&span.sends))
        .map(str::to_owned)
}

/// Whether the Brain should treat this chat as about a Routine: advisory, read with no model.
#[must_use]
pub fn routine_mode(span: Option<&Span>) -> bool {
    let Some(span) = span else { return false };
    if span.intent.is_some() || span.asked.is_some() || span.selected.is_some() {
        return true;
    }
    span.sends
        .iter()
        .flat_map(|send| &send.person)
        .any(|segment| {
            !stated(segment).is_empty() || phrase::requests_routine(segment).unwrap_or(false)
        })
}

/// Whether the person already chose `text` as the target of this slot's `member`.
fn chosen_target(span: &Span, slot: &Slot, member: &str, text: &str) -> bool {
    let chosen = span.asked.iter().flat_map(|asked| &asked.chosen);
    chosen.into_iter().any(|binding| {
        let picked = binding
            .chosen
            .as_ref()
            .map_or_else(|| "null".to_owned(), json_text);
        (&binding.action, binding.member.as_str()) == (&slot.action, member) && picked == text
    })
}

fn rerun_input(span: &Span, slot: &Slot, (member, kind, value): &(String, String, Value)) -> Value {
    let field = |value: Value, chosen: bool, source: Value| {
        object_of(vec![
            ("member", Value::String(member.clone())),
            ("kind", Value::String(kind.clone())),
            ("value", value),
            ("chosen", Value::Bool(chosen)),
            ("source", source),
        ])
    };
    match kind.as_str() {
        "value" => {
            let text = json_text(value);
            let visible = text.chars().count() <= MAX_RERUN_LITERAL_CHARS
                && !trace::exposes(value, &span.protection.values);
            let chosen = chosen_target(span, slot, member, &text);
            field(
                if visible {
                    Value::String(text)
                } else {
                    Value::Null
                },
                chosen,
                Value::Null,
            )
        }
        "fresh" => {
            let found = slot
                .sources
                .iter()
                .find(|(name, _)| name == member)
                .map(|(_, action)| action);
            let source = found.map_or(Value::Null, |(assistant, action)| {
                object_of(vec![
                    ("assistant", Value::String(assistant.clone())),
                    ("action", Value::String(action.clone())),
                ])
            });
            field(Value::Null, false, source)
        }
        _ => field(Value::Null, false, Value::Null),
    }
}

/// The work a pending rerun question asks the Brain to run again, in order, bounded and redacted.
#[must_use]
pub fn rerun_work(span: Option<&Span>) -> Option<Vec<Value>> {
    let span = span?;
    let asked = span.asked.as_ref().filter(|asked| asked.code == RERUN)?;
    let manifest = asked.manifest.as_ref()?;
    if span.protection.lost {
        return None;
    }
    let mut entries: Vec<(Value, i64)> = Vec::new();
    for slot in &manifest.slots {
        let entry = object_of(vec![
            ("assistant", Value::String(slot.action.0.clone())),
            ("action", Value::String(slot.action.1.clone())),
            (
                "inputs",
                Value::Array(
                    slot.inputs
                        .iter()
                        .map(|item| rerun_input(span, slot, item))
                        .collect(),
                ),
            ),
        ]);
        match entries.last_mut() {
            Some((last, count)) if *last == entry => *count += 1,
            _ => entries.push((entry, 1)),
        }
    }
    let shown: Vec<Value> = entries
        .into_iter()
        .map(|(entry, count)| {
            let Value::Object(members) = entry else {
                return entry;
            };
            let mut members = members;
            members.insert(2, ("count".to_owned(), int(count)));
            Value::Object(members)
        })
        .collect();
    canonical_rerun(&Value::Array(shown.clone()))?;
    Some(shown)
}

/// The Action results a recording send returns to its Brain: while the span selects a Routine, each traced call's
/// result is named by its Team handle `c{k}` (ADR-0122).
#[must_use]
pub fn labeled(span: Option<&Span>, results: Value) -> Value {
    let Some(span) = span.filter(|span| span.selected.is_some()) else {
        return results;
    };
    let Value::Object(members) = results else {
        return results;
    };
    let occurrences = span
        .sends
        .last()
        .map(|send| send.occurrences.as_slice())
        .unwrap_or_default();
    let handle = |interrupt: &str| {
        occurrences
            .iter()
            .position(|item| item.interrupt == interrupt)
            .map(|place| format!("c{}", place + 1))
    };
    Value::Object(
        members
            .into_iter()
            .map(|(interrupt, value)| match handle(&interrupt) {
                Some(call) => {
                    let wrapped = object_of(vec![("call", Value::String(call)), ("result", value)]);
                    (interrupt, wrapped)
                }
                None => (interrupt, value),
            })
            .collect(),
    )
}

/// Keeps one successful call of a recording send as an occurrence: its result's secret strings are protected first, and
/// then its input and result are kept under the span's whole protection. A result whose secret positions cannot be
/// read refuses the recording instead, so nothing unprotected is ever kept.
///
/// The values Team injects into an attempt stay inside the Action executor; a result never echoes one (its projection
/// refuses that), so the span protects the Stored Input values the caller hands it with [`RecordingBook::protect`].
pub fn recorded(
    book: &RecordingBook,
    recording: (&str, &str),
    active: &ActiveAssistant,
    request: &ActionRequest,
    result: &Value,
) {
    let (team_id, recording_id) = recording;
    if occurrence(book, recording, active, request, result).is_none() {
        book.refuse(team_id, recording_id, "routine-recording-unverified");
    }
}

fn occurrence(
    book: &RecordingBook,
    (team_id, recording_id): (&str, &str),
    active: &ActiveAssistant,
    request: &ActionRequest,
    result: &Value,
) -> Option<()> {
    let action = active.action(&request.action)?;
    let pin = action_pins(&active.pinned, [request.action.as_str()])?.remove(&request.action)?;
    let secrets = trace::secret_values(result, &action.output_schema).ok()?;
    book.protect(team_id, recording_id, &secrets);
    let protected: Vec<String> = book
        .get(team_id, Some(recording_id))
        .map(|found| found.protection.values.into_iter().collect())
        .unwrap_or_default();
    let input = trace::keep(&request.input, &action.input_schema, &protected).ok()?;
    let output = trace::keep(result, &action.output_schema, &protected).ok()?;
    let read_only = action.effect == READ_ONLY;
    let mut occurrence = Occurrence::new(
        &request.assistant_id,
        &request.action,
        &pin,
        read_only,
        input,
        output,
    );
    occurrence.interrupt.clone_from(&request.interrupt_id);
    book.occurred(team_id, recording_id, occurrence);
    Some(())
}

#[cfg(test)]
mod tests;
