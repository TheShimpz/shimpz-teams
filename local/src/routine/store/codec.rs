//! A Team's Routine state file: its exact canonical bytes and the fail-closed decoding of every member (schema 13).
//!
//! The file is the canonical JSON (`sort_keys`, compact, UTF-8 unescaped) of the Team's Routines, runs, notices, the
//! instant it last served a claim, its rolling starts, and the queued discards. Decoding admits exactly what Team
//! writes: every member of every record, each status with exactly its own fields, every bound, and every cross-record
//! invariant; anything else is [`StoreError::Malformed`].

use strict_json::{Number, Value};
use team_protocol::identifiers::{canonical_action_id, canonical_assistant_id};
use team_protocol::payload::SHA256_RE;
use team_protocol::python::json::{CANONICAL, dumps};
use team_protocol::python::value::{object_of, py_eq, small_int};
use team_protocol::routine::{
    MAX_ROLLUP_RUNS, ROUTINE_ID_RE, canonical_position, canonical_run_usage, valid_revision,
};
use team_protocol::routine_notice::{MAX_UNRESOLVED_INCIDENTS, REQUEST_KINDS, canonical_notice};
use team_routine::claim::starts::{MAX_STARTS, Start};
use team_routine::definition::definition_bytes;
use team_routine::plan::{MAX_DEFINITION_BYTES, MAX_STEPS, TEAM_DEFINITION_BYTES};
use team_routine::record::{
    self, ACTIVE_SECONDS, HUMAN_LEASE, MAX_COUNTED_MISSES, MAX_DISCARDS, MAX_ROUTINE_NOTICES,
    MAX_ROUTINES, MAX_RUNS, MAX_UNDELIVERED_NOTICES, Notice, Routine, Run, RunStatus, TeamRoutines,
    definition_valid,
};

use super::StoreError;

/// The state file's schema.
pub const SCHEMA: i64 = 13;
/// The human requests one logical run may answer (`action/human.py` `MAX_REQUESTS_PER_TURN`).
pub const MAX_REQUESTS_PER_TURN: i64 = 16;

const ROUTINE_FIELDS: [&str; 24] = [
    "routine_id",
    "name",
    "description",
    "plan",
    "schedule",
    "timezone_source",
    "assistants",
    "anchor",
    "next_run_at",
    "needs_reconfirm",
    "deleting",
    "gap_started_at",
    "missed",
    "reported_missed",
    "revision",
    "paused",
    "confirmation",
    "permitted",
    "failures",
    "rollup_minute",
    "rollup_runs",
    "rollup_usage",
    "run_requested",
    "output_digest",
];
const RUN_FIELDS: [&str; 18] = [
    "run_id",
    "routine_id",
    "status",
    "lease_sha256",
    "lease_key",
    "lease_expires_at",
    "active_seconds_left",
    "request_kind",
    "assistant_id",
    "action",
    "generation",
    "notice_version",
    "requests_used",
    "position",
    "steps",
    "usage",
    "protection_lost",
    "revision",
];
const NOTICE_FIELDS: [&str; 10] = [
    "notice_id",
    "routine_id",
    "run_id",
    "outcome",
    "created_at",
    "detail",
    "version",
    "name",
    "usage",
    "protection_lost",
];
const STATE_FIELDS: [&str; 8] = [
    "schema",
    "team_id",
    "routines",
    "runs",
    "notices",
    "served_at",
    "starts",
    "discards",
];

type Decoded<T> = Result<T, StoreError>;

