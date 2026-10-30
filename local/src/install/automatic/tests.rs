//! Automatic update selection, fencing, and backoff (`tests/test_local_automatic_updates.py`), over a faked lifecycle.

use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use strict_json::Value;
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_core::testing::{CountingEntropy, MemoryAudit};
use team_install::bindings::{Binding, Provenance};
use team_install::trust::ResolveError;

use super::{AutomaticUpdates, Reconciled, next_delay};
use crate::http::activity::Activity;
use crate::http::service::Problem;

const CHAT_ACTIVE: Problem = Problem::new(409, "Team chat is active", "chat-active");

fn sha(digit: char) -> String {
    format!("sha256:{}", digit.to_string().repeat(64))
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn document(assistant: &str, version: &str, source: Option<&str>) -> Value {
    let mut members = vec![
        ("assistant_id".to_owned(), text(assistant)),
        ("assistant_version".to_owned(), text(version)),
    ];
    if let Some(source) = source {
        members.push(("source_digest".to_owned(), text(source)));
    }
    Value::Object(members)
}

/// A published `hello-world` 0.1.0 binding of source `a` in `team`.
fn published(team: &str, digest: char) -> Binding {
    Binding {
        team_id: team.to_owned(),
        binding_digest: sha(digest),
        provenance: Provenance::Published,
        document: document("hello-world", "0.1.0", Some(&sha('a'))),
    }
}

fn candidate(version: &str) -> Value {
    document("hello-world", version, Some(&sha('9')))
}

#[derive(Default)]
struct Fake {
    bindings: Mutex<Option<Vec<Binding>>>,
    latest: Mutex<HashMap<String, Vec<Result<Value, ResolveError>>>>,
    asked: Mutex<Vec<String>>,
    refusing: Mutex<HashMap<String, Problem>>,
    installed: Mutex<Vec<(String, String, String, String)>>,
    sweeps: AtomicUsize,
}

impl Fake {
    fn with(bindings: Vec<Binding>) -> Arc<Self> {
        let fake = Self::default();
        *fake.bindings.lock().expect("usable") = Some(bindings);
        Arc::new(fake)
    }

    /// `source` answers each of `answers` in turn, then the last one again.
    fn answer(&self, source: &str, answers: Vec<Result<Value, ResolveError>>) {
        self.latest
            .lock()
            .expect("usable")
            .insert(source.to_owned(), answers);
    }

    fn installed(&self) -> Vec<(String, String, String, String)> {
        self.installed.lock().expect("usable").clone()
    }
}

impl Reconciled for Fake {
    fn sweep(&self) -> BoxFuture<'_, ()> {
        self.sweeps.fetch_add(1, Ordering::SeqCst);
        Box::pin(async {})
    }

    fn bindings(&self) -> BoxFuture<'_, Result<Vec<Binding>, Problem>> {
        let bindings = self.bindings.lock().expect("usable").clone();
        Box::pin(async move { bindings.ok_or(crate::docker::problem::INTERNAL) })
    }

    fn latest<'a>(&'a self, source: &'a str) -> BoxFuture<'a, Result<Value, ResolveError>> {
        self.asked.lock().expect("usable").push(source.to_owned());
        let mut latest = self.latest.lock().expect("usable");
        let answer = match latest.get_mut(source) {
            Some(answers) if answers.len() > 1 => answers.remove(0),
            Some(answers) => answers[0].clone(),
            None => Err(ResolveError::NotInstallable),
        };
        Box::pin(async move { answer })
    }

    fn install<'a>(
        &'a self,
        team: &'a str,
        assistant: &'a str,
        source: &'a str,
        expected: &'a str,
    ) -> BoxFuture<'a, Result<(), Problem>> {
        let refusal = self.refusing.lock().expect("usable").get(team).cloned();
        if refusal.is_none() {
            self.installed.lock().expect("usable").push((
                team.to_owned(),
                assistant.to_owned(),
                source.to_owned(),
                expected.to_owned(),
            ));
        }
        Box::pin(async move { refusal.map_or(Ok(()), Err) })
    }
}

struct Subject {
    fake: Arc<Fake>,
    audit: Arc<MemoryAudit>,
    updates: AutomaticUpdates,
}

fn subject(fake: Arc<Fake>, interval: Duration) -> Subject {
    let audit = Arc::new(MemoryAudit::default());
    let updates = AutomaticUpdates::with_interval(
        fake.clone(),
        audit.clone(),
        Arc::new(Activity::default()),
        Arc::new(CountingEntropy::default()),
        interval,
    );
    Subject {
        fake,
        audit,
        updates,
    }
}

impl Subject {
    /// Each audited result as `team/assistant result detail`.
    fn audits(&self) -> Vec<String> {
        self.audit
            .events()
            .iter()
            .map(|event| {
                let field = |name: &str| {
                    event
                        .fields()
                        .iter()
                        .find(|(key, _)| key == name)
                        .and_then(|(_, value)| match value {
                            Value::String(text) => Some(text.clone()),
                            _ => None,
                        })
                        .unwrap_or_default()
                };
                format!(
                    "{}/{} {} {}",
                    field("team_id"),
                    field("assistant"),
                    field("result"),
                    field("detail")
                )
            })
            .collect()
    }
}

#[tokio::test(start_paused = true)]
async fn one_candidate_per_source_updates_every_older_binding_with_its_fence() {
    let fake = Fake::with(vec![published("team_1", '1'), published("team_2", '2')]);
    fake.answer(&sha('a'), vec![Ok(candidate("0.2.0"))]);
    let subject = subject(fake, super::INTERVAL);
    assert!(subject.updates.run_once().await);
    let fenced = |team: &str, digest: char| {
        (
            team.to_owned(),
            "hello-world".to_owned(),
            sha('9'),
            sha(digest),
        )
    };
    assert_eq!(
        subject.fake.installed(),
        [fenced("team_1", '1'), fenced("team_2", '2')]
    );
    assert_eq!(*subject.fake.asked.lock().expect("usable"), [sha('a')]);
    assert_eq!(subject.fake.sweeps.load(Ordering::SeqCst), 1);
}

