//! A recorded Routine's card and its one-tap confirmation, the only standing authority (`local/routine/proposal.py`,
//! ADR-0101).
//!
//! When a recording turn ends with the chat agent's `record` call, Team builds the plan from its own trace of the span,
//! admits it against the Team's exact current contracts and budgets, and keeps a proposal in memory, one per Team and
//! person, for 15 minutes: the candidate definition, the Team incarnation and person it is bound to, and the card the
//! reply carries. A recording that cannot become a Routine answers a refusal code instead; the work the turn already
//! did stands. Only an authenticated Supervisor confirms or cancels a card ([`confirm`]).

pub mod card;
pub mod confirm;

use std::collections::{BTreeSet, HashMap};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use strict_json::Value;
use team_core::clock::Clock;
use team_core::entropy::Entropy;
use team_inference::usage::Spent;
use team_protocol::python::value::object_of;
use team_protocol::routine::{
    DAY_SECONDS, MIN_CONTINUOUS_GAP_SECONDS, ROUTINE_ID_RE, encoded_bytes, exact_description,
    exact_name, matches,
};
use team_protocol::routine_proposal::{MAX_PROPOSAL_BYTES, canonical_proposal, canonical_question};
use team_routine::compose::{self, Request};
use team_routine::definition::{capacity, fits, over_budget, run_units};
use team_routine::json::as_str;
use team_routine::plan::Contracts;
use team_routine::record::{Routine, TeamRoutines, change_room, live_runs, new_id, scheduled};
use team_routine::recording::{Existing, Question, Recorded, Recording};
use team_routine::trace::exposes;
use team_storage::private::epoch_seconds;

use super::contracts::contracts;
use super::diagnostics::DiagnosticStore;
use super::recorder::{
    Intent, RecordingBook, Span, continued, creation, selected_work, settings_answer,
};
use super::state::{LostRuns, StateUnavailable, load};
use super::store::{RoutineStore, TeamLock};
use crate::chat::ports::ActiveAssistant;
use crate::chat::problem;
use crate::http::service::Problem;

/// How long a card stays open.
pub const PROPOSAL_SECONDS: i64 = 15 * 60;
const OUTCOME_FIELDS: [&str; 3] = ["name", "description", "replaces"];

/// One card a person may confirm once: the candidate Routine and everything it is bound to.
#[derive(Clone, Debug)]
pub struct Proposal {
    /// Its id.
    pub proposal_id: String,
    /// Its Team.
    pub team_id: String,
    /// The person it is bound to.
    pub principal: String,
    /// The Team incarnation it is bound to.
    pub incarnation: String,
    /// The candidate definition, scheduled, with no confirmation yet.
    pub routine: Routine,
    /// The Routine it replaces.
    pub replaces: Option<String>,
    /// The revision a replacement saw.
    pub expected_revision: Option<i64>,
    /// What a confirmation binds, the card shown included.
    pub digest: String,
    /// When it expires.
    pub expires_at: i64,
    /// What the recording that made the card spent, which the created Routine's notice shows; never bound.
    pub creation: (Option<Value>, Vec<Value>),
    /// Whether the card refines `replaces` (ADR-0122): its confirmation keeps the Routine's scheduling state.
    pub refines: bool,
}

/// Every Team's open cards, one per person, each consumed by its first confirmation.
pub struct ProposalBook {
    clock: Arc<dyn Clock>,
    cards: Mutex<HashMap<(String, String), Proposal>>,
}

impl std::fmt::Debug for ProposalBook {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("ProposalBook")
    }
}

impl ProposalBook {
    /// An empty book on `clock`.
    #[must_use]
    pub fn new(clock: Arc<dyn Clock>) -> Self {
        Self {
            clock,
            cards: Mutex::new(HashMap::new()),
        }
    }

    fn cards(&self) -> MutexGuard<'_, HashMap<(String, String), Proposal>> {
        self.cards.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// When a card made now expires.
    #[must_use]
    pub fn deadline(&self) -> i64 {
        epoch_seconds(self.clock.as_ref()) + PROPOSAL_SECONDS
    }

    /// Keeps a card, replacing the person's earlier one.
    pub fn put(&self, proposal: Proposal) {
        let key = (proposal.team_id.clone(), proposal.principal.clone());
        self.cards().insert(key, proposal);
    }

