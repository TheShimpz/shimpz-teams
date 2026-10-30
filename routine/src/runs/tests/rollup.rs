//! A continuous Routine's healthy runs roll up into one versioned notice per minute (`test_routine_rollup.py`).
//!
//! The rollup carries the Routine's name as it was when each version was written and the minute's summed usage.

use std::collections::BTreeMap;

use team_protocol::routine::MAX_ROLLUP_RUNS;

use super::{duration, finished, run_once};
use crate::json::{int, object, text};
use crate::record::{self, Routine, TeamRoutines, acknowledge};
use crate::testing::{
    ENTROPY, Fixture, NETWORK, ROUTINE_A, added, at_time, bound, claim_bound, edit, edit_run, nine,
    plan_document,
};

const NAME: &str = "Resumo das mudanças de DNS";
const RUN_MS: i64 = 1200;

/// A one-step Routine every `gap` seconds that shows no result, so its healthy runs roll up.
fn rollup_routine(gap: i64) -> Routine {
    let mut value = Fixture {
        schedule: object(vec![("kind", text("continuous")), ("gap", int(gap))]),
        plan: plan_document(
            "dns",
            "UTC",
            "check",
            Some(r#"{"mode": "none", "step": null}"#),
        ),
        ..Fixture::default()
    }
    .build();
    value.name = NAME.to_owned();
    value
}

fn continuous() -> TeamRoutines {
    at_time(&added(&[rollup_routine(5)]), ROUTINE_A, nine())
}

fn run(state: &TeamRoutines, start: i64, end_at: i64) -> TeamRoutines {
    run_once(state, ROUTINE_A, start, end_at, "done")
}

fn delivered(state: &TeamRoutines) -> Vec<(String, i64)> {
    state
        .notices
        .iter()
        .map(|item| (item.notice_id.clone(), item.version))
        .collect()
}

fn instants(state: &TeamRoutines) -> Vec<(i64, i64)> {
    state
        .notices
        .iter()
        .map(|item| (item.created_at, item.version))
        .collect()
}

#[test]
fn healthy_runs_in_one_minute_share_one_notice_whose_count_is_its_version() {
    let mut state = continuous();
    for index in 0..3 {
        state = run(&state, nine() + 10 * index, nine() + 10 * index + 2);
    }
    let [notice] = state.notices.as_slice() else {
        panic!("one notice");
    };
    assert_eq!(
        (
            notice.outcome.as_str(),
            notice.run_id.as_str(),
            notice.created_at,
            &notice.detail,
            notice.version
        ),
        ("healthy", "", nine(), &object(vec![("runs", int(3))]), 3)
    );
    assert_eq!(
        (notice.name.as_str(), notice.usage.clone()),
        (NAME, Some(duration(3 * RUN_MS)))
    );
    assert_eq!(state.routines[0].failures, 0);
    // The next minute starts its own notice.
    let state = run(&state, nine() + 60, nine() + 61);
    assert_eq!(instants(&state), [(nine(), 3), (nine() + 60, 1)]);
    assert_ne!(state.notices[0].notice_id, state.notices[1].notice_id);
}

#[test]
fn an_acknowledged_minute_goes_on_with_a_newer_version_of_the_same_notice() {
    let state = run(&continuous(), nine(), nine() + 1);
    let first = state.notices[0].clone();
    let state = acknowledge(&state, &[(first.notice_id.clone(), 1)]);
    assert_eq!(state.notices, []);
    let state = run(&state, nine() + 10, nine() + 11);
    let [second] = state.notices.as_slice() else {
        panic!("one notice");
    };
    assert_eq!(
        (second.notice_id.as_str(), second.version, &second.detail),
        (first.notice_id.as_str(), 2, &object(vec![("runs", int(2))]))
    );
}

#[test]
fn a_success_ends_the_failure_streak_and_failures_keep_their_own_notices() {
    let state = run_once(&continuous(), ROUTINE_A, nine(), nine() + 1, "failed");
    assert_eq!(state.routines[0].failures, 1);
    let state = run(&state, nine() + 10, nine() + 11);
    let outcomes: Vec<&str> = state
        .notices
        .iter()
        .map(|item| item.outcome.as_str())
        .collect();
    assert_eq!(outcomes, ["failed", "healthy"]);
    assert_eq!(state.routines[0].failures, 0);
}

#[test]
fn a_run_with_its_own_earlier_notice_or_a_scheduled_routine_keeps_one_notice_per_run() {
    let (state, claimed, lease) = claim_bound(&continuous(), nine(), NETWORK);
    let run_id = claimed.run.run_id;
    // A run that already published a notice, such as one a person answered, ends on that same notice.
    let state = edit_run(&state, &run_id, |answered| answered.notice_version = 1);
    let state = finished(&state, &run_id, &lease, nine() + 1).expect("finished");
    let notices: Vec<(&str, &str, i64)> = state
        .notices
        .iter()
        .map(|item| (item.outcome.as_str(), item.notice_id.as_str(), item.version))
        .collect();
    assert_eq!(notices, [("done", run_id.as_str(), 2)]);
    let (state, claimed, lease) = bound();
    let state = finished(&state, &claimed.run.run_id, &lease, nine() + 1).expect("finished");
    assert_eq!(
        state
            .notices
            .iter()
            .map(|item| item.outcome.as_str())
            .collect::<Vec<_>>(),
        ["done"]
    );
}

#[test]
fn a_minute_past_its_bound_rolls_up_nothing_more_and_shows_nothing() {
    let limit = i64::try_from(MAX_ROLLUP_RUNS).expect("a bound");
    let state = edit(&continuous(), ROUTINE_A, |value| {
        value.rollup_minute = nine();
        value.rollup_runs = limit;
    });
    let state = run(&state, nine() + 30, nine() + 31);
    assert_eq!(state.notices, []);
    assert_eq!(state.routines[0].rollup_runs, limit);
}

#[test]
fn an_hour_of_runs_every_five_seconds_delivers_one_notice_a_minute() {
    let (mut state, mut now) = (continuous(), nine());
    let mut versions: BTreeMap<String, i64> = BTreeMap::new();
    for _ in 0..720 {
        state = run(&state, now, now);
        // Admin delivers and acknowledges as it goes; the rollup's versions replace each other.
        versions.extend(delivered(&state));
        state = acknowledge(&state, &delivered(&state));
        // Team removes what each ended run held.
        state.discards.clear();
        now += 5;
    }
    assert_eq!(versions.len(), 60);
    // Every minute's one notice counts its twelve runs.
    assert!(versions.values().all(|version| *version == 12));
}

#[test]
fn a_change_and_a_late_acknowledgment_in_one_minute_keep_counting_one_notice() {
    let state = run(&continuous(), nine(), nine() + 1);
    let first = delivered(&state);
    let state = run(&state, nine() + 10, nine() + 11);
    let changed = record::scheduled(&rollup_routine(6), nine() + 20).expect("scheduled");
    let state = record::update(&state, &changed, 1, nine() + 20, &ENTROPY).expect("updated");
    let state = run(&state, nine() + 30, nine() + 31);
    // The first delivery's acknowledgment arrives only now, after the minute's count grew twice.
    let state = acknowledge(&state, &first);
    let healthy: Vec<_> = state
        .notices
        .iter()
        .filter(|item| item.outcome == "healthy")
        .collect();
    let [last] = healthy.as_slice() else {
        panic!("one rollup");
    };
    assert_eq!(
        (last.notice_id.as_str(), last.version, &last.detail),
        (first[0].0.as_str(), 3, &object(vec![("runs", int(3))]))
    );
    // The minute's usage sums every run of it, across the change.
    assert_eq!(last.usage, Some(duration(3 * RUN_MS)));
    assert_eq!(state.routines[0].revision, 2);
    let state = acknowledge(&state, &[(last.notice_id.clone(), last.version)]);
    assert_eq!(
        state
            .notices
            .iter()
            .map(|item| item.outcome.as_str())
            .collect::<Vec<_>>(),
        ["changed"]
    );
}

#[test]
fn a_clock_stepped_back_into_a_delivered_minute_never_republishes_it() {
    let mut state = continuous();
    let mut deliveries: Vec<Vec<(i64, i64)>> = Vec::new();
    for (start, end_at) in [
        (nine(), nine() + 1),
        (nine() + 10, nine() + 11),
        (nine() + 60, nine() + 61),
    ] {
        state = run(&state, start, end_at);
        if end_at != nine() + 1 {
            deliveries.push(instants(&state));
            state = acknowledge(&state, &delivered(&state));
        }
    }
    // A run whose clock fell back into a delivered minute shows nothing, as a run of none does.
    state = run(&state, nine() + 30, nine() + 31);
    deliveries.push(instants(&state));
    state = run(&state, nine() + 70, nine() + 71);
    deliveries.push(instants(&state));
    assert_eq!(
        deliveries,
        [
            vec![(nine(), 2)],
            vec![(nine() + 60, 1)],
            vec![],
            vec![(nine() + 60, 2)]
        ]
    );
    let state = acknowledge(&state, &delivered(&state));
    let current = &state.routines[0];
    assert_eq!(
        (
            current.rollup_minute,
            current.rollup_runs,
            state.notices.len()
        ),
        (nine() + 60, 2, 0)
    );
}
