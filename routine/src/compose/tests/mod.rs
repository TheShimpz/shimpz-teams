//! `test_routine_recording.py`, `test_routine_provenance.py`, and `test_routine_rerun.py`: Team's deterministic
//! recording of a Routine from a person's recent sends, with no model.
//!
//! Each top-level input member of a plan call is classified by the first rule that applies: a secret refuses, a value
//! the person named is a literal, the send's date in the Routine's zone is the run date, a value one result holds is
//! copied from that one occurrence, and anything else is a literal the assistant chose; what cannot be read is asked,
//! never guessed.

use strict_json::Value;
use team_protocol::payload::{authored_segments, compose_clarified};

use super::{Request, record, record_stated};
use crate::json::{at, elements};
use crate::plan::{ActionContract, Contracts};
use crate::recording::{
    Asked, Existing, Question, Recorded, Recording, RecordingError, Send, latest_work,
};
use crate::testing::json;
use crate::trace::{Kept, Occurrence, Protection, keep};

mod boundary;
mod classification;
mod owner;
mod rerun;
mod schedule;
mod sources;

pub(crate) const PIN: &str =
    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
pub(crate) const OTHER_PIN: &str =
    "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
pub(crate) const DRIFTED_PIN: &str =
    "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
const ZONE_ITEM: &str = r#"{"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "created_on": {"type": "string"}}}"#;
pub(crate) const RECORDS_IN: &str = r#"{"type": "object", "properties": {"zone_id": {"type": "string"}, "type": {"type": "string"},
    "per_page": {"type": "integer"}, "day": {"type": "string"}, "match": {}}, "required": ["zone_id"]}"#;
pub(crate) const DELETE_IN: &str = r#"{"type": "object", "properties": {"zone_id": {"type": "string"},
    "record_id": {"type": "string"}, "api_token": {"type": "string"}}}"#;
/// The members the recorded work of these tests sends to an Action that declares no other input.
const OPEN_IN: &str = r#"{"type": "object", "properties": {"a": {}, "b": {}, "call": {}, "num_results": {}, "page": {},
    "port": {}, "q": {}, "query": {}, "site": {}, "t": {}, "text": {}, "z": {}}}"#;
pub(crate) const SHIMPZ_ID: &str = "023e105f4ecef8ad9ca31a8372d0c353";
pub(crate) const TWIN_ID: &str = "7f1b2c3d4e5f60718293a4b5c6d7e8f9";
pub(crate) const EVERY_HOUR: &str = "A cada hora";
pub(crate) const SELECTED: &str = r#"{"kind": "step_output", "step": "s1", "pointer": "/result", "where": {"name": "shimpz.com"}, "item": "/id"}"#;

fn zones_out() -> Value {
    json(&format!(
        r#"{{"type": "object", "properties": {{"result": {{"type": "array", "items": {ZONE_ITEM}}}}}}}"#
    ))
}

fn contract(pin: &str, schema: &str, read_only: bool) -> ActionContract {
    let mut contract = ActionContract::new(pin.to_owned(), json(schema));
    contract.read_only = read_only;
    contract
}

/// The exact current contracts of the Actions these tests record.
pub(crate) fn contracts() -> Contracts {
    let key = |assistant: &str, action: &str| (assistant.to_owned(), action.to_owned());
    Contracts::from([
        (
            key("cloudflare", "list-zones"),
            contract(PIN, OPEN_IN, true),
        ),
        (
            key("cloudflare", "list-dns-records"),
            contract(PIN, RECORDS_IN, true),
        ),
        (
            key("cloudflare", "delete-dns-record"),
            contract(OTHER_PIN, DELETE_IN, false),
        ),
        (
            key("cloudflare", "renew-certificate"),
            contract(OTHER_PIN, OPEN_IN, false),
        ),
        (key("reports", "fetch"), contract(PIN, OPEN_IN, true)),
        (key("reports", "post"), contract(OTHER_PIN, OPEN_IN, false)),
    ])
}

/// When every test send starts unless it says otherwise: 15:00 UTC on 5 October 2026.
pub(crate) const STARTED_AT: i64 = 1_791_212_400;

