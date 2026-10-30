//! Team's deterministic recording of a Routine plan from a person's recent chat sends, with no model (ADR-0101).
//!
//! This module holds the recording's inputs, questions, and results; [`crate::compose`] builds the plan, while
//! [`crate::provenance`] classifies each input and [`crate::rerun`] names the work a question asks to run again. The
//! chat agent runs the recurring work and calls `record`; Team builds the plan from its own memory-only trace of the
//! recording span: the person's consecutive fresh sends in one Team incarnation, each with its own message, the
//! person's own words in it, the untruncated earlier sends its conversation window carried, its browser timezone, its
//! start, and its successful Action calls in dispatch order.
//!
//! The work is the calls of the one send the record call's intent selected. Earlier sends only provide sources. Each
//! top-level input member of a plan call is classified by the first rule that applies:
//!
//! 1. a secret (withheld, credential-shaped, protected by the span, or bound for a secret destination) refuses;
//! 2. a non-empty string occurring in one line the person wrote, or a number whose JSON text is a whole token of one,
//!    is a literal the person named;
//! 3. the date its own send started on in the Routine's zone is the run date, which each run reads in that zone; the
//!    UTC date of a send that started on another date there is a fixed literal;
//! 4. a long string, a long integer, or a non-empty container is copied from the one call result in the span holding
//!    it: from its one position outside arrays, or through the array item whose single member the person named and no
//!    other item shares; never by an index. An identifier no result holds, or a value inside an array or several
//!    positions that no named member separates, is asked about and never frozen;
//! 5. text no result holds that writes dates near its send's date is a template whose dates each run writes for its
//!    own date ([`crate::dates`]); a date the person wrote stays as written;
//! 6. anything else is a literal the assistant chose, the same on every run.
//!
//! The schedule, output, and timezone are the person's own, read by the protocol's phrase tables: the latest segment
//! that states each one, else the latest send's browser zone. What cannot be read is asked, never guessed.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

use strict_json::Value;
use team_protocol::payload::{CLARIFICATION_LABELS, compose_clarified};
use team_protocol::phrase;
use team_protocol::python::text::strip;
use team_protocol::routine::CONVENTIONAL_TIMEZONE;
use team_protocol::routine_context::NUMBER_TOKEN_RE;

use crate::json::{canonical, int, json_text, object, text};
use crate::plan::Contracts;
use crate::trace::{Occurrence, Protection};

/// The one question only work run again answers: a value no result provides, or a target the person chose instead.
pub const RERUN: &str = "routine-work-rerun";
/// The shortest text a selector's named member holds.
pub const MIN_SELECTOR_CHARS: usize = 2;

/// The original request, question, and answer of a message that is exactly Admin's composed clarification answer.
#[must_use]
pub fn clarified_answer(message: &str) -> Option<(String, String, String)> {
    let lines: Vec<&str> = message.split('\n').collect();
    if lines.len() < 4 {
        return None;
    }
    let (asked_line, given_line) = (lines[lines.len() - 2], lines[lines.len() - 1]);
    for (locale, question_label, answer_label) in CLARIFICATION_LABELS {
        let (Some(asked), Some(given)) = (
            asked_line
                .strip_prefix(question_label)
                .and_then(|rest| rest.strip_prefix(": ")),
            given_line
                .strip_prefix(answer_label)
                .and_then(|rest| rest.strip_prefix(": ")),
        ) else {
            continue;
        };
        let original = lines[..lines.len() - 3].join("\n");
        if !strip(&original).is_empty()
            && !strip(given).is_empty()
            && compose_clarified(&original, asked, given, locale).as_deref() == Some(message)
        {
            return Some((
                strip(&original).to_owned(),
                asked.to_owned(),
                given.to_owned(),
            ));
        }
    }
    None
}

/// The recording was refused; `code` is the stable reason.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RecordingError {
    /// The stable reason.
    pub code: &'static str,
}

impl RecordingError {
    /// A refusal for this reason.
    #[must_use]
    pub const fn new(code: &'static str) -> Self {
        Self { code }
    }
}

impl fmt::Display for RecordingError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code)
    }
}

impl std::error::Error for RecordingError {}

