//! `test_routine_claim.py` and the claim parts of `test_routine_record_time.py`: due Routines are claimed fairly and
//! once under every cap, and missed firings are swept and reported.

use strict_json::Value;

use super::{
    bind_generation, claim, claimable, generation_for, next_due, require_lease, sweep,
    verification_of,
};
use crate::hold::{self, Expected};
use crate::json::{int, object};
use crate::plan::{LEASE_MARGIN_SECONDS, active_seconds, long_run};
use crate::record::{
    self, LEASE_SECONDS, MAX_COUNTED_MISSES, MAX_UNDELIVERED_NOTICES, Notice, Routine,
    RoutineStateError, Run, TeamRoutines, acknowledge, begin_delete, grace_seconds, lease_of,
    mark_scope_changed,
};
use crate::runs;
use crate::starts::MAX_STARTS;
use crate::testing::{
    ENTROPY, Fixture, KEY, ROUTINE_A, added, at_time, claim_bound, confirmed, epoch, every_hour,
    full_notices, held, json, nine, repeated_plan, routine as fixture, routine_of,
    routine_scheduled,
};

const ROUTINE_B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const ROUTINE_C: &str = "cccccccccccccccccccccccccccccccc";

fn claim_at(state: &TeamRoutines, now: i64) -> (TeamRoutines, Option<record::Claim>) {
    claim(state, now, KEY, true, &ENTROPY).expect("a claim attempt")
}

fn details(state: &TeamRoutines) -> Vec<Value> {
    state
        .notices
        .iter()
        .map(|item| item.detail.clone())
        .collect()
}

fn missed(count: i64) -> Value {
    object(vec![("missed", int(count))])
}

#[test]
fn a_due_routine_is_claimed_once_under_a_lease_bound_to_the_routine_key() {
    let state = at_time(&added(&[fixture()]), ROUTINE_A, nine());
    assert!(claimable(&state, nine() - 1, true).is_none());
    assert_eq!(claim_at(&state, nine() - 1), (state.clone(), None));
    assert_eq!(
        claim(&state, nine(), "short", true, &ENTROPY).map(|_| ()),
        Err(RoutineStateError::new("routine-key-invalid"))
    );
    let (state, claimed) = claim_at(&state, nine());
    let claimed = claimed.expect("a claim");
    assert_eq!(
        (claimed.run.status, claimed.run.lease_key.as_str()),
        (record::RunStatus::Leased, KEY)
    );
    assert_eq!(
        record::routine(&state, ROUTINE_A)
            .expect("a Routine")
            .next_run_at,
        epoch(2026, 10, 2, 9, 0, 0)
    );
    // A second claim on the same state finds nothing: the Routine never overlaps, even when due again.
    assert!(claim_at(&state, epoch(2026, 10, 2, 9, 0, 0)).1.is_none());
}

#[test]
fn only_the_live_lease_under_the_current_key_drives_a_run() {
    let (state, claimed, _lease) = crate::testing::claimed();
    let value = record::run(&state, &claimed.run.run_id).expect("a run");
    assert_eq!(
        require_lease(value, &lease_of(&claimed.lease_token, KEY), nine() + 1),
        Ok(())
    );
    let other_key = "f".repeat(64);
    for (token, key, now) in [
        ("other", KEY, nine() + 1),
        (claimed.lease_token.as_str(), other_key.as_str(), nine() + 1),
        (claimed.lease_token.as_str(), KEY, nine() + LEASE_SECONDS),
    ] {
        assert_eq!(
            require_lease(value, &lease_of(token, key), now),
            Err(RoutineStateError::new("lease-invalid"))
        );
    }
    assert_eq!(hold::rekeyed(&state, &other_key), vec![value]);
    assert_eq!(hold::rekeyed(&state, KEY), Vec::<&Run>::new());
}

