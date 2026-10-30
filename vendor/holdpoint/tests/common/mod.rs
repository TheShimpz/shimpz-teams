//! Shared fixtures of the integration tests: a definition that executes a plan of steps from its input.

#![allow(dead_code, reason = "each test binary uses a different part of the shared fixtures")]

use std::sync::{Arc, Mutex};

use holdpoint::testing::{RecordingActivity, ScriptedModel, TestHost};
use holdpoint::{
    ActivityOptions, Authority, Credentials, Ctx, Definition, Host, Interrupt, Limits, ModelOptions, Obligation,
    Outcome, Proof, RetryPolicy, RunInfo, StepResult, Store, Timeouts, Worker, WorkerOptions,
};
use serde_json::{Value, json};

/// Executes `input.steps` in order; each step is an object with a `kind` and its parameters.
pub(crate) struct Plan {
    pub(crate) version: u32,
}

impl Definition for Plan {
    fn id(&self) -> &'static str {
        "plan"
    }

    fn version(&self) -> u32 {
        self.version
    }

    fn run(&self, ctx: &mut Ctx<'_>) -> Result<Outcome, Interrupt> {
        let steps = ctx.input()["steps"].as_array().cloned().unwrap_or_default();
        let mut results = Vec::new();
        for step in steps {
            let result = execute(ctx, &step, results.len())?;
            results.push(result);
            ctx.set_progress(json!({ "done": results.len() }));
        }
        Ok(Outcome::Done { result: Value::Array(results) })
    }
}

fn shown(result: StepResult) -> Value {
    match result {
        StepResult::Ok(output) if output.blobs.is_empty() => output.value,
        StepResult::Ok(output) => json!({ "value": output.value, "blobs": output.blobs }),
        StepResult::Failed(failure) => json!({ "failed": failure.code }),
        StepResult::Uncertain => json!("uncertain"),
    }
}

fn stepped(result: Result<holdpoint::Output, holdpoint::Failure>) -> StepResult {
    result.map_or_else(StepResult::Failed, StepResult::Ok)
}

fn execute(ctx: &mut Ctx<'_>, step: &Value, done: usize) -> Result<Value, Interrupt> {
    let name = step["name"].as_str().unwrap_or("default");
    match step["kind"].as_str() {
        Some("model") => {
            let release: Vec<u64> =
                step["release"].as_array().map_or_else(Vec::new, |ids| ids.iter().filter_map(Value::as_u64).collect());
            let blob = step["blob"].as_u64();
            Ok(shown(stepped(ctx.model(name, &release, |blobs| {
                let image = match blob {
                    Some(id) => blobs.get(id)?.map(|data| data.len()),
                    None => None,
                };
                Ok(json!({ "after": done, "prompt": step["prompt"], "image_bytes": image }))
            })?)))
        }
        Some("activity") => Ok(shown(ctx.activity(name, step["input"].clone())?)),
        Some("local") => {
            Ok(shown(ctx.local(name, &step["input"], |input| Ok(json!(input.as_i64().unwrap_or(0) * 2)))?))
        }
        Some("deferred") => {
            let requests: Vec<(&str, Value)> = step["requests"]
                .as_array()
                .map(|requests| requests.iter().map(|request| (name, request.clone())).collect())
                .unwrap_or_default();
            Ok(Value::Array(ctx.deferred(&requests)?))
        }
        Some("spent") => Ok(json!(ctx.spent())),
        Some("blob") => Ok(json!(ctx.blob(step["id"].as_u64().unwrap_or(0))?.map(|data| data.len()))),
        Some("panic") => {
            Ok(shown(ctx.local(name, &step["input"], |_| std::panic::resume_unwind(Box::new("local bug")))?))
        }
        Some("sleep") => {
            ctx.sleep_until(name, step["due_ms"].as_i64().unwrap_or(0))?;
            Ok(json!("slept"))
        }
        _ => Err(Interrupt::Unrecoverable("bad-plan".into())),
    }
}

pub(crate) struct Fixture {
    pub(crate) worker: Arc<Worker>,
    pub(crate) store: Arc<Store>,
    pub(crate) host: TestHost,
    pub(crate) model: ScriptedModel,
    pub(crate) activity: RecordingActivity,
}

