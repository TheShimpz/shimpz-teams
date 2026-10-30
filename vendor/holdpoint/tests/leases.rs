//! Leases, fencing, takeover proofs, and host inputs racing a park (R5, R13).

#![cfg(all(test, feature = "testing"))]

#[cfg(test)]
mod common;

use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::{Fixture, Setup, file_store, plan};
use holdpoint::testing::{Behavior, RecordingActivity, TestHost};
use holdpoint::{
    ActivityOptions, Ctx, Definition, Driven, Error, Event, Interrupt, ManualClock, Outcome, Proof, RunOptions, Store,
    SystemClock, Wait, Worker,
};
use serde_json::{Value, json};

#[cfg(test)]
fn now() -> i64 {
    holdpoint::Clock::now_ms(&SystemClock)
}

/// A worker whose clock is frozen a minute in the past: its leases look expired to everyone else at once and it
/// never renews them, like a holder that is alive but partitioned.
#[cfg(test)]
fn partitioned(store: Arc<Store>, activity: RecordingActivity) -> Fixture {
    let mut setup = Setup::new(store);
    setup.activity = activity;
    setup.build_with_clock(ManualClock::at(now() - 60_000))
}

#[cfg(test)]
fn two_activities() -> Value {
    plan(json!([{ "kind": "activity", "name": "act", "input": 1 }, { "kind": "activity", "name": "act", "input": 2 }]))
}

#[cfg(test)]
fn wait_for_dispatch(store: &Store, run: &str) {
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while !store.history(run).unwrap().iter().any(|event| matches!(event, Event::StepStarted { .. })) {
        assert!(std::time::Instant::now() < deadline, "no dispatch");
        std::thread::sleep(Duration::from_millis(2));
    }
}

#[cfg(test)]
struct Takeover {
    _dir: tempfile::TempDir,
    stale: std::thread::JoinHandle<Result<Driven, Error>>,
    store: Arc<Store>,
    fresh: Fixture,
    first: Driven,
}

/// A partitioned holder dispatches step 0; a fresh worker on another connection takes the run over.
#[cfg(test)]
fn takeover() -> Takeover {
    let dir = tempfile::tempdir().unwrap();
    let path_store = file_store(&dir);
    let stale =
        partitioned(Arc::clone(&path_store), RecordingActivity::new([Behavior::Sleep { ms: 300, progress: None }]));
    stale.worker.submit("plan", "f1", &two_activities(), &RunOptions::default()).unwrap();
    let stale_worker = Arc::clone(&stale.worker);
    let stale_drive = std::thread::spawn(move || stale_worker.drive("f1"));
    wait_for_dispatch(&path_store, "f1");
    let store = Arc::new(Store::open(&dir.path().join("ledger.sqlite3"), holdpoint::Limits::default()).unwrap());
    let setup = Setup::new(Arc::clone(&store));
    setup.host.set_proof(Proof::NotYet);
    let fresh = setup.build();
    let first = fresh.worker.drive("f1").unwrap();
    Takeover { _dir: dir, stale: stale_drive, store, fresh, first }
}

#[test]
fn a_takeover_dispatches_nothing_before_the_old_session_is_proven_ended() {
    let takeover = takeover();
    assert!(matches!(takeover.first, Driven::Suspended(Wait::Executor(_))));
    assert_eq!(takeover.fresh.activity.calls().len(), 0);
    let obligations = takeover.store.obligations("f1").unwrap();
    assert_eq!(obligations.len(), 1);
    assert_eq!((obligations[0].fence, obligations[0].attempt), (1, None));
}

#[test]
fn a_partitioned_holder_is_fenced_out() {
    let takeover = takeover();
    assert!(matches!(takeover.stale.join().unwrap(), Err(Error::LeaseLost(_))));
    takeover.fresh.host.set_proof(Proof::Ended);
    let driven = takeover.fresh.worker.drive("f1").unwrap();
    assert_eq!(driven, Driven::Ended(Outcome::Done { result: json!(["uncertain", 2]) }));
    let calls = takeover.fresh.activity.calls();
    assert_eq!(calls.len(), 1);
    assert!(calls[0].fence > 1, "the new session dispatches under a newer fence");
    let history = takeover.store.history("f1").unwrap();
    assert!(!history.iter().any(|event| matches!(event, Event::StepCompleted { step: 0, .. })));
}