pub(crate) fn started() -> i64 {
    STARTED_AT
}

/// The Cloudflare zones of these tests, shimpz.com last.
pub(crate) fn zones() -> Value {
    json(&format!(
        r#"{{"result": [
            {{"id": "9a7806061c88ada191ed06f989cc3dac", "name": "example.com", "created_on": "2026-01-01"}},
            {{"id": "1b3f0c5e2a9d47e8b6c1d0f2a3b4c5d6", "name": "other.org", "created_on": "2026-01-02"}},
            {{"id": "5d41402abc4b2a76b9719d911017c592", "name": "blog.dev", "created_on": "2026-01-03"}},
            {{"id": "{SHIMPZ_ID}", "name": "shimpz.com", "created_on": "2026-01-04"}}]}}"#
    ))
}

/// The zones with a second zone named shimpz.com.
pub(crate) fn twins() -> Value {
    let mut items = elements(at(&zones(), "result")).to_vec();
    items.push(json(&format!(
        r#"{{"id": "{TWIN_ID}", "name": "shimpz.com", "created_on": "2026-01-05"}}"#
    )));
    Value::Object(vec![("result".to_owned(), Value::Array(items))])
}

/// The id of the zone at this position of the zones.
pub(crate) fn zone_id(index: usize) -> String {
    crate::json::as_str(at(&elements(at(&zones(), "result"))[index], "id"))
        .expect("an id")
        .to_owned()
}

/// The owner's request: a Routine to list DNS records, answered every 30 seconds, for shimpz.com.
pub(crate) fn owner() -> String {
    let first = compose_clarified(
        "Cria uma rotina pra mim",
        "O que a rotina deve fazer?",
        "Listar registros DNS",
        "pt",
    );
    let second = compose_clarified(
        &first.expect("composed"),
        "Com que frequência?",
        "A cada 30 segundos",
        "pt",
    );
    compose_clarified(
        &second.expect("composed"),
        "De qual zona?",
        "shimpz.com",
        "pt",
    )
    .expect("composed")
}

/// A request, then the person's answer that it runs every hour, as Admin composes it.
pub(crate) fn then_hourly(request: &str) -> String {
    compose_clarified(request, "Com que frequência?", EVERY_HOUR, "pt").expect("composed")
}

/// One successful call of `name` (`assistant/action`) with this input and result.
pub(crate) fn call(name: &str, input: &str, result: &str) -> Occurrence {
    call_value(name, &json(input), &json(result))
}

/// One successful call whose input and result are values.
pub(crate) fn call_value(name: &str, input: &Value, result: &Value) -> Occurrence {
    let (_, action) = name.split_once('/').expect("an Action");
    let output = if action == "list-zones" {
        zones_out()
    } else {
        json("{}")
    };
    let kept = keep(result, &output, &[]).expect("a kept result");
    call_kept(name, input, kept)
}

/// One successful call whose result is already kept.
pub(crate) fn call_kept(name: &str, input: &Value, result: Kept) -> Occurrence {
    let (assistant, action) = name.split_once('/').expect("an Action");
    let current = contracts();
    let contract = &current[&(assistant.to_owned(), action.to_owned())];
    let kept = keep(input, &contract.input_schema, &[]).expect("a kept input");
    Occurrence::new(
        assistant,
        action,
        &contract.pin,
        contract.read_only,
        kept,
        result,
    )
}

/// One successful call whose input is already kept.
pub(crate) fn call_with_input(name: &str, input: Kept, result: &str) -> Occurrence {
    let base = call(name, "{}", result);
    Occurrence::new(
        &base.assistant,
        &base.action,
        &base.pin,
        base.read_only,
        input,
        base.result,
    )
}

/// The listing of every zone.
pub(crate) fn zones_call() -> Occurrence {
    call_value("cloudflare/list-zones", &json("{}"), &zones())
}

