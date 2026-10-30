//! Refining a recorded Routine from its chat (`local/routine/refine.py`, ADR-0122).
//!
//! A recording send that selects one Routine shows the Brain that Routine's bounded definition and its latest run's
//! outcome. When the chat agent then calls `refine` on it, Team builds the next revision from that Routine's plan, the
//! agent's edits, and the calls the current send traced, admits it against the Team's exact current contracts and
//! budgets, and keeps a card like a recording's, which only the person confirms. A refinement asks no question.

use strict_json::Value;
use team_protocol::python::value::{int, object_of};
use team_protocol::routine_context::{canonical_refine, canonical_selected_routine};
use team_protocol::routine_proposal::canonical_refinement;
use team_routine::compose::permitted;
use team_routine::definition::{fits, over_budget, selected};
use team_routine::json::{as_str, at, elements};
use team_routine::plan::Contracts;
use team_routine::record::{Routine, TeamRoutines, change_room, live_runs, new_id};
use team_routine::recording::{Context, Recorded, known_texts, segment_lines, span_calls};
use team_routine::refine::{Refined, refine};
use team_storage::private::epoch_seconds;

use super::diagnostics::view::last_run;
use super::proposal::card::{admissible, bound_digest, card};
use super::proposal::{
    Admitted, PROPOSAL_SECONDS, Proposal, RoutineChat, Stopped, Turn, Write, pinned,
};
use super::recorder::Span;
use super::state::load;
use crate::chat::problem;
use crate::http::service::Problem;

/// The agent's `refine`, of the Routine the send selected at the revision it was shown; anything else is a Brain
/// contract failure.
fn outcome<'v>(span: &Span, proposed: &'v Value) -> Result<&'v Value, Problem> {
    let outcome = canonical_refine(proposed).ok_or(problem::brain_turn_failed())?;
    let routine_id = as_str(at(outcome, "routine_id"));
    let shown = span
        .revisions
        .iter()
        .find(|(id, _)| Some(id.as_str()) == routine_id)
        .map(|(_, revision)| int(*revision));
    if routine_id != span.selected.as_deref() || shown.as_ref() != outcome.get("base_revision") {
        return Err(problem::brain_turn_failed());
    }
    Ok(outcome)
}

/// The Routine the card refines, only at the revision the agent edited and while none of its runs is live.
fn base<'s>(state: &'s TeamRoutines, outcome: &Value) -> Result<&'s Routine, Stopped> {
    let routine_id = as_str(at(outcome, "routine_id")).unwrap_or_default();
    let found = state
        .routines
        .iter()
        .find(|item| item.routine_id == routine_id && !item.deleting);
    let found = found.ok_or_else(|| Stopped::refused("routine-not-found"))?;
    if Some(&int(found.revision)) != outcome.get("base_revision") {
        return Err(Stopped::refused("routine-revision-changed"));
    }
    if live_runs(state)
        .iter()
        .any(|run| run.routine_id == found.routine_id)
    {
        return Err(Stopped::refused("routine-busy"));
    }
    Ok(found)
}

/// The refined plan, built from the base plan, the edits, and the current send's traced calls.
fn refined(
    span: &Span,
    base: &Routine,
    edits: &[Value],
    contracts: &Contracts,
) -> Result<Refined, Stopped> {
    let refused = |error: team_routine::recording::RecordingError| Stopped::refused(error.code);
    let sends = &span.sends;
    let calls = span_calls(sends, contracts).map_err(refused)?;
    let texts: Vec<String> = sends
        .iter()
        .flat_map(|send| {
            segment_lines(&send.person)
                .into_iter()
                .chain(send.window.iter().cloned())
        })
        .collect();
    let known = known_texts(&texts, &span.protection);
    let zone = (base.timezone().to_owned(), base.timezone_source.clone());
    let latest = sends.len().saturating_sub(1);
    let current: Vec<_> = calls
        .iter()
        .filter(|call| call.send == latest)
        .cloned()
        .collect();
    let mut context = Context::new(sends, calls, known, zone, None, contracts);
    refine(&base.plan, edits, &mut context, &current).map_err(refused)
}

