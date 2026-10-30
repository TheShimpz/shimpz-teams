use std::collections::VecDeque;
use std::sync::Mutex;

use strict_json::Value;
use team_action::executor::{self, ActionBatch, ActionFailure, InvokeOutcome};
use team_action::result::MAX_RESULT_BYTES;
use team_core::future::BoxFuture;
use team_core::secret::Secret;
use team_core::stop::Stop;
use team_inference::brain::{BrainError, ModelCredential};
use team_inference::runtime::{
    ActionRequest, ConversationEntry, Runtime, RuntimeAction, RuntimeAssistant, RuntimeContext,
    RuntimeTurn, TurnStatus, resume_capacity,
};

use super::{
    ChatError, ChatOutcome, InvokedAction, MAX_ACTION_ROUNDS, MAX_RESUMABLE_ROUNDS, Outcome,
    Strategy, continue_after_pause, run_until_pause,
};
use crate::progress::Reporter;

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("json")
}

fn action(id: &str) -> RuntimeAction {
    RuntimeAction {
        id: id.to_owned(),
        summary: "Return a greeting.".to_owned(),
        input_schema: json(r#"{"type":"object","additionalProperties":false}"#),
        authorization: false,
        input_files: Vec::new(),
        output_schema: json("{}"),
    }
}

fn context_with(assistants: Vec<RuntimeAssistant>) -> RuntimeContext {
    RuntimeContext {
        thread_id: "team:assistant:conversation".to_owned(),
        team_name: "Marketing".to_owned(),
        assistants,
        credential: ModelCredential {
            provider: "openai".to_owned(),
            model: "gpt-test".to_owned(),
            api_key: Secret::new("not-a-real-key".to_owned()),
        },
        memories: None,
        routines: None,
        routine_question: None,
        routine_mode: false,
        routine_rerun: None,
        routine_schedule: None,
        routine_output: None,
        routine_settings_answer: false,
        routine_selected: None,
        locale: None,
        turn_clock: None,
        attachments: Vec::new(),
        browser: false,
    }
}

fn context_of(actions: Vec<RuntimeAction>) -> RuntimeContext {
    context_with(vec![RuntimeAssistant {
        id: "hello-pulse".to_owned(),
        genesis: "Compose the declared greeting Action into one bounded welcome.".to_owned(),
        actions,
    }])
}

fn context() -> RuntimeContext {
    context_of(vec![action("hello")])
}

fn completed(reply: &str) -> RuntimeTurn {
    RuntimeTurn {
        status: TurnStatus::Completed,
        reply: reply.to_owned(),
        actions: Vec::new(),
        clarification: None,
        memory: Vec::new(),
        routine: None,
        browser: None,
    }
}

fn request(action: &str, assistant_id: &str, interrupt_id: &str) -> ActionRequest {
    ActionRequest {
        interrupt_id: interrupt_id.to_owned(),
        assistant_id: assistant_id.to_owned(),
        action: action.to_owned(),
        input: json(r#"{"name":"Ada"}"#),
    }
}

fn suspension(requests: Vec<ActionRequest>) -> RuntimeTurn {
    RuntimeTurn {
        status: TurnStatus::ActionRequired,
        reply: String::new(),
        actions: requests,
        ..completed("")
    }
}

fn suspended(interrupt_id: &str) -> RuntimeTurn {
    suspension(vec![request("hello", "hello-pulse", interrupt_id)])
}

fn action_batch(round: usize, count: usize) -> RuntimeTurn {
    suspension(
        (0..count)
            .map(|index| ActionRequest {
                interrupt_id: format!("interrupt-{round}-{index}"),
                assistant_id: "hello-pulse".to_owned(),
                action: "hello".to_owned(),
                input: json(&format!(r#"{{"name":"Ada {index}"}}"#)),
            })
            .collect(),
    )
}

/// A Brain that answers queued turns and records every resume's results.
struct FakeRuntime {
    turns: Mutex<VecDeque<Result<RuntimeTurn, BrainError>>>,
    resumes: Mutex<Vec<Value>>,
}

impl FakeRuntime {
    fn new(turns: Vec<RuntimeTurn>) -> Self {
        Self {
            turns: Mutex::new(turns.into_iter().map(Ok).collect()),
            resumes: Mutex::new(Vec::new()),
        }
    }

    fn failing_resume(turns: Vec<RuntimeTurn>) -> Self {
        let runtime = Self::new(turns);
        runtime
            .turns
            .lock()
            .expect("turns")
            .push_back(Err(BrainError::Unavailable));
        runtime
    }

    fn next(&self) -> Result<RuntimeTurn, BrainError> {
        self.turns
            .lock()
            .expect("turns")
            .pop_front()
            .unwrap_or(Err(BrainError::Unavailable))
    }

    fn resumes(&self) -> Vec<Value> {
        self.resumes.lock().expect("resumes").clone()
    }
}

impl Runtime for FakeRuntime {
    fn start<'a>(
        &'a self,
        _context: &'a RuntimeContext,
        _message: &'a str,
        _conversation: &'a [ConversationEntry],
        _stop: &'a Stop,
    ) -> BoxFuture<'a, Result<RuntimeTurn, BrainError>> {
        Box::pin(async move { self.next() })
    }

    fn resume<'a>(
        &'a self,
        _context: &'a RuntimeContext,
        results: Value,
        _stop: &'a Stop,
    ) -> BoxFuture<'a, Result<RuntimeTurn, BrainError>> {
        self.resumes.lock().expect("resumes").push(results);
        Box::pin(async move { self.next() })
    }

    fn purpose<'a>(
        &'a self,
        _context: &'a RuntimeContext,
        _request: &'a ActionRequest,
        _assistant_name: &'a str,
        _summary: &'a str,
        _stop: &'a Stop,
    ) -> BoxFuture<'a, Option<String>> {
        Box::pin(async { None })
    }
}

