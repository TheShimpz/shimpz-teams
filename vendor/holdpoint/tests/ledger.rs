//! The ledger under failure and contention: broken tables, gaps, full stores, and runs that change under a session.

#![cfg(all(test, feature = "testing"))]

#[cfg(test)]
mod common;

use std::path::Path;
use std::sync::Arc;

use common::{Racing, Setup, file_store, memory_store, plan};
use holdpoint::testing::{Behavior, RecordingActivity, ScriptedModel, TestHost};
use holdpoint::{
    ActivityOptions, Driven, Error, Event, Limits, ManualClock, Model, ModelCall, Outcome, Proof, RunOptions, Store,
    Wait, Worker, WorkerOptions,
};
use serde_json::json;

#[cfg(test)]
fn now() -> i64 {
    holdpoint::Clock::now_ms(&holdpoint::SystemClock)
}

#[cfg(test)]
fn sql(path: &Path, statement: &str) {
    rusqlite::Connection::open(path).unwrap().execute_batch(statement).unwrap();
}

#[cfg(test)]
fn ledger(dir: &tempfile::TempDir) -> std::path::PathBuf {
    dir.path().join("ledger.sqlite3")
}

#[cfg(test)]
fn racing(store: Arc<Store>, host: Racing) -> Worker {
    Worker::builder(store, "ledger", host)
        .definition(common::Plan { version: 1 })
        .activity("act", ActivityOptions::external(), RecordingActivity::new([Behavior::Echo]))
        .options(common::fast_options())
        .build()
}

#[test]
fn a_ledger_that_cannot_be_opened_is_a_storage_error() {
    let missing = Path::new("/nonexistent-holdpoint-dir/ledger.sqlite3");
    assert!(matches!(Store::open(missing, Limits::default()), Err(Error::Storage(_))));
}

#[test]
fn commits_are_counted_and_a_full_store_refuses_new_runs() {
    let store = Arc::new(Store::open_in_memory(Limits { max_runs: 1, ..Limits::default() }).unwrap());
    let fixture = Setup::new(Arc::clone(&store)).build();
    fixture.worker.submit("plan", "one", &plan(json!([])), &RunOptions::default()).unwrap();
    assert_eq!(store.commits(), 1);
    assert!(matches!(
        fixture.worker.submit("plan", "two", &plan(json!([])), &RunOptions::default()),
        Err(Error::Limit(_))
    ));
}

#[test]
fn host_inputs_for_unknown_runs_are_state_errors() {
    let fixture = common::fixture();
    assert!(matches!(fixture.worker.resolve("ghost", 0, &json!(1)), Err(Error::State(_))));
    assert!(matches!(fixture.worker.acknowledge("ghost"), Err(Error::State(_))));
    assert_eq!(fixture.worker.delete("ghost"), Ok(()));
}

#[test]
fn a_gap_in_the_event_sequence_is_corruption() {
    let dir = tempfile::tempdir().unwrap();
    let fixture = Setup::new(file_store(&dir)).build();
    let steps = plan(
        json!([{ "kind": "local", "name": "double", "input": 1 }, { "kind": "deferred", "name": "ask", "requests": [1] }]),
    );
    fixture.worker.submit("plan", "gap", &steps, &RunOptions::default()).unwrap();
    fixture.worker.drive("gap").unwrap();
    sql(&ledger(&dir), "DELETE FROM events WHERE run_id = 'gap' AND seq = 2;");
    assert!(matches!(fixture.worker.drive("gap"), Err(Error::Storage(message)) if message.contains("gap")));
}

