//! `test_routine_incident.py` and the hold parts of `test_routine_record_time.py`: a held run's incident is settled
//! and noticed exactly once, keeps its time, answers, usage, and lost protection, and resumes only when it may.

use strict_json::Value;

use super::{
    Expected, PauseReason, current_revision, fence, held_step, incident, pause_incident,
    release_incident, reopen_incident, run_incident, settle_hold, skip_incident, step_detail,
    unknown_step,
};
use crate::claim::{claim, claimable, generation_for, next_due};
use crate::json::{int, object, text};
use crate::plan::active_seconds;
use crate::record::{
    self, RoutineStateError, Run, RunStatus, TeamRoutines, begin_delete, grace_seconds, set_paused,
};
use crate::runs::{self, Request};
use crate::testing::{
    ENTROPY, KEY, ROUTINE_A, STEP, added, bound, claimed, edit_run, held, json, nine,
    routine as fixture, routine_of,
};

fn replace_dns_record() -> (String, String, Option<i64>, i64) {
    (
        "dns".to_owned(),
        "replace-dns-record".to_owned(),
        Some(STEP),
        1,
    )
}

fn settled() -> (TeamRoutines, String) {
    let (state, run_id) = held();
    let state =
        settle_hold(&state, &run_id, nine() + 1, replace_dns_record(), &ENTROPY).expect("settled");
    (state, run_id)
}

fn last_notice(state: &TeamRoutines) -> &record::Notice {
    state.notices.last().expect("a notice")
}

/// The outcome, detail, and version of the state's latest notice.
fn latest(state: &TeamRoutines) -> (String, Value, i64) {
    let notice = last_notice(state);
    (
        notice.outcome.clone(),
        notice.detail.clone(),
        notice.version,
    )
}

/// The settled incident paused by recovery.
fn paused(state: &TeamRoutines, run_id: &str) -> TeamRoutines {
    pause_incident(state, run_id, nine() + 2, PauseReason::Exhausted, &ENTROPY).expect("paused")
}

#[test]
fn a_hold_names_its_step_and_its_notice_goes_on_through_the_incident() {
    let (state, run_id) = settled();
    let held = incident(&state, &run_id).expect("an incident");
    // The held run is its own incident: the same record, indexed when it was held.
    let call = (
        held.assistant_id.as_str(),
        held.action.as_str(),
        held.position,
    );
    assert_eq!(
        (held.status, call),
        (
            RunStatus::Unresolved,
            ("dns", "replace-dns-record", Some(STEP))
        )
    );
    assert_eq!(last_notice(&state).notice_id, run_id);
    assert_eq!(latest(&state), ("held".to_owned(), object(vec![]), 1));
    assert_eq!(held.notice_version, 1);
    let paused = paused(&state, &run_id);
    assert!(
        record::routine(&paused, ROUTINE_A)
            .expect("a Routine")
            .paused
    );
    assert_eq!(
        latest(&paused),
        (
            "paused".to_owned(),
            object(vec![("reason", text("exhausted"))]),
            2
        )
    );
}

#[test]
fn a_person_setting_a_held_run_aside_is_never_the_routines_missed_schedule_skip() {
    let (state, run_id) = settled();
    let skipped = skip_incident(
        &paused(&state, &run_id),
        &run_id,
        nine() + 3,
        None,
        &ENTROPY,
    )
    .expect("skipped");
    assert_eq!(
        incident(&skipped, &run_id).expect("an incident").status,
        RunStatus::Skipped
    );
    let notice = last_notice(&skipped);
    assert_eq!(
        (
            notice.outcome.as_str(),
            notice.run_id.as_str(),
            notice.version
        ),
        ("user-skipped", run_id.as_str(), 3)
    );
    assert_eq!(notice.name, fixture().name);
    let again = pause_incident(&skipped, &run_id, nine(), PauseReason::Exhausted, &ENTROPY);
    assert_eq!(
        again.map(|_| ()),
        Err(RoutineStateError::new("incident-not-unresolved"))
    );
}