    /// The person's live card with exactly this id, consumed; `None` when unknown, expired, or someone else's.
    #[must_use]
    pub fn take(&self, team_id: &str, proposal_id: &str, principal: &str) -> Option<Proposal> {
        let key = (team_id.to_owned(), principal.to_owned());
        let found = {
            let mut cards = self.cards();
            if cards.get(&key)?.proposal_id != proposal_id {
                return None;
            }
            cards.remove(&key)?
        };
        (found.expires_at > epoch_seconds(self.clock.as_ref())).then_some(found)
    }

    /// Drops every card that would replace a Routine being deleted.
    pub fn drop_routine(&self, team_id: &str, routine_id: &str) {
        self.cards().retain(|(team, _), card| {
            team != team_id || card.replaces.as_deref() != Some(routine_id)
        });
    }

    /// Drops every card of the Team.
    pub fn drop_team(&self, team_id: &str) {
        self.cards().retain(|(team, _), _| team != team_id);
    }

    /// Drops every card.
    pub fn clear(&self) {
        self.cards().clear();
    }
}

/// Which of a Team's live runs lost their protection, checked under its held lock as a write starts.
pub type LostCheck = Arc<dyn Fn(&TeamLock<'_>, &TeamRoutines) -> BTreeSet<String> + Send + Sync>;

/// The Routine state, recordings, and cards a chat turn about a Routine runs with.
#[derive(Clone)]
pub struct RoutineChat {
    /// The Routine state.
    pub routines: Arc<RoutineStore>,
    /// The Routine diagnostics, for a selected Routine's latest run.
    pub diagnostics: Arc<DiagnosticStore>,
    /// The recording spans.
    pub recordings: Arc<RecordingBook>,
    /// The open cards.
    pub proposals: Arc<ProposalBook>,
    /// Which of the Team's live runs lost their protection, sealed as a write starts.
    pub lost: LostCheck,
    /// The wall clock.
    pub clock: Arc<dyn Clock>,
    /// The random source.
    pub entropy: Arc<dyn Entropy>,
}

/// A completed recording turn as its card is admitted.
pub struct Turn<'a> {
    /// Its Team.
    pub team_id: &'a str,
    /// Its send in the recording span.
    pub recording: &'a str,
    /// The verified person who sent it.
    pub principal: Option<&'a str>,
    /// The Team's network and Assistants now, when its identity is still the one the turn ran against.
    pub current: Option<(&'a str, &'a [ActiveAssistant])>,
    /// The Routine scope pin of each Assistant the turn's Brain saw.
    pub scopes: &'a [(String, String)],
}

/// What a turn's `record` asked for: the call itself, or the intent Team recorded again from a bound answer.
pub enum Proposed<'a> {
    /// The chat agent's call.
    Call(&'a Value),
    /// The span's stored intent, which a bound answer records again.
    Answered(Intent),
}

/// The write a turn's outcome commits with its reply.
#[derive(Debug)]
pub enum Write {
    /// Keep a card, which ends the span.
    Keep(Box<Proposal>),
    /// Keep the question the span asked, and the intent the answer records again.
    Ask(Box<Question>, Intent),
    /// End the span.
    Finish,
}

