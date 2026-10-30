//! The agent loop: replies, tool calls of every kind, images, bounds, folding, and resumption.

#![cfg(all(test, feature = "testing"))]

use std::sync::Arc;

use holdpoint::agent::entry::{Entry, ModelRequest, ToolKind, ToolSpec};
use holdpoint::testing::{Behavior, RecordingActivity, ScriptedModel, TestHost};
use holdpoint::{
    ActivityOptions, Agent, AgentInput, AgentLimits, Driven, Failure, Fold, LimitKind, Limits, ModelOptions,
    ModelReply, Outcome, RunOptions, Store, Wait, Worker,
};
use serde_json::{Value, json};

#[cfg(test)]
struct Harness {
    worker: Worker,
    model: ScriptedModel,
    browser: RecordingActivity,
    store: Arc<Store>,
}

#[cfg(test)]
fn harness_on(store: Arc<Store>, browser: RecordingActivity) -> Harness {
    let model = ScriptedModel::default();
    let agent = Agent::new(1)
        .local_tool("add", |arguments| {
            Ok(json!(arguments["a"].as_i64().unwrap_or(0) + arguments["b"].as_i64().unwrap_or(0)))
        })
        .local_tool("broken", |_| Err(Failure::permanent("broken", "it broke")));
    let worker = Worker::builder(Arc::clone(&store), "agent-test", TestHost::default())
        .definition(agent)
        .model("scripted", ModelOptions::default(), model.clone())
        .activity("look", ActivityOptions::idempotent(), browser.clone())
        .build();
    Harness { worker, model, browser, store }
}

#[cfg(test)]
fn harness() -> Harness {
    let store = Arc::new(Store::open_in_memory(Limits::default()).unwrap());
    harness_on(store, RecordingActivity::new([Behavior::Echo]))
}

#[cfg(test)]
fn object(properties: &Value, required: &[&str]) -> Value {
    json!({ "type": "object", "additionalProperties": false, "properties": properties, "required": required })
}

#[cfg(test)]
fn tool(name: &str, kind: ToolKind, parameters: Value) -> ToolSpec {
    ToolSpec {
        name: name.into(),
        description: format!("the {name} tool"),
        parameters,
        kind,
        strict: false,
        validate: true,
    }
}

#[cfg(test)]
fn input(limits: AgentLimits, fold: Option<Fold>) -> AgentInput {
    let number = json!({ "type": "integer" });
    AgentInput {
        model: "scripted".into(),
        model_name: "test-model".into(),
        system: "Be brief.".into(),
        messages: vec![Entry::User { text: "Find jobs".into() }],
        tools: vec![
            tool("add", ToolKind::Local, object(&json!({ "a": number, "b": number }), &["a", "b"])),
            tool("broken", ToolKind::Local, object(&json!({}), &[])),
            tool("look", ToolKind::Activity, object(&json!({ "url": { "type": "string" } }), &["url"])),
            tool("ask", ToolKind::Deferred, object(&json!({ "question": { "type": "string" } }), &["question"])),
            tool("done", ToolKind::Final, object(&json!({ "jobs": { "type": "integer" } }), &["jobs"])),
        ],
        limits,
        max_output_tokens: 512,
        effort: Some("low".into()),
        fold,
    }
}

#[cfg(test)]
fn text(text: &str) -> ModelReply {
    ModelReply { response: json!({ "parts": [{ "type": "text", "text": text }], "stop": "end_turn" }), cost: 1 }
}

#[cfg(test)]
fn calls(calls: &[(&str, &str, Value)]) -> ModelReply {
    let parts: Vec<Value> = calls
        .iter()
        .map(|(id, name, arguments)| json!({ "type": "tool_call", "id": id, "name": name, "arguments": arguments }))
        .collect();
    ModelReply {
        response: json!({ "parts": parts, "stop": "tool_use", "usage": { "input": 10, "output": 2 } }),
        cost: 1,
    }
}

#[cfg(test)]
fn start(harness: &Harness, id: &str, input: &AgentInput) -> Driven {
    harness.worker.submit("agent", id, &serde_json::to_value(input).unwrap(), &RunOptions::default()).unwrap();
    harness.worker.drive(id).unwrap()
}

#[cfg(test)]
fn requests(harness: &Harness) -> Vec<ModelRequest> {
    harness.model.requests().into_iter().map(|request| serde_json::from_value(request).unwrap()).collect()
}

#[cfg(test)]
fn reply(text: &str) -> Driven {
    Driven::Ended(Outcome::Done { result: json!({ "reply": text }) })
}

