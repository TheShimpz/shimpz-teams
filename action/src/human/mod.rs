//! Closed validation of one reviewed Action human-request suspension and its replay transcript (`action/human.py`).
//!
//! A request is admitted only when the Assistant protocol's rules admit it against the binding's reviewed message
//! catalog (ADR-0091), its kind is one the Action declares, and its advertised fingerprint is exactly the SHA-256 of
//! its canonical form, compared in constant time. A response is admitted only against that canonical request, and a
//! transcript only in exact ordinal order within the Action's and the turn's budgets. A Stored Input request is
//! answered by sealing the value for injection, never by a replay response (ADR-0059).

use assistant_protocol::catalog::catalog_by_id;
use assistant_protocol::human_request::request_error;
use strict_json::{Number, Value};
use subtle::ConstantTimeEq;
use team_core::secret::Secret;
use team_protocol::content::SHA256_RE;

use crate::journal::{canonical, sha256_hex};

/// The most requests one Action makes.
pub const MAX_REQUESTS_PER_ACTION: usize = 8;
/// The most requests one Team turn makes.
pub const MAX_REQUESTS_PER_TURN: usize = 16;
/// The kinds that authorize an Action rather than supply a value.
pub const AUTHORIZATION_KINDS: [&str; 4] =
    ["approval", "auth:password", "auth:totp", "auth:passkey"];
/// The copy fields of a request, each a reviewed catalog reference.
pub const COPY_FIELDS: [&str; 4] = ["title", "description", "label", "placeholder"];
/// The copy fields of each option.
pub const OPTION_COPY_FIELDS: [&str; 2] = ["label", "description"];

/// A human request or response broke its reviewed closed contract.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct HumanRequestError(pub &'static str);

/// Whether `kind` authorizes an Action.
#[must_use]
pub fn authorizes(kind: &str) -> bool {
    AUTHORIZATION_KINDS.contains(&kind)
}

/// One admitted request, safe to bind into a Team-owned challenge.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HumanRequest {
    /// Its kind.
    pub kind: String,
    /// Its ordinal within the Action.
    pub ordinal: usize,
    /// Its SHA-256 fingerprint.
    pub fingerprint: String,
    /// Its canonical bytes, fingerprint included.
    pub canonical: Vec<u8>,
    /// The Stored Input a password request names.
    pub stored_input: Option<String>,
    /// The canonical catalog entries its copy references, sorted by id.
    pub catalog: Vec<u8>,
}

impl HumanRequest {
    /// The request object.
    #[must_use]
    pub fn payload(&self) -> Value {
        strict_json::parse(&self.canonical).unwrap_or(Value::Null)
    }

    /// The referenced catalog entries.
    #[must_use]
    pub fn messages(&self) -> Value {
        strict_json::parse(&self.catalog).unwrap_or(Value::Null)
    }
}

/// Admits one request against the Action's declared request kinds and Stored Inputs and the binding's reviewed
/// `messages`, binding its advertised fingerprint to its canonical bytes.
///
/// # Errors
/// [`HumanRequestError`] for anything else.
pub fn validate_request(
    value: &Value,
    capabilities: &[String],
    stored_inputs: &[String],
    messages: &Value,
) -> Result<HumanRequest, HumanRequestError> {
    let invalid = HumanRequestError("Assistant Action human request is invalid");
    let Value::Object(members) = value else {
        return Err(invalid);
    };
    let fingerprint = match value.get("fingerprint") {
        Some(Value::String(fingerprint)) if SHA256_RE.is_full(fingerprint) => fingerprint.clone(),
        _ => return Err(invalid),
    };
    let request = Value::Object(
        members
            .iter()
            .filter(|(name, _)| name != "fingerprint")
            .cloned()
            .collect(),
    );
    let catalog = catalog_by_id(messages);
    let kind = match request.get("kind") {
        Some(Value::String(kind)) if capabilities.contains(kind) => kind.clone(),
        _ => return Err(invalid),
    };
    if request_error(&request, &catalog).is_some() {
        return Err(invalid);
    }
    let expected = sha256_hex(&canonical(&request));
    if !bool::from(fingerprint.as_bytes().ct_eq(expected.as_bytes())) {
        return Err(HumanRequestError(
            "Assistant Action human request fingerprint is invalid",
        ));
    }
    let stored_input = declared_stored_input(&request, stored_inputs)?;
    let ordinal = match request.get("ordinal") {
        Some(Value::Number(Number::Int(ordinal))) => {
            usize::try_from(*ordinal).map_err(|_| invalid)?
        }
        _ => return Err(invalid),
    };
    Ok(HumanRequest {
        kind,
        ordinal,
        canonical: framed(&request, &fingerprint),
        fingerprint,
        stored_input,
        catalog: referenced_catalog(&request, &catalog),
    })
}