/// A settled, paused incident, its Routine's cadence, the card opened on it, and its Rodar at nine plus ten.
fn rodar() -> (TeamRoutines, String, i64, Expected, TeamRoutines) {
    let (state, run_id) = settled();
    let state = paused(&state, &run_id);
    let cadence = record::routine(&state, ROUTINE_A)
        .expect("a Routine")
        .next_run_at;
    let generation = incident(&state, &run_id)
        .expect("an incident")
        .generation
        .clone();
    let expected = Expected {
        revision: 1,
        generation,
        current: 1,
    };
    let asked = run_incident(&state, &run_id, nine() + 10, &expected, &ENTROPY).expect("asked");
    (state, run_id, cadence, expected, asked)
}

#[test]
fn rodar_keeps_one_fresh_run_pending_and_never_moves_the_cadence() {
    let (state, run_id, cadence, expected, asked) = rodar();
    let requested = record::routine(&asked, ROUTINE_A).expect("a Routine");
    let scheduling = (
        requested.paused,
        requested.run_requested,
        requested.next_run_at,
    );
    assert_eq!(scheduling, (false, nine() + 10, cadence));
    assert_eq!(
        latest(&asked),
        ("user-skipped".to_owned(), object(vec![]), 3)
    );
    assert_eq!(next_due(&asked, nine() + 5), Some(nine() + 10));
    // The card's state is checked in the same write.
    let changed_card = Expected {
        current: 2,
        ..expected
    };
    let refused = run_incident(&state, &run_id, nine() + 10, &changed_card, &ENTROPY);
    assert_eq!(
        refused.map(|_| ()),
        Err(RoutineStateError::new("incident-changed"))
    );
}

#[test]
fn a_rodar_outlasts_any_delay_and_one_firing_serves_it() {
    let (_state, _run_id, cadence, _expected, asked) = rodar();
    // A Team catching up its notices waits; the request outlasts the schedule's own grace and is never missed.
    let late = cadence - 60;
    assert!(
        late - (nine() + 10)
            > grace_seconds(record::routine(&asked, ROUTINE_A).expect("a Routine"))
    );
    let (claimed_state, _claim) = claim(&asked, late, KEY, true, &ENTROPY).expect("a claim");
    let after = record::routine(&claimed_state, ROUTINE_A).expect("a Routine");
    assert_eq!((after.run_requested, after.next_run_at), (0, cadence));
    assert_eq!(
        claimed_state.starts.last(),
        Some(&(ROUTINE_A.to_owned(), late, 1))
    );
    // A firing due at the same time serves the request too: one run, never two.
    let (both, _claim) = claim(&asked, cadence, KEY, true, &ENTROPY).expect("a claim");
    assert_eq!(
        record::routine(&both, ROUTINE_A)
            .expect("a Routine")
            .run_requested,
        0
    );
    assert_eq!(record::live_runs(&both).len(), 1);
}

#[test]
fn deleting_a_routine_sets_a_run_held_afterwards_aside_as_it_is_indexed() {
    let (state, run_id) = held();
    let (state, _runs) = begin_delete(&state, ROUTINE_A).expect("deleting");
    let state =
        settle_hold(&state, &run_id, nine() + 1, replace_dns_record(), &ENTROPY).expect("settled");
    assert_eq!(
        incident(&state, &run_id).expect("an incident").status,
        RunStatus::Skipped
    );
    let notice = last_notice(&state);
    assert_eq!(
        (notice.outcome.as_str(), &notice.detail),
        ("user-skipped", &object(vec![]))
    );
    assert_eq!(current_revision(&state, ROUTINE_A), 0);
}

#[test]
fn a_resume_starts_a_fresh_streak_and_a_deleting_routine_never_pauses_or_resumes() {
    let state = crate::testing::edit(&added(&[fixture()]), ROUTINE_A, |value| {
        value.failures = 3;
        value.paused = true;
    });
    let resumed = set_paused(&state, ROUTINE_A, false).expect("resumed");
    let resumed = record::routine(&resumed, ROUTINE_A).expect("a Routine");
    assert_eq!((resumed.paused, resumed.failures), (false, 0));
    let paused = set_paused(&state, ROUTINE_A, true).expect("paused");
    assert_eq!(
        record::routine(&paused, ROUTINE_A)
            .expect("a Routine")
            .failures,
        3
    );
    let (deleting, _runs) = begin_delete(&state, ROUTINE_A).expect("deleting");
    for value in [true, false] {
        assert_eq!(
            set_paused(&deleting, ROUTINE_A, value).map(|_| ()),
            Err(RoutineStateError::new("routine-not-found"))
        );
    }
}