pub(crate) fn fast_options() -> WorkerOptions {
    WorkerOptions {
        lease_ms: 2_000,
        inline_wait_ms: 200,
        poll_ms: 5,
        abandon_grace_ms: 50,
        max_abandoned: 16,
        proof_retry_ms: 20,
        concurrency: 4,
    }
}

pub(crate) fn model_options() -> ModelOptions {
    ModelOptions {
        retry: RetryPolicy { initial_ms: 1, max_interval_ms: 5, max_attempts: Some(5), ..RetryPolicy::default() },
        timeouts: Timeouts { start_to_close_ms: Some(2_000), ..Timeouts::default() },
    }
}

pub(crate) struct Setup {
    pub(crate) store: Arc<Store>,
    pub(crate) host: TestHost,
    pub(crate) model: ScriptedModel,
    pub(crate) activity: RecordingActivity,
    pub(crate) activity_options: ActivityOptions,
    pub(crate) options: WorkerOptions,
    pub(crate) version: u32,
}

impl Setup {
    pub(crate) fn new(store: Arc<Store>) -> Self {
        Self {
            store,
            host: TestHost::default(),
            model: ScriptedModel::default(),
            activity: RecordingActivity::new([holdpoint::testing::Behavior::Echo]),
            activity_options: ActivityOptions::external(),
            options: fast_options(),
            version: 1,
        }
    }

    pub(crate) fn build(self) -> Fixture {
        self.build_with_clock(holdpoint::SystemClock)
    }

    pub(crate) fn build_with_clock(self, clock: impl holdpoint::Clock + 'static) -> Fixture {
        let worker = Worker::builder(Arc::clone(&self.store), "test", self.host.clone())
            .clock(clock)
            .definition(Plan { version: self.version })
            .model("model", model_options(), self.model.clone())
            .activity("act", self.activity_options.clone(), self.activity.clone())
            .activity(
                "idempotent",
                ActivityOptions {
                    retry: RetryPolicy {
                        initial_ms: 1,
                        max_interval_ms: 2,
                        max_attempts: Some(4),
                        ..RetryPolicy::default()
                    },
                    ..ActivityOptions::idempotent()
                },
                self.activity.clone(),
            )
            .options(self.options)
            .build();
        Fixture {
            worker: Arc::new(worker),
            store: self.store,
            host: self.host,
            model: self.model,
            activity: self.activity,
        }
    }
}

pub(crate) fn memory_store() -> Arc<Store> {
    Arc::new(Store::open_in_memory(Limits::default()).expect("in-memory store"))
}

pub(crate) fn file_store(dir: &tempfile::TempDir) -> Arc<Store> {
    Arc::new(Store::open(&dir.path().join("ledger.sqlite3"), Limits::default()).expect("file store"))
}

pub(crate) fn fixture() -> Fixture {
    Setup::new(memory_store()).build()
}

pub(crate) fn plan(steps: Value) -> Value {
    Value::Object(serde_json::Map::from_iter([("steps".to_owned(), steps)]))
}

pub(crate) type Hook = Arc<Mutex<Option<Box<dyn FnOnce() + Send>>>>;

/// A host whose hooks can run one action, the way another process acts while a hook is in flight.
#[derive(Clone, Default)]
pub(crate) struct Racing {
    pub(crate) on_authorize: Hook,
    pub(crate) on_prove: Hook,
    pub(crate) on_release: Hook,
    pub(crate) proof: Arc<Mutex<Option<Proof>>>,
}

pub(crate) fn run_hook(hook: &Hook) {
    if let Some(action) = hook.lock().unwrap().take() {
        action();
    }
}

impl Host for Racing {
    fn authorize(&self, _run: &RunInfo<'_>) -> Authority {
        run_hook(&self.on_authorize);
        Authority::Granted(Credentials::none())
    }

    fn prove_ended(&self, _run: &RunInfo<'_>, _obligation: &Obligation) -> Proof {
        run_hook(&self.on_prove);
        self.proof.lock().unwrap().unwrap_or(Proof::Ended)
    }

    fn release(&self, _run: &RunInfo<'_>, _fence: u64) -> Result<(), String> {
        run_hook(&self.on_release);
        Ok(())
    }
}