fn require(condition: bool) -> Decoded<()> {
    if condition {
        Ok(())
    } else {
        Err(StoreError::Malformed)
    }
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn int(value: i64) -> Value {
    Value::Number(Number::Int(value))
}

fn optional(value: Option<&Value>) -> Value {
    value.cloned().unwrap_or(Value::Null)
}

/// The canonical bytes of `state` for `team_id`.
#[must_use]
pub fn encode(state: &TeamRoutines, team_id: &str) -> Vec<u8> {
    let payload = object_of(vec![
        ("schema", int(SCHEMA)),
        ("team_id", text(team_id)),
        (
            "routines",
            Value::Array(state.routines.iter().map(routine_value).collect()),
        ),
        (
            "runs",
            Value::Array(state.runs.iter().map(run_value).collect()),
        ),
        (
            "notices",
            Value::Array(state.notices.iter().map(notice_value).collect()),
        ),
        ("served_at", int(state.served_at)),
        (
            "starts",
            Value::Array(
                state
                    .starts
                    .iter()
                    .map(|(routine_id, at, steps)| {
                        Value::Array(vec![text(routine_id), int(*at), int(*steps)])
                    })
                    .collect(),
            ),
        ),
        (
            "discards",
            Value::Array(
                state
                    .discards
                    .iter()
                    .map(|(run_id, generation)| Value::Array(vec![text(run_id), text(generation)]))
                    .collect(),
            ),
        ),
    ]);
    dumps(&payload, CANONICAL).into_bytes()
}

fn routine_value(item: &Routine) -> Value {
    let assistants = item
        .assistants
        .iter()
        .map(|(assistant, pin)| Value::Array(vec![text(assistant), text(pin)]))
        .collect();
    object_of(vec![
        ("routine_id", text(&item.routine_id)),
        ("name", text(&item.name)),
        ("description", text(&item.description)),
        ("plan", item.plan.clone()),
        ("schedule", item.schedule.clone()),
        ("timezone_source", text(&item.timezone_source)),
        ("assistants", Value::Array(assistants)),
        ("anchor", int(item.anchor)),
        ("next_run_at", int(item.next_run_at)),
        ("needs_reconfirm", Value::Bool(item.needs_reconfirm)),
        ("deleting", Value::Bool(item.deleting)),
        ("gap_started_at", int(item.gap_started_at)),
        ("missed", int(item.missed)),
        ("reported_missed", int(item.reported_missed)),
        ("revision", int(item.revision)),
        ("paused", Value::Bool(item.paused)),
        ("confirmation", optional(item.confirmation.as_ref())),
        ("permitted", Value::Array(item.permitted.clone())),
        ("failures", int(item.failures)),
        ("rollup_minute", int(item.rollup_minute)),
        ("rollup_runs", int(item.rollup_runs)),
        ("rollup_usage", optional(item.rollup_usage.as_ref())),
        ("run_requested", int(item.run_requested)),
        ("output_digest", text(&item.output_digest)),
    ])
}

fn run_value(item: &Run) -> Value {
    object_of(vec![
        ("run_id", text(&item.run_id)),
        ("routine_id", text(&item.routine_id)),
        ("status", text(item.status.as_str())),
        ("lease_sha256", text(&item.lease_sha256)),
        ("lease_key", text(&item.lease_key)),
        ("lease_expires_at", int(item.lease_expires_at)),
        ("active_seconds_left", int(item.active_seconds_left)),
        ("request_kind", text(&item.request_kind)),
        ("assistant_id", text(&item.assistant_id)),
        ("action", text(&item.action)),
        ("generation", text(&item.generation)),
        ("notice_version", int(item.notice_version)),
        ("requests_used", int(item.requests_used)),
        ("position", item.position.map_or(Value::Null, int)),
        ("steps", int(item.steps)),
        ("usage", item.usage.clone()),
        ("protection_lost", Value::Bool(item.protection_lost)),
        ("revision", int(item.revision)),
    ])
}

fn notice_value(item: &Notice) -> Value {
    object_of(vec![
        ("notice_id", text(&item.notice_id)),
        ("routine_id", text(&item.routine_id)),
        ("run_id", text(&item.run_id)),
        ("outcome", text(&item.outcome)),
        ("created_at", int(item.created_at)),
        ("detail", item.detail.clone()),
        ("version", int(item.version)),
        ("name", text(&item.name)),
        ("usage", optional(item.usage.as_ref())),
        ("protection_lost", Value::Bool(item.protection_lost)),
    ])
}

/// The members of an object with exactly `fields`, in any order.
fn exactly<'v>(value: &'v Value, fields: &[&str]) -> Decoded<&'v [(String, Value)]> {
    let Value::Object(members) = value else {
        return Err(StoreError::Malformed);
    };
    require(
        members.len() == fields.len()
            && members
                .iter()
                .all(|(name, _)| fields.contains(&name.as_str())),
    )?;
    Ok(members)
}

