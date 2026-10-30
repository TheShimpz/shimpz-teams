use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use strict_json::Value;
use team_browser::ingest::PutRefused;
use team_browser::task::{Answered, BrowserCall, StepFailed, TaskBrain, Usage};
use team_core::entropy::OsEntropy;
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_core::testing::MemoryAudit;
use team_protocol::browser_view::NOT_RUNNING;
use tokio::sync::Notify;
use tokio::time::Instant;

use super::{BrowserService, BrowserTurn, ServiceParts, WebFiles, language};
use crate::browser::audit::Actor;
use crate::browser::fake::{Fixture, driver, field, image};
use crate::browser::view::Refused;
use crate::http::service::Principal;

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

/// One scripted step: an answer, a failure, or a wait for the turn's Stop.
#[derive(Clone)]
enum Step {
    Answer(&'static str),
    Fail,
    UntilStopped,
}

/// The Brain of one task: its steps in order; a check finds every answer complete.
struct ScriptedBrain {
    steps: Mutex<VecDeque<Step>>,
    calls: Mutex<Vec<(BrowserCall, Value)>>,
    stop: Stop,
    asked: Notify,
}

impl ScriptedBrain {
    fn new(steps: &[Step], stop: &Stop) -> Arc<Self> {
        Arc::new(Self {
            steps: Mutex::new(steps.iter().cloned().collect()),
            calls: Mutex::new(Vec::new()),
            stop: stop.clone(),
            asked: Notify::new(),
        })
    }