#[test]
fn the_oldest_due_routine_wins_and_the_rolling_team_ceiling_holds_to_the_second() {
    let eleven = epoch(2026, 10, 1, 23, 0, 0);
    let state = at_time(
        &at_time(
            &added(&[routine_of(ROUTINE_A), routine_of(ROUTINE_B)]),
            ROUTINE_A,
            eleven,
        ),
        ROUTINE_B,
        eleven - 60,
    );
    assert_eq!(
        claimable(&state, eleven, true).map(|item| item.routine_id.as_str()),
        Some(ROUTINE_B)
    );
    // The Team's daily steps count every start in the last 24 hours, whatever Routine made it, even a deleted one.
    let first = eleven - 86_400 + 30;
    let capped = TeamRoutines {
        starts: (0..MAX_STARTS)
            .map(|index| (ROUTINE_C.to_owned(), first + index, 1))
            .collect(),
        ..state
    };
    assert!(claimable(&capped, eleven, true).is_none());
    // The window rolls to the second: the oldest start leaves it exactly 24 hours after it was made.
    assert!(claimable(&capped, first + 86_400 - 1, true).is_none());
    assert_eq!(next_due(&capped, eleven), Some(first + 86_400));
    let (after, claimed) = claim_at(&capped, first + 86_400);
    let claimed = claimed.expect("a claim");
    assert_eq!(
        (claimed.run.routine_id.as_str(), after.starts.len()),
        (ROUTINE_B, usize::try_from(MAX_STARTS).expect("a count"))
    );
    assert_eq!(
        after.starts.last(),
        Some(&(ROUTINE_B.to_owned(), first + 86_400, 1))
    );
}

#[test]
fn reconfirmation_and_deletion_stop_claims() {
    let state = mark_scope_changed(
        &at_time(&added(&[fixture()]), ROUTINE_A, nine()),
        ROUTINE_A,
        nine(),
        &ENTROPY,
    )
    .expect("marked");
    assert!(claimable(&state, nine(), true).is_none());
    assert_eq!(
        state
            .notices
            .iter()
            .map(|item| (item.outcome.as_str(), item.detail.clone()))
            .collect::<Vec<_>>(),
        [("scope-changed", object(vec![]))]
    );
    let (deleting, runs) =
        begin_delete(&at_time(&added(&[fixture()]), ROUTINE_A, nine()), ROUTINE_A)
            .expect("deleting");
    assert_eq!(runs, []);
    assert!(claimable(&deleting, nine(), true).is_none());
}

#[test]
fn only_one_late_firing_is_made_up_and_the_others_are_reported_once() {
    let state = at_time(&added(&[routine_of(ROUTINE_B)]), ROUTINE_B, nine());
    let (state, _claim) = claim_at(&state, nine() + 40 * 60);
    assert_eq!(
        record::routine(&state, ROUTINE_B)
            .expect("a Routine")
            .next_run_at,
        nine() + 86_400
    );
    assert_eq!(state.notices, []);
    let state = at_time(&added(&[routine_of(ROUTINE_B)]), ROUTINE_B, nine() - 86_400);
    let (state, _claim) = claim_at(&state, nine() + 1200);
    assert_eq!(details(&state), [missed(1)]);
    assert_eq!(
        record::routine(&state, ROUTINE_B)
            .expect("a Routine")
            .missed,
        0
    );
}

#[test]
fn a_long_outage_is_one_skipped_notice_updated_as_the_gap_grows() {
    let now = nine() + 72 * 3600;
    // A firing at most 12 hours late is still made up; every earlier one is skipped.
    let state = sweep(&at_time(&added(&[fixture()]), ROUTINE_A, nine()), now).expect("swept");
    assert_eq!(details(&state), [missed(3)]);
    assert_eq!(
        record::routine(&state, ROUTINE_A)
            .expect("a Routine")
            .next_run_at,
        now
    );
    assert_eq!(sweep(&state, now), Ok(state.clone()));
    let later = sweep(&state, now + 5 * 86_400).expect("swept");
    let first: &Notice = &state.notices[0];
    assert_eq!(
        later
            .notices
            .iter()
            .map(|item| (item.notice_id.clone(), item.detail.clone()))
            .collect::<Vec<_>>(),
        [(first.notice_id.clone(), missed(8))]
    );
    // Acknowledging an older version keeps the updated notice; after delivery, the same gap is reported again under
    // the same notice, never as a second one.
    let unchanged = acknowledge(&later, &[(first.notice_id.clone(), first.version)]);
    assert_eq!(unchanged.notices, later.notices);
    let delivered = acknowledge(
        &later,
        &later
            .notices
            .iter()
            .map(|item| (item.notice_id.clone(), item.version))
            .collect::<Vec<_>>(),
    );
    let again = sweep(&delivered, now + 7 * 86_400).expect("swept");
    assert_eq!(
        again
            .notices
            .iter()
            .map(|item| (item.notice_id.clone(), item.detail.clone()))
            .collect::<Vec<_>>(),
        [(first.notice_id.clone(), missed(10))]
    );
}