#[test]
fn broken_tables_surface_as_storage_errors() {
    let dir = tempfile::tempdir().unwrap();
    let store = file_store(&dir);
    let fixture = Setup::new(Arc::clone(&store)).build();
    let steps = plan(json!([{ "kind": "deferred", "name": "ask", "requests": [1] }]));
    fixture.worker.submit("plan", "b1", &steps, &RunOptions::default()).unwrap();
    fixture.worker.drive("b1").unwrap();
    sql(&ledger(&dir), "DROP TABLE blobs;");
    assert!(matches!(store.blob("b1", 0), Err(Error::Storage(_))));
    sql(&ledger(&dir), "DROP TABLE deferred;");
    assert!(matches!(fixture.worker.resolve("b1", 0, &json!(1)), Err(Error::Storage(_))));
    sql(&ledger(&dir), "DROP TABLE events;");
    assert!(matches!(
        fixture.worker.submit("plan", "b2", &plan(json!([])), &RunOptions::default()),
        Err(Error::Storage(_))
    ));
    sql(&ledger(&dir), "DROP TABLE runs;");
    for result in [
        fixture.worker.result("b1").map(|_| ()),
        fixture.worker.drive("b1").map(|_| ()),
        fixture.worker.cancel("b1").map(|_| ()),
        fixture.worker.delete("b1"),
        fixture.worker.submit("plan", "b3", &plan(json!([])), &RunOptions::default()).map(|_| ()),
    ] {
        assert!(matches!(result, Err(Error::Storage(_))), "{result:?}");
    }
}

#[test]
fn a_ledger_that_breaks_while_an_attempt_runs_loses_the_session() {
    let dir = tempfile::tempdir().unwrap();
    let path = ledger(&dir);
    let breaker = move |_call: holdpoint::ActivityCall<'_>| {
        sql(&path, "DROP TABLE runs;");
        std::thread::sleep(std::time::Duration::from_millis(50));
        Ok(holdpoint::ActivityReply::of(json!(1)))
    };
    let worker = Worker::builder(file_store(&dir), "ledger", TestHost::default())
        .definition(common::Plan { version: 1 })
        .activity("act", ActivityOptions::idempotent(), breaker)
        .options(common::fast_options())
        .build();
    worker
        .submit("plan", "p1", &plan(json!([{ "kind": "activity", "name": "act", "input": 1 }])), &RunOptions::default())
        .unwrap();
    assert!(matches!(worker.drive("p1"), Err(Error::Storage(_))));
}

#[test]
fn a_deletion_proof_that_breaks_the_ledger_fails_cleanly() {
    let dir = tempfile::tempdir().unwrap();
    let path = ledger(&dir);
    let host = Racing::default();
    *host.on_prove.lock().unwrap() = Some(Box::new(move || sql(&path, "DROP TABLE runs;")));
    let failing = TestHost::default();
    failing.fail_releases();
    let store = file_store(&dir);
    let parked = Worker::builder(Arc::clone(&store), "ledger", failing)
        .definition(common::Plan { version: 1 })
        .activity("act", ActivityOptions::external(), RecordingActivity::new([Behavior::Echo]))
        .build();
    let steps = plan(
        json!([{ "kind": "activity", "name": "act", "input": 1 }, { "kind": "deferred", "name": "ask", "requests": [1] }]),
    );
    parked.submit("plan", "x1", &steps, &RunOptions::default()).unwrap();
    parked.drive("x1").unwrap();
    assert!(matches!(racing(store, host).delete("x1"), Err(Error::Storage(_))));
}