fn at<'v>(value: &'v Value, name: &str) -> &'v Value {
    value.get(name).unwrap_or(&Value::Null)
}

fn string(value: &Value) -> Decoded<String> {
    match value {
        Value::String(text) => Ok(text.clone()),
        _ => Err(StoreError::Malformed),
    }
}

fn boolean(value: &Value) -> Decoded<bool> {
    match value {
        Value::Bool(flag) => Ok(*flag),
        _ => Err(StoreError::Malformed),
    }
}

fn integer(value: &Value) -> Decoded<i64> {
    small_int(value).ok_or(StoreError::Malformed)
}

fn bounded(value: &Value, low: i64, high: i64) -> Decoded<i64> {
    small_int(value)
        .filter(|number| (low..=high).contains(number))
        .ok_or(StoreError::Malformed)
}

/// An epoch instant Team writes: an integer from 0 below 2^40.
fn instant(value: &Value) -> Decoded<i64> {
    bounded(value, 0, (1 << 40) - 1)
}

fn count(value: &Value) -> Decoded<i64> {
    bounded(value, 0, MAX_COUNTED_MISSES * 1024)
}

fn nullable(value: &Value) -> Option<Value> {
    (!matches!(value, Value::Null)).then(|| value.clone())
}

fn sha256_text(value: &str) -> bool {
    SHA256_RE.is_full(value)
}

fn routine_id_text(value: &str) -> bool {
    ROUTINE_ID_RE.is_full(value)
}

fn assistants(value: &Value) -> Decoded<Vec<(String, String)>> {
    let Value::Array(pairs) = value else {
        return Err(StoreError::Malformed);
    };
    pairs
        .iter()
        .map(|pair| match pair {
            Value::Array(items) if items.len() == 2 => Ok((string(&items[0])?, string(&items[1])?)),
            _ => Err(StoreError::Malformed),
        })
        .collect()
}

fn decode_routine(value: &Value) -> Decoded<Routine> {
    exactly(value, &ROUTINE_FIELDS)?;
    let Value::Array(permitted) = at(value, "permitted") else {
        return Err(StoreError::Malformed);
    };
    let output_digest = string(at(value, "output_digest"))?;
    require(output_digest.is_empty() || sha256_text(&output_digest))?;
    let rollup_usage = nullable(at(value, "rollup_usage"));
    require(
        rollup_usage
            .as_ref()
            .is_none_or(|usage| canonical_run_usage(usage).is_some()),
    )?;
    let decoded = Routine {
        routine_id: string(at(value, "routine_id"))?,
        name: string(at(value, "name"))?,
        schedule: at(value, "schedule").clone(),
        assistants: assistants(at(value, "assistants"))?,
        plan: at(value, "plan").clone(),
        anchor: instant(at(value, "anchor"))?,
        next_run_at: instant(at(value, "next_run_at"))?,
        description: string(at(value, "description"))?,
        needs_reconfirm: boolean(at(value, "needs_reconfirm"))?,
        deleting: boolean(at(value, "deleting"))?,
        gap_started_at: instant(at(value, "gap_started_at"))?,
        missed: count(at(value, "missed"))?,
        reported_missed: count(at(value, "reported_missed"))?,
        revision: integer(at(value, "revision"))?,
        timezone_source: string(at(value, "timezone_source"))?,
        paused: boolean(at(value, "paused"))?,
        confirmation: nullable(at(value, "confirmation")),
        permitted: permitted.clone(),
        failures: count(at(value, "failures"))?,
        rollup_minute: instant(at(value, "rollup_minute"))?,
        rollup_runs: bounded(
            at(value, "rollup_runs"),
            0,
            i64::try_from(MAX_ROLLUP_RUNS).unwrap_or(i64::MAX),
        )?,
        rollup_usage,
        run_requested: instant(at(value, "run_requested"))?,
        output_digest,
    };
    require(definition_valid(&decoded) && definition_bytes(&decoded) <= MAX_DEFINITION_BYTES)?;
    Ok(decoded)
}

