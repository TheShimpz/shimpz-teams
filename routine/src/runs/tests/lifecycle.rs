//! A claimed run's transitions: leases, freezes, thaws, ends, failure streaks, the watchdog, and the removal queue.

use super::{
    claimed_and_stopped, duration, empty, ended, failure, finished, frozen, human_check, refused,
    request, worker,
};
use crate::claim::{bind_generation, claim, claimable, generation_for, require_lease};
use crate::definition;
use crate::hold::{self, fence, rekeyed};
use crate::json::{int, object};
use crate::plan::{LEASE_MARGIN_SECONDS, active_seconds};
use crate::record::{
    self, ACTIVE_SECONDS, HUMAN_LEASE, LEASE_SECONDS, MAX_DISCARDS, MAX_ROUTINE_NOTICES,
    MAX_ROUTINES, MAX_UNDELIVERED_NOTICES, Notice, Routine, Run, RunStatus, TeamRoutines,
    begin_delete, complete_delete, lease_of, mark_scope_changed, put_notice,
};
use crate::runs::{MAX_USAGE_MS, complete_recovered, end, freeze, joined_usage, spend, thaw};
use crate::testing::{
    ENTROPY, Fixture, KEY, NETWORK, ROUTINE_A, added, at_time, bound, claim_bound, claimed,
    default_plan, done_detail, edit, epoch, every_hour, full_notices, json, nine,
    routine as fixture,
};

#[test]
fn worker_transitions_need_the_live_lease() {
    let (state, claimed_run, lease) = bound();
    let run_id = claimed_run.run.run_id.as_str();
    let forged = lease_of("forged", KEY);
    refused(
        spend(&state, run_id, &forged, nine() + 1, (1, 1000)),
        "lease-invalid",
    );
    refused(
        freeze(
            &state,
            run_id,
            &forged,
            nine() + 1,
            &human_check(),
            &ENTROPY,
        ),
        "lease-invalid",
    );
    refused(
        finished(&state, run_id, &forged, nine() + 1),
        "lease-invalid",
    );
    refused(
        bind_generation(&state, run_id, &forged, nine() + 1, "net_2"),
        "lease-invalid",
    );
    refused(
        finished(&state, run_id, &lease, nine() + LEASE_SECONDS),
        "lease-invalid",
    );
}

#[test]
fn a_frozen_run_holds_its_routine_and_resumes_under_a_fresh_lease() {
    let (state, claimed_run, lease) = bound();
    let run_id = claimed_run.run.run_id.as_str();
    let state = spend(&state, run_id, &lease, nine(), (30, 30_000)).expect("spent");
    let state = frozen(&state, run_id, &lease).expect("frozen");
    let parked = record::run(&state, run_id).expect("a run");
    let left = active_seconds(1) - 30;
    assert_eq!(
        (
            parked.status,
            parked.lease_sha256.as_str(),
            parked.active_seconds_left
        ),
        (RunStatus::Frozen, "", left)
    );
    assert!(claimable(&state, epoch(2026, 10, 9, 9, 0, 0), true).is_none());
    refused(frozen(&state, run_id, &lease), "run-not-running");
    let (state, token) = thaw(&state, run_id, nine() + 50, 0, &ENTROPY).expect("thawed");
    let human = lease_of(&token, HUMAN_LEASE);
    assert_eq!(
        require_lease(
            record::run(&state, run_id).expect("a run"),
            &human,
            nine() + 51
        ),
        Ok(())
    );
    assert_eq!(rekeyed(&state, &"f".repeat(64)), Vec::<&Run>::new());
    let state = spend(&state, run_id, &human, nine() + 51, (10, 10_000)).expect("spent");
    let resumed = record::run(&state, run_id).expect("a run");
    assert_eq!(resumed.active_seconds_left, left - 10);
    // The person's answer runs at once, so its lease covers the run's active time left and a margin.
    assert_eq!(
        resumed.lease_expires_at,
        nine() + 50 + left + LEASE_MARGIN_SECONDS
    );
    refused(
        thaw(&state, run_id, nine() + 60, 0, &ENTROPY),
        "run-not-frozen",
    );
}

#[test]
fn invalid_freezes_and_durations_are_refused() {
    let (state, claimed_run, lease) = claimed();
    let run_id = claimed_run.run.run_id.as_str();
    for wrong in [
        request("mail", "dns", "list", 1),
        request("human", "Dns", "list", 1),
        request("human", "dns", "Bad", 1),
        request("human", "dns", "check", 2),
        request("human", "dns", "check", 0),
        request("human", "dns", "notify", 1),
    ] {
        refused(
            freeze(&state, run_id, &lease, nine(), &wrong, &ENTROPY),
            "freeze-invalid",
        );
    }
    for elapsed in [(-1, 0), (1, -1)] {
        refused(
            spend(&state, run_id, &lease, nine(), elapsed),
            "invalid-duration",
        );
    }
}