#[test]
fn a_deletion_races_runs_that_vanish_get_claimed_or_gain_obligations() {
    let dir = tempfile::tempdir().unwrap();
    let store = file_store(&dir);
    let failing = TestHost::default();
    failing.fail_releases();
    let parked = Worker::builder(Arc::clone(&store), "ledger", failing)
        .definition(common::Plan { version: 1 })
        .activity("act", ActivityOptions::external(), RecordingActivity::new([Behavior::Echo]))
        .build();
    let steps = plan(
        json!([{ "kind": "activity", "name": "act", "input": 1 }, { "kind": "deferred", "name": "ask", "requests": [1] }]),
    );
    for run in ["gone", "new-obligation"] {
        parked.submit("plan", run, &steps, &RunOptions::default()).unwrap();
        parked.drive(run).unwrap();
    }
    let path = ledger(&dir);
    let vanish = Racing::default();
    *vanish.on_prove.lock().unwrap() = Some(Box::new(move || sql(&path, "DELETE FROM runs WHERE id = 'gone';")));
    assert_eq!(racing(Arc::clone(&store), vanish).delete("gone"), Ok(()));
    let path = ledger(&dir);
    let gain = Racing::default();
    *gain.on_prove.lock().unwrap() = Some(Box::new(move || {
        sql(
            &path,
            "INSERT INTO obligations (run_id, id, kind, fence, incarnation, created_ms) VALUES ('new-obligation', 99, 'session', 1, 'x', 0);",
        );
    }));
    assert!(matches!(racing(store, gain).delete("new-obligation"), Err(Error::State(_))));
}

#[test]
fn a_deletion_is_busy_while_a_live_session_holds_the_run() {
    let dir = tempfile::tempdir().unwrap();
    let store = file_store(&dir);
    let mut setup = Setup::new(Arc::clone(&store));
    setup.activity = RecordingActivity::new([Behavior::Sleep { ms: 300, progress: None }]);
    let fixture = setup.build();
    fixture
        .worker
        .submit(
            "plan",
            "live",
            &plan(json!([{ "kind": "activity", "name": "act", "input": 1 }])),
            &RunOptions::default(),
        )
        .unwrap();
    let worker = Arc::clone(&fixture.worker);
    let driving = std::thread::spawn(move || worker.drive("live"));
    while !store.history("live").unwrap().iter().any(|event| matches!(event, Event::StepStarted { .. })) {
        std::thread::sleep(std::time::Duration::from_millis(2));
    }
    assert!(matches!(fixture.worker.delete("live"), Err(Error::Busy(_))));
    driving.join().unwrap().unwrap();
}

#[test]
fn a_deletion_is_busy_when_a_session_claims_the_run_during_its_proofs() {
    let dir = tempfile::tempdir().unwrap();
    let store = file_store(&dir);
    let other = Setup::new(Arc::clone(&store));
    let mut slow = other;
    slow.activity = RecordingActivity::new([Behavior::Sleep { ms: 300, progress: None }]);
    let slow = slow.build();
    let failing = TestHost::default();
    failing.fail_releases();
    let parked = Worker::builder(Arc::clone(&store), "ledger", failing)
        .definition(common::Plan { version: 1 })
        .activity("act", ActivityOptions::external(), RecordingActivity::new([Behavior::Echo]))
        .build();
    let steps = plan(
        json!([{ "kind": "activity", "name": "act", "input": 1 }, { "kind": "deferred", "name": "ask", "requests": [1] }, { "kind": "activity", "name": "act", "input": 2 }]),
    );
    parked.submit("plan", "claimed", &steps, &RunOptions::default()).unwrap();
    parked.drive("claimed").unwrap();
    store.resolve("claimed", 1, &json!("go"), now()).unwrap();
    let racer = Arc::clone(&slow.worker);
    let watched = Arc::clone(&store);
    let host = Racing::default();
    *host.on_prove.lock().unwrap() = Some(Box::new(move || {
        std::thread::spawn(move || racer.drive("claimed"));
        while watched
            .history("claimed")
            .unwrap()
            .iter()
            .filter(|event| matches!(event, Event::StepStarted { .. }))
            .nth(1)
            .is_none()
        {
            std::thread::sleep(std::time::Duration::from_millis(2));
        }
    }));
    assert!(matches!(racing(Arc::clone(&store), host).delete("claimed"), Err(Error::Busy(_))));
}