#[test]
fn an_unproven_obligation_survives_later_clean_sessions() {
    let dir = tempfile::tempdir().unwrap();
    let store = file_store(&dir);
    let dead = partitioned(Arc::clone(&store), RecordingActivity::new([Behavior::Sleep { ms: 150, progress: None }]));
    dead.worker.submit("plan", "f2", &two_activities(), &RunOptions::default()).unwrap();
    let dead_worker = Arc::clone(&dead.worker);
    let dead_drive = std::thread::spawn(move || dead_worker.drive("f2"));
    wait_for_dispatch(&store, "f2");

    let second = Setup::new(Arc::clone(&store));
    second.host.set_proof(Proof::NotYet);
    let second = second.build();
    assert!(matches!(second.worker.drive("f2").unwrap(), Driven::Suspended(Wait::Executor(_))));
    let _ = dead_drive.join();

    let third = Setup::new(Arc::clone(&store));
    third.host.set_proof(Proof::NotYet);
    let third = third.build();
    assert!(matches!(third.worker.drive("f2").unwrap(), Driven::Suspended(Wait::Executor(_))));
    assert_eq!(third.host.proofs().first().map(|obligation| obligation.fence), Some(1));
    third.host.set_proof(Proof::Ended);
    assert!(matches!(third.worker.drive("f2").unwrap(), Driven::Ended(Outcome::Done { .. })));
    assert_eq!(store.obligations("f2").unwrap().len(), 0);
}

#[test]
fn a_failed_clean_release_leaves_an_obligation_for_the_next_session() {
    let mut setup = Setup::new(common::memory_store());
    setup.activity_options = ActivityOptions::external();
    let fixture = setup.build();
    fixture.host.fail_releases();
    let steps = plan(json!([
        { "kind": "activity", "name": "act", "input": 1 },
        { "kind": "deferred", "name": "ask", "requests": [1] },
        { "kind": "activity", "name": "act", "input": 2 }
    ]));
    fixture.worker.submit("plan", "f3", &steps, &RunOptions::default()).unwrap();
    assert_eq!(fixture.worker.drive("f3").unwrap(), Driven::Suspended(Wait::Deferred));
    assert_eq!(fixture.store.obligations("f3").unwrap().len(), 1);
    fixture.worker.resolve("f3", 1, &json!("go")).unwrap();
    fixture.host.set_proof(Proof::NotYet);
    assert!(matches!(fixture.worker.drive("f3").unwrap(), Driven::Suspended(Wait::Executor(_))));
    fixture.host.set_proof(Proof::Ended);
    assert!(matches!(fixture.worker.drive("f3").unwrap(), Driven::Ended(Outcome::Done { .. })));
}

#[test]
fn a_clean_park_releases_through_the_host() {
    let fixture = common::fixture();
    let steps = plan(
        json!([{ "kind": "activity", "name": "act", "input": 1 }, { "kind": "deferred", "name": "ask", "requests": [1] }]),
    );
    fixture.worker.submit("plan", "f4", &steps, &RunOptions::default()).unwrap();
    assert_eq!(fixture.worker.drive("f4").unwrap(), Driven::Suspended(Wait::Deferred));
    assert_eq!(fixture.host.released(), [1]);
    assert_eq!(fixture.store.obligations("f4").unwrap().len(), 0);
}

/// Resolves its own deferred request through another connection right before asking for it, the way a host's
/// answer lands between a session's read of the history and its park.
#[cfg(test)]
struct Racer {
    race: Arc<Mutex<Option<Arc<Store>>>>,
}

