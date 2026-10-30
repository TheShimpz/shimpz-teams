//! Confirming or revoking a card (`proposal.confirm`, `proposal.revoke`, ADR-0101).
//!
//! Only an authenticated Supervisor answers a card. Confirming consumes it first, then rechecks the person, the
//! incarnation, every pin, the budgets, and a replaced Routine's revision, and commits the Routine with its confirmation
//! record in one write; a repeated confirmation answers with the Routine that record names. A revoke drops the card;
//! one already confirmed answers with the Routine it committed, so a lost answer never reports a Routine as cancelled.

use std::collections::BTreeMap;
use std::sync::Arc;

use strict_json::Value;
use team_core::audit::AuditSink;
use team_protocol::python::value::{int, object_of};
use team_routine::record::{TeamRoutines, create, refine, scheduled, update};

use super::{Proposal, RoutineChat};
use crate::audit::{Outcome, Principal};
use crate::chat::ports::TeamView;
use crate::chat::problem;
use crate::coordination::locks::SpaceLocks;
use crate::http::service::Problem;
use crate::routine::contracts::{context_unavailable, current_pins_unlocked};
use crate::routine::state::{StateUnavailable, load};

/// What answering a card reads and records beside the Routine state.
pub struct Answering<'a> {
    /// The Team as the controller holds it.
    pub team: &'a dyn TeamView,
    /// The Space's lifecycle locks.
    pub locks: &'a SpaceLocks,
    /// The audit journal.
    pub audit: &'a dyn AuditSink,
    /// The request's verified principal.
    pub actor: &'a Principal,
    /// The authenticated Supervisor who answers, if a person does.
    pub supervisor: Option<&'a str>,
}

fn answer(team_id: &str, proposal_id: &str, routine_id: Option<&str>, status: &str) -> Value {
    let text = |value: &str| Value::String(value.to_owned());
    object_of(vec![
        ("team_id", text(team_id)),
        ("proposal_id", text(proposal_id)),
        ("routine_id", routine_id.map_or(Value::Null, text)),
        ("status", text(status)),
    ])
}

fn changed() -> Problem {
    Problem::new(
        409,
        "Team capabilities changed; ask again",
        "team-context-changed",
    )
}

fn unavailable(_: StateUnavailable) -> Problem {
    problem::routine_state_unavailable()
}

/// The answer an earlier confirmation of this exact card gave, by its confirmation record.
fn answered(
    state: &TeamRoutines,
    team_id: &str,
    proposal_id: &str,
    bound: (&str, &str),
) -> Option<Value> {
    let found = state.routines.iter().find(|item| {
        item.confirmation.as_ref().is_some_and(|confirmation| {
            let field = |name: &str| {
                confirmation
                    .get(name)
                    .and_then(team_protocol::python::value::string)
            };
            (
                field("proposal_id"),
                field("principal"),
                field("incarnation"),
            ) == (Some(proposal_id), Some(bound.0), Some(bound.1))
        })
    })?;
    let status = if found.revision == 1 {
        "created"
    } else {
        "changed"
    };
    Some(answer(
        team_id,
        proposal_id,
        Some(&found.routine_id),
        status,
    ))
}