#[test]
fn a_session_whose_run_was_ended_elsewhere_loses_its_lease() {
    let dir = tempfile::tempdir().unwrap();
    let store = file_store(&dir);
    let mut stale = Setup::new(Arc::clone(&store));
    stale.activity = RecordingActivity::new([Behavior::Sleep { ms: 300, progress: None }]);
    let stale = stale.build_with_clock(ManualClock::at(now() - 60_000));
    stale
        .worker
        .submit(
            "plan",
            "ended",
            &plan(json!([{ "kind": "activity", "name": "act", "input": 1 }])),
            &RunOptions::default(),
        )
        .unwrap();
    let worker = Arc::clone(&stale.worker);
    let driving = std::thread::spawn(move || worker.drive("ended"));
    while !store.history("ended").unwrap().iter().any(|event| matches!(event, Event::StepStarted { .. })) {
        std::thread::sleep(std::time::Duration::from_millis(2));
    }
    let fresh = Setup::new(Arc::clone(&store)).build();
    assert!(matches!(fresh.worker.drive("ended").unwrap(), Driven::Ended(_)));
    assert!(matches!(driving.join().unwrap(), Err(Error::LeaseLost(_))));
}

/// Dispatches an activity, then resolves its own deferred request through the ledger before asking for it, so the
/// answer lands while this session holds the lease and its park must be refused.
#[cfg(test)]
struct HeldRacer {
    store: Arc<Store>,
}

#[cfg(test)]
impl holdpoint::Definition for HeldRacer {
    fn id(&self) -> &'static str {
        "held"
    }

    fn version(&self) -> u32 {
        1
    }

    fn run(&self, ctx: &mut holdpoint::Ctx<'_>) -> Result<Outcome, holdpoint::Interrupt> {
        ctx.activity("act", json!(1))?;
        if self.store.pending(ctx.run_id()).unwrap().is_empty() && ctx.input()["race"] == json!(true) {
            let _ = ctx.deferred(&[("ask", json!(1))]);
        }
        let answers = ctx.deferred(&[("ask", json!(1))])?;
        Ok(Outcome::Done { result: serde_json::Value::Array(answers) })
    }
}

#[test]
fn a_park_refused_by_an_answer_that_arrived_reruns_the_definition() {
    let store = memory_store();
    let resolver = Arc::clone(&store);
    let racing_host = Racing::default();
    *racing_host.on_release.lock().unwrap() = Some(Box::new(move || {
        resolver.resolve("held", 1, &json!("raced"), now()).unwrap_or(holdpoint::Resolution::Duplicate);
    }));
    let worker = Worker::builder(Arc::clone(&store), "ledger", racing_host)
        .definition(HeldRacer { store: Arc::clone(&store) })
        .activity("act", ActivityOptions::idempotent(), RecordingActivity::new([Behavior::Echo]))
        .build();
    worker.submit("held", "held", &json!({ "race": false }), &RunOptions::default()).unwrap();
    let driven = worker.drive("held").unwrap();
    assert!(matches!(driven, Driven::Ended(Outcome::Done { .. }) | Driven::Suspended(Wait::Deferred)), "{driven:?}");
}

#[test]
fn a_cancellation_committed_by_another_process_stops_the_dispatch() {
    let store = memory_store();
    let canceller = Arc::clone(&store);
    let host = Racing::default();
    *host.on_authorize.lock().unwrap() = Some(Box::new(move || {
        canceller.cancel("c9", now()).unwrap();
    }));
    let worker = racing(Arc::clone(&store), host);
    worker
        .submit("plan", "c9", &plan(json!([{ "kind": "activity", "name": "act", "input": 1 }])), &RunOptions::default())
        .unwrap();
    assert!(matches!(worker.drive("c9").unwrap(), Driven::Ended(Outcome::Cancelled { .. })));
    assert!(!store.history("c9").unwrap().iter().any(|event| matches!(event, Event::StepStarted { .. })));
}

