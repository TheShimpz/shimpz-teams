//! A person answers a frozen Routine run (`local/routine/human.py`, ADR-0086): a fresh challenge when the decision
//! opens, then the run resumes in the Team's execution slot.
//!
//! Routine challenges live in their own store, never beside chat's one pending challenge, so a frozen run never blocks
//! chat. Expiry or dismissal of a Routine challenge leaves the run frozen, and so does an approval refused for an
//! assurance that is missing (403); only a denial ends it denied.

use std::collections::BTreeSet;
use std::sync::Arc;

use base64::Engine as _;
use strict_json::Value;
use team_action::challenges::{DEFAULT_TTL, HumanRequirement, challenge_payload, render_copy};
use team_action::human::{Admission, append_response};
use team_assistant::language::catalog_digest;
use team_chat::progress::Reporter;
use team_integrations::challenge::{
    ChallengeError, ChallengeStore, Claim, Limits, MAX_PENDING_CHALLENGES, Pending,
};
use team_protocol::python::value::{object_of, string};
use team_routine::record::{self, Lease, Routine, Run, RunStatus};
use team_routine::runs;
use team_routine::trace::Protection;

use super::replay::Protected;
use super::run::LeasedRun;
use super::run::finish::public_challenge;
use super::service::RoutineService;
use crate::chat::continuation::{Decoded, decode};
use crate::chat::pending::{PendingLocalChat, Requirements};
use crate::chat::ports::ActiveAssistant;
use crate::chat::problem;
use crate::http::service::{Body, Problem};

/// One frozen run's challenge payload: the run it resumes and its decoded continuation.
pub type Frozen = (String, Decoded);

/// The Routine human challenges: one per Team, never retained past expiry, since an expired one leaves its run frozen.
pub type RoutineChallenges = ChallengeStore<HumanRequirement, Frozen>;

/// The store with the chat challenge's capacity and lifetime.
///
/// # Errors
/// Never in practice: the limits are in range.
pub fn challenges(
    entropy: Arc<dyn team_core::entropy::Entropy>,
) -> Result<RoutineChallenges, ChallengeError> {
    ChallengeStore::new(
        Limits {
            capacity: MAX_PENDING_CHALLENGES,
            ttl: DEFAULT_TTL,
            retain_expired: false,
        },
        HumanRequirement::valid,
        entropy,
    )
}

fn not_frozen() -> Problem {
    Problem::new(
        409,
        "Routine run is not waiting for a person",
        "routine-run-not-frozen",
    )
}

fn expired() -> Problem {
    // An expired or dismissed routine challenge leaves the run frozen, ready to be opened again.
    Problem::new(
        409,
        "Action human request expired; open it again",
        "human-request-expired",
    )
}