/// The declared Stored Input a password request names.
fn declared_stored_input(
    request: &Value,
    stored_inputs: &[String],
) -> Result<Option<String>, HumanRequestError> {
    match request.get("stored_input") {
        Some(Value::String(id)) if stored_inputs.contains(id) => Ok(Some(id.clone())),
        Some(Value::String(_)) => Err(HumanRequestError(
            "Assistant Action Stored Input request is undeclared",
        )),
        _ => Ok(None),
    }
}

/// The canonical bytes of `request` with its `fingerprint`.
fn framed(request: &Value, fingerprint: &str) -> Vec<u8> {
    let Value::Object(members) = request else {
        return canonical(request);
    };
    let mut framed = members.clone();
    framed.push((
        "fingerprint".to_owned(),
        Value::String(fingerprint.to_owned()),
    ));
    canonical(&Value::Object(framed))
}

/// The canonical catalog entries `request` references, sorted by id.
fn referenced_catalog(
    request: &Value,
    catalog: &assistant_protocol::human_request::Catalog<'_>,
) -> Vec<u8> {
    let mut referenced = referenced_messages(request);
    referenced.sort();
    referenced.dedup();
    let entries = referenced
        .iter()
        .filter_map(|id| {
            catalog
                .get(id.as_str())
                .map(|message| Value::Object(message.to_vec()))
        })
        .collect();
    canonical(&Value::Array(entries))
}

/// The message ids of every non-null copy reference of an admitted request.
#[must_use]
pub fn referenced_messages(request: &Value) -> Vec<String> {
    let mut references: Vec<&Value> = COPY_FIELDS
        .iter()
        .filter_map(|field| request.get(field))
        .collect();
    if let Some(Value::Array(options)) = request.get("options") {
        for option in options {
            references.extend(
                OPTION_COPY_FIELDS
                    .iter()
                    .filter_map(|field| option.get(field)),
            );
        }
    }
    references
        .into_iter()
        .filter_map(|reference| match reference.get("message") {
            Some(Value::String(id)) => Some(id.clone()),
            _ => None,
        })
        .collect()
}

/// One Team-admitted response bound to the exact request that produced it.
#[derive(Clone, Debug, PartialEq)]
pub struct HumanResponse {
    /// The request kind.
    pub kind: String,
    /// The request ordinal.
    pub ordinal: usize,
    /// The request fingerprint.
    pub fingerprint: String,
    /// The person's answer.
    pub value: Value,
}

impl HumanResponse {
    /// The closed replay frame the Assistant SDK consumes.
    #[must_use]
    pub fn payload(&self) -> Value {
        Value::Object(vec![
            ("kind".to_owned(), Value::String(self.kind.clone())),
            (
                "ordinal".to_owned(),
                Value::Number(Number::Int(i64::try_from(self.ordinal).unwrap_or(i64::MAX))),
            ),
            (
                "fingerprint".to_owned(),
                Value::String(self.fingerprint.clone()),
            ),
            ("value".to_owned(), self.value.clone()),
        ])
    }
}

