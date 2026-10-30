//! The recovery card of a held Routine run (`local/routine/card.py`, ADR-0092 section 7, ADR-0101): Rodar.
//!
//! A card exists only when automatic verification could not prove what the failed call did. It shows the call and the
//! failure Team recorded for it, and carries an entity tag: the digest of exactly the state its answer must still find.
//! An answer names that tag, as an HTTP `If-Match` names the entity it expects; Team answers it only in the Team's
//! execution slot while the state there has exactly that tag, and checks the same state again in the answer's own
//! write. Rodar sets the held run aside without verifying it and requests one fresh run of the current revision; it
//! never starts while the held attempt's workload could still be running, while another run of the Routine is live,
//! or once the Routine is deleted.

use sha2::{Digest, Sha256};
use strict_json::{Number, Value};
use subtle::ConstantTimeEq;
use team_protocol::python::json::canonical;
use team_protocol::python::value::object_of;
use team_protocol::routine_run::canonical_card_answer_request;
use team_routine::hold::{self, Expected};
use team_routine::record::{self, Routine, Run};

use super::incident::OpenedRecovery;
use super::service::RoutineService;
use crate::chat::problem;
use crate::http::service::{Body, Problem};

/// A held run's card state as Team finds it now, and its entity tag.
struct Tagged {
    tag: String,
    expected: Expected,
    opened: OpenedRecovery,
    value: Run,
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn int(value: i64) -> Value {
    Value::Number(Number::Int(value))
}

fn conflict(message: &'static str, code: &'static str) -> Problem {
    Problem::new(409, message, code)
}

impl RoutineService {
    /// The unresolved incident's card state now, and the entity tag over exactly what an answer must still find.
    fn tagged(&self, team_id: &str, incident_id: &str) -> Result<Tagged, Problem> {
        let value = self.unresolved(team_id, incident_id)?;
        let opened = self.open_recovery(team_id, incident_id)?;
        let current = hold::current_revision(&self.load(team_id)?, &value.routine_id);
        let material = Value::Array(vec![
            text(&opened.recovery.binding.incarnation),
            text(incident_id),
            text(&value.routine_id),
            int(value.revision),
            text(&value.generation),
            opened
                .cursor
                .operation_id
                .as_deref()
                .map_or(Value::Null, text),
            int(current),
        ]);
        let tag = crate::chat::hex(&Sha256::digest(canonical(&material)));
        let expected = Expected {
            revision: value.revision,
            generation: value.generation.clone(),
            current,
        };
        Ok(Tagged {
            tag,
            expected,
            opened,
            value,
        })
    }

    /// The held operation's latest recorded diagnostic, never one of an earlier operation of the run.
    fn card_evidence(
        &self,
        team_id: &str,
        opened: &OpenedRecovery,
        incident_id: &str,
    ) -> (&'static str, Value) {
        let incarnation = &opened.recovery.binding.incarnation;
        let diagnostics = &self.parts.diagnostics;
        let Ok(found) = diagnostics.read(team_id, incarnation, incident_id, self.now()) else {
            return ("unavailable", Value::Null);
        };
        let operation = opened.cursor.operation_id.as_deref();
        match found
            .iter()
            .rev()
            .find(|item| Some(item.operation_id.as_str()) == operation)
        {
            Some(latest) => ("recorded", latest.view()),
            None => ("absent", Value::Null),
        }
    }

    /// Opens the recovery card of one held run for the person who will answer it.
    ///
    /// # Errors
    /// `routine-incident-unavailable`, `routine-incident-unverifiable`, or a state problem.
    pub fn open_routine_card(&self, team_id: &str, incident_id: &str) -> Result<Value, Problem> {
        let tagged = self.tagged(team_id, incident_id)?;
        // The call the hold indexed, the one its incident view names.
        let (assistant_id, action, position, steps) = hold::held_step(&tagged.value);
        let (mut evidence, mut diagnostic) =
            self.card_evidence(team_id, &tagged.opened, incident_id);
        let same_call = diagnostic.get("assistant_id") == Some(&text(&assistant_id))
            && diagnostic.get("action") == Some(&text(&action))
            && diagnostic.get("position") == Some(&position.map_or(Value::Null, int));
        if evidence == "recorded" && !same_call {
            (evidence, diagnostic) = ("absent", Value::Null);
        }
        Ok(object_of(vec![
            ("team_id", text(team_id)),
            ("run_id", text(incident_id)),
            ("assistant_id", text(&assistant_id)),
            ("action", text(&action)),
            ("position", position.map_or(Value::Null, int)),
            ("steps", int(steps)),
            ("evidence", text(evidence)),
            ("diagnostic", diagnostic),
            ("tag", text(&tagged.tag)),
        ]))
    }

