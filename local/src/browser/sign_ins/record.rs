//! A Sign-in's sealed record: its exact fields, read strictly and written as the Python Team writes it (compact,
//! sorted keys, ASCII escapes, times as Python floats).

use strict_json::{Number, Value};
use team_protocol::python::json::{self, Options};
use team_protocol::python::value::{exact_object, field};
use team_storage::private::instant_text;

use super::{IDLE_SECONDS, LIFETIME_SECONDS, NEEDS_PERSON, READY};

const KEYS: [&str; 14] = [
    "team_id",
    "sign_in_id",
    "incarnation",
    "generation",
    "state",
    "sites",
    "sites_truncated",
    "created_at",
    "saved_at",
    "last_used_at",
    "chunks",
    "writer",
    "checkout",
    "deleting",
];
const DELETING_KEYS: [&str; 4] = ["event_id", "initiator_id", "factor", "origin"];
const RECORD_JSON: Options = Options {
    ensure_ascii: true,
    sort_keys: true,
    compact: true,
};

/// A deletion's activity metadata (ADR-0120), kept in its fence until its completion is recorded; an empty event id
/// marks a deletion of the Team's own lifecycle (expiry, a new incarnation, the Team's deletion).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Deleting {
    /// The deletion's activity event.
    pub event_id: String,
    /// The verified person who asked for it.
    pub initiator_id: String,
    /// The second factor they confirmed it with.
    pub factor: String,
    /// The Admin Origin they asked from.
    pub origin: String,
}

impl Deleting {
    /// A deletion of the Team's own lifecycle.
    #[must_use]
    pub fn internal() -> Self {
        Self {
            event_id: String::new(),
            initiator_id: String::new(),
            factor: String::new(),
            origin: String::new(),
        }
    }

    /// Whether each field is a bounded string.
    #[must_use]
    pub fn admitted(&self) -> bool {
        [
            &self.event_id,
            &self.initiator_id,
            &self.factor,
            &self.origin,
        ]
        .iter()
        .all(|field| field.chars().count() <= 255)
    }

    fn parse(value: &Value) -> Result<Option<Self>, Malformed> {
        if *value == Value::Null {
            return Ok(None);
        }
        let members = exact_object(value, &DELETING_KEYS).ok_or(Malformed)?;
        let text = |key| match field(members, key) {
            Value::String(text) => Some(text.clone()),
            _ => None,
        };
        let deleting = Self {
            event_id: text("event_id").ok_or(Malformed)?,
            initiator_id: text("initiator_id").ok_or(Malformed)?,
            factor: text("factor").ok_or(Malformed)?,
            origin: text("origin").ok_or(Malformed)?,
        };
        if deleting.admitted() {
            Ok(Some(deleting))
        } else {
            Err(Malformed)
        }
    }

    fn value(&self) -> Value {
        Value::Object(
            DELETING_KEYS
                .iter()
                .zip([
                    &self.event_id,
                    &self.initiator_id,
                    &self.factor,
                    &self.origin,
                ])
                .map(|(key, value)| ((*key).to_owned(), Value::String(value.clone())))
                .collect(),
        )
    }
}

/// The checkout a record holds.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Checkout {
    /// Its id.
    pub id: String,
    /// `person` or `agent`.
    pub kind: String,
}

/// A Sign-in's record.
#[derive(Clone, Debug, PartialEq)]
pub struct Record {
    pub team_id: String,
    pub sign_in_id: String,
    pub incarnation: String,
    pub generation: i64,
    pub state: String,
    pub sites: Vec<String>,
    pub sites_truncated: bool,
    pub created_at: f64,
    pub saved_at: f64,
    pub last_used_at: f64,
    pub chunks: i64,
    pub writer: String,
    pub checkout: Option<Checkout>,
    pub deleting: Option<Deleting>,
}

fn text(value: &Value) -> Option<String> {
    match value {
        Value::String(text) => Some(text.clone()),
        _ => None,
    }
}

fn integer(value: &Value) -> Option<i64> {
    match value {
        Value::Number(Number::Int(number)) => Some(*number),
        _ => None,
    }
}

/// A time as Python's `time.time()` wrote it: a finite, non-negative float.
fn seconds(value: &Value) -> Option<f64> {
    match value {
        Value::Number(Number::Float(number)) if number.is_finite() && *number >= 0.0 => {
            Some(*number)
        }
        _ => None,
    }
}

fn float(value: f64) -> Value {
    Value::Number(Number::Float(value))
}

/// The record's field is not one this store writes.
#[derive(Clone, Copy, Debug)]
struct Malformed;

fn checkout(value: &Value) -> Result<Option<Checkout>, Malformed> {
    if *value == Value::Null {
        return Ok(None);
    }
    let members = exact_object(value, &["id", "kind"]).ok_or(Malformed)?;
    let held = Checkout {
        id: text(field(members, "id")).ok_or(Malformed)?,
        kind: text(field(members, "kind")).ok_or(Malformed)?,
    };
    if held.kind == "person" || held.kind == "agent" {
        Ok(Some(held))
    } else {
        Err(Malformed)
    }
}