/// Each base step that does not continue, with its effect and whether it was removed or replaced.
fn removed(base: &Routine, refined: &Refined) -> Vec<Value> {
    let base_steps = elements(at(&base.plan, "steps"));
    refined
        .removed
        .iter()
        .map(|(position, change)| {
            let step = position
                .checked_sub(1)
                .and_then(|index| base_steps.get(index))
                .unwrap_or(&Value::Null);
            let (assistant, action) = (at(step, "assistant"), at(step, "action"));
            let read_only = base
                .permitted
                .iter()
                .find(|item| at(item, "assistant") == assistant && at(item, "action") == action)
                .map_or(Value::Null, |item| at(item, "read_only").clone());
            object_of(vec![
                (
                    "position",
                    i64::try_from(*position).map_or(Value::Null, int),
                ),
                ("assistant", assistant.clone()),
                ("action", action.clone()),
                ("read_only", read_only),
                ("change", Value::String(change.clone())),
            ])
        })
        .collect()
}

/// The refinement's card: the recording card of the resulting Routine, each step's change, and what went.
#[must_use]
pub fn refinement_card(
    proposal_id: &str,
    base: &Routine,
    candidate: &Routine,
    refined: &Refined,
    expires_at: i64,
) -> Value {
    let recorded = Recorded {
        document: candidate.plan.clone(),
        origins: refined.origins.clone(),
        permitted: candidate.permitted.clone(),
        schedule: candidate.schedule.clone(),
        timezone_source: candidate.timezone_source.clone(),
        chosen: Vec::new(),
        repair_from: None,
    };
    let shown = card(
        proposal_id,
        candidate,
        &recorded,
        (Some(&base.routine_id), expires_at),
    );
    let steps = elements(at(&shown, "steps"))
        .iter()
        .zip(&refined.changes)
        .map(|(step, (change, at_base))| {
            let Value::Object(mut members) = step.clone() else {
                return step.clone();
            };
            members.push(("change".to_owned(), Value::String(change.clone())));
            let place = at_base
                .and_then(|place| i64::try_from(place).ok())
                .map_or(Value::Null, int);
            members.push(("base".to_owned(), place));
            Value::Object(members)
        })
        .collect();
    let removed = removed(base, refined);
    let field = |name: &str| at(&shown, name).clone();
    object_of(vec![
        ("proposal_id", field("proposal_id")),
        ("expires_at", field("expires_at")),
        ("routine_id", Value::String(base.routine_id.clone())),
        ("base_revision", int(base.revision)),
        ("name", Value::String(candidate.name.clone())),
        ("description", Value::String(candidate.description.clone())),
        ("schedule", field("schedule")),
        ("timezone", field("timezone")),
        ("timezone_source", field("timezone_source")),
        ("output", field("output")),
        ("last_changed", Value::Bool(refined.last_changed)),
        ("steps", Value::Array(steps)),
        ("removed", Value::Array(removed)),
    ])
}

impl RoutineChat {
    /// The selected Routine as its recording send's Brain sees it, and its zone; `None` when it cannot be shown.
    #[must_use]
    pub fn chat_selected(&self, team_id: &str, span: &Span) -> Option<(Value, String)> {
        let state = load(&self.routines, team_id).ok()?;
        let found = state.routines.iter().find(|item| {
            Some(item.routine_id.as_str()) == span.selected.as_deref() && !item.deleting
        })?;
        let now = epoch_seconds(self.clock.as_ref());
        let latest = last_run(
            &self.diagnostics,
            team_id,
            &span.incarnation,
            &found.routine_id,
            now,
        );
        let view = selected(found, latest)?;
        canonical_selected_routine(&view)?;
        Some((view, found.timezone().to_owned()))
    }