/// What a turn's `record` or `refine` ended in: its write and the terminal member its reply carries.
#[derive(Debug)]
pub struct Admitted {
    /// The write.
    pub write: Write,
    /// The terminal member.
    pub field: (&'static str, Value),
}

impl Admitted {
    /// A refusal that ends the span.
    #[must_use]
    pub fn refusal(code: &str) -> Self {
        let body = object_of(vec![("code", Value::String(code.to_owned()))]);
        Self {
            write: Write::Finish,
            field: ("routine_refusal", body),
        }
    }
}

/// Why a recording is no card.
#[derive(Debug)]
pub(crate) enum Stopped {
    /// A refusal, by its stable code.
    Refused(String),
    /// A question, with what the span protects as it asks.
    Asked(Box<Question>, BTreeSet<String>),
    /// A failure of the turn.
    Failed(Problem),
}

impl Stopped {
    pub(crate) fn refused(code: &str) -> Self {
        Self::Refused(code.to_owned())
    }
}

impl From<StateUnavailable> for Stopped {
    fn from(_: StateUnavailable) -> Self {
        Self::Failed(problem::routine_state_unavailable())
    }
}

/// The chat agent's `record` call exactly as Brain reports it; anything else is a Brain contract failure.
fn outcome(value: &Value) -> Result<(String, String, Option<String>), Problem> {
    let Value::Object(members) = value else {
        return Err(problem::brain_turn_failed());
    };
    let exact = members.len() == OUTCOME_FIELDS.len()
        && OUTCOME_FIELDS.iter().all(|name| value.get(name).is_some());
    let field = |name: &str| value.get(name).unwrap_or(&Value::Null);
    let replaces = field("replaces");
    let valid = exact
        && exact_name(field("name"))
        && exact_description(field("description"))
        && (matches!(replaces, Value::Null) || matches(replaces, &ROUTINE_ID_RE));
    if !valid {
        return Err(problem::brain_turn_failed());
    }
    let text = |name: &str| as_str(field(name)).unwrap_or_default().to_owned();
    Ok((
        text("name"),
        text("description"),
        as_str(replaces).map(str::to_owned),
    ))
}

/// Whether a record call is for the stored intent's work: what it replaces, and its name or its continued work.
fn same_work(stored: &Intent, intent: &Intent) -> bool {
    stored.replaces == intent.replaces && (stored.name == intent.name || stored.work == intent.work)
}

/// The Routine a recording replaces, only one its record call was shown and only at the revision it was shown.
fn replaced<'s>(state: &'s TeamRoutines, intent: &Intent) -> Result<Option<&'s Routine>, Stopped> {
    let Some(replaces) = &intent.replaces else {
        return Ok(None);
    };
    let found = state
        .routines
        .iter()
        .find(|item| item.routine_id == *replaces && !item.deleting);
    let (Some(found), Some(shown)) = (found, intent.revision) else {
        return Err(Stopped::refused("routine-not-found"));
    };
    if found.revision != shown {
        return Err(Stopped::refused("routine-revision-changed"));
    }
    Ok(Some(found))
}

/// The recorded plan, or the question to ask first.
fn recorded(
    intent: &Intent,
    span: &Span,
    contracts: &Contracts,
    existing: Option<&Routine>,
) -> Result<Recording, Stopped> {
    let kept = existing.map(|routine| Existing {
        plan: routine.plan.clone(),
        schedule: routine.schedule.clone(),
    });
    // A record call for other work than the stored intent supersedes its question and the work it asked to repeat,
    // though never the choices the person already answered nor the span's one repair.
    let mut asked = span.asked.clone();
    if let (Some(stored), Some(question)) = (&span.intent, &mut asked)
        && !same_work(stored, intent)
    {
        question.pending = None;
        question.manifest = None;
    }
    let work = match &intent.work {
        None => None,
        // The selected work left the bounded span: the person records again.
        Some(work) => Some(
            span.send_ids
                .iter()
                .position(|id| id == work)
                .ok_or_else(|| Stopped::refused("routine-record-again"))?,
        ),
    };
    let request = Request {
        sends: &span.sends,
        protection: &span.protection,
        contracts,
        work,
        asked: asked.as_ref(),
        existing: kept.as_ref(),
    };
    compose::record(&request).map_err(|error| Stopped::refused(error.code))
}

/// What to ask when the stated schedule outgrows the Team's daily steps: the shortest interval that fits.
fn room(candidate: &Routine, others: &[&Routine]) -> Result<Question, Stopped> {
    let fits = capacity(others) / run_units(candidate).max(1);
    if fits < 1 {
        return Err(Stopped::refused("routine-step-budget"));
    }
    let shortest = MIN_CONTINUOUS_GAP_SECONDS.max((DAY_SECONDS + fits - 1) / fits);
    let mut question = Question::new("routine-interval-over-budget", Vec::new());
    question.value = Some(shortest);
    Ok(question)
}

/// The candidate Routine a recording defines, with what it recorded and the Routine it replaces.
pub(crate) struct Candidate {
    pub(crate) span: Span,
    pub(crate) routine: Routine,
    pub(crate) recorded: Recorded,
    pub(crate) existing: Option<Routine>,
}

/// Each Assistant the permitted Actions use, with the scope pin its turn saw, sorted.
pub(crate) fn pinned(
    permitted: &[Value],
    scopes: &[(String, String)],
) -> Result<Vec<(String, String)>, Stopped> {
    let used: BTreeSet<&str> = permitted
        .iter()
        .filter_map(|item| item.get("assistant").and_then(as_str))
        .collect();
    used.into_iter()
        .map(|assistant| {
            let scope = scopes
                .iter()
                .find(|(id, _)| id == assistant)
                .map(|(_, scope)| scope.clone());
            scope
                .map(|scope| (assistant.to_owned(), scope))
                .ok_or_else(|| Stopped::refused("team-context-changed"))
        })
        .collect()
}