/// The records of shimpz.com.
pub(crate) fn records() -> Occurrence {
    call(
        "cloudflare/list-dns-records",
        &format!(r#"{{"zone_id": "{SHIMPZ_ID}"}}"#),
        r#"{"result": [{"type": "A"}]}"#,
    )
}

/// One send of these calls with this message, in Sao Paulo, at the default start, in Portuguese.
pub(crate) fn send(calls: Vec<Occurrence>, message: &str) -> Send {
    Send {
        message: message.to_owned(),
        person: authored_segments(message),
        window: Vec::new(),
        timezone: Some("America/Sao_Paulo".to_owned()),
        started_at: started(),
        locale: Some("pt".to_owned()),
        occurrences: calls,
    }
}

/// A send of these calls every hour.
pub(crate) fn hourly(calls: Vec<Occurrence>) -> Send {
    send(calls, EVERY_HOUR)
}

/// A recording span and how it is recorded.
pub(crate) struct Span {
    pub(crate) sends: Vec<Send>,
    /// The output the person stated; `None` reads it from the sends' own words.
    pub(crate) mode: Option<&'static str>,
    pub(crate) protection: Protection,
    pub(crate) contracts: Contracts,
    pub(crate) asked: Option<Asked>,
    pub(crate) existing: Option<Existing>,
}

impl Span {
    pub(crate) fn of(sends: Vec<Send>) -> Self {
        Self {
            sends,
            mode: Some("show"),
            protection: Protection::default(),
            contracts: contracts(),
            asked: None,
            existing: None,
        }
    }

    pub(crate) fn asked(mut self, asked: Asked) -> Self {
        self.asked = Some(asked);
        self
    }

    pub(crate) fn record(&self) -> Result<Recording, RecordingError> {
        let request = Request {
            sends: &self.sends,
            protection: &self.protection,
            contracts: &self.contracts,
            work: latest_work(&self.sends),
            asked: self.asked.as_ref(),
            existing: self.existing.as_ref(),
        };
        match self.mode {
            Some(mode) => record_stated(&request, Some(mode)),
            None => record(&request),
        }
    }

    pub(crate) fn recorded(&self) -> Recorded {
        match self.record() {
            Ok(Recording::Recorded(recorded)) => *recorded,
            other => panic!("not recorded: {other:?}"),
        }
    }

    pub(crate) fn question(&self) -> Question {
        match self.record() {
            Ok(Recording::Asked(question)) => *question,
            other => panic!("not asked: {other:?}"),
        }
    }

    pub(crate) fn code(&self) -> &'static str {
        match self.record() {
            Err(error) => error.code,
            other => panic!("not refused: {other:?}"),
        }
    }
}

/// The input of a recorded plan's step.
pub(crate) fn input_of(recorded: &Recorded, step: usize) -> &Value {
    at(&elements(at(&recorded.document, "steps"))[step], "input")
}

/// The input of a recorded plan's last step.
pub(crate) fn last_input(recorded: &Recorded) -> &Value {
    let steps = elements(at(&recorded.document, "steps"));
    at(&steps[steps.len() - 1], "input")
}

/// The Actions of a recorded plan's steps, in order.
pub(crate) fn actions(recorded: &Recorded) -> Vec<&str> {
    elements(at(&recorded.document, "steps"))
        .iter()
        .filter_map(|step| crate::json::as_str(at(step, "action")))
        .collect()
}

/// The span's record of a question asked after `after` sends, with every choice already answered.
pub(crate) fn asked_after(question: &Question, after: usize) -> Asked {
    Asked {
        code: question.code.clone(),
        after,
        pending: question.pending.clone(),
        manifest: question.manifest.clone(),
        wire: None,
        chosen: question.chosen.clone(),
        repair_from: question.repair_from,
    }
}

/// The span's record of a question asked after `after` sends, without the choices answered before it.
pub(crate) fn pending_after(question: &Question, after: usize) -> Asked {
    Asked {
        chosen: Vec::new(),
        ..asked_after(question, after)
    }
}

/// A target question of these options.
pub(crate) fn binding_question(options: Vec<crate::recording::Target>) -> Question {
    Question::new("routine-binding-ambiguous", options)
}

/// A post of this text.
pub(crate) fn post(text: &str) -> Occurrence {
    call_value(
        "reports/post",
        &Value::Object(vec![("t".to_owned(), crate::json::text(text))]),
        &json("{}"),
    )
}