fn int(request: &Value, key: &str) -> Option<i64> {
    match request.get(key) {
        Some(Value::Number(Number::Int(value))) => Some(*value),
        _ => None,
    }
}

fn option_values(request: &Value) -> Option<Vec<&str>> {
    let Some(Value::Array(options)) = request.get("options") else {
        return None;
    };
    Some(
        options
            .iter()
            .filter_map(|option| match option.get("value") {
                Some(Value::String(value)) => Some(value.as_str()),
                _ => None,
            })
            .collect(),
    )
}

fn optional(request: &Value) -> bool {
    request.get("required") == Some(&Value::Bool(false))
}

fn admitted(request: &Value, kind: &str, value: &Value) -> bool {
    if authorizes(kind) {
        return *value == Value::Bool(true);
    }
    if matches!(kind, "input:select" | "input:choice") {
        let Value::String(choice) = value else {
            return false;
        };
        return option_values(request).is_some_and(|allowed| {
            allowed.contains(&choice.as_str()) || (choice.is_empty() && optional(request))
        });
    }
    if kind == "input:choices" {
        let (Value::Array(items), Some(allowed)) = (value, option_values(request)) else {
            return false;
        };
        let chosen: Option<Vec<&str>> = items
            .iter()
            .map(|item| match item {
                Value::String(choice) => Some(choice.as_str()),
                _ => None,
            })
            .collect();
        let Some(chosen) = chosen else { return false };
        let unique = chosen
            .iter()
            .enumerate()
            .all(|(index, choice)| !chosen[..index].contains(choice));
        let count = i64::try_from(chosen.len()).unwrap_or(i64::MAX);
        let bounded = int(request, "min_selections").is_some_and(|minimum| minimum <= count)
            && int(request, "max_selections").is_some_and(|maximum| count <= maximum);
        return unique && bounded && chosen.iter().all(|choice| allowed.contains(choice));
    }
    let Value::String(text) = value else {
        return false;
    };
    let length = i64::try_from(text.chars().count()).unwrap_or(i64::MAX);
    (optional(request) || !text.is_empty())
        && int(request, "min_length").is_some_and(|minimum| minimum <= length)
        && int(request, "max_length").is_some_and(|maximum| length <= maximum)
}

/// Admits one person's decision against its canonical reviewed request.
///
/// # Errors
/// [`HumanRequestError`] for a value the request does not admit.
pub fn admit_response(
    request: &HumanRequest,
    value: &Value,
) -> Result<HumanResponse, HumanRequestError> {
    if !admitted(&request.payload(), &request.kind, value) {
        return Err(HumanRequestError(
            "human response does not match its reviewed request",
        ));
    }
    Ok(HumanResponse {
        kind: request.kind.clone(),
        ordinal: request.ordinal,
        fingerprint: request.fingerprint.clone(),
        value: value.clone(),
    })
}

/// The memory-only replay responses of one Brain Action interrupt.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ActionTranscript {
    /// The interrupt.
    pub interrupt_id: String,
    /// The admitted responses in order.
    pub responses: Vec<HumanResponse>,
}

impl ActionTranscript {
    /// An empty transcript of `interrupt_id`.
    #[must_use]
    pub fn new(interrupt_id: &str) -> Self {
        Self {
            interrupt_id: interrupt_id.to_owned(),
            responses: Vec::new(),
        }
    }

    /// Refuses a request that is not the next ordinal or exceeds the Action budget.
    ///
    /// # Errors
    /// [`HumanRequestError`].
    pub fn require_next(&self, request: &HumanRequest) -> Result<(), HumanRequestError> {
        if self.responses.len() >= MAX_REQUESTS_PER_ACTION
            || request.ordinal != self.responses.len()
        {
            return Err(HumanRequestError(
                "Assistant Action human request sequence is invalid",
            ));
        }
        Ok(())
    }

