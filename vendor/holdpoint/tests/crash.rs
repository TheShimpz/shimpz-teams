//! The crash table: every row aborts a child process at a failpoint, fifty times in a row; the parent then resumes
//! the run on the same ledger and checks that it ends correctly, that completed steps never ran again, and that an
//! interrupted non-idempotent effect was never repeated.

#![cfg(all(test, feature = "testing", feature = "failpoints"))]

#[cfg(test)]
mod common;

use std::fs::OpenOptions;
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Arc;

use common::{Plan, fast_options, model_options};
use holdpoint::testing::TestHost;
use holdpoint::{
    Activity, ActivityCall, ActivityOptions, ActivityReply, Driven, Failure, Limits, Model, ModelCall, ModelReply,
    Outcome, RetryPolicy, RunOptions, Store, Wait, Worker,
};
use serde_json::{Value, json};

const RUNS: usize = 50;
const RUN: &str = "crash";

/// Appends one line per execution to a log shared by the parent and its children.
#[cfg(test)]
struct Logged {
    log: PathBuf,
    name: &'static str,
}

#[cfg(test)]
impl Logged {
    fn append(&self, line: &str) {
        let mut file = OpenOptions::new().create(true).append(true).open(&self.log).unwrap();
        writeln!(file, "{} {line}", self.name).unwrap();
    }
}

#[cfg(test)]
impl Activity for Logged {
    fn run(&self, call: ActivityCall<'_>) -> Result<ActivityReply, Failure> {
        self.append(&call.attempt.effect_key());
        Ok(ActivityReply::of(call.input.clone()))
    }
}

#[cfg(test)]
impl Model for Logged {
    fn call(&self, call: ModelCall<'_>) -> Result<ModelReply, Failure> {
        self.append(&call.attempt.effect_key());
        Ok(ModelReply { response: json!("answer"), cost: 1 })
    }
}

#[cfg(test)]
fn worker(dir: &Path) -> Worker {
    let store = Arc::new(Store::open(&dir.join("ledger.sqlite3"), Limits::default()).unwrap());
    let log = dir.join("effects.log");
    let retry = RetryPolicy { initial_ms: 1, max_interval_ms: 2, max_attempts: Some(5), ..RetryPolicy::default() };
    Worker::builder(store, "crash", TestHost::default())
        .definition(Plan { version: 1 })
        .model("model", model_options(), Logged { log: log.clone(), name: "model" })
        .activity("act", ActivityOptions::external(), Logged { log: log.clone(), name: "act" })
        .activity(
            "idempotent",
            ActivityOptions { retry, ..ActivityOptions::idempotent() },
            Logged { log, name: "idempotent" },
        )
        .options(holdpoint::WorkerOptions { lease_ms: 100, inline_wait_ms: 30, ..fast_options() })
        .build()
}

#[cfg(test)]
fn input(now_ms: i64) -> Value {
    common::plan(json!([
        { "kind": "activity", "name": "act", "input": 1 },
        { "kind": "model", "name": "model" },
        { "kind": "deferred", "name": "ask", "requests": [{ "q": "go?" }] },
        { "kind": "sleep", "name": "pause", "due_ms": now_ms + 20 },
        { "kind": "activity", "name": "idempotent", "input": 2 }
    ]))
}

/// Drives the run to its end, answering every deferred request and waiting out every timer.
#[cfg(test)]
fn finish(worker: &Worker) -> Outcome {
    for _ in 0..1_000 {
        let driven = match worker.drive(RUN) {
            Err(holdpoint::Error::Busy(_)) => {
                // The crashed process's lease has not expired yet.
                std::thread::sleep(std::time::Duration::from_millis(10));
                continue;
            }
            driven => driven.unwrap(),
        };
        match driven {
            Driven::Ended(outcome) => return outcome,
            Driven::Suspended(Wait::Deferred) => {
                for request in worker.pending(RUN).unwrap() {
                    worker.resolve(RUN, request.step, &json!("ok")).unwrap();
                }
            }
            Driven::Suspended(_) => std::thread::sleep(std::time::Duration::from_millis(5)),
        }
    }
    panic!("the run never ended");
}

/// The child: submits (unless told the run exists) and drives the run until the failpoint aborts the process.
#[test]
fn crash_child() {
    let Ok(dir) = std::env::var("HOLDPOINT_CRASH_DIR") else {
        return;
    };
    let worker = worker(Path::new(&dir));
    let now = holdpoint::Clock::now_ms(&holdpoint::SystemClock);
    worker.submit("plan", RUN, &input(now), &RunOptions::default()).unwrap();
    finish(&worker);
}

#[cfg(test)]
fn crash_once(failpoint: &str) -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    let status = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "crash_child", "--nocapture", "--test-threads=1"])
        .env("HOLDPOINT_CRASH_DIR", dir.path())
        .env("HOLDPOINT_FAILPOINT", failpoint)
        .status()
        .unwrap();
    assert!(!status.success(), "the child never reached {failpoint}");
    dir
}

#[cfg(test)]
fn executions(dir: &Path, name: &str) -> usize {
    let log = std::fs::read_to_string(dir.join("effects.log")).unwrap_or_default();
    log.lines().filter(|line| line.starts_with(&format!("{name} "))).count()
}

/// One crash-table row: the run must end done, with `act` either completed once or uncertain and never repeated.
#[cfg(test)]
fn row(failpoint: &str, act: &Value) {
    for _ in 0..RUNS {
        let dir = crash_once(failpoint);
        let outcome = finish(&worker(dir.path()));
        let expected = json!([act, "answer", ["ok"], "slept", 2]);
        assert_eq!(outcome, Outcome::Done { result: expected }, "{failpoint}");
        let acted = executions(dir.path(), "act");
        assert!(acted <= 1, "{failpoint}: the non-idempotent effect ran {acted} times");
        if act == &json!(1) {
            assert_eq!(acted, 1, "{failpoint}");
        }
        assert!(executions(dir.path(), "model") <= 2, "{failpoint}");
        assert!(executions(dir.path(), "idempotent") >= 1, "{failpoint}");
    }
}

#[test]
fn crash_after_submit() {
    row("submit.committed", &json!(1));
}

#[test]
fn crash_inside_the_first_commit() {
    row("store.commit@2", &json!(1));
}

#[test]
fn crash_after_the_dispatch_commits() {
    row("dispatch.committed", &json!("uncertain"));
}

#[test]
fn crash_while_the_attempt_runs() {
    row("attempt.running", &json!("uncertain"));
}

#[test]
fn crash_after_the_effect_before_its_result_commits() {
    row("activity.returned", &json!("uncertain"));
}

#[test]
fn crash_after_the_result_commits() {
    row("activity.committed", &json!(1));
}

#[test]
fn crash_after_the_model_answers() {
    row("model.returned", &json!(1));
}

#[test]
fn crash_before_the_deferred_request_commits() {
    row("park.before", &json!(1));
}

#[test]
fn crash_after_the_deferred_request_commits() {
    row("park.committed", &json!(1));
}

#[test]
fn crash_after_the_resolution_commits() {
    row("resolve.committed", &json!(1));
}

#[test]
fn crash_while_a_timer_waits() {
    row("timer.waiting", &json!(1));
}

#[test]
fn crash_before_the_end_commits() {
    row("finish.before", &json!(1));
}

#[test]
fn crash_after_the_end_commits() {
    row("finish.committed", &json!(1));
}

#[test]
fn crash_during_the_idempotent_retry_path() {
    row("dispatch.committed@3", &json!(1));
}