fn changed() -> Problem {
    Problem::new(
        409,
        "Team capabilities changed; the run ended",
        "team-context-changed",
    )
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

/// Whether the requirement's Assistant still runs the catalog and pack its copy was rendered from (ADR-0091).
fn copy_binding_current(requirement: &HumanRequirement, assistants: &[ActiveAssistant]) -> bool {
    assistants
        .iter()
        .find(|active| active.id() == requirement.assistant_id)
        .is_some_and(|active| {
            let current = catalog_digest(&active.binding.contract.machine_contract);
            requirement.copy.catalog_digest == current
                && requirement.copy.pack_digest == active.pack.pack_digest
        })
}

/// A person's answer: its challenge, `submit` or `deny`, and its value.
fn resume_body(body: &Body) -> Result<(String, bool, Value), Problem> {
    let Body::Json(body @ Value::Object(members)) = body else {
        return Err(problem::human_response_invalid());
    };
    let submit = match body.get("decision").and_then(string) {
        Some("submit") => true,
        Some("deny") => false,
        _ => return Err(problem::human_response_invalid()),
    };
    let expected = if submit { 3 } else { 2 };
    let challenge = body.get("challenge_id").and_then(string);
    let (Some(challenge), true) = (challenge, members.len() == expected) else {
        return Err(problem::human_response_invalid());
    };
    if submit && body.get("value").is_none() {
        return Err(problem::human_response_invalid());
    }
    let value = body.get("value").cloned().unwrap_or(Value::Null);
    Ok((challenge.to_owned(), submit, value))
}

/// One frozen run about to resume.
struct Resuming {
    team_id: String,
    value: Run,
    routine: Routine,
    pending: PendingLocalChat,
    /// The answered request, whose copy must still come from the Team's current binding (ADR-0091).
    requirement: Option<HumanRequirement>,
}

impl RoutineService {
    /// The Routine human challenges.
    #[must_use]
    pub fn human_challenges(&self) -> &RoutineChallenges {
        &self.human
    }

    fn frozen_run(&self, team_id: &str, run_id: &str) -> Result<(Run, Routine), Problem> {
        let state = self.load(team_id)?;
        let value = record::run(&state, run_id).map_err(|_| problem::routine_run_not_found())?;
        let routine = record::routine(&state, &value.routine_id)
            .map_err(|_| problem::routine_run_not_found())?;
        if value.status != RunStatus::Frozen {
            return Err(not_frozen());
        }
        Ok((value.clone(), routine.clone()))
    }

    fn decoded(&self, team_id: &str, run_id: &str) -> Result<Decoded, Problem> {
        let unavailable = problem::routine_state_unavailable;
        let blob = self
            .parts
            .store
            .lock(team_id)
            .and_then(|lock| lock.continuation(run_id))
            .map_err(|_| unavailable())?;
        let envelope = strict_json::parse(&blob).map_err(|_| unavailable())?;
        let (Some(kind), Some(Value::Array(bindings)), Some(payload)) = (
            envelope.get("kind").and_then(string),
            envelope.get("bindings"),
            envelope.get("payload").and_then(string),
        ) else {
            return Err(unavailable());
        };
        let bindings: Vec<String> = bindings
            .iter()
            .filter_map(string)
            .map(str::to_owned)
            .collect();
        let payload = base64::engine::general_purpose::STANDARD
            .decode(payload)
            .map_err(|_| unavailable())?;
        decode(kind, &payload, &bindings).map_err(|_| unavailable())
    }

    /// Ends a frozen run whose Team changed, removing everything it held, its challenge included.
    fn end_changed(&self, team_id: &str, value: &Run) -> Problem {
        let detail = object_of(vec![("code", text("team-context-changed"))]);
        match self.end_frozen(team_id, &value.run_id, "failed", detail) {
            Ok(true) => {}
            Ok(false) => return not_frozen(),
            Err(problem) => return problem,
        }
        self.cancel_routine_challenge(team_id, &value.run_id);
        let _settled = self.settle(team_id, &value.routine_id);
        changed()
    }

    /// Whether a Team whose context could not be set up provably changed since the run froze: only an Assistant the
    /// Team no longer runs is proof. The caller holds the Team's stripe.
    async fn proven_changed(&self, team_id: &str, pending: &PendingLocalChat) -> bool {
        let Ok(scope) = self.parts.team.scope(team_id, true).await else {
            return false;
        };
        let current: BTreeSet<&str> = pending
            .assistant_ids
            .iter()
            .filter(|id| {
                scope
                    .assistant(id)
                    .is_some_and(|active| active.routine_scope().is_some())
            })
            .map(String::as_str)
            .collect();
        current.len() != pending.assistant_ids.len()
    }

    /// The Team's running Assistants, which must be exactly as the run left them; otherwise the run ends and nothing
    /// replays. A Team that cannot be read keeps the run frozen. The caller holds the Team's stripe.
    async fn current_context(
        &self,
        team_id: &str,
        value: &Run,
        pending: &PendingLocalChat,
        requirement: Option<&HumanRequirement>,
    ) -> Result<Vec<ActiveAssistant>, Problem> {
        let assistants = match self.routine_scope(team_id, &pending.assistant_ids).await {
            Ok((_name, network, assistants)) => Some((network, assistants)),
            Err(_) if !self.proven_changed(team_id, pending).await => {
                return Err(super::contracts::context_unavailable());
            }
            Err(_) => None,
        };
        let current = assistants.filter(|(network, assistants)| {
            super::replay::segment::routine_identity(network, assistants) == pending.identity
                && requirement
                    .is_none_or(|requirement| copy_binding_current(requirement, assistants))
        });
        match current {
            Some((_network, assistants)) => Ok(assistants),
            None => Err(self.end_changed(team_id, value)),
        }
    }

    /// Publishes the run's one challenge and its public copy; a request whose copy cannot hide what the run protects
    /// is withdrawn at once. The person sees the request only without what its run protects; after a restart that is
    /// every parameter.
    fn publish_challenge(
        &self,
        team_id: &str,
        value: &Run,
        requirement: HumanRequirement,
        decoded: Decoded,
    ) -> Result<Value, Problem> {
        let lost = value.protection_lost || self.protection_lost(team_id, value);
        // One routine challenge per Team at a time: opening another returns the earlier run to waiting, still frozen.
        let _cancelled = self.human.cancel_team(team_id);
        let challenge = self
            .human
            .create(team_id, requirement, (value.run_id.clone(), decoded))
            .map_err(|_| problem::human_request_invalid())?;
        let protection = if lost {
            Protection {
                lost: true,
                ..Protection::default()
            }
        } else {
            self.parts
                .protections
                .grow(&value.run_id, std::iter::empty())
        };
        let shown = challenge_payload(&challenge)
            .ok()
            .and_then(|descriptor| public_challenge(&descriptor, &protection));
        shown.ok_or_else(|| {
            let _cancelled = self.human.cancel_team(team_id);
            Problem::new(
                409,
                "Action human request cannot be shown",
                "human-request-invalid",
            )
        })
    }

    /// A person opened a frozen run's decision: a fresh one-use challenge for its exact request, rendered in the Admin
    /// interface language from the same binding's pack, never showing what the run protects.
    ///
    /// # Errors
    /// `routine-run-not-found`, `routine-run-not-frozen`, `human-request-invalid`, `team-context-changed`, or a state
    /// problem.
    pub async fn open_routine_challenge(
        &self,
        team_id: &str,
        run_id: &str,
        locale: &str,
    ) -> Result<Value, Problem> {
        // The frozen read, the challenge's publication, and a Stop or deletion ending the run are serialized by the
        // Team's stripe, so a run ended meanwhile is never given a challenge.
        let _team = self.parts.coordination.locks.team(team_id).await;
        let (value, _routine) = self.frozen_run(team_id, run_id)?;
        if value.request_kind != "human" {
            return Ok(object_of(vec![
                ("team_id", text(team_id)),
                ("run_id", text(&value.run_id)),
                ("status", text("integrations-required")),
            ]));
        }
        let decoded = self.decoded(team_id, &value.run_id)?;
        let Requirements::Human(frozen) = &decoded.requirements else {
            return Err(problem::routine_state_unavailable());
        };
        let assistants = self
            .current_context(team_id, &value, &decoded.pending, Some(frozen))
            .await?;
        let requirement = relocalized(frozen, &assistants, locale)?;
        let shown = self.publish_challenge(team_id, &value, requirement, decoded)?;
        let Value::Object(mut members) = shown else {
            return Err(problem::human_request_invalid());
        };
        members.push(("run_id".to_owned(), text(&value.run_id)));
        Ok(Value::Object(members))
    }

    /// Drops the Team's routine challenge when it belongs to this run, consumed by its exact id.
    pub(crate) fn cancel_routine_challenge(&self, team_id: &str, run_id: &str) {
        if let Ok(Some(challenge)) = self.human.current(team_id)
            && challenge.payload.0 == run_id
        {
            let _claimed = self.human.claim(team_id, &challenge.id);
        }
    }

    /// Validates one exact answer to a frozen run's challenge, then replays the run from its continuation; the
    /// challenge is consumed only once the replay holds the Team's slot and the run resumed.
    ///
    /// # Errors
    /// `invalid-body`, `human-request-expired`, `invalid-human-response`, the slot's refusal, or a state problem.
    pub async fn resume_routine_human(
        &self,
        team_id: &str,
        run_id: &str,
        body: &Body,
        progress: &Reporter,
    ) -> Result<Value, Problem> {
        let (challenge_id, submit, answer) = resume_body(body)?;
        let (value, routine) = self.frozen_run(team_id, run_id)?;
        let challenge = self
            .human
            .get(team_id, &challenge_id)
            .map_err(|_| expired())?;
        if challenge.payload.0 != value.run_id {
            return Err(Problem::new(
                409,
                "Action human request belongs to another run",
                "human-request-expired",
            ));
        }
        if !submit {
            return self.deny(team_id, &value, &routine, &challenge.id).await;
        }
        let pending = challenge.payload.1.pending.clone();
        let requirement = challenge.metadata.clone();
        let admission = append_response(
            &pending.transcripts,
            &requirement.interrupt_id,
            &requirement.request,
            &answer,
            pending.requests_used,
        )
        .map_err(|_| problem::human_response_mismatch())?;
        let resuming = Resuming {
            team_id: team_id.to_owned(),
            value,
            routine,
            pending,
            requirement: Some(requirement),
        };
        self.replay(resuming, progress, Some((admission, challenge.id.clone())))
            .await
    }

    /// After the person connected the Integration, replays the run; a still-missing one freezes it again.
    ///
    /// # Errors
    /// `routine-run-not-frozen`, the slot's refusal, or a state problem.
    pub async fn resume_routine_integrations(
        &self,
        team_id: &str,
        run_id: &str,
        progress: &Reporter,
    ) -> Result<Value, Problem> {
        let (value, routine) = self.frozen_run(team_id, run_id)?;
        if value.request_kind != "integrations" {
            return Err(Problem::new(
                409,
                "Routine run is waiting for an answer",
                "routine-run-not-frozen",
            ));
        }
        let pending = self.decoded(team_id, &value.run_id)?.pending;
        let resuming = Resuming {
            team_id: team_id.to_owned(),
            value,
            routine,
            pending,
            requirement: None,
        };
        self.replay(resuming, progress, None).await
    }

    /// Ends the frozen run denied in the Team's slot, consuming its challenge only once the run ends.
    async fn deny(
        &self,
        team_id: &str,
        value: &Run,
        routine: &Routine,
        challenge_id: &str,
    ) -> Result<Value, Problem> {
        {
            let _execution = self.admit_routine(team_id, &routine.routine_id)?;
            let _team = self.parts.coordination.locks.team(team_id).await;
            let ended = self.human.claim_after(team_id, challenge_id, |_challenge| {
                match self.end_frozen(team_id, &value.run_id, "denied", object_of(Vec::new())) {
                    Ok(true) => Ok(()),
                    Ok(false) => Err(not_frozen()),
                    Err(problem) => Err(problem),
                }
            });
            match ended {
                Ok(_) => {}
                Err(Claim::Store(_)) => return Err(expired()),
                Err(Claim::Commit(problem)) => return Err(problem),
            }
        }
        let _settled = self.settle(team_id, &value.routine_id);
        Ok(object_of(vec![
            ("team_id", text(team_id)),
            ("run_id", text(&value.run_id)),
            ("status", text("denied")),
        ]))
    }

    /// Thaws the run; an answered run consumes its challenge in the same step, after `before` sealed what must be
    /// kept first, so a run that stays frozen keeps its challenge answerable.
    fn thaw(
        &self,
        resuming: &Resuming,
        answered: (Option<&str>, &dyn Fn() -> Result<(), Problem>),
        requests_used: usize,
    ) -> Result<Lease, Problem> {
        let (challenge_id, before) = answered;
        let (team_id, run_id) = (&resuming.team_id, &resuming.value.run_id);
        let now = self.now();
        let entropy = self.parts.entropy.clone();
        let used = i64::try_from(requests_used).unwrap_or(i64::MAX);
        let thaw = || -> Result<String, Problem> {
            let thawed = self.update(team_id, |state| {
                match runs::thaw(&state, run_id, now, used, entropy.as_ref()) {
                    Ok((next, token)) => match before() {
                        Ok(()) => (next, Ok(Some(token))),
                        Err(problem) => (state, Err(problem)),
                    },
                    Err(_) => (state, Ok(None)),
                }
            })?;
            thawed?.ok_or_else(not_frozen)
        };
        let token = match challenge_id {
            None => thaw()?,
            Some(challenge_id) => {
                let mut token = None;
                let claimed = self.human.claim_after(team_id, challenge_id, |_challenge| {
                    token = Some(thaw()?);
                    Ok::<(), Problem>(())
                });
                match claimed {
                    Ok(_) => token.ok_or_else(not_frozen)?,
                    Err(Claim::Store(_)) => return Err(expired()),
                    Err(Claim::Commit(problem)) => return Err(problem),
                }
            }
        };
        Ok(record::lease_of(&token, record::HUMAN_LEASE))
    }

    /// Thaws the run under an internal lease and continues it, with no model, in its own generation. The Team's slot
    /// is held before anything is consumed: a concurrent chat refuses the replay while the run stays frozen.
    async fn replay(
        &self,
        resuming: Resuming,
        progress: &Reporter,
        answered: Option<(Admission, String)>,
    ) -> Result<Value, Problem> {
        let (team_id, run_id) = (resuming.team_id.clone(), resuming.value.run_id.clone());
        let routine_id = resuming.routine.routine_id.clone();
        let execution = self.admit_routine(&team_id, &routine_id)?;
        let outcome = {
            let _registered = self.register(
                (&team_id, &run_id),
                &execution,
                resuming.value.active_seconds_left,
            )?;
            let (transcripts, requests_used) = match &answered {
                Some((admission, _)) => (admission.transcripts.clone(), admission.requests_used),
                None => (
                    resuming.pending.transcripts.clone(),
                    resuming.pending.requests_used,
                ),
            };
            let lease = self
                .resumed(&resuming, answered.as_ref(), requests_used)
                .await?;
            // The run is thawed and its challenge consumed, so it goes on: its end finishes a removal that fails now.
            let _removed = self
                .parts
                .store
                .lock(&team_id)
                .and_then(|lock| lock.delete_continuation(&run_id));
            let run = LeasedRun {
                team_id: team_id.clone(),
                run_id: run_id.clone(),
                lease,
                execution: &execution,
                routine: resuming.routine.clone(),
                transcripts,
                requests_used,
                protected: Protected::default(),
            };
            let outcome = self
                .execute(&run, &resuming.value, progress, Some(&resuming.pending))
                .await?;
            if outcome == "held" {
                self.recover_run(&run, progress).await
            } else {
                outcome
            }
        };
        drop(execution);
        self.after_run(&team_id, &routine_id, outcome).await;
        Ok(super::run::segment_answer(&team_id, &run_id, outcome))
    }

    /// Under the Team's stripe: the Team checked exactly as the run left it, an answered Stored Input sealed inside
    /// the thaw's write, and the run thawed.
    async fn resumed(
        &self,
        resuming: &Resuming,
        answered: Option<&(Admission, String)>,
        requests_used: usize,
    ) -> Result<Lease, Problem> {
        let team_id = &resuming.team_id;
        let _team = self.parts.coordination.locks.team(team_id).await;
        let requirement = resuming.requirement.as_ref();
        let assistants = self
            .current_context(team_id, &resuming.value, &resuming.pending, requirement)
            .await?;
        let seal = || -> Result<(), Problem> {
            let (Some((admission, challenge_id)), Some(requirement)) = (answered, requirement)
            else {
                return Ok(());
            };
            let Some((name, secret)) = &admission.stored_input else {
                return Ok(());
            };
            let claimed = Pending {
                id: challenge_id.clone(),
                team_id: team_id.clone(),
                expires_at: tokio::time::Instant::now(),
                metadata: requirement.clone(),
                payload: resuming.pending.clone(),
            };
            crate::chat::stored::seal(
                &self.parts.chat,
                team_id,
                &claimed,
                &assistants,
                (name, secret),
            )
        };
        let challenge_id = answered.map(|(_, challenge_id)| challenge_id.as_str());
        self.thaw(resuming, (challenge_id, &seal), requests_used)
    }
}

/// The frozen request rendered in `locale` from exactly the binding's pack it was rendered from before.
fn relocalized(
    frozen: &HumanRequirement,
    assistants: &[ActiveAssistant],
    locale: &str,
) -> Result<HumanRequirement, Problem> {
    let invalid = || {
        Problem::new(
            409,
            "Action human request changed; the run stays frozen",
            "human-request-invalid",
        )
    };
    let active = assistants
        .iter()
        .find(|active| active.id() == frozen.assistant_id)
        .ok_or_else(invalid)?;
    let pack = &active.pack;
    let copy = &frozen.copy;
    if (pack.catalog_digest.as_str(), pack.pack_digest.as_str())
        != (copy.catalog_digest.as_str(), copy.pack_digest.as_str())
    {
        return Err(invalid());
    }
    let copy = render_copy(&frozen.request, pack, locale, frozen.help_text.as_deref())
        .map_err(|_| invalid())?;
    Ok(HumanRequirement {
        copy,
        purpose: None,
        purpose_locale: None,
        ..frozen.clone()
    })
}