/// One fresh send of the person in a recording span.
#[derive(Clone, Debug, PartialEq)]
pub struct Send {
    /// The message.
    pub message: String,
    /// The person's authored segments of the message, in order: its original text, then each composed answer.
    pub person: Vec<String>,
    /// The person's own lines of each untruncated user entry of the conversation window the request carried.
    pub window: Vec<String>,
    /// The browser's validated IANA zone, or `None`.
    pub timezone: Option<String>,
    /// When it started.
    pub started_at: i64,
    /// The chat's interface language, or `None`: it decides how a date the recorded work wrote is read.
    pub locale: Option<String>,
    /// Its successful Action calls in dispatch order.
    pub occurrences: Vec<Occurrence>,
}

/// One target of a choice: its value, and the label a person reads beside it.
#[derive(Clone, Debug, PartialEq)]
pub struct Target {
    /// The value an input would take.
    pub value: Value,
    /// Its label, if any.
    pub label: Option<String>,
}

impl Target {
    /// A target of this value and label.
    #[must_use]
    pub fn new(value: Value, label: Option<&str>) -> Self {
        Self {
            value,
            label: label.map(str::to_owned),
        }
    }
}

/// An Assistant and Action.
pub type ActionKey = (String, String);

/// The choice a target question binds: the Action input it asked about, and every target that input may take.
///
/// The choice is one logical target: an answer binds every occurrence of that input whose value is one of these
/// targets. Once the person chose a target, `chosen` holds it, and the work run again must send exactly that target
/// wherever it sent one of them.
#[derive(Clone, Debug, PartialEq)]
pub struct Pending {
    /// The Action the question is about.
    pub action: ActionKey,
    /// The input member it asked about.
    pub member: String,
    /// Every target the input may take.
    pub targets: Vec<Target>,
    /// The target the person chose, once chosen.
    pub chosen: Option<Value>,
}

impl Pending {
    /// Whether two choices are the same: the same input, offering the same targets.
    #[must_use]
    pub fn same_choice(&self, other: &Self) -> bool {
        self.action == other.action
            && self.member == other.member
            && self.targets.len() == other.targets.len()
            && self
                .targets
                .iter()
                .zip(&other.targets)
                .all(|(left, right)| {
                    team_protocol::python::value::py_eq(&left.value, &right.value)
                        && left.label == right.label
                })
    }

    /// Whether this choice offers a value whose JSON text is `text`.
    #[must_use]
    pub fn offers(&self, text: &str) -> bool {
        self.targets
            .iter()
            .any(|target| json_text(&target.value) == text)
    }
}

/// One call a rerun must make: its Action, and what each input member must be.
///
/// Each input is `(member, kind, value)`: `value` must be sent exactly; `clock` must be the date its own send started on,
/// in the Routine's own zone; `fresh` must be a value one of the rerun send's own results holds, and `value` then keeps
/// the value the work sent. `sources` names, for a fresh member, the Action of the earliest eligible source that
/// returned its value, never the call itself.
#[derive(Clone, Debug, PartialEq)]
pub struct Slot {
    /// The Action.
    pub action: ActionKey,
    /// What each input member must be.
    pub inputs: Vec<(String, String, Value)>,
    /// The source Action of each fresh member.
    pub sources: Vec<(String, ActionKey)>,
}

/// The work a rerun question asks to run again, one slot per occurrence, in dispatch order.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Manifest {
    /// Its slots.
    pub slots: Vec<Slot>,
}

/// The question a span last asked, which the person's later sends may answer.
#[derive(Clone, Debug, PartialEq)]
pub struct Asked {
    /// Its code.
    pub code: String,
    /// How many sends the span held when it asked: only later sends answer it.
    pub after: usize,
    /// The choice it binds, if a target question.
    pub pending: Option<Pending>,
    /// The work it asks to run again, if a rerun question.
    pub manifest: Option<Manifest>,
    /// The question as the person was asked it, which the Brain sees beside a freely typed answer.
    pub wire: Option<Value>,
    /// Every target choice the person already answered, each bound until the span ends.
    pub chosen: Vec<Pending>,
    /// How many sends the span held when it first asked for work to run again.
    pub repair_from: Option<usize>,
}

