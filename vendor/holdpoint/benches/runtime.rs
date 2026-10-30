//! Native benchmarks of the runtime's own overhead, with a scripted model and no network: a plain agent turn, a
//! turn that suspends on a deferred tool and resumes, and the recovery replay of a 300-step history.
//!
//! `cargo bench -p holdpoint --features testing --bench runtime -- [--check] [--json PATH]`

use std::io::Write as _;
use std::path::Path;
use std::sync::Arc;
use std::time::Instant;

use holdpoint::agent::entry::{Entry, ToolKind, ToolSpec};
use holdpoint::testing::TestHost;
use holdpoint::{
    Agent, AgentInput, AgentLimits, Driven, Limits, ModelCall, ModelOptions, ModelReply, RunOptions, Store, Worker,
};
use serde_json::{Value, json};

const WARMUP: usize = 10;
const RUNS: usize = 100;

fn reply(parts: &Value, stop: &str) -> ModelReply {
    ModelReply { response: json!({ "parts": parts, "stop": stop }), cost: 1 }
}

/// Calls the deferred tool `ask` on the first call, a local tool while `rounds` remain, then replies.
fn model(rounds: usize) -> impl Fn(ModelCall<'_>) -> Result<ModelReply, holdpoint::Failure> + Send + Sync {
    move |call| {
        let entries = call.request["entries"].as_array().map_or(0, Vec::len);
        let done = entries.saturating_sub(1) / 2;
        let asking = call.request["entries"][0]["text"] == "ask";
        Ok(if asking && entries == 1 {
            reply(&json!([{ "type": "tool_call", "id": "ask-1", "name": "ask", "arguments": {} }]), "tool_use")
        } else if done < rounds {
            let id = format!("call-{done}");
            reply(&json!([{ "type": "tool_call", "id": id, "name": "note", "arguments": { "n": done } }]), "tool_use")
        } else {
            reply(&json!([{ "type": "text", "text": "Olá." }]), "end_turn")
        })
    }
}

fn worker(store: Arc<Store>, rounds: usize) -> Worker {
    Worker::builder(store, "bench", TestHost::default())
        .definition(Agent::new(1).local_tool("note", |arguments| Ok(arguments.clone())))
        .model("scripted", ModelOptions::default(), model(rounds))
        .build()
}

fn input(text: &str) -> Value {
    let open = json!({ "type": "object", "additionalProperties": false, "properties": {} });
    let note = json!({ "type": "object", "additionalProperties": false, "properties": { "n": { "type": "integer" } } });
    let tools = vec![
        ToolSpec {
            name: "ask".into(),
            description: "ask".into(),
            parameters: open,
            kind: ToolKind::Deferred,
            strict: false,
            validate: true,
        },
        ToolSpec {
            name: "note".into(),
            description: "note".into(),
            parameters: note,
            kind: ToolKind::Local,
            strict: false,
            validate: true,
        },
    ];
    let input = AgentInput {
        model: "scripted".into(),
        model_name: "fake".into(),
        system: "Be brief.".into(),
        messages: vec![Entry::User { text: text.into() }],
        tools,
        limits: AgentLimits { max_model_steps: 400, ..AgentLimits::default() },
        max_output_tokens: 256,
        effort: None,
        fold: None,
    };
    serde_json::to_value(input).unwrap_or(Value::Null)
}

struct Sample {
    name: &'static str,
    store: &'static str,
    millis: Vec<f64>,
    commits: f64,
}

fn summary(sample: &Sample) -> Value {
    let mut sorted = sample.millis.clone();
    sorted.sort_by(f64::total_cmp);
    let median = sorted[sorted.len() / 2];
    let p90 = sorted[sorted.len() * 9 / 10];
    json!({ "scenario": sample.name, "store": sample.store, "median_ms": median, "p90_ms": p90, "commits": sample.commits, "runs": sorted.len() })
}

fn measure(
    name: &'static str,
    store_kind: &'static str,
    store: &Arc<Store>,
    mut scenario: impl FnMut(usize),
) -> Sample {
    for index in 0..WARMUP {
        scenario(1_000_000 + index);
    }
    let before = store.commits();
    let mut millis = Vec::with_capacity(RUNS);
    for index in 0..RUNS {
        let started = Instant::now();
        scenario(index);
        millis.push(started.elapsed().as_secs_f64() * 1_000.0);
    }
    #[expect(clippy::cast_precision_loss, reason = "commit counts are small")]
    let commits = (store.commits() - before) as f64 / RUNS as f64;
    Sample { name, store: store_kind, millis, commits }
}

fn plain(store: &Arc<Store>, kind: &'static str) -> Sample {
    let worker = worker(Arc::clone(store), 0);
    let input = input("Oi");
    measure("plain-turn", kind, store, |index| {
        let id = format!("plain-{index}");
        worker.submit("agent", &id, &input, &RunOptions::default()).ok();
        assert!(matches!(worker.drive(&id), Ok(Driven::Ended(_))));
    })
}

