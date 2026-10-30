use std::collections::VecDeque;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use strict_json::Value;
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_protocol::browser::{
    MAX_CALL_ATTEMPTS, MAX_CALL_OUTPUT_TOKENS, MAX_HISTORY_BYTES, MAX_OBJECTIVE_CHARS,
    MAX_OBSERVATION_BYTES, MAX_REQUEST_TEXT_BYTES, MAX_SUMMARY_BYTES, canonical_thread,
    history_bytes,
};
use tokio::time::Instant;

use super::{
    Answered, BrowserCall, FINISH_SHARE, IMAGE_TOKENS, INCOMPLETE_NOTE, MAX_RECOVERIES,
    MAX_SECONDS, MAX_SPEND, Outcome, PROMPT_TOKENS, RESTARTED_NOTICE, Status, StepFailed, Task,
    TaskBrain, TaskHost, TaskSeams, UNSCREENED_NOTE, Usage, WAIT_SECONDS,
};
use crate::cua::{CuaError, CuaSession, Observation};
use crate::read::{self, Reading};
use crate::testing::{Browser, Driver, Element, View, json, page_text};

const DONE: &str = r#"{"kind": "done", "summary": "Prazo: 30/11."}"#;
const COMPLETE: &str = r#"{"complete": true, "missing": null}"#;
const WAIT: &str = r#"{"kind": "wait"}"#;

fn usage() -> Usage {
    Usage {
        model_calls: 1,
        provider_requests: 1,
        input_tokens: 1_000,
        output_tokens: 100,
        ..Usage::default()
    }
}

/// One scripted answer of the fake Brain, and how long the Brain takes to give it.
type Script = Box<dyn FnMut(BrowserCall, &Value) -> (Result<Value, StepFailed>, Duration) + Send>;

/// A Brain answering from a script, recording every call and checking none is sent with its time spent.
struct FakeBrain {
    script: Mutex<Script>,
    calls: Mutex<Vec<(BrowserCall, Value, Instant)>>,
    usage: Usage,
}

impl FakeBrain {
    fn new(
        script: impl FnMut(BrowserCall, &Value) -> (Result<Value, StepFailed>, Duration)
        + Send
        + 'static,
    ) -> Arc<Self> {
        Self::counting(usage(), script)
    }

    fn counting(
        usage: Usage,
        script: impl FnMut(BrowserCall, &Value) -> (Result<Value, StepFailed>, Duration)
        + Send
        + 'static,
    ) -> Arc<Self> {
        Arc::new(Self {
            script: Mutex::new(Box::new(script)),
            calls: Mutex::default(),
            usage,
        })
    }

    /// Answers each step in turn, checks with `verdicts` (complete once they run out), folds with "Fatos.", and
    /// finishes with `last`.
    fn steps(
        steps: &[Result<&str, StepFailed>],
        verdicts: &[Result<&str, StepFailed>],
        last: Option<&str>,
    ) -> Arc<Self> {
        let mut steps: VecDeque<Result<Value, StepFailed>> =
            steps.iter().map(|step| step.map(json)).collect();
        let mut verdicts: VecDeque<Result<Value, StepFailed>> =
            verdicts.iter().map(|verdict| verdict.map(json)).collect();
        let last = last.map(json);
        Self::new(move |call, _| {
            let answer = match call {
                BrowserCall::Step => steps.pop_front().unwrap_or(Err(StepFailed)),
                BrowserCall::Check => verdicts.pop_front().unwrap_or_else(|| Ok(json(COMPLETE))),
                BrowserCall::Fold => Ok(json(r#"{"memory": "Fatos."}"#)),
                BrowserCall::Final => Ok(last.clone().unwrap_or(Value::Null)),
            };
            (answer, Duration::ZERO)
        })
    }

    fn calls(&self) -> MutexGuard<'_, Vec<(BrowserCall, Value, Instant)>> {
        self.calls.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn requests(&self, call: BrowserCall) -> Vec<Value> {
        self.calls()
            .iter()
            .filter(|(made, _, _)| *made == call)
            .map(|(_, request, _)| request.clone())
            .collect()
    }

    fn operations(&self) -> Vec<BrowserCall> {
        self.calls().iter().map(|(call, _, _)| *call).collect()
    }
}

impl TaskBrain for FakeBrain {
    fn call(
        &self,
        call: BrowserCall,
        request: Value,
        deadline: Instant,
    ) -> BoxFuture<'_, Result<Answered, StepFailed>> {
        Box::pin(async move {
            assert!(
                deadline > Instant::now(),
                "a call is never sent with its time spent"
            );
            self.calls().push((call, request.clone(), deadline));
            let (answer, took) =
                (self.script.lock().unwrap_or_else(PoisonError::into_inner))(call, &request);
            tokio::time::sleep(took).await;
            answer.map(|value| Answered {
                value,
                usage: self.usage,
                model: "gpt-6-luna".to_owned(),
            })
        })
    }
}

type Reopen = Box<dyn FnMut(bool) -> Result<(Driver, bool), CuaError> + Send>;

/// The Browser a task runs in: whether it is intact, and the sessions that replace a failed one.
struct FakeHost {
    intact: Mutex<Box<dyn FnMut() -> bool + Send>>,
    reopen: Mutex<Reopen>,
    reopened: Mutex<Vec<(Instant, bool)>>,
}

impl FakeHost {
    fn new() -> Self {
        Self {
            intact: Mutex::new(Box::new(|| true)),
            reopen: Mutex::new(Box::new(|_| Err(CuaError::fatal("driver-unavailable")))),
            reopened: Mutex::default(),
        }
    }