type Invoke =
    Box<dyn Fn(&executor::ActionRequest) -> Result<InvokeOutcome, ActionFailure> + Send + Sync>;

/// A durable batch that logs every hook.
struct FakeBatch {
    events: Vec<String>,
    invoke: Invoke,
    fail_prepare: bool,
}

impl FakeBatch {
    fn answering(result: &str) -> Self {
        let result = json(result);
        Self::with(Box::new(move |_| {
            Ok(InvokeOutcome::Completed(result.clone()))
        }))
    }

    fn with(invoke: Invoke) -> Self {
        Self {
            events: Vec::new(),
            invoke,
            fail_prepare: false,
        }
    }

    fn invoked(&self) -> Vec<String> {
        self.events
            .iter()
            .filter_map(|event| event.strip_prefix("invoke ").map(str::to_owned))
            .collect()
    }
}

impl ActionBatch for FakeBatch {
    fn prepare<'a>(
        &'a mut self,
        requests: &'a [executor::ActionRequest],
    ) -> BoxFuture<'a, Result<(), ActionFailure>> {
        Box::pin(async move {
            if self.fail_prepare {
                return Err(ActionFailure::StateUnavailable);
            }
            let inputs: Vec<String> = requests
                .iter()
                .map(|request| format!("{}={:?}", request.interrupt_id, request.input))
                .collect();
            self.events.push(format!("prepare {}", inputs.join(",")));
            Ok(())
        })
    }

    fn invoke<'a>(
        &'a mut self,
        request: &'a executor::ActionRequest,
        _stop: &'a Stop,
    ) -> BoxFuture<'a, Result<InvokeOutcome, ActionFailure>> {
        Box::pin(async move {
            self.events.push(format!("invoke {}", request.interrupt_id));
            (self.invoke)(request)
        })
    }

    fn delivered<'a>(
        &'a mut self,
        requests: &'a [executor::ActionRequest],
    ) -> BoxFuture<'a, Result<(), ActionFailure>> {
        Box::pin(async move {
            let ids: Vec<&str> = requests
                .iter()
                .map(|request| request.interrupt_id.as_str())
                .collect();
            self.events.push(format!("delivered {}", ids.join(",")));
            Ok(())
        })
    }

    fn terminate(&mut self) -> BoxFuture<'_, Result<bool, ActionFailure>> {
        Box::pin(async { Ok(true) })
    }
}