fn deferred(store: &Arc<Store>, kind: &'static str) -> Sample {
    let worker = worker(Arc::clone(store), 0);
    let input = input("ask");
    measure("deferred-suspend-resume", kind, store, |index| {
        let id = format!("ask-{index}");
        worker.submit("agent", &id, &input, &RunOptions::default()).ok();
        assert!(matches!(worker.drive(&id), Ok(Driven::Suspended(_))));
        let step =
            worker.pending(&id).ok().and_then(|pending| pending.first().map(|request| request.step)).unwrap_or(0);
        worker.resolve(&id, step, &json!("yes")).ok();
        assert!(matches!(worker.drive(&id), Ok(Driven::Ended(_))));
    })
}

/// A run with 150 local tool rounds (300 steps) parked on a deferred request; each sample re-drives it, which is
/// exactly the recovery path: load the journal, replay every step, and park again.
fn replay(store: &Arc<Store>, kind: &'static str) -> Sample {
    let worker = worker(Arc::clone(store), 150);
    let mut long = input("ask");
    long["messages"][0]["text"] = json!("long");
    let id = "replay-300";
    worker.submit("agent", id, &long, &RunOptions::default()).ok();
    let fixed = Worker::builder(Arc::clone(store), "bench", TestHost::default())
        .definition(Agent::new(1).local_tool("note", |arguments| Ok(arguments.clone())))
        .model("scripted", ModelOptions::default(), move |call: ModelCall<'_>| {
            let entries = call.request["entries"].as_array().map_or(0, Vec::len);
            let done = entries.saturating_sub(1) / 2;
            Ok(if done < 150 {
                let id = format!("call-{done}");
                reply(
                    &json!([{ "type": "tool_call", "id": id, "name": "note", "arguments": { "n": done } }]),
                    "tool_use",
                )
            } else {
                reply(&json!([{ "type": "tool_call", "id": "ask-end", "name": "ask", "arguments": {} }]), "tool_use")
            })
        })
        .build();
    assert!(matches!(fixed.drive(id), Ok(Driven::Suspended(_))));
    let steps = store.history(id).map_or(0, |events| events.len());
    assert!(steps > 600, "the history holds {steps} events");
    drop(worker);
    measure("replay-300-steps", kind, store, |_| {
        assert!(matches!(fixed.drive(id), Ok(Driven::Suspended(_))));
    })
}

fn main() {
    let arguments: Vec<String> = std::env::args().collect();
    let check = arguments.iter().any(|argument| argument == "--check");
    let json_path =
        arguments.iter().position(|argument| argument == "--json").and_then(|index| arguments.get(index + 1));
    let base = arguments
        .iter()
        .position(|argument| argument == "--dir")
        .and_then(|index| arguments.get(index + 1))
        .map_or_else(std::env::temp_dir, std::path::PathBuf::from);
    let dir = base.join(format!("holdpoint-bench-{}", std::process::id()));
    std::fs::create_dir_all(&dir).ok();
    let file =
        Arc::new(Store::open(&dir.join("ledger.sqlite3"), Limits::default()).unwrap_or_else(|_| std::process::exit(2)));
    let memory = Arc::new(Store::open_in_memory(Limits::default()).unwrap_or_else(|_| std::process::exit(2)));
    let samples = [
        plain(&file, "file"),
        deferred(&file, "file"),
        replay(&file, "file"),
        plain(&memory, "memory"),
        deferred(&memory, "memory"),
        replay(&memory, "memory"),
    ];
    let results: Vec<Value> = samples.iter().map(summary).collect();
    let report = json!({ "runtime": "holdpoint (native)", "dir": base.display().to_string(), "results": results });
    let text = serde_json::to_string_pretty(&report).unwrap_or_default();
    if let Some(path) = json_path {
        std::fs::write(Path::new(path), &text).ok();
    }
    std::fs::remove_dir_all(&dir).ok();
    let mut out = std::io::stdout().lock();
    for result in &results {
        writeln!(out, "{}", serde_json::to_string(result).unwrap_or_default()).ok();
    }
    if check && !within_guardrails(&results) {
        std::process::exit(1);
    }
}

/// The plan's guardrails on the durable (file) store: plain turn ≤ 11 ms, suspend + resolve + continue ≤ 15 ms,
/// replay of 300 steps ≤ 100 ms (medians).
fn within_guardrails(results: &[Value]) -> bool {
    let limits = [("plain-turn", 11.0), ("deferred-suspend-resume", 15.0), ("replay-300-steps", 100.0)];
    limits.iter().all(|(scenario, limit)| {
        results
            .iter()
            .filter(|result| result["store"] == "file" && result["scenario"] == *scenario)
            .all(|result| result["median_ms"].as_f64().is_some_and(|median| median <= *limit))
    })
}