#[test]
fn counting_misses_is_bounded() {
    let days = MAX_COUNTED_MISSES + 5;
    let state = sweep(
        &at_time(&added(&[routine_of(ROUTINE_B)]), ROUTINE_B, nine()),
        nine() + days * 86_400,
    )
    .expect("swept");
    assert_eq!(state.notices[0].detail, missed(MAX_COUNTED_MISSES));
    assert_eq!(
        record::routine(&state, ROUTINE_B)
            .expect("a Routine")
            .next_run_at,
        nine() + days * 86_400
    );
}

#[test]
fn a_full_notice_queue_blocks_claims_and_keeps_counting_skips() {
    let full = full_notices(MAX_UNDELIVERED_NOTICES);
    let state = TeamRoutines {
        notices: full.clone(),
        ..at_time(&added(&[routine_of(ROUTINE_B)]), ROUTINE_B, nine())
    };
    let state = sweep(&state, nine() + 5 * 86_400).expect("swept");
    assert_eq!(
        (
            state.notices.len(),
            record::routine(&state, ROUTINE_B)
                .expect("a Routine")
                .missed
        ),
        (32, 5)
    );
    assert!(claimable(&state, nine() + 5 * 86_400, true).is_none());
    let state = sweep(
        &acknowledge(&state, &[(full[0].notice_id.clone(), 1)]),
        nine() + 5 * 86_400,
    )
    .expect("swept");
    assert_eq!(
        state.notices.last().map(|item| item.detail.clone()),
        Some(missed(5))
    );
}

#[test]
fn a_claim_sweeps_first_so_a_stale_firing_never_starts() {
    let (state, claimed) = claim_at(
        &at_time(&added(&[fixture()]), ROUTINE_A, nine()),
        nine() + 30 * 86_400,
    );
    assert!(claimed.is_some());
    assert_eq!(details(&state), [missed(30)]);
}

#[test]
fn a_claim_with_nothing_due_still_returns_the_swept_state() {
    // A daily 09:00 Routine checked at 22:00 after a long outage: its misses are skipped, and nothing is due.
    let state = at_time(&added(&[fixture()]), ROUTINE_A, nine() - 5 * 86_400);
    let (swept, claimed) = claim_at(&state, epoch(2026, 10, 1, 22, 0, 0));
    assert!(claimed.is_none());
    assert_eq!(details(&swept), [missed(6)]);
    assert_eq!(
        record::routine(&swept, ROUTINE_A)
            .expect("a Routine")
            .next_run_at,
        epoch(2026, 10, 2, 9, 0, 0)
    );
}

#[test]
fn a_run_binds_its_generation_from_the_trusted_network_once() {
    let (state, claimed, lease) = crate::testing::claimed();
    let run_id = claimed.run.run_id.as_str();
    assert_eq!(
        bind_generation(&state, run_id, &lease, nine(), "bad id").map(|_| ()),
        Err(RoutineStateError::new("generation-invalid"))
    );
    let state = bind_generation(&state, run_id, &lease, nine(), "net_1").expect("bound");
    assert_eq!(
        record::run(&state, run_id).expect("a run").generation,
        format!("net_1:routine:{run_id}")
    );
    assert_eq!(
        bind_generation(&state, run_id, &lease, nine(), "net_1"),
        Ok(state.clone())
    );
    assert_eq!(
        bind_generation(&state, run_id, &lease, nine(), "net_2").map(|_| ()),
        Err(RoutineStateError::new("generation-invalid"))
    );
}