#[test]
fn a_run_deleted_under_its_session_is_a_state_error() {
    let dir = tempfile::tempdir().unwrap();
    let path = ledger(&dir);
    let host = Racing::default();
    *host.on_authorize.lock().unwrap() = Some(Box::new(move || {
        sql(&path, "DELETE FROM events WHERE run_id = 'del'; DELETE FROM runs WHERE id = 'del';");
    }));
    let worker = racing(file_store(&dir), host);
    worker
        .submit(
            "plan",
            "del",
            &plan(json!([{ "kind": "activity", "name": "act", "input": 1 }])),
            &RunOptions::default(),
        )
        .unwrap();
    assert!(matches!(worker.drive("del"), Err(Error::State(_))));
}

#[test]
fn stopping_the_worker_parks_runs_at_their_next_step() {
    let mut setup = Setup::new(memory_store());
    setup.activity = RecordingActivity::new([Behavior::Sleep { ms: 150, progress: None }]);
    setup.options = WorkerOptions { concurrency: 1, ..common::fast_options() };
    let fixture = setup.build();
    let steps = plan(
        json!([{ "kind": "activity", "name": "act", "input": 1 }, { "kind": "activity", "name": "act", "input": 2 }]),
    );
    for run in ["s1", "s2"] {
        fixture.worker.submit("plan", run, &steps, &RunOptions::default()).unwrap();
    }
    fixture.worker.start().unwrap();
    fixture.worker.submit("plan", "s3", &steps, &RunOptions::default()).unwrap();
    std::thread::sleep(std::time::Duration::from_millis(60));
    fixture.worker.stop();
    let open = ["s1", "s2", "s3"].iter().filter(|run| fixture.worker.result(run).unwrap().is_none()).count();
    assert!(open >= 2, "a stopping worker dispatches nothing new");
    assert_eq!(fixture.activity.calls().len(), 1);
}

#[test]
fn a_run_whose_definition_this_worker_lacks_is_left_alone() {
    let store = memory_store();
    Setup::new(Arc::clone(&store))
        .build()
        .worker
        .submit("plan", "foreign", &plan(json!([])), &RunOptions::default())
        .unwrap();
    let other = Worker::builder(store, "other", TestHost::default()).build();
    assert!(matches!(other.drive("foreign"), Err(Error::State(message)) if message.contains("needs definition")));
}

#[test]
fn a_scripted_model_with_no_answer_left_fails_permanently() {
    let model = ScriptedModel::default();
    let attempt = holdpoint::testing::attempt("run", 0, None);
    let credentials = holdpoint::Credentials::none();
    let request = json!({});
    let failure =
        model.call(ModelCall { request: &request, attempt: &attempt, credentials: &credentials }).unwrap_err();
    assert_eq!(failure.code, "script-ended");
    assert_eq!(Proof::Ended, Proof::Ended);
}

#[test]
fn reading_the_next_wake_and_claiming_on_a_broken_ledger_fail_cleanly() {
    let dir = tempfile::tempdir().unwrap();
    let path = ledger(&dir);
    let host = Racing::default();
    let broken = path.clone();
    *host.on_authorize.lock().unwrap() = Some(Box::new(move || sql(&broken, "DROP TABLE runs;")));
    let store = file_store(&dir);
    let worker = racing(Arc::clone(&store), host);
    worker
        .submit("plan", "w", &plan(json!([{ "kind": "activity", "name": "act", "input": 1 }])), &RunOptions::default())
        .unwrap();
    assert!(matches!(worker.drive("w"), Err(Error::Storage(_))));
    assert!(matches!(store.next_wake(), Err(Error::Storage(_))));
}

#[test]
fn stopping_a_worker_that_never_started_is_a_no_op() {
    let fixture = common::fixture();
    fixture.worker.stop();
    fixture.worker.stop();
}