#[test]
fn a_team_freezes_at_most_eight_runs() {
    let (state, claimed_run, lease) = claimed();
    let mut runs = state.runs.clone();
    runs.extend((0..8).map(|index| {
        Run::new(
            &format!("{index:032x}"),
            &format!("{:032x}", index + 1),
            RunStatus::Frozen,
        )
    }));
    let state = TeamRoutines { runs, ..state };
    refused(
        frozen(&state, &claimed_run.run.run_id, &lease),
        "frozen-limit",
    );
}

#[test]
fn team_ends_runs_without_their_lease_by_state() {
    let (state, claimed_run, _lease) = bound();
    let run_id = claimed_run.run.run_id.as_str();
    assert_eq!(
        ended(&state, run_id, "stopped", empty())
            .expect("ended")
            .runs,
        []
    );
    for outcome in ["done", "denied"] {
        refused(ended(&state, run_id, outcome, empty()), "invalid-outcome");
    }
    let (state, claimed_run, lease) = bound();
    let run_id = claimed_run.run.run_id.as_str();
    let parked = frozen(&state, run_id, &lease).expect("frozen");
    assert_eq!(
        ended(&parked, run_id, "denied", empty())
            .expect("denied")
            .notices[0]
            .outcome,
        "denied"
    );
    refused(
        ended(&parked, run_id, "done", done_detail()),
        "invalid-outcome",
    );
    refused(
        end(
            &state,
            run_id,
            nine(),
            "stopped",
            empty(),
            Some(RunStatus::Frozen),
            &ENTROPY,
        ),
        "run-changed",
    );
}

#[test]
fn worker_outcomes_are_closed_and_notices_are_valid_with_positive_versions() {
    let (state, claimed_run, lease) = bound();
    let run_id = claimed_run.run.run_id.as_str();
    let done = finished(&state, run_id, &lease, nine() + 9).expect("finished");
    assert_eq!(
        (done.runs.len(), done.notices[0].outcome.as_str()),
        (0, "done")
    );
    refused(
        ended(&state, run_id, "stopped", object(vec![("result", empty())])),
        "notice-invalid",
    );
    refused(record::run(&done, run_id), "run-not-found");
    let notice = Notice::new(
        &"n".repeat(32),
        ROUTINE_A,
        "",
        "done",
        nine(),
        done_detail(),
    );
    let state = put_notice(&state, notice).expect("put");
    assert_eq!(
        state.notices.last().map(|item| item.detail.clone()),
        Some(done_detail())
    );
    let mut zero = Notice::new(
        &"n".repeat(32),
        ROUTINE_A,
        "",
        "skipped",
        nine(),
        object(vec![("missed", int(1))]),
    );
    zero.version = 0;
    refused(put_notice(&state, zero), "notice-invalid");
}

#[test]
fn in_flight_outcomes_always_fit_above_the_claim_bound() {
    let (state, claimed_run, lease) = claimed();
    let state = TeamRoutines {
        notices: full_notices(MAX_UNDELIVERED_NOTICES),
        ..state
    };
    let state = finished(&state, &claimed_run.run.run_id, &lease, nine()).expect("finished");
    assert_eq!(state.notices.len(), MAX_UNDELIVERED_NOTICES + 1);
    let over = TeamRoutines {
        notices: full_notices(MAX_UNDELIVERED_NOTICES + MAX_ROUTINE_NOTICES),
        ..state
    };
    refused(
        mark_scope_changed(&over, ROUTINE_A, nine(), &ENTROPY),
        "notices-full",
    );
}

#[test]
fn leases_and_active_time_expire() {
    let (state, claimed_run, lease) = claimed();
    let run_id = claimed_run.run.run_id.as_str();
    assert_eq!(hold::expired(&state, nine() + 10), Vec::<&Run>::new());
    let spent = spend(
        &state,
        run_id,
        &lease,
        nine(),
        (ACTIVE_SECONDS, ACTIVE_SECONDS * 1000),
    )
    .expect("spent");
    let expired: Vec<&str> = hold::expired(&spent, nine() + 10)
        .iter()
        .map(|item| item.run_id.as_str())
        .collect();
    assert_eq!(expired, [run_id]);
    refused(
        require_lease(
            record::run(&spent, run_id).expect("a run"),
            &lease,
            nine() + 10,
        ),
        "lease-invalid",
    );
    assert_eq!(hold::expired(&state, nine() + LEASE_SECONDS).len(), 1);
}