#[test]
fn ids_lease_digests_and_generations_have_their_shapes() {
    let id = record::new_id(&ENTROPY).expect("an id");
    assert!(
        id.len() == 32
            && id
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    );
    assert_eq!(record::new_token(&ENTROPY).map(|token| token.len()), Ok(43));
    assert_eq!(record::lease_sha256("token").len(), 64);
    assert_eq!(grace_seconds(&fixture()), 12 * 3600);
    assert_eq!(
        grace_seconds(&routine_scheduled(
            ROUTINE_A,
            json(r#"{"kind": "monthly", "day": 1, "time": "09:00"}"#)
        )),
        12 * 3600
    );
    let run = "a".repeat(32);
    assert_eq!(
        generation_for("net_1", &run, ""),
        format!("net_1:routine:{run}")
    );
    let continued = generation_for("net_1", &run, "s1");
    assert_eq!(
        verification_of(&continued, &run),
        Ok(format!("net_1:routine:{run}:v1"))
    );
    assert!(verification_of(&continued, &"b".repeat(32)).is_err());
}

fn long_routine(routine_id: &str, steps: usize, schedule: Option<Value>) -> Routine {
    let mut fixture = Fixture {
        id: routine_id.to_owned(),
        plan: repeated_plan(steps, r#"{"mode": "none", "step": null}"#),
        ..Fixture::default()
    };
    if let Some(schedule) = schedule {
        fixture.schedule = schedule;
    }
    confirmed(fixture.build())
}

#[test]
fn a_long_routine_waits_while_admin_holds_a_long_run_and_a_short_one_is_still_served() {
    let state = at_time(
        &at_time(
            &added(&[long_routine(ROUTINE_B, 40, None), fixture()]),
            ROUTINE_B,
            nine() - 60,
        ),
        ROUTINE_A,
        nine(),
    );
    assert!(long_run(40));
    assert_eq!(
        claimable(&state, nine(), true).map(|item| item.routine_id.as_str()),
        Some(ROUTINE_B)
    );
    assert_eq!(
        claimable(&state, nine(), false).map(|item| item.routine_id.as_str()),
        Some(ROUTINE_A)
    );
    let (_state, short) = claim(&state, nine(), KEY, false, &ENTROPY).expect("a claim");
    let short = short.expect("a claim");
    assert_eq!(
        (short.run.routine_id.as_str(), short.active_seconds),
        (ROUTINE_A, active_seconds(1))
    );
    let (_state, long) = claim_at(&state, nine());
    let long = long.expect("a claim");
    assert_eq!(
        (long.run.routine_id.as_str(), long.active_seconds),
        (ROUTINE_B, active_seconds(40))
    );
    assert_eq!(long.run.lease_expires_at, nine() + LEASE_SECONDS);
}

#[test]
fn a_segment_extends_its_lease_over_its_active_time_once() {
    let (state, claimed) = claim_at(
        &at_time(
            &added(&[long_routine(ROUTINE_B, 40, None)]),
            ROUTINE_B,
            nine(),
        ),
        nine(),
    );
    let claimed = claimed.expect("a claim");
    let lease = lease_of(&claimed.lease_token, KEY);
    let run_id = claimed.run.run_id.as_str();
    let bound = bind_generation(&state, run_id, &lease, nine() + 10, "net_1").expect("bound");
    let left = record::run(&bound, run_id)
        .expect("a run")
        .active_seconds_left;
    let expected = nine() + 10 + left + LEASE_MARGIN_SECONDS;
    assert_eq!(
        record::run(&bound, run_id).expect("a run").lease_expires_at,
        expected
    );
    // A retried request binding the same run again never renews it.
    let again = bind_generation(&bound, run_id, &lease, nine() + 500, "net_1").expect("bound");
    assert_eq!(
        record::run(&again, run_id).expect("a run").lease_expires_at,
        expected
    );
}

#[test]
fn a_start_reserves_every_step_of_its_revision_under_the_team_daily_steps() {
    let state = at_time(
        &added(&[long_routine(ROUTINE_B, 40, Some(every_hour()))]),
        ROUTINE_B,
        nine(),
    );
    let capped = TeamRoutines {
        starts: (0..80)
            .map(|index| (ROUTINE_C.to_owned(), nine() - 3600 + index, 250))
            .collect(),
        ..state
    };
    assert!(claimable(&capped, nine(), true).is_none());
    // Only once enough starts leave the window does the next one fit.
    let freed = nine() - 3600 + 86_400;
    assert_eq!(next_due(&capped, nine()), Some(freed));
    let (after, _claim) = claim_at(&at_time(&capped, ROUTINE_B, freed), freed);
    assert_eq!(
        after.starts.last(),
        Some(&(ROUTINE_B.to_owned(), freed, 40))
    );
}

fn continuous(gap: i64) -> TeamRoutines {
    let schedule = object(vec![
        ("kind", crate::json::text("continuous")),
        ("gap", int(gap)),
    ]);
    at_time(
        &added(&[routine_scheduled(ROUTINE_A, schedule)]),
        ROUTINE_A,
        nine(),
    )
}

#[test]
fn the_next_run_is_due_its_gap_after_the_previous_one_started_and_never_overlaps() {
    let (state, claimed) = claim_at(&continuous(5), nine());
    let claimed = claimed.expect("a claim");
    // While it runs nothing else of it starts, however long it takes.
    for later in [nine() + 5, nine() + 600] {
        assert!(claimable(&state, later, true).is_none());
    }
    assert_eq!(next_due(&state, nine() + 600), None);
    // A run that outlasted its gap makes the next one due at its end: no overlap, no backlog.
    let lease = lease_of(&claimed.lease_token, KEY);
    let ended = runs::finish(
        &state,
        &claimed.run.run_id,
        &lease,
        nine() + 40,
        None,
        &ENTROPY,
    )
    .expect("finished");
    assert_eq!(
        record::routine(&ended, ROUTINE_A)
            .expect("a Routine")
            .next_run_at,
        nine() + 40
    );
    assert_eq!(
        claimable(&ended, nine() + 40, true).map(|item| item.routine_id.as_str()),
        Some(ROUTINE_A)
    );
    // A shorter run keeps the interval from its own start; any ending counts, a Team-decided one included.
    let (state, claimed) = claim_at(&ended, nine() + 40);
    let claimed = claimed.expect("a claim");
    let stopped = runs::end(
        &state,
        &claimed.run.run_id,
        nine() + 42,
        "stopped",
        object(vec![]),
        None,
        &ENTROPY,
    )
    .expect("ended");
    assert_eq!(
        record::routine(&stopped, ROUTINE_A)
            .expect("a Routine")
            .next_run_at,
        nine() + 45
    );
    assert!(claimable(&stopped, nine() + 44, true).is_none());
    assert_eq!(next_due(&stopped, nine() + 42), Some(nine() + 45));
}

#[test]
fn a_continuous_routine_never_skips_a_backlog_and_waits_out_its_cap_to_the_second() {
    // Every twelve hours: at most two starts in any rolling 24 hours, even when a person forces earlier ones.
    let mut state = continuous(43_200);
    // A long outage reports no missed runs: it simply starts when it may.
    let swept = sweep(&state, nine() + 7 * 86_400).expect("swept");
    assert_eq!(swept.notices, []);
    assert_eq!(
        record::routine(&swept, ROUTINE_A)
            .expect("a Routine")
            .missed,
        0
    );
    let mut starts = Vec::new();
    for _ in 0..2 {
        let now = nine() + 100 * i64::try_from(starts.len()).expect("a count");
        let (claimed_state, claimed) = claim_at(&at_time(&state, ROUTINE_A, now), now);
        starts.push(now);
        state = runs::end(
            &claimed_state,
            &claimed.expect("a claim").run.run_id,
            now + 1,
            "stopped",
            object(vec![]),
            None,
            &ENTROPY,
        )
        .expect("ended");
    }
    // Its third start waits until its first leaves the rolling window, exactly.
    let boundary = starts[0] + 86_400;
    assert!(claimable(&state, boundary - 1, true).is_none());
    assert_eq!(next_due(&state, boundary - 1), Some(boundary));
    assert_eq!(
        claimable(&state, boundary, true).map(|item| item.routine_id.as_str()),
        Some(ROUTINE_A)
    );
}

#[test]
fn rodar_on_a_set_aside_run_starts_the_routine_now_whatever_its_schedule() {
    let (state, claimed, lease) = claim_bound(&continuous(5), nine(), "net_1");
    let run_id = claimed.run.run_id;
    let state = hold::fence(&state, &run_id, &lease, nine()).expect("fenced");
    let state = hold::settle_hold(&state, &run_id, nine() + 1, hold::unknown_step(), &ENTROPY)
        .expect("settled");
    // Held for a day: the incident still holds the Routine, whatever its gap.
    let later = nine() + 86_400;
    assert!(claimable(&state, later, true).is_none());
    let generation = hold::incident(&state, &run_id)
        .expect("an incident")
        .generation
        .clone();
    let expected = Expected {
        revision: 1,
        generation,
        current: 1,
    };
    let ran = hold::run_incident(&state, &run_id, later, &expected, &ENTROPY).expect("ran");
    assert_eq!(
        record::routine(&ran, ROUTINE_A)
            .expect("a Routine")
            .run_requested,
        later
    );
    assert_eq!(
        claimable(&ran, later, true).map(|item| item.routine_id.as_str()),
        Some(ROUTINE_A)
    );
    // A scheduled Routine's next firing is its own: ending a run leaves it unchanged.
    let (scheduled, _run_id) = held();
    let before = record::routine(&scheduled, ROUTINE_A)
        .expect("a Routine")
        .next_run_at;
    assert_eq!(
        record::rebase_continuous(&scheduled, ROUTINE_A, nine() + 10),
        scheduled
    );
    assert_eq!(
        record::routine(&scheduled, ROUTINE_A)
            .expect("a Routine")
            .next_run_at,
        before
    );
}