    /// The transcript with the next exact response; a Stored Input request is answered by injection instead, and an
    /// Action authorizes at most once.
    ///
    /// # Errors
    /// [`HumanRequestError`].
    pub fn append(&self, request: &HumanRequest, value: &Value) -> Result<Self, HumanRequestError> {
        if request.stored_input.is_some() {
            return Err(HumanRequestError(
                "Assistant Action Stored Input is answered by injection",
            ));
        }
        self.require_next(request)?;
        if authorizes(&request.kind)
            && self
                .responses
                .iter()
                .any(|response| authorizes(&response.kind))
        {
            return Err(HumanRequestError(
                "Assistant Action requested authorization more than once",
            ));
        }
        let mut next = self.clone();
        next.responses.push(admit_response(request, value)?);
        Ok(next)
    }

    /// Whether the transcript holds the response to the one authorization `human_requests` declares.
    #[must_use]
    pub fn authorized(&self, human_requests: &[String]) -> bool {
        let declared: Vec<&String> = human_requests
            .iter()
            .filter(|kind| authorizes(kind))
            .collect();
        declared.len() == 1
            && self
                .responses
                .iter()
                .any(|response| response.kind == *declared[0])
    }

    /// The replay frames in admitted order.
    #[must_use]
    pub fn payloads(&self) -> Vec<Value> {
        self.responses.iter().map(HumanResponse::payload).collect()
    }
}

/// One admitted response and the turn's request budget after it.
#[derive(Debug)]
pub struct Admission {
    /// The transcripts, unchanged for a Stored Input answer.
    pub transcripts: Vec<ActionTranscript>,
    /// The requests the turn has used.
    pub requests_used: usize,
    /// The Stored Input id and value to seal, for a Stored Input answer.
    pub stored_input: Option<(String, Secret)>,
}

/// Admits one response within the Team-wide turn budget. A Stored Input answer counts against the budget but stays
/// out of the transcript: the caller seals it, and the replay receives it injected.
///
/// # Errors
/// [`HumanRequestError`] beyond the budget, for an ambiguous or out-of-order transcript, or an invalid value.
pub fn append_response(
    transcripts: &[ActionTranscript],
    interrupt_id: &str,
    request: &HumanRequest,
    value: &Value,
    requests_used: usize,
) -> Result<Admission, HumanRequestError> {
    if requests_used >= MAX_REQUESTS_PER_TURN {
        return Err(HumanRequestError(
            "Team turn exceeded its human request limit",
        ));
    }
    let matching: Vec<&ActionTranscript> = transcripts
        .iter()
        .filter(|transcript| transcript.interrupt_id == interrupt_id)
        .collect();
    if matching.len() > 1 {
        return Err(HumanRequestError("Action human transcript is ambiguous"));
    }
    let current = matching.first().map_or_else(
        || ActionTranscript::new(interrupt_id),
        |current| (*current).clone(),
    );
    if let Some(stored_input) = &request.stored_input {
        current.require_next(request)?;
        let Value::String(secret) = admit_response(request, value)?.value else {
            return Err(HumanRequestError(
                "human response does not match its reviewed request",
            ));
        };
        return Ok(Admission {
            transcripts: transcripts.to_vec(),
            requests_used: requests_used + 1,
            stored_input: Some((stored_input.clone(), Secret::new(secret))),
        });
    }
    let updated = current.append(request, value)?;
    let mut admitted: Vec<ActionTranscript> = transcripts
        .iter()
        .filter(|transcript| transcript.interrupt_id != interrupt_id)
        .cloned()
        .collect();
    admitted.push(updated);
    Ok(Admission {
        transcripts: admitted,
        requests_used: requests_used + 1,
        stored_input: None,
    })
}

/// The transcripts of interrupts not yet completed; memory-only responses go with their completed interrupt.
#[must_use]
pub fn retain_unfinished(
    transcripts: &[ActionTranscript],
    completed: &[String],
) -> Vec<ActionTranscript> {
    transcripts
        .iter()
        .filter(|transcript| !completed.contains(&transcript.interrupt_id))
        .cloned()
        .collect()
}

#[cfg(test)]
mod tests;