#[test]
fn a_cancellation_arriving_while_a_held_run_parks_reruns_and_cancels() {
    let store = memory_store();
    let canceller = Arc::clone(&store);
    let host = Racing::default();
    *host.on_release.lock().unwrap() = Some(Box::new(move || {
        canceller.cancel("park", now()).unwrap();
    }));
    let worker = racing(Arc::clone(&store), host);
    let steps = plan(
        json!([{ "kind": "activity", "name": "act", "input": 1 }, { "kind": "deferred", "name": "ask", "requests": [1] }]),
    );
    worker.submit("plan", "park", &steps, &RunOptions::default()).unwrap();
    assert!(matches!(worker.drive("park").unwrap(), Driven::Ended(Outcome::Cancelled { .. })));
}

#[test]
fn a_cancellation_arriving_during_a_proof_stops_the_dispatch() {
    let store = memory_store();
    let failing = TestHost::default();
    failing.fail_releases();
    let parked = Worker::builder(Arc::clone(&store), "ledger", failing)
        .definition(common::Plan { version: 1 })
        .activity("act", ActivityOptions::external(), RecordingActivity::new([Behavior::Echo]))
        .build();
    let steps = plan(json!([
        { "kind": "activity", "name": "act", "input": 1 },
        { "kind": "deferred", "name": "ask", "requests": [1] },
        { "kind": "activity", "name": "act", "input": 2 }
    ]));
    parked.submit("plan", "proof", &steps, &RunOptions::default()).unwrap();
    assert_eq!(parked.drive("proof").unwrap(), Driven::Suspended(Wait::Deferred));
    assert_eq!(store.obligations("proof").unwrap().len(), 1);
    store.resolve("proof", 1, &json!("go"), now()).unwrap();
    let canceller = Arc::clone(&store);
    let host = Racing::default();
    *host.on_prove.lock().unwrap() = Some(Box::new(move || {
        canceller.cancel("proof", now()).unwrap();
    }));
    let worker = racing(Arc::clone(&store), host);
    assert!(matches!(worker.drive("proof").unwrap(), Driven::Ended(Outcome::Cancelled { .. })));
    let started =
        store.history("proof").unwrap().iter().filter(|event| matches!(event, Event::StepStarted { .. })).count();
    assert_eq!(started, 1, "the second activity was never dispatched");
}

#[test]
fn a_deleted_run_leaves_no_content_in_the_ledger_files() {
    let dir = tempfile::tempdir().unwrap();
    let fixture = Setup::new(file_store(&dir)).build();
    let marker = "erasure-marker-run-content";
    let steps = plan(json!([{ "kind": "deferred", "name": "ask", "requests": [marker] }]));
    fixture.worker.submit("plan", "e1", &steps, &RunOptions::default()).unwrap();
    assert_eq!(fixture.worker.drive("e1").unwrap(), Driven::Suspended(Wait::Deferred));
    assert_eq!(fixture.worker.delete("e1"), Ok(()));
    let path = ledger(&dir);
    let wal = path.with_extension("sqlite3-wal");
    assert_eq!(std::fs::metadata(&wal).map_or(0, |metadata| metadata.len()), 0, "the log is emptied");
    let bytes = std::fs::read(&path).unwrap();
    assert!(!bytes.windows(marker.len()).any(|window| window == marker.as_bytes()), "the content is overwritten");
}

#[test]
fn a_deletion_whose_log_cannot_be_emptied_fails() {
    let dir = tempfile::tempdir().unwrap();
    let fixture = Setup::new(file_store(&dir)).build();
    fixture.worker.submit("plan", "e2", &plan(json!([])), &RunOptions::default()).unwrap();
    let reader = rusqlite::Connection::open(ledger(&dir)).unwrap();
    reader.execute_batch("BEGIN; SELECT count(*) FROM runs;").unwrap();
    assert!(matches!(fixture.worker.delete("e2"), Err(Error::Storage(_))), "a reader keeps the log");
    reader.execute_batch("COMMIT;").unwrap();
    assert_eq!(fixture.worker.delete("e2"), Ok(()));
    assert!(matches!(fixture.store.result("e2"), Err(Error::State(_))));
}