type Validate = Box<dyn Fn(&str, &str, &Value) -> Result<Value, ChatError> + Send + Sync>;

/// A controller adapter with counted context validations and scripted pauses.
struct Fake {
    stop: Stop,
    progress: Reporter,
    validations: Mutex<usize>,
    validated: Mutex<Vec<String>>,
    pauses: Mutex<VecDeque<bool>>,
    validate: Validate,
    drift: bool,
    max_rounds: usize,
    record_invoked: bool,
}

impl Default for Fake {
    fn default() -> Self {
        Self {
            stop: Stop::new(),
            progress: Reporter::silent(),
            validations: Mutex::new(0),
            validated: Mutex::new(Vec::new()),
            pauses: Mutex::new(VecDeque::new()),
            validate: Box::new(|_, _, input| Ok(input.clone())),
            drift: false,
            max_rounds: MAX_ACTION_ROUNDS,
            record_invoked: true,
        }
    }
}

impl Fake {
    fn pausing(pauses: &[bool]) -> Self {
        Self {
            pauses: Mutex::new(pauses.iter().copied().collect()),
            ..Self::default()
        }
    }

    fn validations(&self) -> usize {
        *self.validations.lock().expect("validations")
    }
}

impl Strategy for Fake {
    fn stop(&self) -> &Stop {
        &self.stop
    }

    fn validate_context(&self) -> BoxFuture<'_, Result<(), ChatError>> {
        *self.validations.lock().expect("validations") += 1;
        let drift = self.drift;
        Box::pin(async move {
            if drift {
                Err(ChatError::ContextChanged)
            } else {
                Ok(())
            }
        })
    }

    fn validate_action(
        &self,
        assistant_id: &str,
        action: &RuntimeAction,
        input: &Value,
    ) -> Result<Value, ChatError> {
        self.validated
            .lock()
            .expect("validated")
            .push(format!("{assistant_id}/{}", action.id));
        (self.validate)(assistant_id, &action.id, input)
    }

    fn pause_before_batch<'a>(
        &'a self,
        _batch: &'a [ActionRequest],
    ) -> BoxFuture<'a, Result<bool, ChatError>> {
        let pause = self
            .pauses
            .lock()
            .expect("pauses")
            .pop_front()
            .unwrap_or(false);
        Box::pin(async move { Ok(pause) })
    }

    fn progress(&self) -> &Reporter {
        &self.progress
    }

    fn max_rounds(&self) -> usize {
        self.max_rounds
    }

    fn record_invoked(&self) -> bool {
        self.record_invoked
    }
}

async fn run(
    runtime: &FakeRuntime,
    context: &RuntimeContext,
    strategy: &Fake,
    batch: &mut FakeBatch,
) -> Result<ChatOutcome, ChatError> {
    match run_until_pause(runtime, context, "Hello", &[], strategy, batch).await? {
        Outcome::Completed(outcome) => Ok(outcome),
        other => panic!("the turn paused: {other:?}"),
    }
}