/// None yet, or exactly this run's own or continuation generation in a Team network; never a verification's.
#[must_use]
pub fn generation_of(run_id: &str, generation: &str) -> bool {
    if generation.is_empty() {
        return true;
    }
    let Some((network, rest)) = generation.split_once(":routine:") else {
        return false;
    };
    let own = rest == run_id || rest.strip_suffix(":s1") == Some(run_id);
    network.len() == 64
        && network
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
        && own
}

/// The parts of a run record its status admits, by name.
struct RunShape<'v> {
    value: &'v Value,
}

impl RunShape<'_> {
    fn text(&self, name: &str) -> &str {
        match at(self.value, name) {
            Value::String(text) => text,
            _ => "",
        }
    }

    fn no_call(&self) -> bool {
        self.text("assistant_id").is_empty()
            && self.text("action").is_empty()
            && matches!(at(self.value, "position"), Value::Null)
            && small_int(at(self.value, "steps")) == Some(0)
    }

    fn no_request(&self) -> bool {
        self.text("request_kind").is_empty() && self.no_call()
    }

    fn unleased(&self) -> bool {
        self.text("lease_sha256").is_empty()
            && self.text("lease_key").is_empty()
            && small_int(at(self.value, "lease_expires_at")) == Some(0)
    }

    /// A named Assistant Action.
    fn action(&self) -> bool {
        canonical_assistant_id(at(self.value, "assistant_id")).is_some()
            && canonical_action_id(at(self.value, "action")).is_some()
    }

    /// A held run's incident: unleased, of its held generation, at its held call by position, or none sealed.
    fn incident(&self) -> bool {
        self.unleased()
            && !self.text("generation").is_empty()
            && self.text("request_kind").is_empty()
            && (self.no_call()
                || (self.action()
                    && canonical_position(at(self.value, "position"), at(self.value, "steps"))
                        .is_some()))
    }

    /// Each status admits exactly its own fields, so teardown and recovery never act on a mixed record.
    fn admits(&self, status: RunStatus) -> bool {
        match status {
            RunStatus::Leased => {
                let key = self.text("lease_key");
                sha256_text(self.text("lease_sha256"))
                    && (key == HUMAN_LEASE || sha256_text(key))
                    && self.no_request()
            }
            RunStatus::Frozen => {
                self.unleased()
                    && REQUEST_KINDS.contains(&self.text("request_kind"))
                    && self.action()
                    && matches!(at(self.value, "position"), Value::Null)
                    && small_int(at(self.value, "steps")) == Some(0)
            }
            RunStatus::Held => {
                self.unleased() && self.no_request() && !self.text("generation").is_empty()
            }
            RunStatus::Unresolved | RunStatus::Skipped => self.incident(),
        }
    }
}

