//! Routine run outcomes for delivery (ADR-0086).
//!
//! Admin's automatic delivery only reads notices and acknowledges exact versions; it never changes a run. A held run is
//! settled through its recovery card instead (ADR-0092).

use std::collections::BTreeMap;

use strict_json::{Number, Value};
use team_protocol::identifiers::canonical_team_id;
pub(crate) use team_protocol::python::value::object_of;
use team_protocol::python::value::small_int;
use team_protocol::routine::encoded_bytes;
use team_protocol::routine_notice::MAX_NOTICE_BATCH_BYTES;
use team_protocol::routine_run::MAX_ACK_DELIVERIES;
use team_routine::record::{self, Notice};
use team_storage::private::instant_text;

use super::state::{self, LostRuns, StateUnavailable};
use super::store::RoutineStore;

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

/// One undelivered notice as Admin receives it for `team_id`.
#[must_use]
pub fn wire(team_id: &str, notice: &Notice) -> Value {
    object_of(vec![
        ("team_id", text(team_id)),
        ("notice_id", text(&notice.notice_id)),
        ("version", Value::Number(Number::Int(notice.version))),
        ("routine_id", text(&notice.routine_id)),
        ("name", text(&notice.name)),
        (
            "run_id",
            if notice.run_id.is_empty() {
                Value::Null
            } else {
                text(&notice.run_id)
            },
        ),
        ("outcome", text(&notice.outcome)),
        ("created_at", text(&instant_text(notice.created_at))),
        ("detail", notice.detail.clone()),
        ("usage", notice.usage.clone().unwrap_or(Value::Null)),
        ("protection_lost", Value::Bool(notice.protection_lost)),
    ])
}

/// One delivery batch, and the Teams whose state could not be read, which the caller audits.
#[derive(Clone, Debug, PartialEq)]
pub struct Batch {
    /// `{"notices": [...], "more": bool}`.
    pub body: Value,
    /// The identified Teams whose damaged state was skipped; their notices stay undelivered until they read again.
    pub skipped: Vec<String>,
}

/// A bounded batch of every readable Team's undelivered notices, for Admin to write to each Team's transcript.
///
/// A batch's encoded notice list stays within the protocol's byte bound and always holds at least one notice, which
/// fits even at its bound, so Admin drains any backlog by acknowledging each batch and asking again while `more`.
///
/// # Errors
/// [`StateUnavailable`] when the Teams cannot be enumerated; one identified Team's damaged state is only skipped.
pub fn batch(store: &RoutineStore) -> Result<Batch, StateUnavailable> {
    let mut notices: Vec<Value> = Vec::new();
    let mut skipped = Vec::new();
    // The list's brackets.
    let mut size = 2;
    for team in store.teams()? {
        let Ok(current) = store.load(&team) else {
            skipped.push(team);
            continue;
        };
        for notice in &current.notices {
            let item = wire(&team, notice);
            let cost = encoded_bytes(&item) + usize::from(!notices.is_empty());
            if !notices.is_empty() && size + cost > MAX_NOTICE_BATCH_BYTES {
                return Ok(Batch {
                    body: object_of(vec![
                        ("notices", Value::Array(notices)),
                        ("more", Value::Bool(true)),
                    ]),
                    skipped,
                });
            }
            notices.push(item);
            size += cost;
        }
    }
    Ok(Batch {
        body: object_of(vec![
            ("notices", Value::Array(notices)),
            ("more", Value::Bool(false)),
        ]),
        skipped,
    })
}

/// Why an acknowledgement was refused.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AckError {
    /// The body is not 1 to 256 exact deliveries (`routine-deliveries-invalid`, 422).
    Invalid,
    /// A delivery names an invalid Team (`invalid-team-id`, 422).
    InvalidTeam,
    /// A Team's state could not be written (`routine-state-unavailable`, 503).
    Unavailable,
}

impl From<StateUnavailable> for AckError {
    fn from(_error: StateUnavailable) -> Self {
        Self::Unavailable
    }
}

/// The exact notice versions of an acknowledgement body, by Team.
///
/// # Errors
/// [`AckError::Invalid`] or [`AckError::InvalidTeam`].
pub fn deliveries(body: &Value) -> Result<BTreeMap<String, Vec<(String, i64)>>, AckError> {
    let listed = match body {
        Value::Object(members) if members.len() == 1 => body.get("deliveries"),
        _ => None,
    };
    let Some(Value::Array(items)) = listed else {
        return Err(AckError::Invalid);
    };
    if items.is_empty() || items.len() > MAX_ACK_DELIVERIES {
        return Err(AckError::Invalid);
    }
    let mut by_team: BTreeMap<String, Vec<(String, i64)>> = BTreeMap::new();
    for item in items {
        let exact = matches!(item, Value::Object(members) if members.len() == 3);
        let notice = match item.get("notice_id") {
            Some(Value::String(notice)) => Some(notice.clone()),
            _ => None,
        };
        let version = item.get("version").and_then(small_int);
        let team = item.get("team_id");
        let (true, Some(notice), Some(version), Some(team)) = (exact, notice, version, team) else {
            return Err(AckError::Invalid);
        };
        let team = canonical_team_id(team).ok_or(AckError::InvalidTeam)?;
        let pairs = by_team.entry(team.to_owned()).or_default();
        if !pairs.contains(&(notice.clone(), version)) {
            pairs.push((notice, version));
        }
    }
    Ok(by_team)
}

/// Admin wrote these exact notice versions to the transcripts; a notice updated since stays undelivered.
///
/// # Errors
/// [`AckError`].
pub fn acknowledge(store: &RoutineStore, lost: LostRuns<'_>, body: &Value) -> Result<(), AckError> {
    for (team, delivered) in deliveries(body)? {
        state::update(store, &team, lost, |current| {
            Ok::<_, AckError>((record::acknowledge(&current, &delivered), ()))
        })?;
    }
    Ok(())
}

#[cfg(test)]
mod tests;