#[cfg(test)]
impl Definition for Racer {
    fn id(&self) -> &'static str {
        "racer"
    }

    fn version(&self) -> u32 {
        1
    }

    fn run(&self, ctx: &mut Ctx<'_>) -> Result<Outcome, Interrupt> {
        if let Some(store) = self.race.lock().unwrap().take() {
            store.resolve(ctx.run_id(), 0, &json!("raced"), now()).unwrap();
        }
        let answers = ctx.deferred(&[("ask", json!(1))])?;
        Ok(Outcome::Done { result: Value::Array(answers) })
    }
}

#[test]
fn a_resolution_landing_before_the_park_is_never_lost() {
    let dir = tempfile::tempdir().unwrap();
    let store = file_store(&dir);
    let race = Arc::new(Mutex::new(None));
    let worker = Worker::builder(Arc::clone(&store), "t", TestHost::default())
        .definition(Racer { race: Arc::clone(&race) })
        .build();
    worker.submit("racer", "f5", &Value::Null, &RunOptions::default()).unwrap();
    assert_eq!(worker.drive("f5").unwrap(), Driven::Suspended(Wait::Deferred));
    *race.lock().unwrap() =
        Some(Arc::new(Store::open(&dir.path().join("ledger.sqlite3"), holdpoint::Limits::default()).unwrap()));
    assert_eq!(worker.drive("f5").unwrap(), Driven::Ended(Outcome::Done { result: json!(["raced"]) }));
}

#[test]
fn a_live_lease_makes_another_worker_busy() {
    let dir = tempfile::tempdir().unwrap();
    let store = file_store(&dir);
    let mut setup = Setup::new(Arc::clone(&store));
    setup.activity = RecordingActivity::new([Behavior::Sleep { ms: 300, progress: None }]);
    let first = setup.build();
    first.worker.submit("plan", "f6", &two_activities(), &RunOptions::default()).unwrap();
    let worker = Arc::clone(&first.worker);
    let driving = std::thread::spawn(move || worker.drive("f6"));
    wait_for_dispatch(&store, "f6");
    let other = Arc::new(Store::open(&dir.path().join("ledger.sqlite3"), holdpoint::Limits::default()).unwrap());
    let second = Setup::new(other).build();
    assert!(matches!(second.worker.drive("f6"), Err(Error::Busy(_))));
    assert!(matches!(driving.join().unwrap(), Ok(Driven::Ended(_))));
}

#[test]
fn deletion_waits_for_proofs_and_live_leases() {
    let fixture = common::fixture();
    fixture.host.fail_releases();
    let steps = plan(
        json!([{ "kind": "activity", "name": "act", "input": 1 }, { "kind": "deferred", "name": "ask", "requests": [1] }]),
    );
    fixture.worker.submit("plan", "f7", &steps, &RunOptions::default()).unwrap();
    fixture.worker.drive("f7").unwrap();
    fixture.host.set_proof(Proof::NotYet);
    assert!(matches!(fixture.worker.delete("f7"), Err(Error::State(_))));
    fixture.host.set_proof(Proof::Ended);
    assert_eq!(fixture.worker.delete("f7"), Ok(()));
    assert!(matches!(fixture.worker.result("f7"), Err(Error::State(_))));
    assert_eq!(fixture.worker.delete("f7"), Ok(()));
}

#[test]
fn acknowledging_keeps_only_a_tombstone() {
    let fixture = common::fixture();
    let input = plan(json!([{ "kind": "local", "name": "double", "input": 3 }]));
    fixture.worker.submit("plan", "f8", &input, &RunOptions::default()).unwrap();
    assert!(matches!(fixture.worker.acknowledge("f8"), Err(Error::State(_))));
    fixture.worker.drive("f8").unwrap();
    fixture.worker.acknowledge("f8").unwrap();
    fixture.worker.acknowledge("f8").unwrap();
    assert!(matches!(fixture.worker.result("f8"), Err(Error::State(_))));
    assert_eq!(fixture.worker.history("f8").unwrap(), []);
    assert_eq!(fixture.worker.submit("plan", "f8", &input, &RunOptions::default()), Ok(holdpoint::Submitted::Existing));
    assert!(matches!(fixture.worker.drive("f8"), Err(Error::State(_))));
}