    fn intact(mut self, intact: impl FnMut() -> bool + Send + 'static) -> Self {
        self.intact = Mutex::new(Box::new(intact));
        self
    }

    fn reopening(
        mut self,
        reopen: impl FnMut(bool) -> Result<(Driver, bool), CuaError> + Send + 'static,
    ) -> Self {
        self.reopen = Mutex::new(Box::new(reopen));
        self
    }

    fn reopened(&self) -> Vec<(Instant, bool)> {
        self.reopened
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }
}

impl TaskHost for FakeHost {
    fn intact(&self) -> BoxFuture<'_, bool> {
        Box::pin(async move { (self.intact.lock().unwrap_or_else(PoisonError::into_inner))() })
    }

    fn reopen(
        &self,
        _failed: CuaSession,
        deadline: Instant,
        relaunch: bool,
    ) -> BoxFuture<'_, Result<(CuaSession, bool), CuaError>> {
        Box::pin(async move {
            self.reopened
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .push((deadline, relaunch));
            let reopened = (self.reopen.lock().unwrap_or_else(PoisonError::into_inner))(relaunch);
            let (driver, relaunched) = reopened?;
            Ok((driver.opened().await, relaunched))
        })
    }

    fn downloads(&self, _deadline: Instant) -> BoxFuture<'_, Option<Value>> {
        Box::pin(async { None })
    }
}

/// A task over `driver` with `brain` and `host`, its budget, Stop, and deadline as given.
async fn task(
    driver: &Driver,
    brain: Arc<FakeBrain>,
    host: Arc<FakeHost>,
    stop: Stop,
    deadline: Instant,
) -> Task {
    let seams = TaskSeams {
        brain,
        price: Arc::new(|_| (10, 50)),
        stop,
        host,
    };
    Task::new(
        "Veja o prazo.".to_owned(),
        "pt-BR".to_owned(),
        driver.opened().await,
        seams,
        deadline,
    )
}

async fn run(driver: &Driver, brain: &Arc<FakeBrain>, host: FakeHost) -> Outcome {
    let deadline = Instant::now() + MAX_SECONDS;
    task(
        driver,
        Arc::clone(brain),
        Arc::new(host),
        Stop::new(),
        deadline,
    )
    .await
    .run()
    .await
}

fn done(summary: &str, steps: usize) -> Outcome {
    Outcome {
        summary: Some(summary.to_owned()),
        steps,
        completeness: Some("complete"),
        ..Outcome::new(Status::Done)
    }
}

fn failed(reason: &str, steps: usize, cause: Option<&str>) -> Outcome {
    Outcome {
        reason: Some(reason.to_owned()),
        steps,
        cause: cause.map(str::to_owned),
        ..Outcome::new(Status::Failed)
    }
}

fn last_steps(brain: &FakeBrain) -> Vec<Value> {
    let requests = brain.requests(BrowserCall::Step);
    match requests
        .last()
        .and_then(|request| request.get("thread"))
        .and_then(|thread| thread.get("steps"))
    {
        Some(Value::Array(steps)) => steps.clone(),
        _ => Vec::new(),
    }
}

fn outcomes(steps: &[Value]) -> Vec<String> {
    steps
        .iter()
        .map(|entry| match entry.get("outcome") {
            Some(Value::String(outcome)) => outcome.clone(),
            _ => String::new(),
        })
        .collect()
}