#[test]
fn a_plain_reply_ends_the_run() {
    let harness = harness();
    harness.model.push(Ok(text("Here they are.")));
    assert_eq!(start(&harness, "a1", &input(AgentLimits::default(), None)), reply("Here they are."));
    let request = &requests(&harness)[0];
    assert_eq!(request.system, "Be brief.");
    assert_eq!(request.tools.len(), 5);
    assert_eq!(request.effort.as_deref(), Some("low"));
}

#[test]
fn every_tool_call_gets_exactly_one_result_before_the_next_call() {
    let harness = harness();
    harness.model.push(Ok(calls(&[
        ("c1", "add", json!({ "a": 2, "b": 3 })),
        ("c2", "look", json!({ "url": "https://example.test" })),
        ("c3", "broken", json!({})),
        ("c4", "nope", json!({})),
        ("c5", "add", json!({ "a": "two" })),
    ])));
    harness.model.push(Ok(text("Done.")));
    assert_eq!(start(&harness, "a2", &input(AgentLimits::default(), None)), reply("Done."));
    let second = &requests(&harness)[1];
    let results: Vec<(String, Value)> = second
        .entries
        .iter()
        .filter_map(|entry| match entry {
            Entry::ToolResult { call_id, outcome } => Some((call_id.clone(), serde_json::to_value(outcome).unwrap())),
            _ => None,
        })
        .collect();
    let ids: Vec<&str> = results.iter().map(|(id, _)| id.as_str()).collect();
    assert_eq!(ids, ["c1", "c2", "c3", "c4", "c5"]);
    assert_eq!(results[0].1, json!({ "outcome": "ok", "value": 5 }));
    assert_eq!(results[1].1, json!({ "outcome": "ok", "value": { "url": "https://example.test" } }));
    assert_eq!(results[2].1["code"], "broken");
    assert_eq!(results[3].1["code"], "unknown-tool");
    assert_eq!(results[4].1["code"], "invalid-arguments");
}

#[test]
fn an_image_is_shown_to_the_next_call_only_and_then_released() {
    let store = Arc::new(Store::open_in_memory(Limits::default()).unwrap());
    let png = b"\x89PNG\r\n\x1a\nfake image".to_vec();
    let harness = harness_on(store, RecordingActivity::new([Behavior::EchoWithBlob(png)]));
    harness.model.push(Ok(calls(&[("c1", "look", json!({ "url": "u" }))])));
    harness.model.push(Ok(calls(&[("c2", "add", json!({ "a": 1, "b": 1 }))])));
    harness.model.push(Ok(text("Seen.")));
    assert_eq!(start(&harness, "a3", &input(AgentLimits::default(), None)), reply("Seen."));
    let requests = requests(&harness);
    assert_eq!(requests[1].images.len(), 1);
    assert_eq!(requests[1].images[0].media_type, "image/png");
    assert_eq!(requests[1].images[0].call_id, "c1");
    assert_eq!(requests[2].images.len(), 0);
    assert_eq!(harness.store.blob("a3", 256).unwrap(), None);
}

#[test]
fn deferred_calls_wait_for_the_host_together() {
    let harness = harness();
    harness.model.push(Ok(calls(&[
        ("c1", "ask", json!({ "question": "Which city?" })),
        ("c2", "add", json!({ "a": 1, "b": 2 })),
        ("c3", "ask", json!({ "question": "Which salary?" })),
    ])));
    harness.model.push(Ok(text("Thanks.")));
    assert_eq!(start(&harness, "a4", &input(AgentLimits::default(), None)), Driven::Suspended(Wait::Deferred));
    let pending = harness.worker.pending("a4").unwrap();
    assert_eq!(pending.len(), 2);
    assert_eq!(pending[0].request, json!({ "call_id": "c1", "arguments": { "question": "Which city?" } }));
    harness.worker.resolve("a4", pending[1].step, &json!({ "outcome": "denied", "reason": "private" })).unwrap();
    assert_eq!(harness.worker.drive("a4").unwrap(), Driven::Suspended(Wait::Deferred));
    harness.worker.resolve("a4", pending[0].step, &json!("Curitiba")).unwrap();
    assert_eq!(harness.worker.drive("a4").unwrap(), reply("Thanks."));
    let results: Vec<Value> = requests(&harness)[1]
        .entries
        .iter()
        .filter_map(|entry| match entry {
            Entry::ToolResult { outcome, .. } => Some(serde_json::to_value(outcome).unwrap()),
            _ => None,
        })
        .collect();
    assert_eq!(results[0], json!({ "outcome": "ok", "value": "Curitiba" }));
    assert_eq!(results[1], json!({ "outcome": "ok", "value": 3 }));
    assert_eq!(results[2], json!({ "outcome": "denied", "reason": "private" }));
}

