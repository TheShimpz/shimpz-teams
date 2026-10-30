//! `test_routine_runs.py`, the completion parts of `test_routine_output.py`, and `test_routine_rollup.py`: a claimed
//! run spends, freezes, thaws, and ends exactly once with its notice, by its Routine's output disposition.

use strict_json::Value;

use super::{Request, end, finish, freeze, spend};
use crate::claim::claim;
use crate::json::{int, object, text};
use crate::record::{Lease, RoutineStateError, RunStatus, TeamRoutines, Transition, lease_of};
use crate::testing::{ENTROPY, KEY, STEP, at_time, claim_bound, nine};

mod completion;
mod lifecycle;
mod rollup;

fn request(kind: &str, assistant: &str, action: &str, position: i64) -> Request {
    Request {
        kind: kind.to_owned(),
        assistant_id: assistant.to_owned(),
        action: action.to_owned(),
        position,
    }
}

fn human_check() -> Request {
    request("human", "dns", "check", STEP)
}

fn empty() -> Value {
    object(vec![])
}

fn failure() -> Value {
    object(vec![("code", text("x"))])
}

fn refused<T: std::fmt::Debug>(result: Result<T, RoutineStateError>, code: &'static str) {
    assert_eq!(result.map(|_| ()).err(), Some(RoutineStateError::new(code)));
}

/// Team ends the run at nine with this outcome and detail.
fn ended(state: &TeamRoutines, run_id: &str, outcome: &str, detail: Value) -> Transition {
    end(state, run_id, nine(), outcome, detail, None, &ENTROPY)
}

/// The run frozen at nine for a person's answer to its first step.
fn frozen(state: &TeamRoutines, run_id: &str, lease: &Lease) -> Transition {
    freeze(state, run_id, lease, nine(), &human_check(), &ENTROPY)
}

/// The run's worker finishes it at `now` with no shown slot.
fn finished(state: &TeamRoutines, run_id: &str, lease: &Lease, now: i64) -> Transition {
    finish(state, run_id, lease, now, None, &ENTROPY)
}

/// The Routine claimed at `now` and its run, which Team stops at once.
fn claimed_and_stopped(state: &TeamRoutines, routine_id: &str, now: i64) -> TeamRoutines {
    let (claimed_state, claimed) =
        claim(&at_time(state, routine_id, now), now, KEY, true, &ENTROPY).expect("a claim");
    let run_id = claimed.expect("a claim").run.run_id;
    end(
        &claimed_state,
        &run_id,
        now,
        "stopped",
        empty(),
        None,
        &ENTROPY,
    )
    .expect("ended")
}

/// The Routine claimed and bound at `start`, charged one second, and ended at `end_at` by its worker or by Team.
fn run_once(
    state: &TeamRoutines,
    routine_id: &str,
    start: i64,
    end_at: i64,
    outcome: &str,
) -> TeamRoutines {
    let (state, claimed, lease) = claim_bound(
        &at_time(state, routine_id, start),
        start,
        crate::testing::NETWORK,
    );
    let run_id = claimed.run.run_id;
    let state = spend(&state, &run_id, &lease, start, (1, 1200)).expect("spent");
    if outcome == "done" {
        return finished(&state, &run_id, &lease, end_at).expect("finished");
    }
    let detail = object(vec![("code", text("assistant-rpc-failed"))]);
    end(&state, &run_id, end_at, outcome, detail, None, &ENTROPY).expect("ended")
}

fn run_status(state: &TeamRoutines, run_id: &str) -> RunStatus {
    crate::record::run(state, run_id).expect("a run").status
}

fn worker(token: &str) -> Lease {
    lease_of(token, KEY)
}

fn duration(milliseconds: i64) -> Value {
    object(vec![("duration_ms", int(milliseconds))])
}
