//! Opt-in live smoke of the provider adapters: one agent run per provider with a local tool call, so the second
//! call replays the first call's opaque reasoning or thinking. Costs a fraction of a cent.
//!
//! `HOLDPOINT_OPENAI_KEY_FILE=... HOLDPOINT_ANTHROPIC_KEY_FILE=... cargo run -p holdpoint --example live_smoke
//!  --features openai,anthropic,testing`

use std::sync::Arc;

use holdpoint::agent::entry::{Entry, ToolKind, ToolSpec};
use holdpoint::providers::anthropic::Anthropic;
use holdpoint::providers::openai::OpenAi;
use holdpoint::providers::{ApiKey, Pricing};
use holdpoint::{
    Agent, AgentInput, AgentLimits, Authority, Credentials, Host, Limits, ModelOptions, Obligation, Proof, RunInfo,
    RunOptions, Store, Worker,
};
use serde_json::json;

struct KeyHost(String);

impl Host for KeyHost {
    fn authorize(&self, _run: &RunInfo<'_>) -> Authority {
        Authority::Granted(Credentials::new(ApiKey(self.0.clone())))
    }

    fn prove_ended(&self, _run: &RunInfo<'_>, _obligation: &Obligation) -> Proof {
        Proof::Ended
    }
}

fn input(model_name: &str, effort: Option<&str>) -> serde_json::Value {
    let parameters = json!({ "type": "object", "additionalProperties": false, "required": ["a", "b"],
        "properties": { "a": { "type": "integer" }, "b": { "type": "integer" } } });
    let add = ToolSpec {
        name: "add".into(),
        description: "Adds two integers.".into(),
        parameters,
        kind: ToolKind::Local,
        strict: true,
        validate: true,
    };
    let input = AgentInput {
        model: "live".into(),
        model_name: model_name.into(),
        system: "Reason carefully before calling tools.".into(),
        messages: vec![Entry::User {
            text:
                "A shop sells pens in packs of 7 and pencils in packs of 11. Ana buys the smallest number of packs so \
                   that she has exactly 100 items in total. Work out how many of each pack she buys, then use the add \
                   tool to add the number of pen packs to the number of pencil packs, and answer with that sum."
                    .into(),
        }],
        tools: vec![add],
        limits: AgentLimits { max_model_steps: 4, ..AgentLimits::default() },
        max_output_tokens: 2_000,
        effort: effort.map(str::to_owned),
        fold: None,
    };
    serde_json::to_value(input).unwrap_or_default()
}

fn run(
    name: &str,
    key_file: &str,
    model: impl holdpoint::Model + 'static,
    model_name: &str,
    effort: Option<&str>,
) -> Result<(), String> {
    let key = std::fs::read_to_string(key_file).map_err(|error| format!("{name}: {error}"))?.trim().to_owned();
    let store = Arc::new(Store::open_in_memory(Limits::default()).map_err(|error| error.to_string())?);
    let worker = Worker::builder(Arc::clone(&store), "live", KeyHost(key))
        .definition(Agent::new(1).local_tool("add", |arguments| {
            Ok(json!(arguments["a"].as_i64().unwrap_or(0) + arguments["b"].as_i64().unwrap_or(0)))
        }))
        .model("live", ModelOptions::default(), model)
        .build();
    worker
        .submit(
            "agent",
            name,
            &input(model_name, effort),
            &RunOptions { deadline_ms: None, spend_ceiling: Some(100_000) },
        )
        .map_err(|error| error.to_string())?;
    let driven = worker.drive(name).map_err(|error| error.to_string())?;
    let history = store.history(name).map_err(|error| error.to_string())?;
    let encoded: Vec<String> = history.iter().filter_map(|event| serde_json::to_string(event).ok()).collect();
    let opaque = encoded.iter().any(|event| event.contains("\"type\":\"opaque\""));
    let parts: Vec<serde_json::Value> = history
        .iter()
        .filter_map(|event| match event {
            holdpoint::Event::StepCompleted { result, .. } => result.get("parts").cloned(),
            _ => None,
        })
        .map(|parts| {
            json!(parts.as_array().map(|parts| parts.iter().map(|part| part["type"].clone()).collect::<Vec<_>>()))
        })
        .collect();
    let report = json!({ "provider": name, "outcome": driven_text(&driven), "events": history.len(), "opaque_journaled": opaque, "parts": parts });
    let mut out = std::io::stdout().lock();
    std::io::Write::write_all(&mut out, format!("{report}\n").as_bytes()).map_err(|error| error.to_string())
}

fn driven_text(driven: &holdpoint::Driven) -> String {
    format!("{driven:?}")
}

fn main() -> Result<(), String> {
    // Micro-dollars per million tokens: GPT-6 Luna and Claude Haiku 5.5 list at US$0.10 in and US$0.50 out.
    let pricing = Pricing { input: 100_000, output: 500_000, cache_read: 10_000, cache_write: 125_000 };
    if let Ok(file) = std::env::var("HOLDPOINT_OPENAI_KEY_FILE") {
        run("openai", &file, OpenAi::new("https://api.openai.com", pricing), "gpt-6-luna", Some("medium"))?;
    }
    if let Ok(file) = std::env::var("HOLDPOINT_ANTHROPIC_KEY_FILE") {
        run("anthropic", &file, Anthropic::new("https://api.anthropic.com", pricing), "claude-haiku-5-5", None)?;
    }
    Ok(())
}