#[test]
fn the_wake_hint_is_the_earliest_future_firing_of_a_routine_that_may_start() {
    let ids: Vec<String> = (1..7)
        .map(|index: u32| {
            char::from_digit(index, 16)
                .expect("a digit")
                .to_string()
                .repeat(32)
        })
        .collect();
    let mut state = added(&ids.iter().map(|id| routine_of(id)).collect::<Vec<_>>());
    for (index, routine) in state.routines.iter_mut().enumerate() {
        let offset = [-1, 10, 20, 30, 40, 50][index];
        routine.next_run_at = nine() + offset;
        match index {
            1 => routine.paused = true,
            2 => routine.deleting = true,
            3 => routine.needs_reconfirm = true,
            _ => {}
        }
    }
    assert_eq!(next_due(&state, nine()), Some(nine() + 40));
    // A Routine with a live run or an unresolved incident wakes nothing; its own end or resolution does.
    let busy = TeamRoutines {
        runs: vec![Run::new(&"f".repeat(32), &ids[4], RunStatus::Frozen)],
        ..state.clone()
    };
    assert_eq!(next_due(&busy, nine()), Some(nine() + 50));
    // A leased run holds the Team's one slot: no other Routine of the Team is claimed or hinted until it ends.
    let leased = TeamRoutines {
        runs: vec![Run::new(&"f".repeat(32), &ids[4], RunStatus::Leased)],
        ..state
    };
    assert_eq!(
        claimable(&busy, nine(), true).map(|item| item.routine_id.clone()),
        Some(ids[0].clone())
    );
    assert!(claimable(&leased, nine(), true).is_none());
    assert_eq!(next_due(&leased, nine()), None);
    let mut unresolved = Run::new(&"e".repeat(32), &ids[5], RunStatus::Unresolved);
    unresolved.generation = "g".to_owned();
    let mut runs = busy.runs.clone();
    runs.push(unresolved);
    let incident_held = TeamRoutines { runs, ..busy };
    assert_eq!(next_due(&incident_held, nine()), None);
}

