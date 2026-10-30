//! A committed turn's memory changes, recorded as activity before they are saved (ADR-0084, ADR-0121).
//!
//! Team commits each change as the machine actor and names the turn's author as its initiator; an event carries the
//! memory's opaque id, never its topic, preference, or the turn's request nonce.

use std::sync::Arc;

use strict_json::Value;
use team_core::entropy::token_hex;
use team_inference::memory::{Change, Plan, Source};
use team_protocol::python::value::object_of;

use super::problem;
use super::service::{Admitted, ChatService};
use crate::audit::{Outcome, Principal};
use crate::http::service::Problem;

/// The memory could not be saved; the turn fails.
#[must_use]
pub const fn store_failed() -> Problem {
    Problem::new(503, "Team memory could not be saved", "memory-store-failed")
}

fn activity(change: &Change, event_id: &str, phase: &str, source: &Source) -> Value {
    let mut members = vec![
        ("event_id", Value::String(event_id.to_owned())),
        ("phase", Value::String(phase.to_owned())),
        ("subject", Value::String(change.memory_id.clone())),
        ("incarnation", Value::String(source.incarnation.clone())),
        ("initiator_id", Value::String(source.actor_id.clone())),
    ];
    if phase == "failed" {
        members.push(("outcome", Value::String("memory-store-failed".to_owned())));
    }
    object_of(members)
}

impl ChatService {
    async fn record_memory(
        &self,
        team_id: &str,
        source: &Source,
        events: &[(Change, String)],
        phase: &str,
    ) -> Result<(), team_core::audit::AuditUnavailable> {
        let result = if phase == "failed" {
            Outcome::Error
        } else {
            Outcome::Ok
        };
        for (change, event_id) in events {
            let event = Principal::team()
                .event(change.operation, result)
                .with("team_id", Value::String(team_id.to_owned()))
                .with("activity", activity(change, event_id, phase, source));
            self.parts.audit.record(event).await?;
        }
        Ok(())
    }

    /// Commits a completed turn only when Stop did not win, saving its memory changes as it commits: one `requested`
    /// event per change first, then the save, then each change settled `completed` or `failed`.
    ///
    /// # Errors
    /// `chat-stopped` when Stop won; `memory-store-failed` when the memory could not be planned, recorded, or saved.
    pub(super) async fn commit_turn(
        &self,
        admitted: &Admitted,
        team_id: &str,
        changes: &[Value],
        source: Option<&Source>,
    ) -> Result<(), Problem> {
        let planned = match source.filter(|_| !changes.is_empty()) {
            None => None,
            Some(source) => Some((source, self.plan_memory(team_id, changes, source).await?)),
        };
        let Some((source, plan)) = planned.filter(|(_, plan)| !plan.changes.is_empty()) else {
            return admitted
                .execution
                .commit(|| Ok::<(), ()>(()))
                .map(|_| ())
                .ok_or(problem::chat_stopped());
        };
        let entropy = self.parts.entropy.as_ref();
        let events = plan
            .changes
            .iter()
            .map(|change| Ok((change.clone(), token_hex(entropy, 16)?)))
            .collect::<Result<Vec<_>, team_core::entropy::EntropyUnavailable>>()
            .map_err(|_| store_failed())?;
        if admitted.execution.is_cancelled() {
            return Err(problem::chat_stopped());
        }
        self.record_memory(team_id, source, &events, "requested")
            .await
            .map_err(|_| store_failed())?;
        let store = Arc::clone(&self.parts.inference);
        let saved = admitted
            .execution
            .commit(|| store.save_knowledge(team_id, &plan));
        let phase = if matches!(saved, Some(Ok(()))) {
            "completed"
        } else {
            "failed"
        };
        // A settle the journal cannot record after the save never fails the committed turn.
        let _ = self.record_memory(team_id, source, &events, phase).await;
        match saved {
            None => Err(problem::chat_stopped()),
            Some(Err(_)) => Err(store_failed()),
            Some(Ok(())) => Ok(()),
        }
    }

    async fn plan_memory(
        &self,
        team_id: &str,
        changes: &[Value],
        source: &Source,
    ) -> Result<Plan, Problem> {
        let store = Arc::clone(&self.parts.inference);
        let entropy = Arc::clone(&self.parts.entropy);
        let (team, changes, source) = (
            team_id.to_owned(),
            Value::Array(changes.to_vec()),
            source.clone(),
        );
        let now = team_storage::private::epoch_seconds(self.parts.clock.as_ref());
        tokio::task::spawn_blocking(move || {
            store.plan_knowledge(&team, &changes, &source, now, entropy.as_ref())
        })
        .await
        .map_err(|_| store_failed())?
        .map_err(|_| store_failed())
    }
}