impl Asked {
    /// A question of this code asked after `after` sends, with nothing else kept.
    #[must_use]
    pub fn new(code: &str, after: usize) -> Self {
        Self {
            code: code.to_owned(),
            after,
            pending: None,
            manifest: None,
            wire: None,
            chosen: Vec::new(),
            repair_from: None,
        }
    }
}

/// What Team asks the person before a card.
#[derive(Clone, Debug)]
pub struct Question {
    /// Its code.
    pub code: String,
    /// The targets it offers.
    pub options: Vec<Target>,
    /// Its numeric value, for a question that carries one.
    pub value: Option<i64>,
    /// The choice a target question binds, which only Team keeps.
    pub pending: Option<Pending>,
    /// The work a rerun must repeat, which only Team keeps.
    pub manifest: Option<Manifest>,
    /// Every choice already answered, which only Team keeps.
    pub chosen: Vec<Pending>,
    /// When the span first asked for its one repair, which only Team keeps.
    pub repair_from: Option<usize>,
}

impl PartialEq for Question {
    fn eq(&self, other: &Self) -> bool {
        (&self.code, &self.options, self.value) == (&other.code, &other.options, other.value)
    }
}

impl Question {
    /// A question of this code offering these targets.
    #[must_use]
    pub fn new(code: &str, options: Vec<Target>) -> Self {
        Self {
            code: code.to_owned(),
            options,
            value: None,
            pending: None,
            manifest: None,
            chosen: Vec::new(),
            repair_from: None,
        }
    }

    /// The question as the chat reply carries it: each target as its exact JSON text, which no client rounds.
    #[must_use]
    pub fn wire(&self) -> Value {
        let options = self
            .options
            .iter()
            .map(|target| {
                object(vec![
                    ("value", text(&json_text(&target.value))),
                    ("label", target.label.as_deref().map_or(Value::Null, text)),
                ])
            })
            .collect();
        object(vec![
            ("code", text(&self.code)),
            ("options", Value::Array(options)),
            ("value", self.value.map_or(Value::Null, int)),
        ])
    }
}

/// Why a recording step stopped short of a plan: a question to ask first, or a refusal.
#[derive(Clone, Debug, PartialEq)]
pub enum Stop {
    /// Ask the person first.
    Ask(Box<Question>),
    /// The recording is refused.
    Refuse(RecordingError),
}

impl From<RecordingError> for Stop {
    fn from(error: RecordingError) -> Self {
        Self::Refuse(error)
    }
}

impl Stop {
    /// A question of this code.
    #[must_use]
    pub fn ask(code: &str) -> Self {
        Self::Ask(Box::new(Question::new(code, Vec::new())))
    }

    /// A refusal of this code.
    #[must_use]
    pub const fn refuse(code: &'static str) -> Self {
        Self::Refuse(RecordingError::new(code))
    }
}

/// A replaced Routine: its plan, and the schedule a replacement keeps unless the person states another.
#[derive(Clone, Debug, PartialEq)]
pub struct Existing {
    /// Its plan document.
    pub plan: Value,
    /// Its schedule.
    pub schedule: Value,
}

/// Each step's input origins, by step id and member, in the plan's order.
pub type Origins = Vec<(String, Vec<(String, String)>)>;

/// The recorded plan document (its zone included), each input's origin, the permitted Actions, and when it runs.
///
/// It also keeps what the span settled, which a question Team still asks about the plan carries on: every target
/// choice the person answered, and when the span first asked for its one repair.
#[derive(Clone, Debug, PartialEq)]
pub struct Recorded {
    /// The plan document.
    pub document: Value,
    /// Each step's input origins.
    pub origins: Origins,
    /// Every Action the plan may call at its pin.
    pub permitted: Vec<Value>,
    /// The schedule.
    pub schedule: Value,
    /// Where its timezone came from.
    pub timezone_source: String,
    /// Every target choice the person answered.
    pub chosen: Vec<Pending>,
    /// When the span first asked for its one repair.
    pub repair_from: Option<usize>,
}

impl Recorded {
    /// The origin of one step's input member.
    #[must_use]
    pub fn origin(&self, step: &str, member: &str) -> Option<&str> {
        self.origins
            .iter()
            .find(|(id, _)| id == step)
            .and_then(|(_, members)| members.iter().find(|(name, _)| name == member))
            .map(|(_, origin)| origin.as_str())
    }
}