fn approval() -> Value {
    json(r#"{"kind":"approval","ordinal":0,"fingerprint":"aaaa"}"#)
}

#[tokio::test]
async fn a_human_request_pauses_without_resuming_the_brain_or_running_later_actions() {
    let runtime = FakeRuntime::new(vec![
        suspension(vec![
            request("hello", "hello-pulse", "first"),
            request("hello", "hello-pulse", "second"),
        ]),
        completed("Done"),
    ]);
    let mut batch = FakeBatch::with(Box::new(|request| {
        Ok(if request.interrupt_id == "first" {
            InvokeOutcome::HumanRequest(approval())
        } else {
            InvokeOutcome::Completed(json("{}"))
        })
    }));
    let outcome = run_until_pause(
        &runtime,
        &context(),
        "Use both",
        &[],
        &Fake::default(),
        &mut batch,
    )
    .await;
    let Ok(Outcome::Human(paused)) = outcome else {
        panic!("not a human pause: {outcome:?}");
    };
    assert_eq!(paused.action.interrupt_id, "first");
    assert_eq!(paused.request, approval());
    assert_eq!(paused.continuation.round_index, 0);
    assert_eq!(paused.completed_interrupts, Vec::<String>::new());
    assert_eq!(batch.invoked(), vec!["first"]);
    assert_eq!(runtime.resumes().len(), 0);
}

#[tokio::test]
async fn a_human_pause_names_the_actions_completed_before_it_in_the_same_batch() {
    let runtime = FakeRuntime::new(vec![suspension(vec![
        request("hello", "hello-pulse", "first"),
        request("hello", "hello-pulse", "second"),
    ])]);
    let mut batch = FakeBatch::with(Box::new(|request| {
        Ok(if request.interrupt_id == "second" {
            InvokeOutcome::HumanRequest(approval())
        } else {
            InvokeOutcome::Completed(json("{}"))
        })
    }));
    let outcome = run_until_pause(
        &runtime,
        &context(),
        "Use both",
        &[],
        &Fake::default(),
        &mut batch,
    )
    .await;
    let Ok(Outcome::Human(paused)) = outcome else {
        panic!("not a human pause");
    };
    assert_eq!(paused.completed_interrupts, vec!["first"]);
}

#[tokio::test]
async fn a_direct_reply_never_invokes_an_action() {
    let runtime = FakeRuntime::new(vec![completed("Hello")]);
    let mut batch = FakeBatch::answering("{}");
    let outcome = run(&runtime, &context(), &Fake::default(), &mut batch)
        .await
        .expect("outcome");
    assert_eq!(
        (outcome.reply.as_str(), outcome.actions.len()),
        ("Hello", 0)
    );
    assert_eq!(batch.events.len(), 0);
}

#[tokio::test]
async fn an_action_result_is_returned_to_the_model_before_the_final_reply() {
    let runtime = FakeRuntime::new(vec![suspended("interrupt-1"), completed("Hello, Ada.")]);
    let mut batch = FakeBatch::answering(r#"{"message":"Hello, Ada."}"#);
    let outcome = run(&runtime, &context(), &Fake::default(), &mut batch)
        .await
        .expect("outcome");
    assert_eq!(batch.invoked(), vec!["interrupt-1"]);
    assert_eq!(
        runtime.resumes(),
        vec![json(r#"{"interrupt-1":{"message":"Hello, Ada."}}"#)]
    );
    assert_eq!(outcome.reply, "Hello, Ada.");
    assert_eq!(
        outcome.actions,
        vec![InvokedAction {
            assistant_id: "hello-pulse".to_owned(),
            action: "hello".to_owned()
        }]
    );
}

#[tokio::test]
async fn a_pause_happens_after_full_validation_and_before_any_side_effect() {
    let runtime = FakeRuntime::new(vec![
        suspension(vec![
            request("hello", "hello-pulse", "first"),
            request("hello", "hello-pulse", "second"),
        ]),
        completed("Finished"),
    ]);
    let pausing = Fake::pausing(&[true]);
    let mut batch = FakeBatch::answering(r#"{"ok":true}"#);
    let paused = run_until_pause(&runtime, &context(), "Use", &[], &pausing, &mut batch).await;
    let Ok(Outcome::Suspended(paused)) = paused else {
        panic!("not suspended");
    };
    assert_eq!(pausing.validated.lock().expect("validated").len(), 2);
    assert_eq!(paused.requests.len(), 2);
    assert_eq!((batch.events.len(), runtime.resumes().len()), (0, 0));
    let resumed = continue_after_pause(
        &runtime,
        &context(),
        paused.continuation,
        &Fake::default(),
        &mut batch,
    )
    .await;
    let Ok(Outcome::Completed(outcome)) = resumed else {
        panic!("not completed");
    };
    assert_eq!(outcome.reply, "Finished");
    assert_eq!(batch.invoked(), vec!["first", "second"]);
    assert!(batch.events[0].starts_with("prepare "));
    let resumed = &runtime.resumes()[0];
    assert!(resumed.get("first").is_some() && resumed.get("second").is_some());
}

#[tokio::test]
async fn a_paused_batch_revalidates_and_rejects_context_drift_before_invocation() {
    let runtime = FakeRuntime::new(vec![suspended("interrupt-1"), completed("Done")]);
    let mut batch = FakeBatch::answering("{}");
    let paused = run_until_pause(
        &runtime,
        &context(),
        "Use",
        &[],
        &Fake::pausing(&[true]),
        &mut batch,
    )
    .await;
    let Ok(Outcome::Suspended(paused)) = paused else {
        panic!("not suspended");
    };
    let drifted = Fake {
        validate: Box::new(|_, _, _| Err(ChatError::ContextChanged)),
        ..Fake::default()
    };
    let refused = continue_after_pause(
        &runtime,
        &context(),
        paused.continuation,
        &drifted,
        &mut batch,
    )
    .await;
    assert_eq!(refused, Err(ChatError::ContextChanged));
    assert_eq!((batch.events.len(), runtime.resumes().len()), (0, 0));
}

#[tokio::test]
async fn multiple_rounds_remain_bounded_and_a_repeated_interrupt_never_replays() {
    let runtime = FakeRuntime::new(vec![suspended("one"), suspended("two"), completed("Done")]);
    let mut batch = FakeBatch::answering(r#"{"message":"ok"}"#);
    let outcome = run(&runtime, &context(), &Fake::default(), &mut batch)
        .await
        .expect("outcome");
    assert_eq!(outcome.actions.len(), 2);
    assert_eq!(runtime.resumes().len(), 2);

    let runtime = FakeRuntime::new(vec![suspended("interrupt-1"), suspended("interrupt-1")]);
    let mut batch = FakeBatch::answering(r#"{"message":"ok"}"#);
    let refused = run(&runtime, &context(), &Fake::default(), &mut batch).await;
    assert_eq!(
        refused,
        Err(ChatError::Orchestration(
            "Brain repeated an Action interrupt across rounds"
        ))
    );
    assert_eq!(batch.invoked(), vec!["interrupt-1"]);
    assert_eq!(runtime.resumes().len(), 1);
    // The repeated batch was never acknowledged.
    assert!(
        !batch
            .events
            .iter()
            .any(|event| event.starts_with("delivered"))
    );
}

#[tokio::test]
async fn a_batch_whose_worst_case_results_cannot_fit_one_resume_fails_before_any_invocation() {
    let capacity = resume_capacity(&context(), MAX_RESULT_BYTES);
    assert_eq!(capacity, 7);
    let runtime = FakeRuntime::new(vec![action_batch(0, capacity + 1)]);
    let mut batch = FakeBatch::answering("{}");
    let refused = run(&runtime, &context(), &Fake::default(), &mut batch).await;
    assert_eq!(
        refused,
        Err(ChatError::Orchestration(
            "Action batch results could exceed the Brain request"
        ))
    );
    assert_eq!(batch.events.len(), 0);
    // The serialized turn context counts too: large attachments leave room for fewer results.
    let mut heavy = context();
    heavy.attachments = vec![json(&format!(
        r#"{{"id":"{}","content":{{"type":"opaque","pad":"{}"}}}}"#,
        "a".repeat(32),
        "x".repeat(1_400_000)
    ))];
    assert_eq!(resume_capacity(&heavy, MAX_RESULT_BYTES), 5);
}

#[tokio::test]
async fn an_undeclared_or_invalid_later_request_prevents_every_batch_side_effect() {
    for later in ["shell", "hello"] {
        let runtime = FakeRuntime::new(vec![suspension(vec![
            request("hello", "hello-pulse", "first"),
            request(
                later,
                "hello-pulse",
                if later == "hello" { "first" } else { "second" },
            ),
        ])]);
        let mut batch = FakeBatch::answering("{}");
        let refused = run(&runtime, &context(), &Fake::default(), &mut batch).await;
        assert!(
            matches!(refused, Err(ChatError::Orchestration(_))),
            "{later}"
        );
        assert_eq!(batch.events.len(), 0);
    }
    let runtime = FakeRuntime::new(vec![suspension(vec![request(
        "shell",
        "hello-pulse",
        "only",
    )])]);
    let mut batch = FakeBatch::answering("{}");
    assert_eq!(
        run(&runtime, &context(), &Fake::default(), &mut batch).await,
        Err(ChatError::Orchestration(
            "Brain requested an undeclared Action contract"
        ))
    );
}

#[tokio::test]
async fn every_batch_input_is_validated_before_the_first_side_effect() {
    let runtime = FakeRuntime::new(vec![suspension(vec![
        request("hello", "hello-pulse", "first"),
        request("lookup", "hello-pulse", "second"),
    ])]);
    let refusal = super::Refusal {
        status: 422,
        message: "invalid lookup input",
        code: "invalid-action-input",
    };
    let strategy = Fake {
        validate: Box::new(move |_, action, input| {
            if action == "lookup" {
                Err(ChatError::Refused(refusal))
            } else {
                Ok(input.clone())
            }
        }),
        ..Fake::default()
    };
    let mut batch = FakeBatch::answering("{}");
    let refused = run(
        &runtime,
        &context_of(vec![action("hello"), action("lookup")]),
        &strategy,
        &mut batch,
    )
    .await;
    assert_eq!(refused, Err(ChatError::Refused(refusal)));
    assert_eq!(
        *strategy.validated.lock().expect("validated"),
        vec!["hello-pulse/hello", "hello-pulse/lookup"]
    );
    assert_eq!(batch.events.len(), 0);
}

#[tokio::test]
async fn a_cancelled_turn_never_starts_or_resumes_work() {
    let runtime = FakeRuntime::new(vec![completed("Done")]);
    let strategy = Fake::default();
    strategy.stop.stop();
    let mut batch = FakeBatch::answering("{}");
    assert_eq!(
        run(&runtime, &context(), &strategy, &mut batch).await,
        Err(ChatError::Stopped)
    );
    assert_eq!(strategy.validations(), 0);
    assert_eq!(runtime.turns.lock().expect("turns").len(), 1);
}

#[tokio::test]
async fn a_stop_during_an_action_discards_its_result_and_never_resumes() {
    let runtime = FakeRuntime::new(vec![suspended("interrupt-1"), completed("Done")]);
    let strategy = Fake::default();
    let stop = strategy.stop.clone();
    let mut batch = FakeBatch::with(Box::new(move |_| {
        stop.stop();
        Ok(InvokeOutcome::Completed(json("{}")))
    }));
    assert_eq!(
        run(&runtime, &context(), &strategy, &mut batch).await,
        Err(ChatError::Stopped)
    );
    assert_eq!(runtime.resumes().len(), 0);
    // A failure while stopped is the stopped turn itself.
    let runtime = FakeRuntime::new(vec![suspended("interrupt-1")]);
    let strategy = Fake::default();
    let stop = strategy.stop.clone();
    let mut batch = FakeBatch::with(Box::new(move |_| {
        stop.stop();
        Err(ActionFailure::Uncertain)
    }));
    assert_eq!(
        run(&runtime, &context(), &strategy, &mut batch).await,
        Err(ChatError::Stopped)
    );
}

#[tokio::test]
async fn the_action_round_limit_stops_an_unbounded_model_loop() {
    let turns = (0..=MAX_ACTION_ROUNDS)
        .map(|index| suspended(&format!("interrupt-{index}")))
        .collect();
    let runtime = FakeRuntime::new(turns);
    let mut batch = FakeBatch::answering(r#"{"message":"ok"}"#);
    assert_eq!(
        run(&runtime, &context(), &Fake::default(), &mut batch).await,
        Err(ChatError::Orchestration(
            "Brain exceeded the Action round limit"
        ))
    );
}

#[tokio::test]
async fn a_turn_with_its_own_round_limit_runs_every_round_and_may_keep_no_invoked_actions() {
    let rounds = MAX_ACTION_ROUNDS * 3;
    let mut turns: Vec<_> = (0..rounds)
        .map(|index| suspended(&format!("interrupt-{index}")))
        .collect();
    turns.push(completed("Done"));
    let replay = Fake {
        max_rounds: rounds,
        record_invoked: false,
        ..Fake::default()
    };
    let mut batch = FakeBatch::answering(r#"{"message":"ok"}"#);
    let outcome = run(&FakeRuntime::new(turns), &context(), &replay, &mut batch)
        .await
        .expect("outcome");
    assert_eq!((outcome.reply.as_str(), outcome.actions.len()), ("Done", 0));
    let turns = (0..=rounds)
        .map(|index| suspended(&format!("interrupt-{index}")))
        .collect();
    let limited = Fake {
        max_rounds: rounds,
        ..Fake::default()
    };
    assert_eq!(
        run(&FakeRuntime::new(turns), &context(), &limited, &mut batch).await,
        Err(ChatError::Orchestration(
            "Brain exceeded the Action round limit"
        ))
    );
    // A Routine plan has at most 256 steps (team-routine `plan::MAX_STEPS`; the controller checks the two agree).
    assert_eq!(MAX_RESUMABLE_ROUNDS, 256);
}

#[tokio::test]
async fn two_assistants_can_own_the_same_local_action_id() {
    let shared = action("lookup");
    let team = context_with(vec![
        RuntimeAssistant {
            id: "places".to_owned(),
            genesis: "Resolve places first.".to_owned(),
            actions: vec![shared.clone()],
        },
        RuntimeAssistant {
            id: "weather".to_owned(),
            genesis: "Use resolved places for weather.".to_owned(),
            actions: vec![shared],
        },
    ]);
    let runtime = FakeRuntime::new(vec![
        suspension(vec![request("lookup", "places", "place-1")]),
        suspension(vec![request("lookup", "weather", "weather-1")]),
        completed("Integrated."),
    ]);
    let mut batch = FakeBatch::answering(r#"{"ok":true}"#);
    let outcome = run(&runtime, &team, &Fake::default(), &mut batch)
        .await
        .expect("outcome");
    let owners: Vec<&str> = outcome
        .actions
        .iter()
        .map(|item| item.assistant_id.as_str())
        .collect();
    assert_eq!(owners, vec!["places", "weather"]);
}

#[tokio::test]
async fn the_context_validation_count_preserves_every_security_boundary() {
    let shapes: [(&[usize], bool, usize); 7] = [
        (&[], false, 2),
        (&[1], false, 5),
        (&[4], false, 8),
        (&[1, 1, 1, 1], false, 14),
        (&[7], false, 11),
        (&[1, 1, 1, 1, 1, 1, 1, 1], false, 26),
        (&[1], true, 6),
    ];
    for (sizes, pause, expected) in shapes {
        let mut turns: Vec<_> = sizes
            .iter()
            .enumerate()
            .map(|(round, count)| action_batch(round, *count))
            .collect();
        turns.push(completed("Done"));
        let runtime = FakeRuntime::new(turns);
        let strategy = Fake::pausing(if pause { &[true, false] } else { &[] });
        let mut batch = FakeBatch::answering(r#"{"message":"ok"}"#);
        let mut outcome =
            run_until_pause(&runtime, &context(), "Exercise", &[], &strategy, &mut batch)
                .await
                .expect("outcome");
        if let Outcome::Suspended(paused) = outcome {
            assert!(pause);
            outcome = continue_after_pause(
                &runtime,
                &context(),
                paused.continuation,
                &strategy,
                &mut batch,
            )
            .await
            .expect("outcome");
        }
        assert!(matches!(outcome, Outcome::Completed(_)));
        assert_eq!(strategy.validations(), expected, "{sizes:?} {pause}");
    }
}

#[tokio::test]
async fn context_drift_ends_the_turn_before_its_brain_start() {
    let runtime = FakeRuntime::new(vec![completed("Done")]);
    let drifted = Fake {
        drift: true,
        ..Fake::default()
    };
    let mut batch = FakeBatch::answering("{}");
    assert_eq!(
        run(&runtime, &context(), &drifted, &mut batch).await,
        Err(ChatError::ContextChanged)
    );
    assert_eq!(runtime.turns.lock().expect("turns").len(), 1);
}

#[tokio::test]
async fn batch_hooks_receive_normalized_requests_around_resume() {
    let runtime = FakeRuntime::new(vec![suspended("interrupt-1"), completed("Done")]);
    let normalizing = Fake {
        validate: Box::new(|_, _, _| Ok(json(r#"{"name":"Normalized"}"#))),
        ..Fake::default()
    };
    let mut batch = FakeBatch::answering(r#"{"message":"ok"}"#);
    run(&runtime, &context(), &normalizing, &mut batch)
        .await
        .expect("outcome");
    assert_eq!(
        batch.events,
        vec![
            format!("prepare interrupt-1={:?}", json(r#"{"name":"Normalized"}"#)),
            "invoke interrupt-1".to_owned(),
            "delivered interrupt-1".to_owned(),
        ]
    );
    assert_eq!(
        runtime.resumes(),
        vec![json(r#"{"interrupt-1":{"message":"ok"}}"#)]
    );
}

#[tokio::test]
async fn a_prepare_failure_stops_before_invoke_resume_or_delivery() {
    let runtime = FakeRuntime::new(vec![suspended("interrupt-1"), completed("Done")]);
    let mut batch = FakeBatch::answering("{}");
    batch.fail_prepare = true;
    assert_eq!(
        run(&runtime, &context(), &Fake::default(), &mut batch).await,
        Err(ChatError::Action(ActionFailure::StateUnavailable))
    );
    assert_eq!((batch.events.len(), runtime.resumes().len()), (0, 0));
}

#[tokio::test]
async fn a_resume_failure_never_marks_the_batch_delivered() {
    let runtime = FakeRuntime::failing_resume(vec![suspended("interrupt-1")]);
    let mut batch = FakeBatch::answering(r#"{"message":"ok"}"#);
    assert_eq!(
        run(&runtime, &context(), &Fake::default(), &mut batch).await,
        Err(ChatError::Brain(BrainError::Unavailable))
    );
    assert!(
        !batch
            .events
            .iter()
            .any(|event| event.starts_with("delivered"))
    );
}

#[tokio::test]
async fn a_question_after_actions_ran_is_refused() {
    let mut asked = completed("Qual?");
    asked.clarification = Some(json(r#"{"question":"Qual?"}"#));
    let runtime = FakeRuntime::new(vec![suspended("interrupt-1"), asked]);
    let mut batch = FakeBatch::answering("{}");
    assert_eq!(
        run(&runtime, &context(), &Fake::default(), &mut batch).await,
        Err(ChatError::Orchestration(
            "Brain asked a clarification after Actions ran"
        ))
    );
}

#[tokio::test]
async fn the_browser_is_admitted_only_where_it_was_offered() {
    let mut browsing = completed("");
    browsing.status = TurnStatus::BrowserRequired;
    browsing.browser = Some(team_inference::runtime::BrowserRequest {
        interrupt_id: "b-1".to_owned(),
        objective: "Find it.".to_owned(),
    });
    let runtime = FakeRuntime::new(vec![browsing]);
    let mut batch = FakeBatch::answering("{}");
    assert_eq!(
        run(&runtime, &context(), &Fake::default(), &mut batch).await,
        Err(ChatError::Orchestration(
            "Brain called the Browser where it was not offered"
        ))
    );
}

#[tokio::test]
async fn a_file_taking_action_receives_the_files_its_turn_selected() {
    let id = "0123456789abcdef0123456789abcdef";
    let mut context = context_of(vec![RuntimeAction {
        input_files: vec!["document".to_owned()],
        ..action("upload")
    }]);
    context.attachments = vec![json(&format!(
        r#"{{"id":"{id}","name":"report.pdf","media_type":"application/pdf","size":25,"sha256":"{}","content":{{"type":"opaque","reason":"unsupported"}}}}"#,
        "a".repeat(64)
    ))];
    let upload = ActionRequest {
        input: json(&format!(r#"{{"document":"{id}"}}"#)),
        ..request("upload", "hello-pulse", "interrupt-1")
    };
    let runtime = FakeRuntime::new(vec![suspension(vec![upload]), completed("Done")]);
    let mut batch = FakeBatch::with(Box::new(|request| {
        let ids: Vec<&str> = request.files.iter().map(|file| file.id.as_str()).collect();
        assert_eq!(ids, ["0123456789abcdef0123456789abcdef"]);
        Ok(InvokeOutcome::Completed(json("{}")))
    }));
    run(&runtime, &context, &Fake::default(), &mut batch)
        .await
        .expect("outcome");
    assert_eq!(batch.invoked(), ["interrupt-1"]);
}