#[test]
fn a_resumed_agent_never_calls_the_model_again_for_finished_steps() {
    let store = Arc::new(Store::open_in_memory(Limits::default()).unwrap());
    let first = harness_on(Arc::clone(&store), RecordingActivity::new([Behavior::Echo]));
    first.model.push(Ok(calls(&[("c1", "look", json!({ "url": "u" })), ("c2", "ask", json!({ "question": "ok?" }))])));
    assert_eq!(start(&first, "a5", &input(AgentLimits::default(), None)), Driven::Suspended(Wait::Deferred));
    drop(first);
    let second = harness_on(store, RecordingActivity::new([Behavior::Echo]));
    second.model.push(Ok(text("Resumed.")));
    let step = second.worker.pending("a5").unwrap()[0].step;
    second.worker.resolve("a5", step, &json!("yes")).unwrap();
    assert_eq!(second.worker.drive("a5").unwrap(), reply("Resumed."));
    assert_eq!(second.model.requests().len(), 1);
    assert_eq!(second.browser.calls().len(), 0);
}

#[test]
fn a_final_result_ends_the_run_only_when_alone() {
    let harness = harness();
    harness.model.push(Ok(calls(&[("c1", "done", json!({ "jobs": 10 })), ("c2", "add", json!({ "a": 1, "b": 1 }))])));
    harness.model.push(Ok(calls(&[("c3", "done", json!({ "jobs": "ten" }))])));
    harness.model.push(Ok(calls(&[("c4", "done", json!({ "jobs": 10 }))])));
    let driven = start(&harness, "a6", &input(AgentLimits::default(), None));
    assert_eq!(driven, Driven::Ended(Outcome::Done { result: json!({ "final": { "jobs": 10 } }) }));
    let second = &requests(&harness)[1];
    assert!(
        matches!(&second.entries[2], Entry::ToolResult { outcome, .. } if serde_json::to_value(outcome).unwrap()["code"] == "final-not-alone")
    );
    let third = &requests(&harness)[2];
    assert!(
        matches!(third.entries.last(), Some(Entry::ToolResult { outcome, .. }) if serde_json::to_value(outcome).unwrap()["code"] == "invalid-arguments")
    );
}

#[test]
fn too_many_calls_are_all_refused() {
    let harness = harness();
    let limits = AgentLimits { max_tool_calls: 1, ..AgentLimits::default() };
    harness
        .model
        .push(Ok(calls(&[("c1", "add", json!({ "a": 1, "b": 1 })), ("c2", "add", json!({ "a": 1, "b": 1 }))])));
    harness.model.push(Ok(text("Fine.")));
    assert_eq!(start(&harness, "a7", &input(limits, None)), reply("Fine."));
    assert_eq!(harness.browser.calls().len(), 0);
}

#[test]
fn a_truncated_response_is_never_accepted() {
    let harness = harness();
    let truncated = json!({ "parts": [{ "type": "tool_call", "id": "c1", "name": "look", "arguments": { "url": "u" } }], "stop": "max_tokens" });
    harness.model.push(Ok(ModelReply { response: truncated, cost: 1 }));
    let driven = start(&harness, "a8", &input(AgentLimits::default(), None));
    assert!(matches!(driven, Driven::Ended(Outcome::Limited { kind: LimitKind::Output, .. })));
    assert_eq!(harness.browser.calls().len(), 0);
}

#[test]
fn the_step_bound_asks_for_a_final_answer_without_tools() {
    let harness = harness();
    let limits = AgentLimits { max_model_steps: 1, ..AgentLimits::default() };
    harness.model.push(Ok(calls(&[("c1", "add", json!({ "a": 1, "b": 1 }))])));
    harness.model.push(Ok(text("Best answer: 2.")));
    let Driven::Ended(Outcome::Limited { kind: LimitKind::Steps, progress }) =
        start(&harness, "a9", &input(limits, None))
    else {
        panic!("the run was not limited by steps");
    };
    assert_eq!(progress["answer"], json!("Best answer: 2."));
    let last = &requests(&harness)[1];
    assert_eq!(last.tools.len(), 0);
    assert!(matches!(last.entries.last(), Some(Entry::Notice { .. })));
}