#[tokio::test(start_paused = true)]
async fn a_busy_team_defers_without_stopping_another_teams_update() {
    let fake = Fake::with(vec![published("team_1", '1'), published("team_2", '2')]);
    fake.answer(&sha('a'), vec![Ok(candidate("0.2.0"))]);
    fake.refusing
        .lock()
        .expect("usable")
        .insert("team_1".to_owned(), CHAT_ACTIVE);
    let subject = subject(fake, super::INTERVAL);
    assert!(subject.updates.run_once().await);
    assert_eq!(subject.fake.installed().len(), 1);
    assert_eq!(
        subject.audits(),
        [
            "team_1/hello-world error deferred:chat-active",
            "team_2/hello-world ok updated:0.1.0:0.2.0",
        ]
    );
}

#[tokio::test(start_paused = true)]
async fn candidate_absence_protocol_failure_and_unavailability_are_distinct() {
    let fake = Fake::with(vec![published("team_1", '1')]);
    fake.answer(
        &sha('a'),
        vec![
            Err(ResolveError::NotInstallable),
            Err(ResolveError::Protocol),
            Err(ResolveError::Unavailable),
        ],
    );
    let subject = subject(fake, Duration::from_secs(1));
    for _ in 0..4 {
        assert!(subject.updates.run_once().await);
        tokio::time::advance(Duration::from_secs(1)).await;
    }
    // The second failure backs off two intervals, so the third pass skips the binding.
    assert_eq!(
        subject.audits(),
        [
            "team_1/hello-world ok deferred:no-candidate",
            "team_1/hello-world error deferred:developers-protocol-invalid",
            "team_1/hello-world error deferred:developers-unavailable",
        ]
    );
}

#[tokio::test(start_paused = true)]
async fn a_failing_binding_backs_off_on_its_own() {
    let fake = Fake::with(vec![published("team_1", '1')]);
    fake.answer(&sha('a'), vec![Ok(candidate("0.2.0"))]);
    fake.refusing
        .lock()
        .expect("usable")
        .insert("team_1".to_owned(), CHAT_ACTIVE);
    let subject = subject(fake, Duration::from_secs(300));
    subject.updates.run_once().await;
    subject.updates.run_once().await;
    assert_eq!(subject.audits().len(), 1);
    tokio::time::advance(Duration::from_secs(300)).await;
    subject.updates.run_once().await;
    assert_eq!(subject.audits().len(), 2);
}

#[tokio::test(start_paused = true)]
async fn unreadable_bindings_fail_the_pass_and_local_bindings_never_reach_developers() {
    let unreadable = subject(Arc::new(Fake::default()), super::INTERVAL);
    assert!(!unreadable.updates.run_once().await);
    assert_eq!(unreadable.audits(), ["/ error cycle:bindings-unavailable"]);

    let mut local = published("team_1", '4');
    local.provenance = Provenance::Local;
    let subject = subject(Fake::with(vec![local]), super::INTERVAL);
    assert!(subject.updates.run_once().await);
    assert_eq!(subject.fake.asked.lock().expect("usable").len(), 0);
    assert_eq!(subject.fake.installed().len(), 0);
}

#[tokio::test(start_paused = true)]
async fn an_invalid_binding_a_foreign_candidate_and_a_current_one_are_not_installed() {
    let mut invalid = published("team_1", '1');
    invalid.document = document("hello-world", "0.1.0", None);
    let mismatch = published("team_2", '2');
    let mut current = published("team_3", '3');
    current.document = document("hello-world", "0.2.0", Some(&sha('b')));
    let fake = Fake::with(vec![invalid, mismatch, current]);
    fake.answer(
        &sha('a'),
        vec![Ok(document("other", "0.2.0", Some(&sha('9'))))],
    );
    fake.answer(&sha('b'), vec![Ok(candidate("0.2.0"))]);
    let subject = subject(fake, super::INTERVAL);
    assert!(subject.updates.run_once().await);
    assert_eq!(subject.fake.installed().len(), 0);
    assert_eq!(
        subject.audits(),
        [
            "team_1/hello-world error binding:invalid",
            "team_2/hello-world error candidate:identity-mismatch",
        ]
    );
}

#[test]
fn the_loop_waits_the_jittered_interval_and_doubles_after_failures() {
    let (interval, jitter) = (Duration::from_secs(10), Duration::from_secs(3));
    let mut delays = vec![Duration::ZERO];
    for succeeded in [true, false, false] {
        let previous = *delays.last().expect("a delay");
        delays.push(next_delay(previous, succeeded, interval, jitter));
    }
    assert_eq!(delays, [0, 13, 26, 52].map(Duration::from_secs));
    assert_eq!(
        next_delay(Duration::from_mins(50), false, interval, jitter),
        Duration::from_hours(1)
    );
}

#[tokio::test(start_paused = true)]
async fn the_loop_runs_a_pass_at_once_and_ends_on_stop() {
    let subject = subject(Fake::with(Vec::new()), super::INTERVAL);
    let stop = Stop::new();
    let running = subject.updates.run(&stop);
    tokio::pin!(running);
    tokio::select! {
        () = &mut running => panic!("the loop ended on its own"),
        () = tokio::time::sleep(Duration::from_secs(1)) => {}
    }
    assert_eq!(subject.fake.sweeps.load(Ordering::SeqCst), 1);
    stop.stop();
    running.await;
}
