//! Holding a Routine run for a person, and the lifecycle of its incident (`local/routine/incident.py`, ADR-0092 sections
//! 5 and 7).
//!
//! A run seals its immutable recovery snapshot before its first dispatch: the exact cursor binding (Team incarnation,
//! Routine and revision, run), the confirmed plan, and the Routine's name (ADR-0101). A hold fences the run's live
//! lease, seals its incident's compact evidence with its own copy of that snapshot, archives its journal batch, and
//! indexes the run as its own incident; each step is idempotent and `RoutineService::reconcile` resumes from
//! whichever came last, so every crash window recovers without dispatching anything.

use strict_json::{Number, Value};
use team_action::journal::OperationRecord;
use team_protocol::python::json::{Options, canonical, dumps};
use team_protocol::python::value::object_of;
use team_protocol::routine::exact_name;
use team_routine::claim::network_of;
use team_routine::cursor::{Binding, Cursor, binding_valid};
use team_routine::plan::{digest, well_formed};
use team_routine::record::Run;

use super::service::RoutineService;
use crate::chat::problem;
use crate::http::service::Problem;

pub mod card;
pub mod hold;
pub mod settle;

/// The version of a sealed recovery snapshot and of an incident's evidence.
pub const VERSION: i64 = 1;

/// What recovery of one held run is authorized to use: its cursor binding, plan, and its Routine's name.
#[derive(Clone, Debug, PartialEq)]
pub struct Recovery {
    /// The cursor binding.
    pub binding: Binding,
    /// The Routine's name.
    pub name: String,
    /// The confirmed plan document.
    pub plan: Value,
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn int(value: i64) -> Value {
    Value::Number(Number::Int(value))
}

impl Recovery {
    /// The plan's digest.
    #[must_use]
    pub fn plan_digest(&self) -> String {
        digest(&self.plan)
    }

    /// The plan's steps.
    #[must_use]
    pub fn steps(&self) -> &[Value] {
        match self.plan.get("steps") {
            Some(Value::Array(steps)) => steps,
            _ => &[],
        }
    }

    /// Whether it names exactly its run's Routine and the Team incarnation the run's generation runs in.
    #[must_use]
    pub fn of_run(&self, routine_id: &str, generation: &str) -> bool {
        let binding = &self.binding;
        binding.routine_id == routine_id
            && network_of(generation, &binding.run_id).as_deref() == Some(&binding.incarnation)
    }

    /// Whether a cursor runs exactly this snapshot's plan; one of another plan proves nothing about the run.
    #[must_use]
    pub fn runs(&self, cursor: &Cursor) -> bool {
        cursor.plan == self.plan_digest()
    }

    fn document(&self) -> Vec<(&'static str, Value)> {
        let binding = &self.binding;
        vec![
            (
                "binding",
                Value::Array(vec![
                    text(&binding.incarnation),
                    text(&binding.routine_id),
                    int(binding.revision),
                    text(&binding.run_id),
                ]),
            ),
            ("name", text(&self.name)),
            ("plan", self.plan.clone()),
        ]
    }

    /// The canonical bytes a run seals before its first dispatch.
    #[must_use]
    pub fn sealed(&self) -> Vec<u8> {
        let mut members = vec![("version", int(VERSION))];
        members.extend(self.document());
        canonical(&object_of(members))
    }
}

/// A held run's snapshot and the cursor reopened under exactly its binding.
#[derive(Clone, Debug, PartialEq)]
pub struct OpenedRecovery {
    /// The snapshot.
    pub recovery: Recovery,
    /// Its cursor.
    pub cursor: Cursor,
}

fn exactly<'v>(value: &'v Value, fields: &[&str]) -> Option<&'v [(String, Value)]> {
    match value {
        Value::Object(members)
            if members.len() == fields.len()
                && members
                    .iter()
                    .all(|(name, _)| fields.contains(&name.as_str())) =>
        {
            Some(members)
        }
        _ => None,
    }
}

fn binding_of(value: &Value) -> Option<Binding> {
    let Value::Array(parts) = value else {
        return None;
    };
    let [incarnation, routine, revision, run] = parts.as_slice() else {
        return None;
    };
    let as_text = |value: &Value| match value {
        Value::String(text) => Some(text.clone()),
        _ => None,
    };
    let Value::Number(Number::Int(revision)) = revision else {
        return None;
    };
    Some(Binding {
        incarnation: as_text(incarnation)?,
        routine_id: as_text(routine)?,
        revision: *revision,
        run_id: as_text(run)?,
    })
}

/// Admits one recovery snapshot document only for exactly the run it belongs to.
#[must_use]
pub fn read_recovery(value: &Value, run_id: &str) -> Option<Recovery> {
    exactly(value, &["binding", "name", "plan"])?;
    let name = value.get("name")?;
    let snapshot = Recovery {
        binding: binding_of(value.get("binding")?)?,
        name: match name {
            Value::String(name) => name.clone(),
            _ => return None,
        },
        plan: value.get("plan")?.clone(),
    };
    let valid = binding_valid(&snapshot.binding)
        && snapshot.binding.run_id == run_id
        && exact_name(name)
        && well_formed(&snapshot.plan);
    valid.then_some(snapshot)
}

/// Admits one sealed recovery snapshot's bytes only for exactly the run it belongs to.
#[must_use]
pub fn read_sealed(payload: &[u8], run_id: &str) -> Option<Recovery> {
    let value = strict_json::parse(payload).ok()?;
    let Value::Object(members) = &value else {
        return None;
    };
    if value.get("version") != Some(&int(VERSION)) {
        return None;
    }
    let document = Value::Object(
        members
            .iter()
            .filter(|(name, _)| name != "version")
            .cloned()
            .collect(),
    );
    read_recovery(&document, run_id)
}