    /// The Routine a fresh run may start for: listed, idle, and with the held attempt stopped.
    async fn restartable(&self, team_id: &str, tagged: &Tagged) -> Result<Routine, Problem> {
        let routine_id = &tagged.value.routine_id;
        let state = self.load(team_id)?;
        let current = state
            .routines
            .iter()
            .find(|item| item.routine_id == *routine_id && !item.deleting)
            .cloned()
            .ok_or_else(|| conflict("Routine is unavailable", "routine-not-found"))?;
        if record::live_runs(&state)
            .iter()
            .any(|item| item.routine_id == *routine_id)
        {
            return Err(conflict(
                "another run of this Routine is live",
                "routine-busy",
            ));
        }
        if !self.workload_stopped(team_id, &tagged.opened).await? {
            return Err(conflict(
                "the held attempt may still be running",
                "routine-workload-unquiesced",
            ));
        }
        Ok(current)
    }

    /// Rodar: sets the held run aside and requests one fresh run of the Routine's current revision.
    async fn run_card(&self, team_id: &str, tagged: &Tagged) -> Result<(), Problem> {
        let current = self.restartable(team_id, tagged).await?;
        let contracts_changed = || {
            conflict(
                "the Routine's Assistants changed",
                "routine-contracts-changed",
            )
        };
        if current.needs_reconfirm {
            return Err(contracts_changed());
        }
        let pinned = current.assistants.iter().cloned().collect();
        let parts = &self.parts;
        let locks = &parts.coordination.locks;
        super::contracts::require_pinned(
            parts.team.as_ref(),
            locks,
            team_id,
            &pinned,
            contracts_changed(),
        )
        .await?;
        let now = self.now();
        let entropy = self.parts.entropy.clone();
        let incident_id = tagged.value.run_id.clone();
        self.set_aside(team_id, &incident_id, |state| {
            hold::run_incident(state, &incident_id, now, &tagged.expected, entropy.as_ref())
        })?;
        Ok(())
    }

    /// Answers one recovery card with Rodar, only while the held run's state still has the card's tag; checked and
    /// applied in the Team's execution slot.
    ///
    /// # Errors
    /// `invalid-body`, `routine-card-stale`, a refusal of Rodar, the slot's refusal, or a state problem.
    pub async fn answer_routine_card(
        &self,
        team_id: &str,
        incident_id: &str,
        body: &Body,
    ) -> Result<Value, Problem> {
        let answer = match body {
            Body::Json(body) => canonical_card_answer_request(body),
            _ => None,
        };
        let Some(Value::String(tag)) = answer
            .as_ref()
            .and_then(|answer| answer.get("tag"))
            .cloned()
        else {
            return Err(Problem::new(
                422,
                "a card answer is its tag",
                "invalid-body",
            ));
        };
        let routine_id = self.unresolved(team_id, incident_id)?.routine_id;
        {
            let _execution = self.admit_routine(team_id, &routine_id)?;
            let tagged = self.tagged(team_id, incident_id)?;
            if !bool::from(tagged.tag.as_bytes().ct_eq(tag.as_bytes())) {
                return Err(problem::routine_card_stale());
            }
            self.run_card(team_id, &tagged).await?;
        }
        self.audit("routine-card", true, team_id, &format!("{incident_id}:run"))
            .await;
        Ok(object_of(vec![
            ("team_id", text(team_id)),
            ("run_id", text(incident_id)),
        ]))
    }
}