#[test]
fn a_long_transcript_is_folded_by_a_journaled_call() {
    let store = Arc::new(Store::open_in_memory(Limits::default()).unwrap());
    let script = ScriptedModel::default();
    for index in 0..3 {
        script.push(Ok(calls(&[(&format!("c{index}"), "look", json!({ "url": "x".repeat(150) }))])));
    }
    script.push(Ok(text("Final.")));
    let seen = Arc::new(std::sync::Mutex::new(Vec::new()));
    let (model_script, model_seen) = (script.clone(), Arc::clone(&seen));
    let model = move |call: holdpoint::ModelCall<'_>| {
        model_seen.lock().unwrap().push(call.request.clone());
        if call.request["system"] == "Summarize the work so far." {
            return Ok(text("Memory: looked several times."));
        }
        holdpoint::Model::call(&model_script, call)
    };
    let worker = Worker::builder(store, "t", TestHost::default())
        .definition(Agent::new(1))
        .model("scripted", ModelOptions::default(), model)
        .activity("look", ActivityOptions::idempotent(), RecordingActivity::new([Behavior::Echo]))
        .build();
    let fold = Fold { at_bytes: 600, keep_last: 2, instruction: "Summarize the work so far.".into() };
    let agent_input = serde_json::to_value(input(AgentLimits::default(), Some(fold))).unwrap();
    worker.submit("agent", "a10", &agent_input, &RunOptions::default()).unwrap();
    assert_eq!(worker.drive("a10").unwrap(), reply("Final."));
    let requests: Vec<ModelRequest> =
        seen.lock().unwrap().iter().map(|request| serde_json::from_value(request.clone()).unwrap()).collect();
    let folding = requests.iter().position(|request| request.system == "Summarize the work so far.").unwrap();
    assert_eq!(requests[folding].tools.len(), 0);
    let after = &requests[folding + 1];
    assert!(matches!(&after.entries[0], Entry::Memory { text } if text.contains("looked several times")));
    assert!(after.entries.len() <= 3, "{:?}", after.entries);
}

#[test]
fn a_transform_rewrites_what_each_call_sees() {
    let store = Arc::new(Store::open_in_memory(Limits::default()).unwrap());
    let model = ScriptedModel::default();
    let agent = Agent::new(1).transform(|entries| {
        entries
            .iter()
            .map(|entry| match entry {
                Entry::User { .. } => Entry::User { text: "[redacted]".into() },
                other => other.clone(),
            })
            .collect()
    });
    let worker = Worker::builder(store, "t", TestHost::default())
        .definition(agent)
        .model("scripted", ModelOptions::default(), model.clone())
        .build();
    model.push(Ok(text("ok")));
    let mut agent_input = input(AgentLimits::default(), None);
    agent_input.tools.clear();
    worker.submit("agent", "a11", &serde_json::to_value(&agent_input).unwrap(), &RunOptions::default()).unwrap();
    worker.drive("a11").unwrap();
    assert_eq!(model.requests()[0]["entries"][0]["text"], "[redacted]");
}

#[test]
fn bad_inputs_responses_and_failures_are_unrecoverable() {
    let harness = harness();
    harness.worker.submit("agent", "b1", &json!({ "model": 1 }), &RunOptions::default()).unwrap();
    let reason = |driven: Driven| match driven {
        Driven::Ended(Outcome::Unrecoverable { reason }) => reason,
        other => panic!("{other:?}"),
    };
    assert_eq!(reason(harness.worker.drive("b1").unwrap()), "bad-agent-input");
    harness.model.push(Ok(ModelReply { response: json!({ "nonsense": true }), cost: 0 }));
    assert_eq!(reason(start(&harness, "b2", &input(AgentLimits::default(), None))), "bad-model-response");
    harness.model.push(Err(Failure::permanent("http-400", "bad request").with_ended()));
    assert_eq!(reason(start(&harness, "b3", &input(AgentLimits::default(), None))), "model-failed:http-400");
    let mut bad_schema = input(AgentLimits::default(), None);
    bad_schema.tools.push(tool("loose", ToolKind::Local, json!({ "type": "object" })));
    assert_eq!(reason(start(&harness, "b4", &bad_schema)), "bad-tool-schema:loose");
}