#[test]
fn deletion_keeps_runs_until_they_end_and_keeps_notices() {
    let (state, claimed_run, _lease) = claimed();
    let state = ended(&state, &claimed_run.run.run_id, "stopped", empty()).expect("ended");
    let later = nine() + 86_400;
    let (state, again) = claim(
        &at_time(&state, ROUTINE_A, later),
        later,
        KEY,
        true,
        &ENTROPY,
    )
    .expect("a claim");
    let again = again.expect("a claim").run.run_id;
    let (state, runs) = begin_delete(&state, ROUTINE_A).expect("deleting");
    assert_eq!(
        runs.iter()
            .map(|item| item.run_id.as_str())
            .collect::<Vec<_>>(),
        [again.as_str()]
    );
    refused(
        complete_delete(&state, ROUTINE_A, nine(), &ENTROPY),
        "routine-busy",
    );
    let state = end(&state, &again, later, "stopped", empty(), None, &ENTROPY).expect("ended");
    let state = complete_delete(&state, ROUTINE_A, later, &ENTROPY).expect("deleted");
    assert_eq!(state.routines, []);
    let outcomes: Vec<&str> = state
        .notices
        .iter()
        .map(|item| item.outcome.as_str())
        .collect();
    assert_eq!(outcomes, ["stopped", "stopped", "deleted"]);
    refused(
        complete_delete(&added(&[fixture()]), ROUTINE_A, nine(), &ENTROPY),
        "routine-busy",
    );
}

/// The default Routine claimed at nine and ended there with `outcome`, by Team or, for `done`, by its worker.
fn claimed_and_ended(state: &TeamRoutines, outcome: &str, at: i64) -> TeamRoutines {
    let (claimed_state, claimed_run) = claim(
        &at_time(state, ROUTINE_A, nine()),
        nine(),
        KEY,
        true,
        &ENTROPY,
    )
    .expect("a claim");
    let claimed_run = claimed_run.expect("a claim");
    if outcome == "done" {
        return finished(
            &claimed_state,
            &claimed_run.run.run_id,
            &worker(&claimed_run.lease_token),
            nine(),
        )
        .expect("finished");
    }
    let detail = if outcome == "failed" {
        failure()
    } else {
        empty()
    };
    end(
        &claimed_state,
        &claimed_run.run.run_id,
        at,
        outcome,
        detail,
        None,
        &ENTROPY,
    )
    .expect("ended")
}

#[test]
fn three_failures_in_a_row_pause_the_routine_and_a_success_resets_the_streak() {
    let mut state = added(&[fixture()]);
    for (index, outcome) in ["failed", "failed", "done", "failed", "failed", "failed"]
        .iter()
        .enumerate()
    {
        state = claimed_and_ended(
            &state,
            outcome,
            nine() + i64::try_from(index).expect("an index"),
        );
        state.discards.clear();
        state.starts.clear();
        let current = record::routine(&state, ROUTINE_A).expect("a Routine");
        assert_eq!(
            (current.failures, current.paused),
            ([1, 2, 0, 1, 2, 3][index], index == 5)
        );
    }
    // The pause is said once, as the Routine's own notice with how many runs failed in a row.
    let streaks: Vec<(&str, _)> = state
        .notices
        .iter()
        .filter(|item| item.outcome == "streak-paused")
        .map(|item| (item.run_id.as_str(), item.detail.clone()))
        .collect();
    assert_eq!(streaks, [("", object(vec![("failures", int(3))]))]);
}

#[test]
fn a_paused_routine_says_nothing_more_and_a_stop_is_no_failure() {
    // A Routine already paused, by a person or a failure streak, says nothing more as more runs fail.
    let streak = edit(&added(&[fixture()]), ROUTINE_A, |value| value.failures = 3);
    let (claimed_state, claimed_run) = claim(
        &at_time(&streak, ROUTINE_A, nine()),
        nine(),
        KEY,
        true,
        &ENTROPY,
    )
    .expect("a claim");
    let paused = record::set_paused(&claimed_state, ROUTINE_A, true).expect("paused");
    let failed = ended(
        &paused,
        &claimed_run.expect("a claim").run.run_id,
        "failed",
        failure(),
    )
    .expect("failed");
    assert_eq!(
        failed
            .notices
            .iter()
            .filter(|item| item.outcome == "streak-paused")
            .count(),
        0
    );
    // A Stop or a denial is no execution failure.
    let two = edit(&added(&[fixture()]), ROUTINE_A, |value| value.failures = 2);
    let stopped = claimed_and_ended(&two, "stopped", nine());
    assert_eq!(
        record::routine(&stopped, ROUTINE_A)
            .expect("a Routine")
            .failures,
        2
    );
}