/// What a recording span ends in: a plan, or the question to ask first.
#[derive(Clone, Debug, PartialEq)]
pub enum Recording {
    /// The plan.
    Recorded(Box<Recorded>),
    /// The question.
    Asked(Box<Question>),
}

/// The person's texts, with every whole number token each one writes on its own, and the values the span protects.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Known {
    /// The person's lines.
    pub texts: Vec<String>,
    /// Every whole number token they write.
    pub numbers: BTreeSet<String>,
    /// The values the span protects.
    pub protected: BTreeSet<String>,
}

impl Known {
    /// Whether the person named the value: a non-empty string inside one of their lines, or a number whose JSON text
    /// is one of their whole number tokens; never a boolean.
    #[must_use]
    pub fn names(&self, value: &Value) -> bool {
        match value {
            Value::String(written) => {
                !written.is_empty()
                    && self
                        .texts
                        .iter()
                        .any(|line| line.contains(written.as_str()))
            }
            Value::Number(_) => String::from_utf8(canonical(value))
                .is_ok_and(|number| self.numbers.contains(&number)),
            _ => false,
        }
    }
}

/// The person's texts, with every whole number token each one writes on its own.
#[must_use]
pub fn known_texts(texts: &[String], protection: &Protection) -> Known {
    let numbers = texts
        .iter()
        .flat_map(|line| {
            NUMBER_TOKEN_RE
                .finditer(line)
                .unwrap_or_default()
                .into_iter()
                .map(move |found| line[found.start..found.end].to_owned())
        })
        .collect();
    Known {
        texts: texts.to_vec(),
        numbers,
        protected: protection.values.clone(),
    }
}

/// One call of the span: its global dispatch index, its send, and its occurrence.
#[derive(Clone, Debug, PartialEq)]
pub struct Call {
    /// Its dispatch index in the span.
    pub index: usize,
    /// Its send's position in the span.
    pub send: usize,
    /// Its occurrence.
    pub occurrence: Occurrence,
}

impl Call {
    /// Its Assistant and Action.
    #[must_use]
    pub fn action(&self) -> ActionKey {
        (
            self.occurrence.assistant.clone(),
            self.occurrence.action.clone(),
        )
    }

    /// Whether its reviewed contract proves it read-only.
    #[must_use]
    pub const fn read_only(&self) -> bool {
        self.occurrence.read_only
    }
}

/// Each plan call's classified inputs, as plan sources (a copied value's source naming its call as `node`), and their
/// origins.
pub type Classified = (Vec<(String, Value)>, Vec<(String, String)>);

/// Everything a recording reads and what it derives while it plans.
#[derive(Clone, Debug)]
pub struct Context<'a> {
    /// The span's sends.
    pub sends: &'a [Send],
    /// Every call of the span in dispatch order.
    pub calls: Vec<Call>,
    /// The person's texts and the protected values.
    pub known: Known,
    /// The Routine's zone and where it came from.
    pub zone: (String, String),
    /// The question the span last asked.
    pub asked: Option<&'a Asked>,
    /// The exact current contracts.
    pub contracts: &'a Contracts,
    /// The dispatch indexes of the work's calls, once the work is narrowed to what the person chose.
    pub work: Vec<usize>,
    /// Every call's source representative.
    pub classes: BTreeMap<usize, usize>,
    /// Each plan call's classified inputs and their origins.
    pub inputs: BTreeMap<usize, Classified>,
}

impl<'a> Context<'a> {
    /// A context over a span, before any call is classified.
    #[must_use]
    pub fn new(
        sends: &'a [Send],
        calls: Vec<Call>,
        known: Known,
        zone: (String, String),
        asked: Option<&'a Asked>,
        contracts: &'a Contracts,
    ) -> Self {
        Self {
            sends,
            calls,
            known,
            zone,
            asked,
            contracts,
            work: Vec::new(),
            classes: BTreeMap::new(),
            inputs: BTreeMap::new(),
        }
    }

    /// A call's source representative.
    #[must_use]
    pub fn class(&self, index: usize) -> usize {
        self.classes.get(&index).copied().unwrap_or(index)
    }
}