    fn calls(&self) -> Vec<(BrowserCall, Value)> {
        self.calls.lock().expect("usable").clone()
    }
}

fn answered(value: Value) -> Answered {
    Answered {
        value,
        usage: Usage {
            model_calls: 1,
            provider_requests: 1,
            input_tokens: 10,
            output_tokens: 5,
            ..Usage::default()
        },
        model: "browser-model".to_owned(),
    }
}

impl TaskBrain for ScriptedBrain {
    fn call(
        &self,
        call: BrowserCall,
        request: Value,
        _deadline: Instant,
    ) -> BoxFuture<'_, Result<Answered, StepFailed>> {
        Box::pin(async move {
            self.calls.lock().expect("usable").push((call, request));
            self.asked.notify_waiters();
            match call {
                BrowserCall::Check => Ok(answered(json(r#"{"complete":true,"missing":null}"#))),
                BrowserCall::Fold => Ok(answered(json(r#"{"memory":""}"#))),
                BrowserCall::Final => Ok(answered(json(r#"{"summary":"final"}"#))),
                BrowserCall::Step => {
                    let step = self.steps.lock().expect("usable").pop_front();
                    match step {
                        Some(Step::Answer(text)) => Ok(answered(json(text))),
                        Some(Step::UntilStopped) => {
                            self.stop.stopped().await;
                            Err(StepFailed)
                        }
                        Some(Step::Fail) | None => Err(StepFailed),
                    }
                }
            }
        })
    }
}

/// The Team's files: every download stored, or refused as a test says.
#[derive(Default)]
struct MemoryFiles {
    stored: Mutex<Vec<(String, String, usize)>>,
    refuse: Mutex<Option<PutRefused>>,
}

impl WebFiles for MemoryFiles {
    fn put<'a>(
        &'a self,
        team_id: &'a str,
        name: &'a str,
        data: Vec<u8>,
    ) -> BoxFuture<'a, Result<Value, PutRefused>> {
        Box::pin(async move {
            if let Some(refusal) = *self.refuse.lock().expect("usable") {
                return Err(refusal);
            }
            self.stored.lock().expect("usable").push((
                team_id.to_owned(),
                name.to_owned(),
                data.len(),
            ));
            Ok(json(&format!(
                r#"{{"id":"{}","name":"{name}","media_type":"application/pdf","size":{}}}"#,
                "f".repeat(32),
                data.len()
            )))
        })
    }
}

struct Case {
    fixture: Fixture,
    breaks: Arc<Mutex<VecDeque<usize>>>,
    service: Arc<BrowserService>,
    files: Arc<MemoryFiles>,
    audit: Arc<MemoryAudit>,
    stop: Stop,
}

impl Case {
    fn new() -> Self {
        let mut fixture = Fixture::new();
        let runtime = Arc::new(fixture.runtime(Some(image())));
        let files = Arc::new(MemoryFiles::default());
        let audit = Arc::new(MemoryAudit::default());
        let service = Arc::new(BrowserService::new(ServiceParts {
            runtime,
            files: files.clone(),
            audit: audit.clone(),
            entropy: Arc::new(OsEntropy),
        }));
        let mut attached = std::mem::replace(
            &mut fixture.attached,
            tokio::sync::mpsc::unbounded_channel().1,
        );
        let breaks: Arc<Mutex<VecDeque<usize>>> = Arc::default();
        let scripted = breaks.clone();
        tokio::spawn(async move {
            while let Some((arguments, far)) = attached.recv().await {
                if arguments == ["mcp"] {
                    let replies = scripted.lock().expect("usable").pop_front();
                    tokio::spawn(driver(far, replies, 1));
                }
            }
        });
        let case = Self {
            fixture,
            breaks,
            service,
            files,
            audit,
            stop: Stop::new(),
        };
        case.listing(r#"{"files":[],"refused":[],"skipped":0}"#);
        case
    }

    fn turn(&self, brain: Arc<ScriptedBrain>) -> BrowserTurn {
        BrowserTurn {
            team_id: "team_1".to_owned(),
            incarnation: "d".repeat(64),
            locale: Some("pt-BR".to_owned()),
            stop: self.stop.clone(),
            brain,
            price: Arc::new(|_| (100, 400)),
            actor: Actor::request(&Principal::Machine, &"a".repeat(32)),
        }
    }

    fn details(&self, operation: &str) -> Vec<String> {
        self.audit
            .events()
            .iter()
            .filter(|event| {
                event.fields().iter().any(|(name, value)| {
                    name == "operation" && *value == Value::String(operation.to_owned())
                })
            })
            .filter_map(|event| {
                event
                    .fields()
                    .iter()
                    .find_map(|(name, value)| match (name.as_str(), value) {
                        ("detail", Value::String(detail)) => Some(detail.clone()),
                        _ => None,
                    })
            })
            .collect()
    }

    fn listing(&self, listing: &str) {
        let mut line = listing.as_bytes().to_vec();
        line.push(b'\n');
        self.fixture.execs.script(&["files", "list"], &line, 0);
    }
}

const DONE: &str = r#"{"kind":"done","summary":"Prazo: 30/11."}"#;

#[tokio::test(start_paused = true)]
async fn a_finished_task_ingests_its_downloads_and_always_releases() {
    let case = Case::new();
    case.listing(
        r#"{"files":[{"name":"edital.pdf","size":9}],"refused":[{"name":"x.crdownload","reason":"incomplete","size":1}],"skipped":1}"#,
    );
    case.fixture
        .execs
        .script(&["files", "read", "edital.pdf"], b"9\n%PDF-1.4\n", 0);
    let brain = ScriptedBrain::new(&[Step::Answer(DONE)], &case.stop);
    let (result, terminal) = case
        .service
        .run(&case.turn(brain.clone()), "Find the deadline")
        .await;
    assert_eq!(
        result,
        json(
            r#"{"status":"done","summary":"Prazo: 30/11.","completeness":"complete","unscreened_urls":[],"unscreened_more":0,"files":[{"name":"edital.pdf","media_type":"application/pdf","size":9,"outcome":"kept"},{"name":"x.crdownload","outcome":"incomplete"}]}"#
        )
    );
    assert_eq!(
        terminal,
        json(&format!(
            r#"{{"files":[{{"id":"{}","name":"edital.pdf","media_type":"application/pdf","size":9}}],"lost":[{{"name":"x.crdownload","reason":"incomplete"}}],"skipped":1}}"#,
            "f".repeat(32)
        ))
    );
    assert_eq!(
        *case.files.stored.lock().expect("usable"),
        [("team_1".to_owned(), "edital.pdf".to_owned(), 9)]
    );
    // The Brain saw the objective in the turn's language.
    let (call, request) = &brain.calls()[0];
    assert_eq!(*call, BrowserCall::Step);
    assert_eq!(
        field(request, "language"),
        Some(&Value::String("pt-BR".to_owned()))
    );
    assert_eq!(
        field(request, "objective"),
        Some(&Value::String("Find the deadline".to_owned()))
    );
    let details = case.details("browser-task");
    assert!(
        details[0]
            .starts_with("started:incarnation=dddddddddddd:container=browser-1:address=172.30.0.9")
    );
    assert!(
        details[1].starts_with("done:steps=1:recovered=0:relaunched=0:released=1:incarnation=")
    );
    assert!(!format!("{:?}", case.audit.events()).contains("edital"));
    assert_eq!(case.fixture.daemon.browsers(), Vec::<String>::new());
    assert!(!case.service.runtime().dirty());
}

#[tokio::test(start_paused = true)]
async fn a_needs_person_task_says_why_and_a_failed_brain_ends_it() {
    let case = Case::new();
    let needs = r#"{"kind":"needs_person","reason":"sign-in","sentence":"O site pede login."}"#;
    let brain = ScriptedBrain::new(&[Step::Answer(needs)], &case.stop);
    let (result, terminal) = case.service.run(&case.turn(brain), "x").await;
    assert_eq!(
        result,
        json(
            r#"{"status":"needs-person","reason":"sign-in","sentence":"O site pede login.","files":[]}"#
        )
    );
    assert_eq!(terminal, json(r#"{"files":[],"lost":[],"skipped":0}"#));
    let brain = ScriptedBrain::new(&[Step::Fail, Step::Fail], &case.stop);
    let (result, _) = case.service.run(&case.turn(brain), "x").await;
    assert_eq!(
        result,
        json(r#"{"status":"failed","reason":"model-unavailable","files":[]}"#)
    );
}

#[tokio::test(start_paused = true)]
async fn an_unavailable_or_busy_browser_ends_the_task_before_any_container() {
    let case = Case::new();
    case.fixture.daemon.state().fail.insert("network");
    let brain = ScriptedBrain::new(&[], &case.stop);
    let (result, terminal) = case.service.run(&case.turn(brain), "x").await;
    assert_eq!(
        result,
        json(r#"{"status":"failed","reason":"browser-unavailable","files":[]}"#)
    );
    assert_eq!(terminal, json(r#"{"files":[],"lost":[],"skipped":0}"#));
    assert_eq!(case.details("browser-task"), ["browser-unavailable"]);
    assert_eq!(case.fixture.daemon.state().created.len(), 0);
}

#[tokio::test(start_paused = true)]
async fn downloads_that_cannot_be_read_leave_the_answer_standing_and_say_why() {
    let case = Case::new();
    case.listing("not json");
    let brain = ScriptedBrain::new(&[Step::Answer(DONE)], &case.stop);
    let (result, terminal) = case.service.run(&case.turn(brain), "x").await;
    assert_eq!(
        field(&result, "status"),
        Some(&Value::String("done".to_owned()))
    );
    assert_eq!(
        field(&result, "downloads"),
        Some(&Value::String("unread".to_owned()))
    );
    assert_eq!(terminal, json(r#"{"files":[],"lost":[],"skipped":0}"#));
    assert!(case.details("browser-task")[1].contains(":cause=ingest/browser-lost:"));
}

#[tokio::test(start_paused = true)]
async fn a_failed_release_is_reported_and_keeps_the_space_refusing() {
    let case = Case::new();
    case.fixture.daemon.state().fail.insert("survive");
    let brain = ScriptedBrain::new(&[Step::Answer(DONE)], &case.stop);
    let (result, _) = case.service.run(&case.turn(brain), "x").await;
    assert_eq!(
        result,
        json(
            r#"{"status":"failed","completeness":"complete","unscreened_urls":[],"unscreened_more":0,"reason":"browser-lost","files":[]}"#
        )
    );
    assert!(case.details("browser-task")[1].contains(":released=0:"));
    assert!(case.service.runtime().dirty());
}

#[tokio::test(start_paused = true)]
async fn stop_removes_the_running_browser_and_nothing_is_ingested_after_it() {
    let case = Case::new();
    case.listing(r#"{"files":[{"name":"a.pdf","size":9}],"refused":[],"skipped":0}"#);
    let brain = ScriptedBrain::new(&[Step::UntilStopped], &case.stop);
    let stop = case.stop.clone();
    let asked = brain.clone();
    let stopper = tokio::spawn(async move {
        asked.asked.notified().await;
        stop.stop();
    });
    let (result, terminal) = case.service.run(&case.turn(brain), "x").await;
    stopper.await.expect("joined");
    assert_eq!(
        field(&result, "status"),
        Some(&Value::String("stopped".to_owned()))
    );
    assert_eq!(terminal, json(r#"{"files":[],"lost":[],"skipped":0}"#));
    assert_eq!(case.files.stored.lock().expect("usable").len(), 0);
    // Stop removed the Browser at once; the release proved it and freed the Space.
    let removed = case.fixture.daemon.state().removed.clone();
    assert!(removed.len() >= 2, "{removed:?}");
    assert_eq!(case.fixture.daemon.browsers(), Vec::<String>::new());
    assert!(!case.service.runtime().dirty());
}

#[tokio::test(start_paused = true)]
async fn a_stop_before_the_task_opens_no_channel() {
    let case = Case::new();
    case.stop.stop();
    let brain = ScriptedBrain::new(&[Step::Answer(DONE)], &case.stop);
    let (result, _) = case.service.run(&case.turn(brain.clone()), "x").await;
    assert_eq!(
        result,
        json(r#"{"status":"failed","reason":"browser-unavailable","files":[]}"#)
    );
    assert_eq!(brain.calls().len(), 0);
    let opened = case.fixture.execs.state().execs.clone();
    assert!(!opened.iter().any(|(_, arguments, _)| arguments == &["mcp"]));
}

#[tokio::test(start_paused = true)]
async fn storage_failures_become_closed_losses() {
    for (refusal, reason) in [
        (PutRefused::QuotaExceeded, "quota-exceeded"),
        (PutRefused::NameRefused, "name-refused"),
        (PutRefused::Unavailable, "store-unavailable"),
    ] {
        let case = Case::new();
        case.listing(r#"{"files":[{"name":"a.pdf","size":9}],"refused":[],"skipped":0}"#);
        case.fixture
            .execs
            .script(&["files", "read", "a.pdf"], b"9\n%PDF-1.4\n", 0);
        *case.files.refuse.lock().expect("usable") = Some(refusal);
        let brain = ScriptedBrain::new(&[Step::Answer(DONE)], &case.stop);
        let (_, terminal) = case.service.run(&case.turn(brain), "x").await;
        assert_eq!(
            field(&terminal, "lost"),
            Some(&json(&format!(
                r#"[{{"name":"a.pdf","reason":"{reason}"}}]"#
            )))
        );
    }
}

#[tokio::test(start_paused = true)]
async fn an_egress_replaced_during_the_task_loses_it() {
    let case = Case::new();
    case.listing(r#"{"files":[],"refused":[],"skipped":0}"#);
    let brain = ScriptedBrain::new(&[Step::UntilStopped], &case.stop);
    let daemon = case.fixture.daemon.clone();
    let asked = brain.clone();
    let stop = case.stop.clone();
    tokio::spawn(async move {
        asked.asked.notified().await;
        // The egress restarts under the task: its next proof of the Browser fails, and so does the task.
        daemon.state().restarts += 1;
        tokio::time::sleep(Duration::from_secs(1)).await;
        stop.stop();
    });
    let (result, _) = case.service.run(&case.turn(brain), "x").await;
    let status = field(&result, "status").cloned();
    assert!(
        status == Some(Value::String("stopped".to_owned()))
            || status == Some(Value::String("failed".to_owned()))
    );
}

#[tokio::test(start_paused = true)]
async fn a_screen_view_belongs_to_its_task_alone_and_streams_hold_one_of_two_slots() {
    let case = Case::new();
    assert_eq!(
        case.service.open_view("team_1").await,
        Err(Refused(NOT_RUNNING))
    );
    let brain = ScriptedBrain::new(&[Step::UntilStopped], &case.stop);
    let service = case.service.clone();
    let asked = brain.clone();
    let stop = case.stop.clone();
    let observer = tokio::spawn(async move {
        asked.asked.notified().await;
        let view = service
            .open_view("team_1")
            .await
            .expect("a running task's view");
        let other = service.open_view("team_2").await;
        let wrong = service
            .open_stream("team_1", &"0".repeat(32), "AAAAAAAAAAAAAAAAAAAAAA==", false)
            .await
            .map(|_| ());
        stop.stop();
        (view, other, wrong)
    });
    let _ = case.service.run(&case.turn(brain), "x").await;
    let (view, other, wrong) = observer.await.expect("joined");
    assert_eq!(view.len(), 32);
    assert_eq!(other, Err(Refused(NOT_RUNNING)));
    assert_eq!(wrong, Err(Refused(NOT_RUNNING)));
    assert_eq!(
        case.service.open_view("team_1").await,
        Err(Refused(NOT_RUNNING))
    );
    let first = Arc::clone(&case.service.views)
        .try_acquire_owned()
        .expect("a slot");
    let second = Arc::clone(&case.service.views)
        .try_acquire_owned()
        .expect("a slot");
    assert_eq!(
        case.service
            .open_stream("team_1", &view, "AAAAAAAAAAAAAAAAAAAAAA==", false)
            .await
            .map(|_| ()),
        Err(Refused(team_protocol::browser_view::BUSY))
    );
    drop((first, second));
}

#[test]
fn the_page_language_follows_the_turn() {
    assert_eq!(language(Some("pt-BR")), "pt-BR");
    assert_eq!(language(Some("PT")), "pt-BR");
    assert_eq!(language(Some("en-US")), "en");
    assert_eq!(language(None), "en");
}

#[tokio::test(start_paused = true)]
async fn a_failed_driver_session_is_replaced_only_once_its_exec_is_proven_ended() {
    // The first session opens (four answers) and then breaks as the task observes the page.
    let case = Case::new();
    case.breaks.lock().expect("usable").push_back(4);
    let brain = ScriptedBrain::new(&[Step::Answer(DONE)], &case.stop);
    let (result, _) = case.service.run(&case.turn(brain), "x").await;
    assert_eq!(
        field(&result, "status"),
        Some(&Value::String("done".to_owned()))
    );
    assert!(
        case.details("browser-task")[1]
            .starts_with("done:steps=2:recovered=1:relaunched=0:released=1"),
        "{:?}",
        case.details("browser-task")
    );
    let sessions = case
        .fixture
        .execs
        .state()
        .execs
        .iter()
        .filter(|(_, arguments, _)| arguments == &["mcp"])
        .count();
    assert_eq!(sessions, 2);
    // A failed session whose exec is not proven ended is never replaced: the task is lost and says why.
    let case = Case::new();
    case.breaks.lock().expect("usable").push_back(4);
    case.fixture
        .execs
        .state()
        .running
        .insert(vec!["mcp".to_owned()]);
    let brain = ScriptedBrain::new(&[Step::Answer(DONE)], &case.stop);
    let (result, _) = case.service.run(&case.turn(brain), "x").await;
    assert_eq!(
        field(&result, "status"),
        Some(&Value::String("failed".to_owned()))
    );
    assert!(
        case.details("browser-task")[1]
            .contains(":cause=observe/channel-lost/reopen/driver-not-ended:")
    );
    let sessions = case
        .fixture
        .execs
        .state()
        .execs
        .iter()
        .filter(|(_, arguments, _)| arguments == &["mcp"])
        .count();
    assert_eq!(sessions, 1);
}