#[test]
fn a_hold_without_a_sealed_cursor_names_no_step() {
    let (state, run_id) = held();
    let state =
        settle_hold(&state, &run_id, nine() + 1, unknown_step(), &ENTROPY).expect("settled");
    let step = held_step(incident(&state, &run_id).expect("an incident"));
    assert_eq!(step, unknown_step());
    // Its incident view names no call.
    assert_eq!(
        step_detail(&step),
        json(r#"{"assistant_id": null, "action": null, "position": null, "steps": null}"#)
    );
    assert_eq!(
        step_detail(&replace_dns_record()),
        json(
            r#"{"assistant_id": "dns", "action": "replace-dns-record", "position": 1, "steps": 1}"#
        )
    );
}

#[test]
fn a_completed_continuation_is_recovered_and_resets_the_streak() {
    let (state, claimed, lease) = bound();
    let run_id = claimed.run.run_id.as_str();
    assert_eq!(
        runs::completed(record::run(&state, run_id).expect("a run")),
        "done"
    );
    let state = edit_run(&state, run_id, |run| {
        run.generation = generation_for("net_1", run_id, "s1");
    });
    assert_eq!(
        runs::completed(record::run(&state, run_id).expect("a run")),
        "recovered"
    );
    let state = crate::testing::edit(&state, ROUTINE_A, |value| value.failures = 2);
    let ended = runs::finish(&state, run_id, &lease, nine() + 1, None, &ENTROPY).expect("finished");
    assert_eq!(
        (
            last_notice(&ended).outcome.as_str(),
            record::routine(&ended, ROUTINE_A)
                .expect("a Routine")
                .failures
        ),
        ("recovered", 0)
    );
}

#[test]
fn a_hold_settles_only_a_held_run_and_keeps_its_one_record() {
    let (state, claimed_run, _lease) = bound();
    assert_eq!(
        settle_hold(
            &state,
            &claimed_run.run.run_id,
            nine() + 1,
            unknown_step(),
            &ENTROPY
        )
        .map(|_| ()),
        Err(RoutineStateError::new("run-not-held"))
    );
    let (unbound, unbound_claim, unbound_lease) = claimed();
    assert_eq!(
        fence(&unbound, &unbound_claim.run.run_id, &unbound_lease, nine()).map(|_| ()),
        Err(RoutineStateError::new("generation-invalid"))
    );
    let (fenced, run_id) = held();
    let state =
        settle_hold(&fenced, &run_id, nine() + 1, unknown_step(), &ENTROPY).expect("settled");
    // Indexing needs no room of its own: the claim reserved the run's record, which becomes its incident.
    assert_eq!(
        state
            .runs
            .iter()
            .map(|item| item.run_id.clone())
            .collect::<Vec<_>>(),
        [run_id]
    );
    assert_eq!(record::live_runs(&state), Vec::<&Run>::new());
}

#[test]
fn an_incident_reopens_only_once_unresolved_resumable_and_idle() {
    let (fenced, run_id) = held();
    let state =
        settle_hold(&fenced, &run_id, nine() + 1, unknown_step(), &ENTROPY).expect("settled");
    let generation = generation_for("net_1", &run_id, "s1");
    assert_eq!(
        incident(&state, &"0".repeat(32)).map(|_| ()),
        Err(RoutineStateError::new("incident-not-found"))
    );
    let paused = set_paused(&state, ROUTINE_A, true).expect("paused");
    assert_eq!(
        reopen_incident(&paused, &run_id, nine() + 2, &generation, &ENTROPY).map(|_| ()),
        Err(RoutineStateError::new("routine-not-resumable"))
    );
    assert_eq!(
        reopen_incident(
            &state,
            &run_id,
            nine() + 2,
            &format!("net_1:routine:{}", "0".repeat(32)),
            &ENTROPY
        )
        .map(|_| ()),
        Err(RoutineStateError::new("routine-busy"))
    );
    let skipped = skip_incident(&state, &run_id, nine() + 2, None, &ENTROPY).expect("skipped");
    assert!(reopen_incident(&skipped, &run_id, nine() + 3, &generation, &ENTROPY).is_err());
    assert!(skip_incident(&skipped, &run_id, nine() + 3, None, &ENTROPY).is_err());
    assert!(release_incident(&state, &run_id).is_err());
    // A released incident's record goes; releasing it again finds it already released.
    let released = release_incident(&skipped, &run_id).expect("released");
    assert_eq!(released.runs, []);
    assert_eq!(release_incident(&released, &run_id), Ok(released.clone()));
}

#[test]
fn a_hold_keeps_the_run_balance_and_a_continuation_restores_it() {
    let (state, claimed_run, lease) = bound();
    let run_id = claimed_run.run.run_id.as_str();
    let state = runs::spend(&state, run_id, &lease, nine(), (250, 250_400)).expect("spent");
    let state = fence(&state, run_id, &lease, nine()).expect("fenced");
    let state = settle_hold(&state, run_id, nine() + 1, unknown_step(), &ENTROPY).expect("settled");
    let one_step = active_seconds(1);
    let held_incident = incident(&state, run_id).expect("an incident");
    assert_eq!(held_incident.active_seconds_left, one_step - 250);
    assert_eq!(
        held_incident.usage,
        object(vec![("duration_ms", int(250_400))])
    );
    let (reopened, _token) = reopen_incident(
        &state,
        run_id,
        nine() + 2,
        &generation_for("net_1", run_id, "s1"),
        &ENTROPY,
    )
    .expect("reopened");
    let resumed = record::run(&reopened, run_id).expect("a run");
    assert_eq!(
        (resumed.active_seconds_left, resumed.usage.clone()),
        (one_step - 250, object(vec![("duration_ms", int(250_400))]))
    );
    let spent = edit_run(&state, run_id, |run| run.active_seconds_left = 0);
    assert_eq!(
        reopen_incident(
            &spent,
            run_id,
            nine() + 2,
            &generation_for("net_1", run_id, "s1"),
            &ENTROPY
        )
        .map(|_| ()),
        Err(RoutineStateError::new("run-time-exhausted"))
    );
    let charged = super::spend_incident(&state, run_id, one_step).expect("charged");
    assert_eq!(
        incident(&charged, run_id)
            .expect("an incident")
            .active_seconds_left,
        0
    );
}

#[test]
fn a_runs_answered_human_requests_carry_through_a_hold_and_its_continuation() {
    let (state, claimed_run, lease) = bound();
    let run_id = claimed_run.run.run_id.as_str();
    let state = edit_run(&state, run_id, |run| run.requests_used = 15);
    let state = fence(&state, run_id, &lease, nine()).expect("fenced");
    let state = settle_hold(&state, run_id, nine() + 1, unknown_step(), &ENTROPY).expect("settled");
    assert_eq!(
        incident(&state, run_id).expect("an incident").requests_used,
        15
    );
    let (reopened, _token) = reopen_incident(
        &state,
        run_id,
        nine() + 2,
        &generation_for("net_1", run_id, "s1"),
        &ENTROPY,
    )
    .expect("reopened");
    assert_eq!(
        record::run(&reopened, run_id).expect("a run").requests_used,
        15
    );
    // A thaw keeps the larger count: an answer only ever adds to it.
    let frozen = edit_run(&reopened, run_id, |run| {
        run.status = RunStatus::Frozen;
        run.lease_sha256 = String::new();
        run.lease_key = String::new();
        run.lease_expires_at = 0;
        run.request_kind = "human".to_owned();
        run.assistant_id = "shimpz-cloudflare".to_owned();
        run.action = "list-zones".to_owned();
    });
    for (answered, kept) in [(16, 16), (3, 15)] {
        let (thawed, _token) =
            runs::thaw(&frozen, run_id, nine() + 3, answered, &ENTROPY).expect("thawed");
        assert_eq!(
            record::run(&thawed, run_id).expect("a run").requests_used,
            kept
        );
    }
}

#[test]
fn a_run_notice_carries_its_usage_and_a_lost_protection_for_good() {
    let (state, claimed_run, lease) = bound();
    let run_id = claimed_run.run.run_id.as_str();
    let state = runs::spend(&state, run_id, &lease, nine(), (1, 1500)).expect("spent");
    assert_eq!(
        record::run(&state, run_id).expect("a run").usage,
        object(vec![("duration_ms", int(1500))])
    );
    let state = runs::lose_protection(&state, run_id).expect("lost");
    let state = fence(&state, run_id, &lease, nine()).expect("fenced");
    let state = settle_hold(&state, run_id, nine() + 1, unknown_step(), &ENTROPY).expect("settled");
    let notice = last_notice(&state);
    assert_eq!(
        (
            notice.outcome.as_str(),
            notice.protection_lost,
            notice.usage.clone()
        ),
        ("held", true, Some(object(vec![("duration_ms", int(1500))])))
    );
    assert!(
        incident(&state, run_id)
            .expect("an incident")
            .protection_lost
    );
    let (reopened, _token) = reopen_incident(
        &state,
        run_id,
        nine() + 2,
        &generation_for("net_1", run_id, "s1"),
        &ENTROPY,
    )
    .expect("reopened");
    let resumed = record::run(&reopened, run_id).expect("a run");
    assert!(resumed.protection_lost);
    assert_eq!(resumed.usage, object(vec![("duration_ms", int(1500))]));
}

#[test]
fn a_frozen_run_names_exactly_the_step_of_the_plan_it_waits_at() {
    let (state, claimed_run, lease) = claimed();
    let run_id = claimed_run.run.run_id.as_str();
    for (action, position) in [("check", 2), ("check", 0), ("other", 1)] {
        let request = Request {
            kind: "human".to_owned(),
            assistant_id: "dns".to_owned(),
            action: action.to_owned(),
            position,
        };
        assert_eq!(
            runs::freeze(&state, run_id, &lease, nine(), &request, &ENTROPY).map(|_| ()),
            Err(RoutineStateError::new("freeze-invalid"))
        );
    }
    let request = Request {
        kind: "human".to_owned(),
        assistant_id: "dns".to_owned(),
        action: "check".to_owned(),
        position: STEP,
    };
    let frozen = runs::freeze(&state, run_id, &lease, nine(), &request, &ENTROPY).expect("frozen");
    assert_eq!(
        last_notice(&frozen).detail,
        object(vec![("request_kind", text("human"))])
    );
    let waiting = record::run(&frozen, run_id).expect("a run");
    assert_eq!(
        (
            waiting.status,
            waiting.assistant_id.as_str(),
            waiting.action.as_str()
        ),
        (RunStatus::Frozen, "dns", "check")
    );
    let (thawed, _token) = runs::thaw(&frozen, run_id, nine() + 1, 0, &ENTROPY).expect("thawed");
    let resumed = record::run(&thawed, run_id).expect("a run");
    assert_eq!(
        (
            resumed.status,
            resumed.request_kind.as_str(),
            resumed.action.as_str()
        ),
        (RunStatus::Leased, "", "")
    );
}