#[test]
fn a_refusal_is_reported_as_one() {
    let harness = harness();
    let refusal = json!({ "parts": [{ "type": "text", "text": "I can't help." }], "stop": "refusal" });
    harness.model.push(Ok(ModelReply { response: refusal, cost: 0 }));
    let driven = start(&harness, "r1", &input(AgentLimits::default(), None));
    assert_eq!(driven, Driven::Ended(Outcome::Done { result: json!({ "refused": "I can't help." }) }));
}

#[test]
fn a_transcript_over_its_bound_ends_the_run() {
    let harness = harness();
    let limits = AgentLimits { max_transcript_bytes: 10, ..AgentLimits::default() };
    let driven = start(&harness, "h1", &input(limits, None));
    assert!(matches!(driven, Driven::Ended(Outcome::Limited { kind: LimitKind::History, .. })));
}

#[test]
fn finding_07_a_failed_fold_keeps_the_images_the_transcript_still_needs() {
    let store = Arc::new(Store::open_in_memory(Limits::default()).unwrap());
    let png = b"\x89PNG\r\n\x1a\nfake image".to_vec();
    let failed_once = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let model = move |call: holdpoint::ModelCall<'_>| {
        if call.request["system"] == "Fold." {
            let truncated = json!({ "parts": [{ "type": "text", "text": "partial" }], "stop": "max_tokens" });
            return Ok(ModelReply { response: truncated, cost: 0 });
        }
        if call.request["entries"].as_array().is_some_and(|entries| entries.len() == 1) {
            return Ok(calls(&[("c1", "look", json!({ "url": "x".repeat(400) }))]));
        }
        if !failed_once.swap(true, std::sync::atomic::Ordering::SeqCst) {
            // The call after the fold fails once, so the run parks with the fold committed and recovers later.
            return Err(Failure::retryable("http-503", "unavailable").with_ended());
        }
        let images = call.request["images"].as_array().map_or(0, Vec::len);
        Ok(text(&format!("saw {images} image")))
    };
    let clock = holdpoint::ManualClock::at(1_000);
    let options = ModelOptions {
        retry: holdpoint::RetryPolicy { initial_ms: 60_000, ..holdpoint::RetryPolicy::default() },
        ..ModelOptions::default()
    };
    let worker = Worker::builder(store, "t", TestHost::default())
        .definition(Agent::new(1))
        .model("scripted", options, model)
        .activity("look", ActivityOptions::idempotent(), RecordingActivity::new([Behavior::EchoWithBlob(png)]))
        .clock(clock.clone())
        .build();
    let fold = Fold { at_bytes: 300, keep_last: 0, instruction: "Fold.".into() };
    let agent_input = serde_json::to_value(input(AgentLimits::default(), Some(fold))).unwrap();
    worker.submit("agent", "f1", &agent_input, &RunOptions::default()).unwrap();
    assert!(matches!(worker.drive("f1").unwrap(), Driven::Suspended(Wait::Until(_))));
    clock.advance(61_000);
    assert_eq!(worker.drive("f1").unwrap(), reply("saw 1 image"));
}

#[test]
fn finding_13_repeated_call_ids_run_nothing() {
    let harness = harness();
    harness.model.push(Ok(calls(&[("same", "look", json!({ "url": "a" })), ("same", "look", json!({ "url": "b" }))])));
    harness.model.push(Ok(text("Sorry.")));
    assert_eq!(start(&harness, "d1", &input(AgentLimits::default(), None)), reply("Sorry."));
    assert_eq!(harness.browser.calls().len(), 0);
    let seen = requests(&harness);
    let results = seen[1].entries.iter().filter(|entry| matches!(entry, Entry::ToolResult { .. })).count();
    assert_eq!(results, 1);
}

#[test]
fn finding_14_partial_progress_carries_what_the_run_found() {
    let harness = harness();
    let limits = AgentLimits { max_model_steps: 1, ..AgentLimits::default() };
    harness.model.push(Ok(calls(&[("c1", "look", json!({ "url": "jobs" }))])));
    harness.model.push(Ok(ModelReply { response: json!({ "parts": [], "stop": "max_tokens" }), cost: 0 }));
    let Driven::Ended(Outcome::Limited { progress, .. }) = start(&harness, "p1", &input(limits, None)) else {
        panic!("the run was not limited");
    };
    assert_eq!(progress["findings"], json!([{ "url": "jobs" }]));
}