#[test]
fn a_recovered_run_is_held_or_done_only_under_the_lease_the_watchdog_read() {
    let (state, claimed_run, _lease) = bound();
    let (run_id, lease_sha256) = (
        claimed_run.run.run_id.as_str(),
        claimed_run.run.lease_sha256.as_str(),
    );
    let held = hold::hold_recovered(&state, run_id, lease_sha256).expect("held");
    let held_run = record::run(&held, run_id).expect("a run");
    assert_eq!(
        (
            held_run.status,
            held_run.lease_sha256.as_str(),
            held_run.lease_expires_at
        ),
        (RunStatus::Held, "", 0)
    );
    let done =
        complete_recovered(&state, run_id, lease_sha256, nine() + 5, None, &ENTROPY).expect("done");
    assert_eq!(done.runs, []);
    let unavailable =
        json(r#"{"step": 1, "state": "unavailable", "value": null, "truncated": false}"#);
    let expected = object(vec![
        ("plan", definition::summary(&default_plan(), 1)),
        ("output", unavailable),
    ]);
    let last = done.notices.last().expect("a notice");
    assert_eq!((last.outcome.as_str(), &last.detail), ("done", &expected));
    let (unbound, unclaimed, _lease) = claimed();
    refused(
        hold::hold_recovered(&state, run_id, &"0".repeat(64)),
        "run-changed",
    );
    refused(
        complete_recovered(&state, run_id, &"0".repeat(64), nine(), None, &ENTROPY),
        "run-changed",
    );
    refused(
        hold::hold_recovered(&unbound, &unclaimed.run.run_id, &unclaimed.run.lease_sha256),
        "generation-invalid",
    );
    refused(
        hold::hold_recovered(&held, run_id, lease_sha256),
        "run-not-running",
    );
}

#[test]
fn stale_or_misplaced_transitions_are_refused() {
    let (state, claimed_run, lease) = bound();
    let run_id = claimed_run.run.run_id.as_str();
    let (deleting, _runs) = begin_delete(&state, ROUTINE_A).expect("deleting");
    refused(frozen(&deleting, run_id, &lease), "routine-deleting");
    let queued = TeamRoutines {
        discards: vec![
            (run_id.to_owned(), "g1".to_owned()),
            (run_id.to_owned(), "g2".to_owned()),
        ],
        ..state
    };
    assert_eq!(
        record::discarded(&queued, run_id, "g1").discards,
        [(run_id.to_owned(), "g2".to_owned())]
    );
}

/// The run of the Team's next due Routine claimed, frozen, thawed, fenced, held, continued, and stopped at `now`.
fn held_and_continued(state: &TeamRoutines, run_id: &str, now: i64) -> TeamRoutines {
    let (thawed, token) = thaw(state, run_id, now, 0, &ENTROPY).expect("thawed");
    let fenced = fence(&thawed, run_id, &lease_of(&token, HUMAN_LEASE), now).expect("fenced");
    let settled =
        hold::settle_hold(&fenced, run_id, now, hold::unknown_step(), &ENTROPY).expect("settled");
    let generation = generation_for(NETWORK, run_id, "s1");
    let (reopened, _token) =
        hold::reopen_incident(&settled, run_id, now, &generation, &ENTROPY).expect("reopened");
    end(&reopened, run_id, now, "stopped", empty(), None, &ENTROPY).expect("ended")
}

#[test]
fn the_removal_queue_stays_within_its_bound_while_every_removal_fails() {
    // Claims stop once MAX_ROUTINES removals wait, yet each Routine's one live run may still queue two generations:
    // the one its hold left and its continuation's. Nothing is ever drained here.
    let routines: Vec<Routine> = (0..MAX_ROUTINES)
        .map(|index| {
            Fixture {
                id: format!("{index:032x}"),
                schedule: every_hour(),
                ..Fixture::default()
            }
            .build()
        })
        .collect();
    let (mut state, mut now) = (added(&routines), nine());
    for index in 0..MAX_ROUTINES - 1 {
        state = claimed_and_stopped(&state, &format!("{index:032x}"), now);
        now += 3601;
    }
    let mut parked = Vec::new();
    for _ in 0..MAX_ROUTINES {
        let (claimed_state, claimed_run, lease) = claim_bound(&state, now, NETWORK);
        state = freeze(
            &claimed_state,
            &claimed_run.run.run_id,
            &lease,
            now,
            &human_check(),
            &ENTROPY,
        )
        .expect("frozen");
        parked.push(claimed_run.run.run_id);
    }
    for run_id in parked {
        assert_eq!(super::run_status(&state, &run_id), RunStatus::Frozen);
        state = held_and_continued(&state, &run_id, now);
    }
    assert_eq!(state.discards.len(), MAX_ROUTINES - 1 + 2 * MAX_ROUTINES);
    assert!(state.discards.len() <= MAX_DISCARDS);
}

#[test]
fn usage_sums_active_time_within_its_bound() {
    assert_eq!(joined_usage(&duration(1), &duration(2)), duration(3));
    assert_eq!(
        joined_usage(&duration(MAX_USAGE_MS), &duration(5)),
        duration(MAX_USAGE_MS)
    );
}