/// Admits a candidate against the Team's budgets and its replaced Routine's runs; `asked` is what a budget question
/// carries on.
fn budgeted(
    state: &TeamRoutines,
    candidate: &Routine,
    recorded: &Recorded,
    protected: &BTreeSet<String>,
) -> Result<(), Stopped> {
    let others: Vec<&Routine> = state
        .routines
        .iter()
        .filter(|item| item.routine_id != candidate.routine_id)
        .collect();
    let refused = over_budget(&others, candidate).or_else(|| change_room(state));
    if refused == Some("routine-step-budget") {
        // The person's interval stands; Team asks rather than run it less often than they said (ADR-0101).
        let mut asked = room(candidate, &others)?;
        asked.chosen.clone_from(&recorded.chosen);
        asked.repair_from = recorded.repair_from;
        return Err(Stopped::Asked(Box::new(asked), protected.clone()));
    }
    if refused.is_some() || !fits(candidate) {
        return Err(Stopped::refused(refused.unwrap_or("routine-too-large")));
    }
    Ok(())
}

impl RoutineChat {
    fn now(&self) -> i64 {
        epoch_seconds(self.clock.as_ref())
    }

    /// The turn's recording span, still live, the same person's, usable, and still protected.
    pub(crate) fn live_recording(&self, turn: &Turn<'_>) -> Result<Span, Stopped> {
        let found = self.recordings.live(turn.team_id, Some(turn.recording));
        let Some(found) = found.filter(|found| {
            Some(found.principal.as_str()) == turn.principal && !found.protection.lost
        }) else {
            return Err(Stopped::refused("routine-recording-unavailable"));
        };
        if let Some(code) = &found.refused {
            return Err(Stopped::refused(code));
        }
        Ok(found)
    }

    /// The exact contracts of the Team's Assistants as the turn saw them, in the incarnation the recording began in.
    pub(crate) fn turn_contracts(turn: &Turn<'_>, span: &Span) -> Result<Contracts, Stopped> {
        match turn.current {
            Some((network, assistants)) if network == span.incarnation => Ok(contracts(assistants)),
            _ => Err(Stopped::refused("team-context-changed")),
        }
    }

    /// The intent of a record call, with the replaced Routine's revision as that call's turn was shown it, and its
    /// work; an answer or a continued intent keeps the stored intent's work and revision.
    fn intent(&self, turn: &Turn<'_>, proposed: Proposed<'_>) -> Result<Intent, Problem> {
        let value = match proposed {
            Proposed::Answered(intent) => return Ok(intent),
            Proposed::Call(value) => value,
        };
        let (name, description, replaces) = outcome(value)?;
        let Some(found) = self.recordings.get(turn.team_id, Some(turn.recording)) else {
            return Ok(Intent {
                name,
                replaces,
                revision: None,
                description,
                work: None,
            });
        };
        if let Some(stored) = found
            .intent
            .clone()
            .filter(|_| settings_answer(Some(&found)))
        {
            return Ok(Intent {
                description,
                ..stored
            });
        }
        if let Some(stored) = continued(&found, replaces.as_deref()) {
            return Ok(Intent {
                name,
                description,
                ..stored
            });
        }
        let revision = replaces.as_ref().and_then(|replaces| {
            found
                .revisions
                .iter()
                .find(|(id, _)| id == replaces)
                .map(|(_, revision)| *revision)
        });
        let work = selected_work(&found, replaces.as_deref());
        Ok(Intent {
            name,
            replaces,
            revision,
            description,
            work,
        })
    }

    fn candidate(&self, turn: &Turn<'_>, intent: &Intent) -> Result<Candidate, Stopped> {
        let span = self.live_recording(turn)?;
        let contracts = Self::turn_contracts(turn, &span)?;
        let state = load(&self.routines, turn.team_id)?;
        let existing = replaced(&state, intent)?.cloned();
        let recorded = match recorded(intent, &span, &contracts, existing.as_ref())? {
            Recording::Asked(question) => {
                return Err(Stopped::Asked(question, span.protection.values));
            }
            Recording::Recorded(recorded) => *recorded,
        };
        let routine_id = match &existing {
            Some(existing) => existing.routine_id.clone(),
            None => new_id(self.entropy.as_ref()).map_err(|error| Stopped::refused(error.code))?,
        };
        let assistants = pinned(&recorded.permitted, turn.scopes)?;
        let mut routine = Routine::new(
            &routine_id,
            &intent.name,
            recorded.schedule.clone(),
            assistants,
            recorded.document.clone(),
            0,
            0,
        );
        routine.description.clone_from(&intent.description);
        routine.permitted.clone_from(&recorded.permitted);
        routine
            .timezone_source
            .clone_from(&recorded.timezone_source);
        let routine =
            scheduled(&routine, self.now()).map_err(|error| Stopped::refused(error.code))?;
        budgeted(&state, &routine, &recorded, &span.protection.values)?;
        if let Some(existing) = &existing
            && live_runs(&state)
                .iter()
                .any(|run| run.routine_id == existing.routine_id)
        {
            return Err(Stopped::refused("routine-busy"));
        }
        Ok(Candidate {
            span,
            routine,
            recorded,
            existing,
        })
    }