#[test]
fn round2_08_findings_of_finished_calls_survive_a_later_call_expiring() {
    let store = Arc::new(Store::open_in_memory(Limits::default()).unwrap());
    let slow = RecordingActivity::new([Behavior::Echo, Behavior::Sleep { ms: 3_000, progress: None }]);
    let harness = harness_on(store, slow);
    harness
        .model
        .push(Ok(calls(&[("c1", "look", json!({ "url": "found" })), ("c2", "look", json!({ "url": "slow" }))])));
    let now = holdpoint::Clock::now_ms(&holdpoint::SystemClock);
    let options = RunOptions { deadline_ms: Some(now + 300), spend_ceiling: None };
    harness
        .worker
        .submit("agent", "e1", &serde_json::to_value(input(AgentLimits::default(), None)).unwrap(), &options)
        .unwrap();
    let Driven::Ended(Outcome::Expired { progress }) = harness.worker.drive("e1").unwrap() else {
        panic!("the run did not expire");
    };
    assert_eq!(progress["findings"], json!([{ "url": "found" }]));
}

#[test]
fn round2_09_a_progress_too_large_for_the_ledger_still_lets_the_run_end() {
    let store = Arc::new(Store::open_in_memory(Limits { max_value_bytes: 1_500, ..Limits::default() }).unwrap());
    let harness = harness_on(store, RecordingActivity::new([Behavior::Echo]));
    let limits = AgentLimits { max_model_steps: 1, ..AgentLimits::default() };
    let long = "y".repeat(1_000);
    harness.model.push(Ok(ModelReply {
        response: json!({ "parts": [{ "type": "text", "text": long }, { "type": "tool_call", "id": "c1", "name": "look", "arguments": { "url": "u" } }], "stop": "tool_use" }),
        cost: 0,
    }));
    harness.model.push(Ok(text(&"z".repeat(1_000))));
    let mut agent_input = input(limits, None);
    agent_input.tools.truncate(3);
    let driven = start(&harness, "big", &agent_input);
    assert!(matches!(driven, Driven::Ended(Outcome::Limited { .. })), "{driven:?}");
}

#[test]
fn an_input_without_an_output_bound_gets_the_default() {
    let mut value = serde_json::to_value(input(AgentLimits::default(), None)).unwrap();
    value.as_object_mut().unwrap().remove("max_output_tokens");
    let parsed: AgentInput = serde_json::from_value(value).unwrap();
    assert_eq!(parsed.max_output_tokens, 4_096);
}

#[test]
fn missing_and_non_image_blobs_are_not_shown() {
    let store = Arc::new(Store::open_in_memory(Limits::default()).unwrap());
    let harness = harness_on(store, RecordingActivity::new([Behavior::EchoWithBlob(b"%PDF-1.7".to_vec())]));
    let mut agent_input = input(AgentLimits::default(), None);
    agent_input.messages.push(Entry::ToolResult {
        call_id: "old".into(),
        outcome: holdpoint::agent::entry::ToolOutcome::Ok { value: json!("gone"), images: vec![999] },
    });
    harness.model.push(Ok(calls(&[("c1", "look", json!({ "url": "doc" }))])));
    harness.model.push(Ok(text("read")));
    assert_eq!(start(&harness, "pdf", &agent_input), reply("read"));
    let requests = requests(&harness);
    assert_eq!(requests[0].images.len(), 0, "a released image is left out");
    assert_eq!(requests[1].images.len(), 0, "a document is not an image");
}

#[test]
fn a_fold_with_nothing_to_cut_leaves_the_transcript() {
    let harness = harness();
    let fold = Fold { at_bytes: 1, keep_last: 100, instruction: "Fold.".into() };
    harness.model.push(Ok(text("no fold")));
    assert_eq!(start(&harness, "nf", &input(AgentLimits::default(), Some(fold))), reply("no fold"));
    assert_eq!(harness.model.requests().len(), 1);
}

#[test]
fn tools_validated_by_the_host_unimplemented_or_repeated_are_handled() {
    let harness = harness();
    let mut agent_input = input(AgentLimits::default(), None);
    agent_input
        .tools
        .push(ToolSpec { validate: false, ..tool("free", ToolKind::Local, json!({ "type": "anything" })) });
    harness.model.push(Ok(calls(&[("c1", "free", json!("raw")), ("c2", "look", json!({ "url": "u" }))])));
    harness.model.push(Ok(text("ok")));
    assert_eq!(start(&harness, "t1", &agent_input), reply("ok"));
    let outcome = serde_json::to_value(&requests(&harness)[1].entries[2]).unwrap();
    assert_eq!(outcome["outcome"]["code"], "unavailable", "a local tool without an implementation");
    let mut repeated = input(AgentLimits::default(), None);
    repeated.tools.push(tool("add", ToolKind::Local, json!({ "type": "object", "additionalProperties": false })));
    let reason = match start(&harness, "t2", &repeated) {
        Driven::Ended(Outcome::Unrecoverable { reason }) => reason,
        other => format!("{other:?}"),
    };
    assert_eq!(reason, "repeated-tool:add");
}

