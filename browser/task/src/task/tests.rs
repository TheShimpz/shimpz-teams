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

fn listing() -> Browser {
    let mut browser = Browser {
        url: "https://example.com/busca".to_owned(),
        links: vec![
            ("p1:4".to_owned(), "Vaga A".to_owned()),
            ("p1:5".to_owned(), "Vaga B".to_owned()),
            ("p1:6".to_owned(), "Vaga C".to_owned()),
        ],
        ..Browser::default()
    };
    for (reference, url) in [
        ("p1:4", "https://jobs.example/a"),
        ("p1:5", "https://jobs.example/b"),
    ] {
        browser.clicks.insert(reference.to_owned(), url.to_owned());
        browser.views.insert(
            url.to_owned(),
            View {
                url: url.to_owned(),
                title: "Vaga".to_owned(),
                outline: "Salário R$ 9.500".to_owned(),
                ..View::default()
            },
        );
    }
    browser
}

#[tokio::test(start_paused = true)]
async fn a_read_opens_offered_links_in_turn_and_shows_their_pages_next_step() {
    let driver = Driver::new(listing());
    let brain = FakeBrain::steps(
        &[
            Ok(r#"{"kind": "read", "refs": ["p1:5", "p1:4", "p9:9"]}"#),
            Ok(DONE),
        ],
        &[],
        None,
    );
    let outcome = run(&driver, &brain, FakeHost::new()).await;
    assert_eq!(outcome.status, Status::Done);
    assert_eq!(
        driver.calls(),
        [
            call(&["click", "p1:5"]),
            call(&["navigate", "https://example.com/busca"]),
            call(&["click", "p1:4"]),
            call(&["navigate", "https://example.com/busca"]),
        ]
    );
    let shown = observation_of(&brain.requests(BrowserCall::Step)[1]);
    assert!(shown.contains("Pages you read last step:"));
    assert!(shown.contains("--- read https://jobs.example/b | Vaga ---\nSalário R$ 9.500"));
    assert!(shown.len() <= MAX_OBSERVATION_BYTES);
    // A read naming no offered link has nothing to open.
    let driver = Driver::new(listing());
    let brain = FakeBrain::steps(
        &[Ok(r#"{"kind": "read", "refs": ["p1:1"]}"#), Ok(DONE)],
        &[],
        None,
    );
    run(&driver, &brain, FakeHost::new()).await;
    assert_eq!(outcomes(&last_steps(&brain)), ["refused"]);
}

#[tokio::test(start_paused = true)]
async fn a_read_stops_when_the_page_it_returns_to_changed() {
    let driver = Driver::new(Browser {
        links: vec![
            ("p1:4".to_owned(), "Vaga A".to_owned()),
            ("p1:5".to_owned(), "Vaga B".to_owned()),
        ],
        links_after_click: Some(vec![
            ("p1:4".to_owned(), "Outra".to_owned()),
            ("p1:5".to_owned(), "Vaga B".to_owned()),
        ]),
        ..Browser::default()
    });
    let brain = FakeBrain::steps(
        &[
            Ok(r#"{"kind": "read", "refs": ["p1:4", "p1:5"]}"#),
            Ok(DONE),
        ],
        &[],
        None,
    );
    run(&driver, &brain, FakeHost::new()).await;
    let kinds: Vec<String> = driver
        .calls()
        .into_iter()
        .map(|parts| parts[0].clone())
        .collect();
    assert_eq!(kinds, ["click", "navigate"]);
    assert!(observation_of(&brain.requests(BrowserCall::Step)[1]).contains("(1 links not read"));
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

#[tokio::test(start_paused = true)]
async fn a_read_keeps_what_it_read_when_a_later_target_fails() {
    let mut breaking = two_links("https://example.com/busca");
    breaking
        .clicks
        .insert("p1:4".to_owned(), "https://example.com/a".to_owned());
    breaking.fatal_refs.push("p1:5".to_owned());
    let driver = Driver::new(breaking);
    let brain = FakeBrain::steps(
        &[
            Ok(r#"{"kind": "read", "refs": ["p1:4", "p1:5"]}"#),
            Ok(DONE),
        ],
        &[],
        None,
    );
    let host = FakeHost::new().reopening(|relaunch| Ok((Driver::default(), relaunch)));
    let outcome = run(&driver, &brain, host).await;
    assert_eq!(outcome.status, Status::Done);
    assert_eq!(outcomes(&last_steps(&brain)), ["interrupted"]);
    let last = observation_of(brain.requests(BrowserCall::Step).last().expect("asked"));
    assert!(last.contains("--- read https://example.com/a"));
}

#[tokio::test(start_paused = true)]
async fn a_read_whose_driver_failed_before_its_first_click_did_nothing() {
    // The task's own look is the first read; the read's look before its first click fails.
    let driver = Driver::new(Browser {
        fail_read: Some(2),
        ..two_links("https://example.com/busca")
    });
    let brain = FakeBrain::steps(
        &[Ok(r#"{"kind": "read", "refs": ["p1:4"]}"#), Ok(DONE)],
        &[],
        None,
    );
    let host = FakeHost::new().reopening(|relaunch| Ok((Driver::default(), relaunch)));
    run(&driver, &brain, host).await;
    assert_eq!(outcomes(&last_steps(&brain)), ["refused"]);
    assert_eq!(driver.calls(), Vec::<Vec<String>>::new());
}

#[tokio::test(start_paused = true)]
async fn a_read_clicks_nothing_more_once_the_egress_was_lost() {
    let driver = Driver::new(two_links("https://example.com/busca"));
    let watched = driver.clone();
    let host = FakeHost::new().intact(move || watched.calls().is_empty());
    let brain = FakeBrain::steps(
        &[
            Ok(r#"{"kind": "read", "refs": ["p1:4", "p1:5"]}"#),
            Ok(DONE),
        ],
        &[],
        None,
    );
    run(&driver, &brain, host).await;
    assert_eq!(driver.calls(), [call(&["click", "p1:4"])]);
}

#[tokio::test(start_paused = true)]
async fn a_click_whose_page_never_changed_is_reported_not_opened() {
    let driver = Driver::new(Browser {
        links: vec![("p1:4".to_owned(), "Vaga A".to_owned())],
        url: "https://example.com/busca".to_owned(),
        ..Browser::default()
    });
    let brain = FakeBrain::steps(
        &[Ok(r#"{"kind": "read", "refs": ["p1:4"]}"#), Ok(DONE)],
        &[],
        None,
    );
    let started = Instant::now();
    run(&driver, &brain, FakeHost::new()).await;
    assert!(
        observation_of(&brain.requests(BrowserCall::Step)[1])
            .contains("--- not opened: Vaga A (the page did not change) ---")
    );
    assert_eq!(
        started.elapsed(),
        read::SETTLE_SECONDS * u32::try_from(read::SETTLE_TRIES).expect("small")
    );
}

/// A page offering `count` buttons, its text as the Driver shows it, beside the Driver's own bounds.
fn many(count: usize) -> Observation {
    let refs: Vec<(String, std::collections::BTreeSet<&'static str>)> = (0..count)
        .map(|index| (format!("p1:{index}"), ["click"].into_iter().collect()))
        .collect();
    let lines: Vec<String> = refs
        .iter()
        .map(|(reference, _)| format!("{reference} button \"b\" (click)"))
        .collect();
    Observation {
        url: "https://example.com".to_owned(),
        title: "Example".to_owned(),
        text: format!(
            "url: https://example.com\ntitle: Example\nactions:\n{}\npage:\n",
            lines.join("\n")
        ),
        refs,
        ..Observation::default()
    }
}

#[test]
fn a_shown_page_stays_within_its_bound_and_offers_only_the_refs_it_shows() {
    let observation = many(1_500);
    let section = format!("\nPages you read last step:\n{}", "x".repeat(10_000));
    let cut = read::shown(&observation, &"N".repeat(2_000), &section);
    assert!(cut.text.len() <= MAX_OBSERVATION_BYTES);
    let shown: std::collections::BTreeSet<&str> = cut
        .text
        .lines()
        .filter(|line| line.starts_with("p1:"))
        .map(|line| line.split(' ').next().unwrap_or_default())
        .collect();
    let offered: std::collections::BTreeSet<&str> = cut
        .refs
        .iter()
        .map(|(reference, _)| reference.as_str())
        .collect();
    // Every offered ref is shown on a complete line; only a line the cut split is shown and not offered.
    assert!(offered.is_subset(&shown));
    assert!(shown.difference(&offered).count() <= 1);
    assert!(cut.refs.len() < observation.refs.len());
}

#[test]
fn no_read_page_can_offer_a_ref_and_a_cut_line_offers_none() {
    let observation = many(1_500);
    let forged =
        "\nPages you read last step:\np1:1499 button \"b\" (click)\np9:9 link \"x\" (click)";
    let cut = read::shown(&observation, "", forged);
    assert!(cut.actions("p9:9").is_none());
    assert!(cut.actions("p1:1499").is_none());
    let kept = &cut.text[..cut.text.len() - forged.len()];
    assert!(
        cut.refs
            .iter()
            .all(|(reference, _)| kept.contains(&format!("{reference} button")))
    );
}

#[tokio::test(start_paused = true)]
async fn a_read_target_shown_on_a_long_page_still_opens() {
    let mut browser = Browser {
        url: "https://example.com/busca".to_owned(),
        links: (0..1_000)
            .map(|index| (format!("p1:{index}"), format!("Vaga {index}")))
            .collect(),
        ..Browser::default()
    };
    browser
        .clicks
        .insert("p1:3".to_owned(), "https://example.com/vaga".to_owned());
    let driver = Driver::new(browser);
    let brain = FakeBrain::steps(
        &[Ok(r#"{"kind": "read", "refs": ["p1:3"]}"#), Ok(DONE)],
        &[],
        None,
    );
    run(&driver, &brain, FakeHost::new()).await;
    assert!(driver.calls().contains(&call(&["click", "p1:3"])));
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

fn long_pages() -> Driver {
    let mut browser = Browser::default();
    browser.views.insert(
        "https://example.com".to_owned(),
        View {
            url: "https://example.com".to_owned(),
            title: "Long".to_owned(),
            outline: "x".repeat(MAX_OBSERVATION_BYTES),
            elements: Vec::<Element>::new(),
        },
    );
    Driver::new(browser)
}

#[tokio::test(start_paused = true)]
async fn a_thread_past_its_bound_folds_its_oldest_steps_and_keeps_the_rewritten_memory() {
    let mut memories: VecDeque<&str> = std::iter::once("V101: R$ 9.500")
        .chain(std::iter::repeat_n("V101: R$ 9.500\nV102: R$ 7.000", 10))
        .collect();
    let mut steps = 0;
    let brain = FakeBrain::new(move |call, _| {
        let answer = match call {
            BrowserCall::Fold => Value::Object(vec![(
                "memory".to_owned(),
                Value::String(memories.pop_front().expect("a memory").to_owned()),
            )]),
            BrowserCall::Check => json(COMPLETE),
            _ => {
                steps += 1;
                json(if steps <= 12 { WAIT } else { DONE })
            }
        };
        (Ok(answer), Duration::ZERO)
    });
    let outcome = run(&long_pages(), &brain, FakeHost::new()).await;
    assert_eq!(outcome.status, Status::Done);
    let folds = brain.requests(BrowserCall::Fold);
    assert!(folds.len() >= 2);
    assert_eq!(
        folds[1]
            .get("thread")
            .and_then(|thread| thread.get("memory")),
        Some(&json(r#""V101: R$ 9.500""#))
    );
    let requests = brain.requests(BrowserCall::Step);
    let thread = requests
        .last()
        .and_then(|request| request.get("thread"))
        .expect("a thread");
    assert_eq!(
        thread.get("memory"),
        Some(&json(r#""V101: R$ 9.500\nV102: R$ 7.000""#))
    );
    assert_eq!(canonical_thread(thread).as_ref(), Some(thread));
    for request in &requests {
        assert!(history_bytes(request.get("thread").expect("a thread")) <= MAX_HISTORY_BYTES);
    }
}

#[tokio::test(start_paused = true)]
async fn a_fold_whose_memory_could_not_be_written_counts_its_steps_as_unnoted() {
    let mut steps = 0;
    let brain = FakeBrain::new(move |call, _| match call {
        BrowserCall::Fold => (Err(StepFailed), Duration::ZERO),
        BrowserCall::Check => (Ok(json(COMPLETE)), Duration::ZERO),
        _ => {
            steps += 1;
            (
                Ok(json(if steps <= 12 { WAIT } else { DONE })),
                Duration::ZERO,
            )
        }
    });
    run(&long_pages(), &brain, FakeHost::new()).await;
    let number = |request: &Value, name: &str| match request.get(name) {
        Some(Value::Number(strict_json::Number::Int(number))) => *number,
        _ => 0,
    };
    let folded: i64 = brain
        .requests(BrowserCall::Fold)
        .iter()
        .map(|fold| number(fold, "last") - number(fold, "first") + 1)
        .sum();
    let requests = brain.requests(BrowserCall::Step);
    let last = requests
        .last()
        .and_then(|request| request.get("thread"))
        .expect("a thread");
    assert!(folded > 0);
    assert_eq!(
        (last.get("memory"), number(last, "unnoted")),
        (Some(&json(r#""""#)), folded)
    );
}

#[tokio::test(start_paused = true)]
async fn an_answer_citing_an_unshown_url_goes_back_twice_then_says_so() {
    let cited = r#"{"kind": "done", "summary": "Prazo: 30/11 (https://example.com/edital)."}"#;
    let brain = FakeBrain::steps(&[Ok(cited), Ok(cited), Ok(cited)], &[], None);
    let outcome = run(&Driver::default(), &brain, FakeHost::new()).await;
    assert_eq!(
        outcome,
        Outcome {
            unscreened: vec!["https://example.com/edital".to_owned()],
            ..done("Prazo: 30/11 (https://example.com/edital).", 3)
        }
    );
    let returned = last_steps(&brain);
    assert_eq!(outcomes(&returned), ["returned", "returned"]);
    let note = returned[0].get("step").and_then(|step| step.get("note"));
    assert!(matches!(note, Some(Value::String(note)) if note.starts_with(UNSCREENED_NOTE)));
    // A cited page the task observed is screened.
    let brain = FakeBrain::steps(
        &[Ok(
            r#"{"kind": "done", "summary": "Fonte: https://example.com."}"#,
        )],
        &[],
        None,
    );
    assert_eq!(
        run(&Driver::default(), &brain, FakeHost::new())
            .await
            .unscreened,
        Vec::<String>::new()
    );
}

#[tokio::test(start_paused = true)]
async fn an_incomplete_answer_goes_back_four_times_then_travels_with_what_is_missing() {
    let missing = r#"{"complete": false, "missing": "Faltam 3 vagas."}"#;
    let brain = FakeBrain::steps(&[Ok(DONE); 5], &[Ok(missing); 5], None);
    let outcome = run(&Driver::default(), &brain, FakeHost::new()).await;
    let expected = Outcome {
        completeness: Some("incomplete"),
        missing: Some("Faltam 3 vagas.".to_owned()),
        ..done("Prazo: 30/11.", 5)
    };
    assert_eq!(outcome, expected);
    let steps = last_steps(&brain);
    let notes: Vec<Option<&Value>> = steps
        .iter()
        .map(|entry| entry.get("step").and_then(|step| step.get("note")))
        .collect();
    assert_eq!(notes.len(), 4);
    assert!(notes.iter().all(
        |note| matches!(note, Some(Value::String(note)) if note.starts_with(INCOMPLETE_NOTE))
    ));
    // A check that failed or answered outside its form is reported as not checked, never as complete.
    for verdict in [
        Err(StepFailed),
        Ok(r#"{"complete": "yes"}"#),
        Ok(r#"{"complete": null, "missing": null}"#),
    ] {
        let brain = FakeBrain::steps(&[Ok(DONE)], &[verdict], None);
        let outcome = run(&Driver::default(), &brain, FakeHost::new()).await;
        assert_eq!(outcome.completeness, Some("not-checked"));
    }
}

#[tokio::test(start_paused = true)]
async fn past_the_soft_cutoff_the_brain_answers_from_the_thread_and_never_takes_another_step() {
    let last = r#"{"summary": "Encontrei 7 de 10 vagas."}"#;
    let brain = FakeBrain::steps(&[Ok(WAIT)], &[], Some(last));
    let outcome = task(
        &Driver::default(),
        Arc::clone(&brain),
        Arc::new(FakeHost::new()),
        Stop::new(),
        Instant::now() + Duration::from_secs(30),
    )
    .await
    .run()
    .await;
    let expected = Outcome {
        completeness: Some("not-checked"),
        ..done("Encontrei 7 de 10 vagas.", 0)
    };
    assert_eq!(outcome, expected);
    assert_eq!(brain.operations(), [BrowserCall::Final]);
    // The steps run out the same way, and a final answer that could not be written ends with the budget.
    for (last, expected) in [(Some(last), Some("Encontrei 7 de 10 vagas.")), (None, None)] {
        let brain = FakeBrain::steps(&[Ok(WAIT)], &[], last);
        let mut one = task(
            &Driver::default(),
            brain,
            Arc::new(FakeHost::new()),
            Stop::new(),
            Instant::now() + MAX_SECONDS,
        )
        .await;
        one.state.steps = 1;
        let outcome = one.execute().await;
        assert_eq!(outcome.summary.as_deref(), expected);
        if expected.is_none() {
            assert_eq!(outcome, Outcome::new(Status::Budget).steps(1));
        }
    }
}

async fn spent(brain: Arc<FakeBrain>, spend: u64) -> (Outcome, u64) {
    let mut measured = task(
        &Driver::default(),
        brain,
        Arc::new(FakeHost::new()),
        Stop::new(),
        Instant::now() + MAX_SECONDS,
    )
    .await;
    measured.state.spend = spend;
    let outcome = measured.execute().await;
    (outcome, measured.state.spent)
}

#[tokio::test(start_paused = true)]
async fn spend_is_a_ceiling_of_worst_case_reservations_settled_by_reported_usage() {
    let reported = Usage {
        input_tokens: 10_000,
        output_tokens: 1_000,
        ..usage()
    };
    let mut steps = VecDeque::from([json(WAIT), json(DONE)]);
    let brain = FakeBrain::counting(reported, move |call, _| match call {
        BrowserCall::Step => (Ok(steps.pop_front().expect("a step")), Duration::ZERO),
        _ => (Ok(json(COMPLETE)), Duration::ZERO),
    });
    // Three calls (two steps and the check), each settled at the model's price.
    assert_eq!(
        spent(brain, MAX_SPEND).await.1,
        3 * (10_000 * 10 + 1_000 * 50)
    );
    // Attempts whose usage the Brain did not report keep their worst-case reservation.
    let unreported = Usage {
        provider_requests: 3,
        ..reported
    };
    let brain = FakeBrain::counting(unreported, |call, _| match call {
        BrowserCall::Step => (Ok(json(DONE)), Duration::ZERO),
        _ => (Ok(json(COMPLETE)), Duration::ZERO),
    });
    let floor = 2 * (10_000 * 10 + 1_000 * 50) + 2 * u64::from(MAX_CALL_OUTPUT_TOKENS) * 50;
    assert!(spent(brain, MAX_SPEND).await.1 > floor);
    // A step the normal share cannot reserve finishes the task instead of taking it.
    let brain = FakeBrain::steps(&[Ok(DONE)], &[], None);
    assert_eq!(spent(brain, 1_000).await.0, Outcome::new(Status::Budget));
    // A failed call keeps its whole reservation.
    let brain = FakeBrain::steps(&[Err(StepFailed), Err(StepFailed)], &[], None);
    assert!(spent(brain, MAX_SPEND).await.1 > 0);
}

#[tokio::test(start_paused = true)]
async fn stop_budgets_and_failures_end_the_task_with_their_closed_reason() {
    let stop = Stop::new();
    stop.stop();
    let outcome = task(
        &Driver::default(),
        FakeBrain::steps(&[Ok(DONE)], &[], None),
        Arc::new(FakeHost::new()),
        stop,
        Instant::now() + MAX_SECONDS,
    )
    .await
    .run()
    .await;
    assert_eq!(outcome, Outcome::new(Status::Stopped));
    let blind = Driver::new(Browser {
        fail_reads: Some("tool-failed"),
        ..Browser::default()
    });
    let outcome = run(
        &blind,
        &FakeBrain::steps(&[Ok(DONE)], &[], None),
        FakeHost::new(),
    )
    .await;
    assert_eq!(
        outcome,
        failed(
            "browser-lost",
            0,
            Some("observe/tool-failed/reopen/driver-unavailable")
        )
    );
}

#[tokio::test(start_paused = true)]
async fn a_brain_that_cannot_answer_or_answers_outside_the_closed_set_ends_the_task() {
    let down = FakeBrain::steps(&[Err(StepFailed), Err(StepFailed)], &[], None);
    assert_eq!(
        run(&Driver::default(), &down, FakeHost::new()).await,
        failed("model-unavailable", 0, None)
    );
    // One failure is asked again: a model answers outside its form now and then.
    let once = FakeBrain::steps(&[Err(StepFailed), Ok(DONE)], &[], None);
    assert_eq!(
        run(&Driver::default(), &once, FakeHost::new()).await,
        done("Prazo: 30/11.", 1)
    );
    let invalid = FakeBrain::steps(&[Ok(r#"{"kind": "run_script"}"#)], &[], None);
    assert_eq!(
        run(&Driver::default(), &invalid, FakeHost::new()).await,
        failed("step-invalid", 1, None)
    );
    let fatal = Driver::new(Browser {
        fatal: vec!["click"],
        ..Browser::default()
    });
    let click = FakeBrain::steps(&[Ok(r#"{"kind": "click", "ref": "p1:1"}"#)], &[], None);
    assert_eq!(
        run(&fatal, &click, FakeHost::new()).await,
        failed(
            "browser-lost",
            1,
            Some("act/tool-failed/reopen/driver-unavailable")
        )
    );
}

#[tokio::test(start_paused = true)]
async fn every_call_and_action_waits_for_proof_that_the_task_may_continue() {
    let navigate = r#"{"kind": "navigate", "url": "https://example.com/a"}"#;
    // Stop arrives while the Brain answers: its step never reaches the page.
    for (answer, steps) in [(Ok(navigate), 1), (Err(StepFailed), 0)] {
        let stop = Stop::new();
        let stopping = stop.clone();
        let answer = answer.map(json);
        let brain = FakeBrain::new(move |_, _| {
            stopping.stop();
            (answer.clone(), Duration::ZERO)
        });
        let driver = Driver::default();
        let outcome = task(
            &driver,
            brain,
            Arc::new(FakeHost::new()),
            stop,
            Instant::now() + MAX_SECONDS,
        )
        .await
        .run()
        .await;
        // Stop aborts the Brain request in flight: the task was stopped, not failed.
        assert_eq!(outcome, Outcome::new(Status::Stopped).steps(steps));
        assert_eq!(driver.calls(), Vec::<Vec<String>>::new());
    }
    // The Browser left the egress process it started on, before or while the Brain answered.
    let brain = FakeBrain::steps(&[Ok(navigate)], &[], None);
    let outcome = run(&Driver::default(), &brain, FakeHost::new().intact(|| false)).await;
    assert_eq!(
        outcome,
        failed("browser-lost", 0, Some("runtime/not-intact"))
    );
    let lost = Arc::new(Mutex::new(false));
    let losing = Arc::clone(&lost);
    let brain = FakeBrain::new(move |_, _| {
        *losing.lock().expect("lost") = true;
        (Ok(json(navigate)), Duration::ZERO)
    });
    let driver = Driver::default();
    let outcome = run(
        &driver,
        &brain,
        FakeHost::new().intact(move || !*lost.lock().expect("lost")),
    )
    .await;
    assert_eq!(
        outcome,
        failed("browser-lost", 1, Some("runtime/not-intact"))
    );
    assert_eq!(driver.calls(), Vec::<Vec<String>>::new());
}

#[tokio::test(start_paused = true)]
async fn a_failed_driver_session_is_replaced_and_the_task_continues_where_it_was() {
    let failed_driver = Driver::new(Browser {
        fatal: vec!["click"],
        ..Browser::default()
    });
    let fresh = Driver::default();
    let replacement = fresh.clone();
    let host =
        Arc::new(FakeHost::new().reopening(move |relaunch| Ok((replacement.clone(), relaunch))));
    let brain = FakeBrain::steps(
        &[
            Ok(r#"{"kind": "click", "ref": "p1:1"}"#),
            Ok(r#"{"kind": "scroll", "direction": "down"}"#),
            Ok(DONE),
        ],
        &[],
        None,
    );
    let deadline = Instant::now() + MAX_SECONDS;
    let outcome = task(
        &failed_driver,
        Arc::clone(&brain),
        Arc::clone(&host),
        Stop::new(),
        deadline,
    )
    .await
    .run()
    .await;
    assert_eq!(
        outcome,
        Outcome {
            recovered: 1,
            ..done("Prazo: 30/11.", 3)
        }
    );
    assert_eq!(host.reopened(), [(deadline, false)]);
    // The failed session is never used again, and the click whose effect is unknown is in the thread as such.
    assert_eq!(failed_driver.calls(), [call(&["click", "p1:1"])]);
    assert_eq!(fresh.calls(), [call(&["scroll", "down"])]);
    assert_eq!(outcomes(&last_steps(&brain)), ["interrupted", "ok"]);
}

#[tokio::test(start_paused = true)]
async fn an_observation_that_failed_is_looked_at_again_on_a_fresh_session() {
    let fresh = Driver::default();
    let replacement = fresh.clone();
    let blind = Driver::new(Browser {
        fail_reads: Some("tool-failed"),
        ..Browser::default()
    });
    let brain = FakeBrain::steps(&[Ok(DONE)], &[], None);
    let host = FakeHost::new().reopening(move |relaunch| Ok((replacement.clone(), relaunch)));
    let outcome = run(&blind, &brain, host).await;
    assert_eq!(
        outcome,
        Outcome {
            recovered: 1,
            ..done("Prazo: 30/11.", 2)
        }
    );
    assert_eq!(
        brain.requests(BrowserCall::Step)[0]
            .get("thread")
            .and_then(|thread| thread.get("steps")),
        Some(&json("[]"))
    );
    assert_eq!(fresh.browser().screenshots.len(), 1);
}

#[tokio::test(start_paused = true)]
async fn a_page_chromium_no_longer_reads_starts_chromium_again_and_the_agent_is_told() {
    let refusing = || {
        Driver::new(Browser {
            fail_reads: Some("browser-read-refused"),
            ..Browser::default()
        })
    };
    let relaunches = Arc::new(Mutex::new(Vec::new()));
    let seen = Arc::clone(&relaunches);
    let host = FakeHost::new().reopening(move |relaunch| {
        seen.lock().expect("seen").push(relaunch);
        Ok((Driver::default(), relaunch))
    });
    let brain = FakeBrain::steps(&[Ok(DONE)], &[], None);
    let outcome = run(&refusing(), &brain, host).await;
    assert_eq!(
        outcome,
        Outcome {
            recovered: 1,
            relaunched: 1,
            ..done("Prazo: 30/11.", 2)
        }
    );
    assert_eq!(*relaunches.lock().expect("seen"), [true]);
    // Only the next observation says so; the one after it does not.
    assert!(observation_of(&brain.requests(BrowserCall::Step)[0]).starts_with(RESTARTED_NOTICE));
    let brain = FakeBrain::steps(
        &[Ok(r#"{"kind": "scroll", "direction": "down"}"#), Ok(DONE)],
        &[],
        None,
    );
    let host = FakeHost::new().reopening(|relaunch| Ok((Driver::default(), relaunch)));
    run(&refusing(), &brain, host).await;
    assert!(!observation_of(&brain.requests(BrowserCall::Step)[1]).starts_with(RESTARTED_NOTICE));
}

#[tokio::test(start_paused = true)]
async fn a_session_replaced_on_a_chromium_started_again_is_told_and_counted() {
    // Any failure may need Chromium started again: the replacement says whether it did.
    let blind = Driver::new(Browser {
        fail_reads: Some("tool-failed"),
        ..Browser::default()
    });
    let brain = FakeBrain::steps(&[Ok(DONE)], &[], None);
    let host = FakeHost::new().reopening(|_| Ok((Driver::default(), true)));
    let outcome = run(&blind, &brain, host).await;
    assert_eq!(outcome.relaunched, 1);
    assert!(observation_of(&brain.requests(BrowserCall::Step)[0]).starts_with(RESTARTED_NOTICE));
}

#[tokio::test(start_paused = true)]
async fn recoveries_are_bounded_and_a_failed_one_ends_the_task_with_its_cause() {
    let blind = || {
        Driver::new(Browser {
            fail_reads: Some("tool-failed"),
            ..Browser::default()
        })
    };
    let host = FakeHost::new().reopening(move |relaunch| Ok((blind(), relaunch)));
    let outcome = run(&blind(), &FakeBrain::steps(&[Ok(DONE)], &[], None), host).await;
    let steps = usize::try_from(MAX_RECOVERIES).expect("small");
    assert_eq!(
        outcome,
        Outcome {
            recovered: MAX_RECOVERIES,
            ..failed("browser-lost", steps, Some("observe/tool-failed"))
        }
    );
    let host = FakeHost::new().reopening(|_| Err(CuaError::fatal("driver-not-ended")));
    let outcome = run(&blind(), &FakeBrain::steps(&[], &[], None), host).await;
    assert_eq!(
        outcome,
        failed(
            "browser-lost",
            0,
            Some("observe/tool-failed/reopen/driver-not-ended")
        )
    );
}

#[tokio::test(start_paused = true)]
async fn stop_and_a_lost_egress_win_over_a_recovery() {
    let blind = || {
        Driver::new(Browser {
            fail_reads: Some("tool-failed"),
            ..Browser::default()
        })
    };
    // Stop came while the session was being replaced.
    let stop = Stop::new();
    let stopping = stop.clone();
    let host = Arc::new(FakeHost::new().reopening(move |_| {
        stopping.stop();
        Err(CuaError::fatal("driver-unavailable"))
    }));
    let outcome = task(
        &blind(),
        FakeBrain::steps(&[], &[], None),
        Arc::clone(&host),
        stop,
        Instant::now() + MAX_SECONDS,
    )
    .await
    .run()
    .await;
    assert_eq!(outcome, Outcome::new(Status::Stopped));
    assert_eq!(host.reopened().len(), 1);
    // The egress was lost before the session failed: no session is replaced.
    let mut answers = VecDeque::from([true, false]);
    let host = Arc::new(FakeHost::new().intact(move || answers.pop_front().unwrap_or(false)));
    let outcome = task(
        &blind(),
        FakeBrain::steps(&[], &[], None),
        Arc::clone(&host),
        Stop::new(),
        Instant::now() + MAX_SECONDS,
    )
    .await
    .run()
    .await;
    assert_eq!(
        outcome,
        failed("browser-lost", 0, Some("runtime/not-intact"))
    );
    assert_eq!(host.reopened(), []);
}

#[tokio::test(start_paused = true)]
async fn no_call_outlives_the_deadline_the_task_was_given() {
    let deadline = Instant::now() + Duration::from_secs(500);
    let brain = FakeBrain::steps(&[Ok(WAIT), Ok(DONE)], &[], None);
    let mut given = task(
        &Driver::default(),
        Arc::clone(&brain),
        Arc::new(FakeHost::new()),
        Stop::new(),
        deadline,
    )
    .await;
    given.execute().await;
    assert_eq!(
        given.session.as_ref().and_then(CuaSession::deadline),
        Some(deadline)
    );
    let calls = brain.calls();
    assert!(!calls.is_empty());
    assert!(calls.iter().all(|(_, _, until)| *until <= deadline));
}