/// One held run's sealed, result-free safety evidence.
#[derive(Clone, Debug, PartialEq)]
pub struct Evidence {
    /// The run's Routine.
    pub routine_id: String,
    /// The run's generation when it was held.
    pub generation: String,
    /// The held batch's fingerprint, if the generation held one.
    pub fingerprint: Option<String>,
    /// Each operation: `[interrupt_id, operation_id, state, attempts, origin]`.
    pub operations: Vec<Value>,
    /// Its own copy of the run's recovery snapshot, if the run sealed one.
    pub recovery: Option<Recovery>,
}

impl Evidence {
    /// The journal state of `operation_id`, if the evidence names it.
    #[must_use]
    pub fn state_of(&self, operation_id: Option<&str>) -> Option<String> {
        let operation_id = operation_id?;
        self.operations.iter().find_map(|item| match item {
            Value::Array(fields) if fields.get(1) == Some(&text(operation_id)) => {
                match fields.get(2) {
                    Some(Value::String(state)) => Some(state.clone()),
                    _ => None,
                }
            }
            _ => None,
        })
    }
}

fn operation(record: &OperationRecord) -> Value {
    Value::Array(vec![
        text(&record.interrupt_id),
        text(&record.operation_id),
        text(&record.state),
        int(record.attempts),
        record.origin.as_deref().map_or(Value::Null, text),
    ])
}

/// The compact evidence of one held run's batch, with its own copy of the recovery snapshot.
#[must_use]
pub fn evidence(
    incident_id: &str,
    value: &Run,
    batch: (Option<&str>, &[OperationRecord]),
    snapshot: Option<&Recovery>,
) -> Vec<u8> {
    let (fingerprint, operations) = batch;
    let document = object_of(vec![
        ("version", int(VERSION)),
        ("incident_id", text(incident_id)),
        ("routine_id", text(&value.routine_id)),
        ("generation", text(&value.generation)),
        ("fingerprint", fingerprint.map_or(Value::Null, text)),
        (
            "operations",
            Value::Array(operations.iter().map(operation).collect()),
        ),
        (
            "recovery",
            snapshot.map_or(Value::Null, |snapshot| object_of(snapshot.document())),
        ),
    ]);
    let options = Options {
        ensure_ascii: false,
        sort_keys: true,
        compact: true,
    };
    dumps(&document, options).into_bytes()
}

const EVIDENCE_FIELDS: [&str; 7] = [
    "version",
    "incident_id",
    "routine_id",
    "generation",
    "fingerprint",
    "operations",
    "recovery",
];

/// Admits sealed evidence only for exactly the incident it was sealed for.
#[must_use]
pub fn read_evidence(payload: &[u8], incident_id: &str) -> Option<Evidence> {
    let value = strict_json::parse(payload).ok()?;
    exactly(&value, &EVIDENCE_FIELDS)?;
    let field = |name: &str| value.get(name).unwrap_or(&Value::Null);
    let string = |name: &str| match field(name) {
        Value::String(text) => Some(text.clone()),
        _ => None,
    };
    let (Value::Array(operations), true) = (
        field("operations"),
        field("version") == &int(VERSION) && string("incident_id").as_deref() == Some(incident_id),
    ) else {
        return None;
    };
    let fingerprint = match field("fingerprint") {
        Value::Null => None,
        Value::String(fingerprint) => Some(fingerprint.clone()),
        _ => return None,
    };
    let evidence = Evidence {
        routine_id: string("routine_id")?,
        generation: string("generation")?,
        fingerprint,
        operations: operations.clone(),
        recovery: None,
    };
    with_recovery(evidence, field("recovery"), incident_id)
}

fn with_recovery(mut evidence: Evidence, recovery: &Value, incident_id: &str) -> Option<Evidence> {
    if *recovery == Value::Null {
        return Some(evidence);
    }
    let snapshot = read_recovery(recovery, incident_id)?;
    if !snapshot.of_run(&evidence.routine_id, &evidence.generation) {
        return None;
    }
    evidence.recovery = Some(snapshot);
    Some(evidence)
}

impl RoutineService {
    /// Seals a replayed run's recovery snapshot before its first dispatch; every later segment reseals the same bytes.
    ///
    /// # Errors
    /// `routine-state-unavailable`.
    pub(super) fn seal_recovery(&self, team_id: &str, snapshot: &Recovery) -> Result<(), Problem> {
        let payload = snapshot.sealed();
        if read_sealed(&payload, &snapshot.binding.run_id).is_none() {
            return Err(problem::routine_state_unavailable());
        }
        self.parts
            .store
            .lock(team_id)
            .and_then(|lock| lock.put_recovery(&snapshot.binding.run_id, &payload))
            .map_err(|_| problem::routine_state_unavailable())
    }
}

/// The run's sealed recovery snapshot, which must belong to exactly this run, or `None` for an unreplayed run, read
/// under the Team's held Routine lock.
///
/// # Errors
/// `routine-state-unavailable`.
pub(super) fn snapshot_locked(
    lock: &super::store::TeamLock<'_>,
    value: &Run,
) -> Result<Option<Recovery>, Problem> {
    let unavailable = problem::routine_state_unavailable;
    let Some(payload) = lock.recovery(&value.run_id).map_err(|_| unavailable())? else {
        return Ok(None);
    };
    let snapshot = read_sealed(&payload, &value.run_id).ok_or_else(unavailable)?;
    if !snapshot.of_run(&value.routine_id, &value.generation) {
        return Err(unavailable());
    }
    Ok(Some(snapshot))
}