/// The position of the latest send that ran any Action, or `None` when none did.
#[must_use]
pub fn latest_work(sends: &[Send]) -> Option<usize> {
    sends.iter().rposition(|send| !send.occurrences.is_empty())
}

/// Every reading of the latest authored segment that `read` finds any in; none when no segment has one.
fn latest_reading<T>(sends: &[Send], read: impl Fn(&str) -> Vec<T>) -> Vec<T> {
    let mut latest = Vec::new();
    for segment in sends.iter().flat_map(|send| &send.person) {
        let found = read(segment);
        if !found.is_empty() {
            latest = found;
        }
    }
    latest
}

/// Every schedule the latest authored segment stating any states; none when no segment does.
#[must_use]
pub fn stated_schedules(sends: &[Send]) -> Vec<Value> {
    latest_reading(sends, |segment| {
        phrase::stated(segment, None).unwrap_or_default()
    })
}

/// Every output choice the latest authored segment stating one states; empty when none does.
#[must_use]
pub fn stated_outputs(sends: &[Send]) -> Vec<&'static str> {
    latest_reading(sends, |segment| {
        phrase::outputs(segment).unwrap_or_default()
    })
}

/// The one output choice the latest authored segment stating one states, or `None`.
#[must_use]
pub fn stated_output(sends: &[Send]) -> Option<&'static str> {
    match stated_outputs(sends).as_slice() {
        [one] => Some(one),
        _ => None,
    }
}

/// The Routine's zone and where it came from.
///
/// The one zone the latest authored segment naming any names; else the request's browser zone; else UTC by
/// convention, with no source. A segment naming several zones names none of them.
#[must_use]
pub fn zone(sends: &[Send]) -> (String, String) {
    for segment in sends.iter().flat_map(|send| &send.person).rev() {
        let written = phrase::zones(segment).unwrap_or_default();
        if !written.is_empty() {
            if let [one] = written.as_slice() {
                return (one.clone(), "person".to_owned());
            }
            break;
        }
    }
    match sends.last().and_then(|send| send.timezone.clone()) {
        Some(browser) => (browser, "browser".to_owned()),
        None => (CONVENTIONAL_TIMEZONE.to_owned(), "none".to_owned()),
    }
}

/// Each line of the person's segments: a name or number never spans two.
#[must_use]
pub fn segment_lines(segments: &[String]) -> Vec<String> {
    segments
        .iter()
        .flat_map(|segment| segment.split('\n').map(str::to_owned))
        .collect()
}

/// Every call of the span in dispatch order; one whose Action is no longer at its pin refuses the recording.
///
/// # Errors
///
/// `plan-pin-drift` for a call whose Action has no current contract at its pin.
pub fn span_calls(sends: &[Send], contracts: &Contracts) -> Result<Vec<Call>, RecordingError> {
    let calls: Vec<Call> = sends
        .iter()
        .enumerate()
        .flat_map(|(position, send)| {
            send.occurrences
                .iter()
                .map(move |occurrence| (position, occurrence))
        })
        .enumerate()
        .map(|(index, (send, occurrence))| Call {
            index,
            send,
            occurrence: occurrence.clone(),
        })
        .collect();
    for call in &calls {
        let current = contracts.get(&call.action());
        if current.is_none_or(|contract| contract.pin != call.occurrence.pin) {
            return Err(RecordingError::new("plan-pin-drift"));
        }
    }
    Ok(calls)
}

#[cfg(test)]
mod tests {
    use team_protocol::payload::compose_clarified;

    use super::clarified_answer;

    #[test]
    fn only_an_exact_composed_clarification_answer_is_read_back() {
        let composed =
            compose_clarified("Liste os DNS", "Com que frequência?", "a cada hora", "pt")
                .expect("composed");
        let expected = (
            "Liste os DNS".to_owned(),
            "Com que frequência?".to_owned(),
            "a cada hora".to_owned(),
        );
        assert_eq!(clarified_answer(&composed), Some(expected));
        assert_eq!(clarified_answer(&format!("{composed}\nmais")), None);
        assert_eq!(clarified_answer("Liste os DNS"), None);
        assert_eq!(clarified_answer("\n\nPergunta: x\nResposta: y"), None);
    }
}