#[test]
fn an_uncertain_effect_reaches_the_model_as_uncertain() {
    let store = Arc::new(Store::open_in_memory(Limits::default()).unwrap());
    let model = ScriptedModel::default();
    let worker = Worker::builder(store, "t", TestHost::default())
        .definition(Agent::new(1))
        .model("scripted", ModelOptions::default(), model.clone())
        .activity("look", ActivityOptions::external(), RecordingActivity::new([Behavior::Panic]))
        .build();
    model.push(Ok(calls(&[("c1", "look", json!({ "url": "u" }))])));
    model.push(Ok(text("noted")));
    worker
        .submit(
            "agent",
            "u1",
            &serde_json::to_value(input(AgentLimits::default(), None)).unwrap(),
            &RunOptions::default(),
        )
        .unwrap();
    assert_eq!(worker.drive("u1").unwrap(), reply("noted"));
    assert_eq!(model.requests()[1]["entries"][2]["outcome"], json!({ "outcome": "uncertain" }));
}

#[test]
fn round3_04_answers_that_arrived_survive_a_cancellation_of_the_wait() {
    let harness = harness();
    harness
        .model
        .push(Ok(calls(&[("c1", "ask", json!({ "question": "a?" })), ("c2", "ask", json!({ "question": "b?" }))])));
    assert_eq!(start(&harness, "d1", &input(AgentLimits::default(), None)), Driven::Suspended(Wait::Deferred));
    let first = harness.worker.pending("d1").unwrap()[0].step;
    harness.worker.resolve("d1", first, &json!({ "fact": 42 })).unwrap();
    harness.worker.cancel("d1").unwrap();
    let Driven::Ended(Outcome::Cancelled { progress }) = harness.worker.drive("d1").unwrap() else {
        panic!("the run was not cancelled");
    };
    assert_eq!(progress["findings"], json!([{ "fact": 42 }]));
}

#[test]
fn round3_04_a_result_returned_while_the_run_is_cancelled_reaches_the_progress() {
    let store = Arc::new(Store::open_in_memory(Limits::default()).unwrap());
    let receipt = |_call: holdpoint::ActivityCall<'_>| {
        std::thread::sleep(std::time::Duration::from_millis(150));
        Ok(holdpoint::ActivityReply::of(json!({ "booked": true })))
    };
    let model = ScriptedModel::default();
    let worker = Arc::new(
        Worker::builder(store, "t", TestHost::default())
            .definition(Agent::new(1))
            .model("scripted", ModelOptions::default(), model.clone())
            .activity("look", ActivityOptions::external(), receipt)
            .options(holdpoint::WorkerOptions {
                abandon_grace_ms: 1_000,
                poll_ms: 5,
                ..holdpoint::WorkerOptions::default()
            })
            .build(),
    );
    model.push(Ok(calls(&[("c1", "look", json!({ "url": "u" }))])));
    worker
        .submit(
            "agent",
            "g1",
            &serde_json::to_value(input(AgentLimits::default(), None)).unwrap(),
            &RunOptions::default(),
        )
        .unwrap();
    let canceller = Arc::clone(&worker);
    std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(40));
        canceller.cancel("g1").unwrap();
    });
    let Driven::Ended(Outcome::Cancelled { progress }) = worker.drive("g1").unwrap() else {
        panic!("the run was not cancelled");
    };
    assert_eq!(progress["findings"], json!([{ "booked": true }]));
}

#[cfg(test)]
fn slow_model_worker(sleep_ms: u64, grace_ms: u64) -> Worker {
    let model = move |_call: holdpoint::ModelCall<'_>| {
        std::thread::sleep(std::time::Duration::from_millis(sleep_ms));
        Ok(text("the answer"))
    };
    Worker::builder(Arc::new(Store::open_in_memory(Limits::default()).unwrap()), "t", TestHost::default())
        .definition(Agent::new(1))
        .model("scripted", ModelOptions::default(), model)
        .options(holdpoint::WorkerOptions {
            abandon_grace_ms: grace_ms,
            poll_ms: 5,
            ..holdpoint::WorkerOptions::default()
        })
        .build()
}