/// The run record's scalar members are each of their kind and within their bounds.
fn run_members(value: &Value, shape: &RunShape<'_>) -> Decoded<()> {
    let strings = [
        "run_id",
        "routine_id",
        "status",
        "lease_sha256",
        "lease_key",
        "request_kind",
        "assistant_id",
        "action",
        "generation",
    ];
    require(
        strings
            .iter()
            .all(|name| matches!(at(value, name), Value::String(_))),
    )?;
    let run_id = shape.text("run_id");
    require(
        routine_id_text(run_id)
            && routine_id_text(shape.text("routine_id"))
            && generation_of(run_id, shape.text("generation"))
            && small_int(at(value, "active_seconds_left"))
                .is_some_and(|left| left <= ACTIVE_SECONDS)
            && small_int(at(value, "notice_version")).is_some_and(|version| version >= 0)
            && small_int(at(value, "requests_used"))
                .is_some_and(|used| (0..=MAX_REQUESTS_PER_TURN).contains(&used))
            && valid_revision(at(value, "revision"))
            && small_int(at(value, "steps")).is_some(),
    )?;
    let usage = at(value, "usage");
    require(canonical_run_usage(usage).is_some_and(|canonical| py_eq(&canonical, usage)))
}

fn decode_run(value: &Value) -> Decoded<Run> {
    exactly(value, &RUN_FIELDS)?;
    let shape = RunShape { value };
    run_members(value, &shape)?;
    let protection_lost = boolean(at(value, "protection_lost"))?;
    let lease_expires_at = instant(at(value, "lease_expires_at"))?;
    let status = RunStatus::parse(shape.text("status")).ok_or(StoreError::Malformed)?;
    require(shape.admits(status))?;
    let position = match at(value, "position") {
        Value::Null => None,
        other => Some(integer(other)?),
    };
    let owned = |name: &str| shape.text(name).to_owned();
    Ok(Run {
        run_id: owned("run_id"),
        routine_id: owned("routine_id"),
        status,
        lease_sha256: owned("lease_sha256"),
        lease_key: owned("lease_key"),
        lease_expires_at,
        active_seconds_left: integer(at(value, "active_seconds_left"))?,
        request_kind: owned("request_kind"),
        assistant_id: owned("assistant_id"),
        action: owned("action"),
        generation: owned("generation"),
        notice_version: integer(at(value, "notice_version"))?,
        requests_used: integer(at(value, "requests_used"))?,
        position,
        steps: integer(at(value, "steps"))?,
        usage: at(value, "usage").clone(),
        protection_lost,
        revision: integer(at(value, "revision"))?,
    })
}

fn decode_notice(value: &Value) -> Decoded<Notice> {
    let members = exactly(value, &NOTICE_FIELDS)?;
    let run_id = string(at(value, "run_id"))?;
    // A stored notice is exactly its wire form for some Team, with the empty run id of a Routine outcome as null.
    let mut wire: Vec<(String, Value)> = members
        .iter()
        .filter(|(name, _)| name != "run_id" && name != "created_at")
        .cloned()
        .collect();
    wire.push(("team_id".to_owned(), text("stored")));
    wire.push((
        "run_id".to_owned(),
        if run_id.is_empty() {
            Value::Null
        } else {
            text(&run_id)
        },
    ));
    wire.push(("created_at".to_owned(), text("1970-01-01T00:00:00Z")));
    require(canonical_notice(&Value::Object(wire)).is_some())?;
    Ok(Notice {
        notice_id: string(at(value, "notice_id"))?,
        routine_id: string(at(value, "routine_id"))?,
        run_id,
        outcome: string(at(value, "outcome"))?,
        created_at: instant(at(value, "created_at"))?,
        detail: at(value, "detail").clone(),
        version: integer(at(value, "version"))?,
        name: string(at(value, "name"))?,
        usage: nullable(at(value, "usage")),
        protection_lost: boolean(at(value, "protection_lost"))?,
    })
}

fn decode_discard(value: &Value) -> Decoded<(String, String)> {
    match value {
        Value::Array(items) if items.len() == 2 => {
            let (run_id, generation) = (string(&items[0])?, string(&items[1])?);
            require(routine_id_text(&run_id) && generation_of(&run_id, &generation))?;
            Ok((run_id, generation))
        }
        _ => Err(StoreError::Malformed),
    }
}