impl RoutineChat {
    async fn blocking<T: Send + 'static>(
        &self,
        work: impl FnOnce(&Self) -> Result<T, Problem> + Send + 'static,
    ) -> Result<T, Problem> {
        let this = self.clone();
        tokio::task::spawn_blocking(move || work(&this))
            .await
            .map_err(|_| problem::internal_error())?
    }

    async fn earlier(
        &self,
        team_id: &str,
        proposal_id: &str,
        bound: (String, String),
    ) -> Result<Option<Value>, Problem> {
        let (team, proposal) = (team_id.to_owned(), proposal_id.to_owned());
        self.blocking(move |this| {
            let state = load(&this.routines, &team).map_err(unavailable)?;
            Ok(answered(&state, &team, &proposal, (&bound.0, &bound.1)))
        })
        .await
    }

    /// Commits the card's Routine with its confirmation record in one write; the status it answers with.
    async fn commit(&self, proposal: Proposal, principal: &str) -> Result<&'static str, Problem> {
        let principal = principal.to_owned();
        self.blocking(move |this| {
            let now = team_storage::private::epoch_seconds(this.clock.as_ref());
            let confirmation = object_of(vec![
                ("proposal_id", Value::String(proposal.proposal_id.clone())),
                ("proposal_digest", Value::String(proposal.digest.clone())),
                ("principal", Value::String(principal)),
                ("incarnation", Value::String(proposal.incarnation.clone())),
                ("confirmed_at", int(now)),
            ]);
            let mut confirmed = proposal.routine.clone();
            confirmed.confirmation = Some(confirmation);
            // A refinement keeps the Routine's scheduling state as its own write finds it; any other card schedules anew.
            let value = if proposal.refines {
                confirmed
            } else {
                scheduled(&confirmed, now).map_err(|error| {
                    Problem::new(409, "the Team cannot hold this Routine", error.code)
                })?
            };
            let entropy = Arc::clone(&this.entropy);
            let outcome = crate::routine::state::update(
                &this.routines,
                &proposal.team_id,
                this.lost_runs(),
                |state: TeamRoutines| {
                    let expected = proposal.expected_revision.unwrap_or_default();
                    let written = if proposal.refines {
                        refine(&state, &value, expected, now, entropy.as_ref())
                            .map(|next| (next, "changed"))
                    } else if proposal.replaces.is_none() {
                        let (usage, steps) = proposal.creation.clone();
                        create(&state, &value, now, usage, steps, entropy.as_ref())
                            .map(|next| (next, "created"))
                    } else {
                        update(&state, &value, expected, now, entropy.as_ref())
                            .map(|next| (next, "changed"))
                    };
                    Ok::<_, StateUnavailable>(match written {
                        Ok((next, status)) => (next, Ok(status)),
                        Err(error) => (state, Err(error.code)),
                    })
                },
            );
            outcome
                .map_err(unavailable)?
                .map_err(|code| Problem::new(409, "the Team cannot hold this Routine", code))
        })
        .await
    }

    async fn record(
        &self,
        answering: &Answering<'_>,
        team_id: &str,
        detail: String,
    ) -> Result<(), Problem> {
        let event = answering
            .actor
            .event("routine-proposal", Outcome::Ok)
            .with("team_id", Value::String(team_id.to_owned()))
            .with("detail", Value::String(detail));
        answering
            .audit
            .record(event)
            .await
            .map_err(|_| problem::internal_error())
    }

    /// Confirms the person's card: consumes it, rechecks everything it is bound to, and commits its Routine.
    ///
    /// # Errors
    /// `routine-card-person-required`; `routine-proposal-expired`; `team-context-changed`; the Routine state's own
    /// refusal; `routine-state-unavailable`.
    pub async fn confirm(
        &self,
        answering: &Answering<'_>,
        team_id: &str,
        proposal_id: &str,
    ) -> Result<Value, Problem> {
        let principal = answering
            .supervisor
            .ok_or(problem::routine_card_person_required())?;
        let (routine_id, status) = {
            let _team = answering.locks.team(team_id).await;
            let proposal = self.proposals.take(team_id, proposal_id, principal);
            let incarnation = answering.team.network(team_id).await?;
            let Some(proposal) = proposal else {
                let bound = (principal.to_owned(), incarnation);
                return self
                    .earlier(team_id, proposal_id, bound)
                    .await?
                    .ok_or(Problem::new(
                        409,
                        "this Routine card expired; ask again",
                        "routine-proposal-expired",
                    ));
            };
            if proposal.incarnation != incarnation {
                return Err(changed());
            }
            let pinned: BTreeMap<String, String> =
                proposal.routine.assistants.iter().cloned().collect();
            let ids: Vec<String> = pinned.keys().cloned().collect();
            let current = current_pins_unlocked(answering.team, team_id, &ids)
                .await
                .map_err(|_| context_unavailable())?;
            if current != pinned {
                return Err(changed());
            }
            let routine_id = proposal.routine.routine_id.clone();
            (routine_id, self.commit(proposal, principal).await?)
        };
        self.record(answering, team_id, format!("{status}:{routine_id}"))
            .await?;
        Ok(answer(team_id, proposal_id, Some(&routine_id), status))
    }

    /// Revokes the person's card: drops it; one already gone is already revoked, unless it was confirmed.
    ///
    /// # Errors
    /// `routine-card-person-required`; `routine-state-unavailable`.
    pub async fn revoke(
        &self,
        answering: &Answering<'_>,
        team_id: &str,
        proposal_id: &str,
    ) -> Result<Value, Problem> {
        let principal = answering
            .supervisor
            .ok_or(problem::routine_card_person_required())?;
        {
            let _team = answering.locks.team(team_id).await;
            if self
                .proposals
                .take(team_id, proposal_id, principal)
                .is_none()
            {
                let incarnation = answering.team.network(team_id).await?;
                let bound = (principal.to_owned(), incarnation);
                if let Some(earlier) = self.earlier(team_id, proposal_id, bound).await? {
                    return Ok(earlier);
                }
            }
        }
        self.record(answering, team_id, format!("revoked:{proposal_id}"))
            .await?;
        Ok(answer(team_id, proposal_id, None, "revoked"))
    }
}