fn observation_of(request: &Value) -> String {
    match request.get("observation") {
        Some(Value::String(text)) => text.clone(),
        _ => String::new(),
    }
}

fn call(parts: &[&str]) -> Vec<String> {
    parts.iter().map(|part| (*part).to_owned()).collect()
}

const EVERY_KIND: [Result<&str, StepFailed>; 6] = [
    Ok(r#"{"kind": "navigate", "url": "https://example.com/editais"}"#),
    Ok(r#"{"kind": "type", "ref": "p1:0", "text": "ia"}"#),
    Ok(r#"{"kind": "click", "ref": "p1:1"}"#),
    Ok(r#"{"kind": "scroll", "direction": "down"}"#),
    Ok(WAIT),
    Ok(DONE),
];

#[tokio::test(start_paused = true)]
async fn a_task_executes_each_closed_step_and_the_agents_checked_answer_stands() {
    let driver = Driver::default();
    let brain = FakeBrain::steps(&EVERY_KIND, &[], None);
    let started = Instant::now();
    let outcome = run(&driver, &brain, FakeHost::new()).await;
    assert_eq!(outcome, done("Prazo: 30/11.", 6));
    assert_eq!(
        driver.calls(),
        [
            call(&["navigate", "https://example.com/editais"]),
            call(&["type", "p1:0", "ia"]),
            call(&["click", "p1:1"]),
            call(&["scroll", "down"]),
        ]
    );
    // Only the wait step waited.
    assert_eq!(started.elapsed(), WAIT_SECONDS);
    each_request_extends_the_thread(&brain);
}

/// The first request has no thread yet, each later one only extends it, and the last entry is the whole step.
fn each_request_extends_the_thread(brain: &FakeBrain) {
    let requests = brain.requests(BrowserCall::Step);
    assert_eq!(
        requests[0]
            .get("thread")
            .and_then(|thread| thread.get("steps")),
        Some(&json("[]"))
    );
    // Each step joins the thread with the page it was chosen on, that page's text, and what it saw.
    let mut expected = json(
        r#"{"step": {"kind": "wait"}, "outcome": "ok", "page": {"url": "https://example.com/editais", "title": "Example"}, "text": "", "downloads": null, "screenshot": false}"#,
    );
    if let Value::Object(members) = &mut expected {
        members[3].1 = Value::String(page_text("https://example.com/editais"));
    }
    assert_eq!(last_steps(brain).last(), Some(&expected));
    assert_eq!(
        (
            requests[0].get("objective"),
            requests[0].get("language"),
            requests[0].get("screenshot")
        ),
        (
            Some(&json(r#""Veja o prazo.""#)),
            Some(&json(r#""pt-BR""#)),
            Some(&Value::Null)
        )
    );
    // Each request extends the one before it: earlier entries are never rewritten.
    for pair in requests.windows(2) {
        let thread =
            |request: &Value| match request.get("thread").and_then(|thread| thread.get("steps")) {
                Some(Value::Array(steps)) => steps.clone(),
                _ => Vec::new(),
            };
        let (earlier, later) = (thread(&pair[0]), thread(&pair[1]));
        assert_eq!(later[..earlier.len()], earlier[..]);
    }
    assert_eq!(
        brain.requests(BrowserCall::Check)[0].get("summary"),
        Some(&json(r#""Prazo: 30/11.""#))
    );
}

#[tokio::test(start_paused = true)]
async fn a_navigation_that_did_not_load_is_an_outcome_the_agent_sees_after_looking_again() {
    let driver = Driver::new(Browser {
        unloaded: vec!["navigate"],
        ..Browser::default()
    });
    let brain = FakeBrain::steps(
        &[
            Ok(r#"{"kind": "navigate", "url": "https://www.python.org"}"#),
            Ok(r#"{"kind": "done", "summary": "Não carregou."}"#),
        ],
        &[],
        None,
    );
    assert_eq!(
        run(&driver, &brain, FakeHost::new()).await,
        done("Não carregou.", 2)
    );
    assert_eq!(outcomes(&last_steps(&brain)), ["navigation-failed"]);
}

#[tokio::test(start_paused = true)]
async fn an_error_page_sends_the_browser_back_to_the_last_page_that_loaded_and_says_so() {
    let mut browser = Browser::default();
    browser.views.insert(
        "https://down.example/".to_owned(),
        View {
            url: "chrome-error://chromewebdata/".to_owned(),
            title: "down.example".to_owned(),
            ..View::default()
        },
    );
    let driver = Driver::new(browser);
    let brain = FakeBrain::steps(
        &[
            Ok(r#"{"kind": "navigate", "url": "https://down.example/"}"#),
            Ok(DONE),
        ],
        &[],
        None,
    );
    run(&driver, &brain, FakeHost::new()).await;
    assert_eq!(
        driver.calls().last(),
        Some(&call(&["navigate", "https://example.com"]))
    );
    let observation = observation_of(brain.requests(BrowserCall::Step).last().expect("asked"));
    assert!(observation.starts_with("The last page you opened (down.example) did not load"));
}

#[tokio::test(start_paused = true)]
async fn a_ref_not_offered_for_its_action_and_a_refused_page_action_are_outcomes_not_failures() {
    let driver = Driver::new(Browser {
        refuse: vec!["navigate"],
        ..Browser::default()
    });
    let brain = FakeBrain::steps(
        &[
            Ok(r#"{"kind": "click", "ref": "p1:0"}"#),
            Ok(r#"{"kind": "type", "ref": "p9:9", "text": "x"}"#),
            Ok(r#"{"kind": "navigate", "url": "https://example.com"}"#),
            Ok(
                r#"{"kind": "needs_person", "reason": "sign-in", "sentence": "O site pede login."}"#,
            ),
        ],
        &[],
        None,
    );
    let outcome = run(&driver, &brain, FakeHost::new()).await;
    let expected = Outcome {
        reason: Some("sign-in".to_owned()),
        sentence: Some("O site pede login.".to_owned()),
        steps: 4,
        ..Outcome::new(Status::NeedsPerson)
    };
    assert_eq!(outcome, expected);
    assert_eq!(
        outcomes(&last_steps(&brain)),
        ["refused", "refused", "refused"]
    );
    assert_eq!(driver.calls(), [call(&["navigate", "https://example.com"])]);
}

#[tokio::test(start_paused = true)]
async fn screenshots_are_rationed_and_only_the_current_page_carries_the_image() {
    let driver = Driver::default();
    let shot = r#"{"kind": "screenshot"}"#;
    let brain = FakeBrain::steps(
        &[Ok(shot), Ok(shot), Ok(WAIT), Ok(WAIT), Ok(shot), Ok(DONE)],
        &[],
        None,
    );
    run(&driver, &brain, FakeHost::new()).await;
    assert_eq!(
        driver.browser().screenshots,
        [false, true, false, false, false, true]
    );
    let requests = brain.requests(BrowserCall::Step);
    assert_ne!(requests[1].get("screenshot"), Some(&Value::Null));
    assert_eq!(requests[2].get("screenshot"), Some(&Value::Null));
    let entries = last_steps(&brain);
    assert_eq!(outcomes(&entries), ["ok", "refused", "ok", "ok", "ok"]);
    let shots: Vec<Option<&Value>> = entries
        .iter()
        .map(|entry| entry.get("screenshot"))
        .collect();
    let flags = [false, true, false, false, false].map(Value::Bool);
    assert_eq!(shots, flags.iter().map(Some).collect::<Vec<_>>());
}

/// A Brain whose step takes until the soft cutoff is near, then fails or answers `step`.
fn late_step(start: Instant, step: Option<&str>) -> Arc<FakeBrain> {
    let step = step.map(json);
    FakeBrain::new(move |call, _| match call {
        BrowserCall::Step => {
            let took = (start + MAX_SECONDS)
                .checked_sub(Duration::from_secs(30))
                .expect("a task outlasts its cutoff")
                .saturating_duration_since(Instant::now());
            (step.clone().ok_or(StepFailed), took)
        }
        _ => (Ok(json(r#"{"summary": "Parcial."}"#)), Duration::ZERO),
    })
}

#[tokio::test(start_paused = true)]
async fn a_step_cut_short_by_the_soft_cutoff_finishes_from_the_thread() {
    let brain = late_step(Instant::now(), None);
    let outcome = run(&Driver::default(), &brain, FakeHost::new()).await;
    assert_eq!(outcome.summary.as_deref(), Some("Parcial."));
}

#[tokio::test(start_paused = true)]
async fn a_retry_the_normal_share_cannot_reserve_finishes_the_task() {
    // Measure the first step's reservation, then give the task twice that: a failed step keeps its reservation, so
    // its retry no longer fits the normal share while the final answer still fits the whole.
    let brain = FakeBrain::steps(&[Ok(DONE)], &[], None);
    let driver = Driver::default();
    let mut measuring = task(
        &driver,
        Arc::clone(&brain),
        Arc::new(FakeHost::new()),
        Stop::new(),
        Instant::now() + MAX_SECONDS,
    )
    .await;
    measuring.execute().await;
    let request = brain.requests(BrowserCall::Step)[0].clone();
    let reserve = measuring.state.attempt(super::bound(&request)) * u64::from(MAX_CALL_ATTEMPTS);
    let brain = FakeBrain::new(|call, _| match call {
        BrowserCall::Step => (Err(StepFailed), Duration::ZERO),
        _ => (Ok(json(r#"{"summary": "Parcial."}"#)), Duration::ZERO),
    });
    let mut limited = task(
        &Driver::default(),
        Arc::clone(&brain),
        Arc::new(FakeHost::new()),
        Stop::new(),
        Instant::now() + MAX_SECONDS,
    )
    .await;
    limited.state.spend = 2 * reserve;
    let outcome = limited.execute().await;
    assert_eq!(outcome.summary.as_deref(), Some("Parcial."));
    assert_eq!(brain.operations(), [BrowserCall::Step, BrowserCall::Final]);
}

#[test]
fn the_finishing_share_covers_a_final_answer_and_its_check_at_every_bound() {
    let catalog = json(include_str!("../../../../inference/model_catalog.json"));
    let Some(Value::Array(providers)) = catalog.get("providers") else {
        panic!("the catalog lists providers");
    };
    let price = |provider: &str, model: &str| -> (u64, u64) {
        let models = providers
            .iter()
            .find(|entry| entry.get("id") == Some(&Value::String(provider.to_owned())))
            .and_then(|entry| entry.get("models"));
        let Some(Value::Array(models)) = models else {
            panic!("the provider lists models");
        };
        let entry = models
            .iter()
            .find(|entry| entry.get("id") == Some(&Value::String(model.to_owned())))
            .expect("the Browser model is listed");
        let cents = |name: &str| match entry.get(name) {
            Some(Value::Number(strict_json::Number::Int(cents))) => {
                u64::try_from(*cents).expect("a price")
            }
            _ => panic!("a price"),
        };
        (
            cents("input_usd_per_million_cents"),
            cents("output_usd_per_million_cents"),
        )
    };
    let (price_in, price_out) = [
        price("openai", "gpt-6-luna"),
        price("anthropic", "claude-haiku-5-5"),
    ]
    .into_iter()
    .max()
    .expect("two models");
    let output = u64::from(MAX_CALL_OUTPUT_TOKENS) * price_out;
    let final_bound = MAX_REQUEST_TEXT_BYTES as u64 + IMAGE_TOKENS + PROMPT_TOKENS;
    let check_bound = 4 * MAX_OBJECTIVE_CHARS as u64 + MAX_SUMMARY_BYTES as u64 + PROMPT_TOKENS;
    let needed: u64 = [final_bound, check_bound]
        .iter()
        .map(|bound| (bound * price_in + output) * u64::from(MAX_CALL_ATTEMPTS))
        .sum();
    assert!(MAX_SPEND * FINISH_SHARE / 100 >= needed);
}

#[tokio::test(start_paused = true)]
async fn the_hard_deadline_ends_the_task_and_a_call_with_no_time_left_is_never_sent() {
    let brain = FakeBrain::steps(&[Ok(DONE)], &[], None);
    let outcome = task(
        &Driver::default(),
        Arc::clone(&brain),
        Arc::new(FakeHost::new()),
        Stop::new(),
        Instant::now(),
    )
    .await
    .run()
    .await;
    assert_eq!(outcome, Outcome::new(Status::Budget));
    // A look that took the whole deadline leaves no call to send.
    let driver = Driver::new(Browser {
        read_delay: MAX_SECONDS + Duration::from_secs(1),
        ..Browser::default()
    });
    let brain = FakeBrain::steps(&[Ok(DONE)], &[], Some(r#"{"summary": "x"}"#));
    run(&driver, &brain, FakeHost::new()).await;
    assert_eq!(brain.operations(), []);
}

#[tokio::test(start_paused = true)]
async fn a_final_answer_ends_with_what_stopped_it() {
    let driver = Driver::new(Browser {
        fail_reads: Some("tool-failed"),
        ..Browser::default()
    });
    let brain = FakeBrain::steps(&[], &[], Some(r#"{"summary": "Parcial."}"#));
    let mut finishing = task(
        &driver,
        brain,
        Arc::new(FakeHost::new()),
        Stop::new(),
        Instant::now() + MAX_SECONDS,
    )
    .await;
    finishing.state.steps = 0;
    assert_eq!(
        finishing.execute().await.reason.as_deref(),
        Some("browser-lost")
    );
    let stop = Stop::new();
    let stopping = stop.clone();
    let brain = FakeBrain::new(move |_, _| {
        stopping.stop();
        (Ok(json(r#"{"summary": "Parcial."}"#)), Duration::ZERO)
    });
    let mut finishing = task(
        &Driver::default(),
        brain,
        Arc::new(FakeHost::new()),
        stop,
        Instant::now() + MAX_SECONDS,
    )
    .await;
    finishing.state.steps = 0;
    assert_eq!(finishing.execute().await, Outcome::new(Status::Stopped));
}

/// A reader that may act as `answers` says, then never, and keeps what it is given.
struct Answers {
    answers: VecDeque<bool>,
    kept: Vec<String>,
}

impl Reading for Answers {
    fn may_act(&mut self) -> impl std::future::Future<Output = bool> + Send {
        let answer = self.answers.pop_front().unwrap_or(false);
        async move { answer }
    }

    fn seen(&mut self, _url: &str, _text: &str) {}

    fn keep(&mut self, section: String) {
        self.kept.push(section);
    }
}

#[tokio::test(start_paused = true)]
async fn a_read_stops_before_its_first_target_or_while_its_page_loads_when_the_task_must_stop() {
    let driver = Driver::new(two_links("https://example.com/busca"));
    let mut session = driver.opened().await;
    let observation = read::shown(&session.observe(false).await.expect("observed"), "", "");
    let decision = json(r#"{"kind": "read", "refs": ["p1:4", "p1:5"]}"#);
    let mut never = Answers {
        answers: VecDeque::new(),
        kept: Vec::new(),
    };
    assert_eq!(
        read::run(&mut session, &decision, &observation, &mut never).await,
        Ok("refused")
    );
    assert_eq!(driver.calls(), Vec::<Vec<String>>::new());
    let mut twice = Answers {
        answers: [true, true, false].into(),
        kept: Vec::new(),
    };
    assert_eq!(
        read::run(&mut session, &decision, &observation, &mut twice).await,
        Ok("ok")
    );
    assert_eq!(driver.calls(), [call(&["click", "p1:4"])]);
    assert!(
        twice
            .kept
            .last()
            .expect("kept")
            .contains("--- not opened: Vaga A")
    );
    // A click the page refused opened nothing, and the read goes on.
    let refusing = Driver::new(Browser {
        refuse: vec!["click"],
        ..two_links("https://example.com/busca")
    });
    let mut session = refusing.opened().await;
    let observation = read::shown(&session.observe(false).await.expect("observed"), "", "");
    let mut always = Answers {
        answers: std::iter::repeat_n(true, 20).collect(),
        kept: Vec::new(),
    };
    read::run(&mut session, &decision, &observation, &mut always)
        .await
        .expect("read");
    assert!(
        always
            .kept
            .last()
            .expect("kept")
            .contains("--- not loaded: Vaga A ---")
    );
}

#[tokio::test(start_paused = true)]
async fn no_step_is_taken_past_the_soft_cutoff_and_stop_during_finishing_wins() {
    let driver = Driver::default();
    let brain = late_step(
        Instant::now(),
        Some(r#"{"kind": "navigate", "url": "https://example.com/x"}"#),
    );
    let outcome = run(&driver, &brain, FakeHost::new()).await;
    assert_eq!(outcome.summary.as_deref(), Some("Parcial."));
    assert_eq!(driver.calls(), Vec::<Vec<String>>::new());
    let stop = Stop::new();
    let stopping = stop.clone();
    let brain = FakeBrain::new(move |call, _| {
        if call == BrowserCall::Check {
            stopping.stop();
            return (Ok(json(COMPLETE)), Duration::ZERO);
        }
        (Ok(json(DONE)), Duration::ZERO)
    });
    let outcome = task(
        &Driver::default(),
        brain,
        Arc::new(FakeHost::new()),
        stop,
        Instant::now() + MAX_SECONDS,
    )
    .await
    .run()
    .await;
    assert_eq!(outcome.status, Status::Stopped);
}

fn two_links(url: &str) -> Browser {
    Browser {
        url: url.to_owned(),
        links: vec![
            ("p1:4".to_owned(), "Vaga A".to_owned()),
            ("p1:5".to_owned(), "Vaga B".to_owned()),
        ],
        ..Browser::default()
    }
}

mod budget;
mod reading;
mod thread;