/// One start: its Routine, instant, and the business steps it reserved.
fn decode_start(value: &Value) -> Decoded<Start> {
    match value {
        Value::Array(items) if items.len() == 3 => {
            let routine_id = string(&items[0])?;
            require(routine_id_text(&routine_id))?;
            let at = instant(&items[1])?;
            let steps = bounded(&items[2], 1, i64::try_from(MAX_STEPS).unwrap_or(i64::MAX))?;
            Ok((routine_id, at, steps))
        }
        _ => Err(StoreError::Malformed),
    }
}

fn list(value: &Value, maximum: usize) -> Decoded<&[Value]> {
    match value {
        Value::Array(items) if items.len() <= maximum => Ok(items),
        _ => Err(StoreError::Malformed),
    }
}

/// Decodes a Team's state file, refusing anything Team does not write.
///
/// # Errors
/// [`StoreError::NotJson`] for bytes that are not one strict JSON document, [`StoreError::Malformed`] for any other
/// departure from the contract.
pub fn decode(payload: &[u8], team_id: &str) -> Decoded<TeamRoutines> {
    let value = strict_json::parse(payload).map_err(|_| StoreError::NotJson)?;
    exactly(&value, &STATE_FIELDS)?;
    require(
        small_int(at(&value, "schema")) == Some(SCHEMA) && *at(&value, "team_id") == text(team_id),
    )?;
    let notices = MAX_UNDELIVERED_NOTICES + MAX_ROUTINE_NOTICES;
    let state = TeamRoutines {
        routines: list(at(&value, "routines"), MAX_ROUTINES)?
            .iter()
            .map(decode_routine)
            .collect::<Decoded<_>>()?,
        runs: list(at(&value, "runs"), MAX_RUNS)?
            .iter()
            .map(decode_run)
            .collect::<Decoded<_>>()?,
        notices: list(at(&value, "notices"), notices)?
            .iter()
            .map(decode_notice)
            .collect::<Decoded<_>>()?,
        served_at: instant(at(&value, "served_at"))?,
        starts: list(
            at(&value, "starts"),
            usize::try_from(MAX_STARTS).unwrap_or(usize::MAX),
        )?
        .iter()
        .map(decode_start)
        .collect::<Decoded<_>>()?,
        discards: list(at(&value, "discards"), MAX_DISCARDS)?
            .iter()
            .map(decode_discard)
            .collect::<Decoded<_>>()?,
    };
    require(consistent(&state))?;
    Ok(state)
}

fn distinct<T: Ord>(mut items: Vec<T>) -> bool {
    let total = items.len();
    items.sort();
    items.dedup();
    items.len() == total
}

/// The invariants across records.
fn consistent(state: &TeamRoutines) -> bool {
    let identifiers: Vec<&str> = state
        .routines
        .iter()
        .map(|item| item.routine_id.as_str())
        .collect();
    let definitions: usize = state.routines.iter().map(definition_bytes).sum();
    distinct(identifiers.clone())
        && state.starts.windows(2).all(|pair| pair[0].1 <= pair[1].1)
        && definitions <= TEAM_DEFINITION_BYTES
        && distinct(state.runs.iter().map(|item| item.run_id.as_str()).collect())
        && distinct(state.notices.iter().map(|item| item.notice_id.as_str()).collect())
        && distinct(state.discards.iter().collect())
        && record::live_runs(state).len() <= MAX_ROUTINES
        && state.runs.iter().filter(|item| item.status == RunStatus::Unresolved).count() <= MAX_UNRESOLVED_INCIDENTS
        // No run record outlives its Routine: a deletion completes only once each of its incidents was released.
        && state.runs.iter().all(|item| identifiers.contains(&item.routine_id.as_str()))
}