impl Record {
    /// A Team's first Sign-in, before any Save: generation 0, shown to no one.
    pub fn first(team_id: &str, incarnation: &str, now: f64, sign_in_id: &str) -> Self {
        Self {
            team_id: team_id.to_owned(),
            sign_in_id: sign_in_id.to_owned(),
            incarnation: incarnation.to_owned(),
            generation: 0,
            state: READY.to_owned(),
            sites: Vec::new(),
            sites_truncated: false,
            created_at: now,
            saved_at: now,
            last_used_at: now,
            chunks: 0,
            writer: String::new(),
            checkout: None,
            deleting: None,
        }
    }

    /// The record of `team_id`'s Sign-in `sign_in_id` in `plaintext`, or `None` when it is not exactly one.
    pub fn parse(plaintext: &[u8], team_id: &str, sign_in_id: &str) -> Option<Self> {
        let value = strict_json::parse(plaintext).ok()?;
        let members = exact_object(&value, &KEYS)?;
        let get = |key| field(members, key);
        let Value::Array(sites) = get("sites") else {
            return None;
        };
        let record = Self {
            team_id: text(get("team_id"))?,
            sign_in_id: text(get("sign_in_id"))?,
            incarnation: text(get("incarnation"))?,
            generation: integer(get("generation"))?,
            state: text(get("state")).filter(|state| state == READY || state == NEEDS_PERSON)?,
            sites: sites.iter().map(text).collect::<Option<_>>()?,
            sites_truncated: match get("sites_truncated") {
                Value::Bool(truncated) => *truncated,
                _ => return None,
            },
            created_at: seconds(get("created_at"))?,
            saved_at: seconds(get("saved_at"))?,
            last_used_at: seconds(get("last_used_at"))?,
            chunks: integer(get("chunks"))?,
            writer: text(get("writer"))?,
            checkout: checkout(get("checkout")).ok()?,
            deleting: Deleting::parse(get("deleting")).ok()?,
        };
        (record.team_id == team_id && record.sign_in_id == sign_in_id).then_some(record)
    }

    /// The record's plaintext.
    pub fn plaintext(&self) -> String {
        let value = Value::Object(vec![
            ("team_id".to_owned(), Value::String(self.team_id.clone())),
            (
                "sign_in_id".to_owned(),
                Value::String(self.sign_in_id.clone()),
            ),
            (
                "incarnation".to_owned(),
                Value::String(self.incarnation.clone()),
            ),
            (
                "generation".to_owned(),
                Value::Number(Number::Int(self.generation)),
            ),
            ("state".to_owned(), Value::String(self.state.clone())),
            (
                "sites".to_owned(),
                Value::Array(self.sites.iter().cloned().map(Value::String).collect()),
            ),
            (
                "sites_truncated".to_owned(),
                Value::Bool(self.sites_truncated),
            ),
            ("created_at".to_owned(), float(self.created_at)),
            ("saved_at".to_owned(), float(self.saved_at)),
            ("last_used_at".to_owned(), float(self.last_used_at)),
            ("chunks".to_owned(), Value::Number(Number::Int(self.chunks))),
            ("writer".to_owned(), Value::String(self.writer.clone())),
            (
                "checkout".to_owned(),
                self.checkout.as_ref().map_or(Value::Null, |held| {
                    Value::Object(vec![
                        ("id".to_owned(), Value::String(held.id.clone())),
                        ("kind".to_owned(), Value::String(held.kind.clone())),
                    ])
                }),
            ),
            (
                "deleting".to_owned(),
                self.deleting.as_ref().map_or(Value::Null, Deleting::value),
            ),
        ]);
        json::dumps(&value, RECORD_JSON)
    }

    /// When it expires: 30 days from its last Save or 7 days unused, whichever comes first.
    pub fn expires(&self) -> f64 {
        (self.saved_at + LIFETIME_SECONDS).min(self.last_used_at + IDLE_SECONDS)
    }

    /// Whether the deletion `event_id` fences it.
    pub fn fenced_by(&self, event_id: &str) -> bool {
        self.deleting
            .as_ref()
            .is_some_and(|deleting| deleting.event_id == event_id)
    }

    /// What a Supervisor sees of the Sign-in, never its capsule; `needs_person` when this Team cannot restore it.
    pub fn public(&self, needs_person: bool) -> Value {
        let iso = |seconds: f64| {
            let whole = std::time::Duration::try_from_secs_f64(seconds)
                .ok()
                .and_then(|elapsed| i64::try_from(elapsed.as_secs()).ok())
                .unwrap_or_default();
            Value::String(instant_text(whole))
        };
        let state = if needs_person {
            NEEDS_PERSON
        } else {
            self.state.as_str()
        };
        Value::Object(vec![
            (
                "sign_in_id".to_owned(),
                Value::String(self.sign_in_id.clone()),
            ),
            ("state".to_owned(), Value::String(state.to_owned())),
            (
                "generation".to_owned(),
                Value::Number(Number::Int(self.generation)),
            ),
            (
                "team_incarnation".to_owned(),
                Value::String(self.incarnation.clone()),
            ),
            (
                "sites".to_owned(),
                Value::Array(self.sites.iter().cloned().map(Value::String).collect()),
            ),
            (
                "sites_truncated".to_owned(),
                Value::Bool(self.sites_truncated),
            ),
            ("created_at".to_owned(), iso(self.created_at)),
            ("saved_at".to_owned(), iso(self.saved_at)),
            ("last_used_at".to_owned(), iso(self.last_used_at)),
            ("expires_at".to_owned(), iso(self.expires())),
        ])
    }
}