    /// The card a candidate makes, refused when it outgrows its bound, exposes a protected value, or its description
    /// states settings the card does not have.
    fn carded(
        &self,
        turn: &Turn<'_>,
        intent: &Intent,
        spent: Option<&Spent>,
    ) -> Result<(Proposal, Value), Stopped> {
        let Candidate {
            span,
            routine,
            recorded,
            existing,
        } = self.candidate(turn, intent)?;
        let proposal_id =
            new_id(self.entropy.as_ref()).map_err(|error| Stopped::refused(error.code))?;
        let replaces = existing
            .as_ref()
            .map(|existing| existing.routine_id.clone());
        let view = card::card(
            &proposal_id,
            &routine,
            &recorded,
            (replaces.as_deref(), self.now() + PROPOSAL_SECONDS),
        );
        card::admissible(&view, &span.protection.values)?;
        if canonical_proposal(&view) != Some(&view) {
            return Err(Stopped::Failed(problem::internal_error()));
        }
        let expected_revision = existing.as_ref().map(|existing| existing.revision);
        let proposal = Proposal {
            proposal_id,
            team_id: turn.team_id.to_owned(),
            principal: span.principal.clone(),
            incarnation: span.incarnation.clone(),
            digest: card::bound_digest(&routine, &view, expected_revision),
            routine,
            replaces,
            expected_revision,
            expires_at: self.proposals.deadline(),
            creation: creation(&span, spent),
            refines: false,
        };
        Ok((proposal, view))
    }

    /// A completed turn's `record` as the write that keeps its card, and the terminal member its reply carries; a card
    /// or a refusal ends the span, a question keeps it.
    ///
    /// # Errors
    /// `brain-runtime-failed` for a malformed `record` call; `routine-state-unavailable`; `internal-error` for a card
    /// or question outside the protocol.
    pub fn admit(
        &self,
        turn: &Turn<'_>,
        proposed: Proposed<'_>,
        spent: Option<&Spent>,
    ) -> Result<Admitted, Problem> {
        let intent = self.intent(turn, proposed)?;
        match self.carded(turn, &intent, spent) {
            Ok((proposal, view)) => Ok(Admitted {
                write: Write::Keep(Box::new(proposal)),
                field: ("routine_proposal", view),
            }),
            Err(Stopped::Asked(question, protected)) => {
                let wire = question.wire();
                if exposes(&wire, &protected) {
                    // Nothing the span protects ever reaches the person, a question's targets included.
                    return Ok(Admitted::refusal("routine-secret-literal"));
                }
                if canonical_question(&wire).is_none() {
                    return Err(problem::internal_error());
                }
                Ok(Admitted {
                    write: Write::Ask(question, intent),
                    field: ("routine_question", wire),
                })
            }
            Err(Stopped::Refused(code)) => Ok(Admitted::refusal(&code)),
            Err(Stopped::Failed(problem)) => Err(problem),
        }
    }

    /// Applies a committed outcome's write.
    pub fn apply(&self, team_id: &str, recording: &str, write: Write) {
        match write {
            Write::Keep(proposal) => {
                self.proposals.put(*proposal);
                self.recordings.finish(team_id, recording);
            }
            Write::Ask(question, intent) => {
                self.recordings.asked(team_id, recording, &question, intent);
            }
            Write::Finish => self.recordings.finish(team_id, recording),
        }
    }

    /// The write that runs the [`LostRuns`] check.
    pub(crate) fn lost_runs(&self) -> LostRuns<'_> {
        self.lost.as_ref()
    }
}

/// Whether an encoded card fits its bound.
#[must_use]
pub fn within_bound(view: &Value) -> bool {
    encoded_bytes(view) <= MAX_PROPOSAL_BYTES
}

#[cfg(test)]
mod tests;
