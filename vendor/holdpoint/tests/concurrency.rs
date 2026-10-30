//! Many threads and processes driving the same run at once: every step executes exactly once.

#![cfg(all(test, feature = "testing"))]

#[cfg(test)]
mod common;

use std::fs::OpenOptions;
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::Arc;

use common::{Plan, fast_options, model_options};
use holdpoint::testing::TestHost;
use holdpoint::{
    Activity, ActivityCall, ActivityOptions, ActivityReply, Driven, Error, Failure, Limits, Outcome, RunOptions, Store,
    Wait, Worker,
};
use serde_json::json;

const RUN: &str = "shared";
const PROCESSES: usize = 6;
const THREADS: usize = 6;

#[cfg(test)]
struct Counted {
    log: PathBuf,
}

#[cfg(test)]
impl Activity for Counted {
    fn run(&self, call: ActivityCall<'_>) -> Result<ActivityReply, Failure> {
        let mut file = OpenOptions::new().create(true).append(true).open(&self.log).unwrap();
        writeln!(file, "{}", call.attempt.effect_key()).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(5));
        Ok(ActivityReply::of(call.input.clone()))
    }
}

#[cfg(test)]
fn worker(dir: &Path) -> Worker {
    let store = Arc::new(Store::open(&dir.join("ledger.sqlite3"), Limits::default()).unwrap());
    let model = holdpoint::testing::ScriptedModel::new((0..4).map(|_| json!("answer")), 0);
    Worker::builder(store, "concurrent", TestHost::default())
        .definition(Plan { version: 1 })
        .model("model", model_options(), model)
        .activity("act", ActivityOptions::external(), Counted { log: dir.join("effects.log") })
        .options(holdpoint::WorkerOptions { lease_ms: 500, ..fast_options() })
        .build()
}

#[cfg(test)]
fn steps() -> serde_json::Value {
    common::plan(json!([
        { "kind": "activity", "name": "act", "input": 1 },
        { "kind": "activity", "name": "act", "input": 2 },
        { "kind": "deferred", "name": "ask", "requests": [1] },
        { "kind": "activity", "name": "act", "input": 3 }
    ]))
}

/// Keeps driving until the run ended, treating another session's lease as a reason to try again.
#[cfg(test)]
fn race(worker: &Worker) -> Outcome {
    loop {
        match worker.drive(RUN) {
            Ok(Driven::Ended(outcome)) => return outcome,
            Ok(Driven::Suspended(Wait::Deferred)) => {
                for request in worker.pending(RUN).unwrap() {
                    let _ = worker.resolve(RUN, request.step, &json!("yes"));
                }
            }
            Ok(Driven::Suspended(_)) | Err(Error::Busy(_) | Error::LeaseLost(_)) => {
                std::thread::sleep(std::time::Duration::from_millis(2));
            }
            Err(error) => panic!("{error}"),
        }
    }
}

#[test]
fn race_child() {
    let Ok(dir) = std::env::var("HOLDPOINT_RACE_DIR") else {
        return;
    };
    let outcome = race(&worker(Path::new(&dir)));
    assert!(matches!(outcome, Outcome::Done { .. }));
}

#[cfg(test)]
fn spawn_child(dir: &Path) -> Child {
    Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "race_child", "--nocapture", "--test-threads=1"])
        .env("HOLDPOINT_RACE_DIR", dir)
        .spawn()
        .unwrap()
}

#[test]
fn threads_and_processes_on_one_run_execute_each_step_once() {
    for _ in 0..5 {
        let dir = tempfile::tempdir().unwrap();
        worker(dir.path()).submit("plan", RUN, &steps(), &RunOptions::default()).unwrap();
        let children: Vec<Child> = (0..PROCESSES).map(|_| spawn_child(dir.path())).collect();
        let threads: Vec<_> = (0..THREADS)
            .map(|_| {
                let path = dir.path().to_owned();
                std::thread::spawn(move || race(&worker(&path)))
            })
            .collect();
        for thread in threads {
            assert_eq!(thread.join().unwrap(), Outcome::Done { result: json!([1, 2, ["yes"], 3]) });
        }
        for mut child in children {
            assert!(child.wait().unwrap().success());
        }
        let log = std::fs::read_to_string(dir.path().join("effects.log")).unwrap();
        let mut effects: Vec<&str> = log.lines().collect();
        effects.sort_unstable();
        assert_eq!(effects, ["shared/0", "shared/1", "shared/3"]);
    }
}