#[test]
fn round4_02_a_model_call_outliving_the_deadline_ends_the_run_expired() {
    let worker = slow_model_worker(1_000, 20);
    let now = holdpoint::Clock::now_ms(&holdpoint::SystemClock);
    let options = RunOptions { deadline_ms: Some(now + 100), spend_ceiling: None };
    worker
        .submit("agent", "x1", &serde_json::to_value(input(AgentLimits::default(), None)).unwrap(), &options)
        .unwrap();
    assert!(matches!(worker.drive("x1").unwrap(), Driven::Ended(Outcome::Expired { .. })));
}

#[test]
fn round4_03_an_answer_returned_during_the_expiry_grace_is_in_the_progress() {
    let worker = slow_model_worker(700, 3_000);
    let now = holdpoint::Clock::now_ms(&holdpoint::SystemClock);
    let options = RunOptions { deadline_ms: Some(now + 300), spend_ceiling: None };
    worker
        .submit("agent", "x2", &serde_json::to_value(input(AgentLimits::default(), None)).unwrap(), &options)
        .unwrap();
    let Driven::Ended(Outcome::Expired { progress }) = worker.drive("x2").unwrap() else {
        panic!("the run did not expire");
    };
    assert_eq!(progress["last_text"], json!("the answer"));
}

#[test]
fn round5_02_a_final_answer_crossing_the_deadline_ends_the_run_expired_with_it() {
    let calls_made = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let counted = Arc::clone(&calls_made);
    let model = move |_call: holdpoint::ModelCall<'_>| {
        if counted.fetch_add(1, std::sync::atomic::Ordering::SeqCst) == 0 {
            return Ok(calls(&[("c1", "add", json!({ "a": 1, "b": 2 }))]));
        }
        std::thread::sleep(std::time::Duration::from_millis(900));
        Ok(text("final: 3"))
    };
    let worker = Worker::builder(Arc::new(Store::open_in_memory(Limits::default()).unwrap()), "t", TestHost::default())
        .definition(Agent::new(1).local_tool("add", |arguments| {
            Ok(json!(arguments["a"].as_i64().unwrap_or(0) + arguments["b"].as_i64().unwrap_or(0)))
        }))
        .model("scripted", ModelOptions::default(), model)
        .options(holdpoint::WorkerOptions {
            abandon_grace_ms: 3_000,
            poll_ms: 5,
            ..holdpoint::WorkerOptions::default()
        })
        .build();
    let now = holdpoint::Clock::now_ms(&holdpoint::SystemClock);
    let options = RunOptions { deadline_ms: Some(now + 450), spend_ceiling: None };
    let limits = AgentLimits { max_model_steps: 1, ..AgentLimits::default() };
    worker.submit("agent", "fa", &serde_json::to_value(input(limits, None)).unwrap(), &options).unwrap();
    let Driven::Ended(Outcome::Expired { progress }) = worker.drive("fa").unwrap() else {
        panic!("the run did not expire");
    };
    assert_eq!(progress["answer"], json!("final: 3"));
}

#[test]
fn round5_03_a_cancellation_keeps_the_final_answer_of_the_step_bound() {
    let store = Arc::new(Store::open_in_memory(Limits::default()).unwrap());
    let canceller = Arc::clone(&store);
    let calls_made = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let counted = Arc::clone(&calls_made);
    let model = move |_call: holdpoint::ModelCall<'_>| {
        if counted.fetch_add(1, std::sync::atomic::Ordering::SeqCst) == 0 {
            return Ok(calls(&[("c1", "add", json!({ "a": 1, "b": 2 }))]));
        }
        canceller.cancel("ca", holdpoint::Clock::now_ms(&holdpoint::SystemClock)).unwrap();
        Ok(text("final: 3"))
    };
    let worker = Worker::builder(store, "t", TestHost::default())
        .definition(Agent::new(1).local_tool("add", |arguments| {
            Ok(json!(arguments["a"].as_i64().unwrap_or(0) + arguments["b"].as_i64().unwrap_or(0)))
        }))
        .model("scripted", ModelOptions::default(), model)
        .build();
    let limits = AgentLimits { max_model_steps: 1, ..AgentLimits::default() };
    worker.submit("agent", "ca", &serde_json::to_value(input(limits, None)).unwrap(), &RunOptions::default()).unwrap();
    let Driven::Ended(Outcome::Cancelled { progress }) = worker.drive("ca").unwrap() else {
        panic!("the run was not cancelled");
    };
    assert_eq!(progress["answer"], json!("final: 3"));
}