    fn refined_candidate(
        &self,
        turn: &Turn<'_>,
        span: &Span,
        outcome: &Value,
    ) -> Result<(Routine, Routine, Refined), Stopped> {
        let contracts = Self::turn_contracts(turn, span)?;
        let state = load(&self.routines, turn.team_id)?;
        let base = base(&state, outcome)?.clone();
        let refined = refined(span, &base, elements(at(outcome, "edits")), &contracts)?;
        let actions: Vec<(String, String)> = elements(at(&refined.document, "steps"))
            .iter()
            .map(|step| {
                let field = |name: &str| as_str(at(step, name)).unwrap_or_default().to_owned();
                (field("assistant"), field("action"))
            })
            .collect();
        let mut candidate = base.clone();
        as_str(at(outcome, "description"))
            .unwrap_or_default()
            .clone_into(&mut candidate.description);
        candidate.plan = refined.document.clone();
        candidate.permitted = permitted(&actions, &contracts);
        candidate.assistants = pinned(&candidate.permitted, turn.scopes)?;
        candidate.confirmation = None;
        let others: Vec<&Routine> = state
            .routines
            .iter()
            .filter(|item| item.routine_id != base.routine_id)
            .collect();
        let refused = over_budget(&others, &candidate).or_else(|| change_room(&state));
        if refused.is_some() || !fits(&candidate) {
            return Err(Stopped::refused(refused.unwrap_or("routine-too-large")));
        }
        Ok((base, candidate, refined))
    }

    fn refinement(
        &self,
        turn: &Turn<'_>,
        proposed: &Value,
    ) -> Result<Result<(Proposal, Value), Stopped>, Problem> {
        let span = match self.live_recording(turn) {
            Ok(span) => span,
            Err(stopped) => return Ok(Err(stopped)),
        };
        let outcome = outcome(&span, proposed)?;
        let carded = (|| {
            let (base, candidate, refined) = self.refined_candidate(turn, &span, outcome)?;
            let proposal_id =
                new_id(self.entropy.as_ref()).map_err(|error| Stopped::refused(error.code))?;
            let expires_at = epoch_seconds(self.clock.as_ref()) + PROPOSAL_SECONDS;
            let view = refinement_card(&proposal_id, &base, &candidate, &refined, expires_at);
            admissible(&view, &span.protection.values)?;
            if canonical_refinement(&view) != Some(&view) {
                return Err(Stopped::Failed(problem::internal_error()));
            }
            let proposal = Proposal {
                proposal_id,
                team_id: turn.team_id.to_owned(),
                principal: span.principal.clone(),
                incarnation: span.incarnation.clone(),
                digest: bound_digest(&candidate, &view, Some(base.revision)),
                routine: candidate,
                replaces: Some(base.routine_id.clone()),
                expected_revision: Some(base.revision),
                expires_at: self.proposals.deadline(),
                creation: (None, Vec::new()),
                refines: true,
            };
            Ok((proposal, view))
        })();
        Ok(carded)
    }

    /// A completed turn's `refine` as the write that keeps its card, and the terminal member its reply carries; a
    /// card or a refusal ends the span.
    ///
    /// # Errors
    /// `brain-runtime-failed` for a `refine` outside its contract; `routine-state-unavailable`; `internal-error` for a
    /// card outside the protocol.
    pub fn admit_refine(&self, turn: &Turn<'_>, proposed: &Value) -> Result<Admitted, Problem> {
        match self.refinement(turn, proposed)? {
            Ok((proposal, view)) => Ok(Admitted {
                write: Write::Keep(Box::new(proposal)),
                field: ("routine_refinement", view),
            }),
            Err(Stopped::Refused(code)) => Ok(Admitted::refusal(&code)),
            Err(Stopped::Failed(problem)) => Err(problem),
            Err(Stopped::Asked(..)) => Err(problem::internal_error()),
        }
    }
}
