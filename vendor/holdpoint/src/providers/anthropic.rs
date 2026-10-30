//! The Anthropic Messages API: thinking blocks replayed in order with their signatures, tool use paired with its
//! results, prompt-cache breakpoints on the system prompt and the newest block, and `max_tokens` reported as
//! truncated.

use serde_json::{Map, Value, json};

use super::{Client, Pricing, cost, key, memory_text, reserve};
use crate::agent::entry::{Entry, ModelRequest, ModelResponse, Part, RequestImage, StopReason, ToolOutcome, Usage};
use crate::handler::{Model, ModelCall, ModelReply};
use crate::json as canonical;
use crate::policy::Failure;

const PROVIDER: &str = "anthropic";
const VERSION: &str = "2023-06-01";

/// An Anthropic Messages model.
#[derive(Debug)]
pub struct Anthropic {
    client: Client,
    base_url: String,
    pricing: Pricing,
}

impl Anthropic {
    /// A model served at `base_url` (`https://api.anthropic.com` in production), priced by `pricing`.
    #[must_use]
    pub fn new(base_url: &str, pricing: Pricing) -> Self {
        Self { client: Client::new(), base_url: base_url.trim_end_matches('/').to_owned(), pricing }
    }
}

impl Model for Anthropic {
    fn reserve(&self, request: &Value) -> Option<u64> {
        Some(reserve(&self.pricing, request))
    }

    fn call(&self, call: ModelCall<'_>) -> Result<ModelReply, Failure> {
        let request: ModelRequest = serde_json::from_value(call.request.clone())
            .map_err(|_| Failure::permanent("bad-request", "the request is not a model request").with_ended())?;
        let api_key = key(call.credentials)?;
        let url = format!("{}/v1/messages", self.base_url);
        let headers = [("x-api-key", api_key), ("anthropic-version", VERSION)];
        let answer = self.client.post(&url, &headers, &wire_request(&request), call.attempt.deadline_ms)?;
        let response = parse_response(&answer)?;
        let reported = answer["usage"]["input_tokens"].is_u64() && answer["usage"]["output_tokens"].is_u64();
        let cost = cost(&self.pricing, reported.then_some(&response.usage), call.request);
        let response =
            serde_json::to_value(response).map_err(|_| Failure::permanent("bad-provider-response", "unencodable"))?;
        Ok(ModelReply { response, cost })
    }
}

/// Builds alternating messages, merging consecutive blocks of the same role.
#[derive(Default)]
struct Messages {
    messages: Vec<(&'static str, Vec<Value>)>,
}

impl Messages {
    fn push(&mut self, role: &'static str, block: Value) {
        match self.messages.last_mut() {
            Some((last, blocks)) if *last == role => blocks.push(block),
            _ => self.messages.push((role, vec![block])),
        }
    }

    fn into_values(mut self) -> Vec<Value> {
        if let Some(block) = self.messages.last_mut().and_then(|(_, blocks)| blocks.last_mut()) {
            block["cache_control"] = json!({ "type": "ephemeral" });
        }
        self.messages.into_iter().map(|(role, content)| json!({ "role": role, "content": content })).collect()
    }
}

fn tool_result(call_id: &str, outcome: &ToolOutcome, images: &[RequestImage]) -> Value {
    let mut content = vec![
        json!({ "type": "text", "text": canonical::to_canonical(&serde_json::to_value(outcome).unwrap_or(Value::Null)) }),
    ];
    content.extend(images.iter().filter(|image| image.call_id == call_id).map(|image| {
        json!({ "type": "image", "source": { "type": "base64", "media_type": image.media_type, "data": image.data } })
    }));
    let is_error = matches!(outcome, ToolOutcome::Error { .. } | ToolOutcome::Denied { .. });
    json!({ "type": "tool_result", "tool_use_id": call_id, "content": content, "is_error": is_error })
}

fn assistant_blocks(parts: &[Part], messages: &mut Messages) {
    for part in parts {
        match part {
            Part::Text { text } if !text.is_empty() => {
                messages.push("assistant", json!({ "type": "text", "text": text }));
            }
            Part::ToolCall { id, name, arguments } => {
                messages.push("assistant", json!({ "type": "tool_use", "id": id, "name": name, "input": arguments }));
            }
            Part::Opaque { provider, payload, .. } if provider == PROVIDER => {
                messages.push("assistant", payload.clone());
            }
            Part::Text { .. } | Part::Opaque { .. } => {}
        }
    }
}

/// The Messages API body for `request`.
#[must_use]
pub fn wire_request(request: &ModelRequest) -> Value {
    let mut messages = Messages::default();
    for entry in &request.entries {
        match entry {
            Entry::User { text } => messages.push("user", json!({ "type": "text", "text": text })),
            Entry::Memory { text } => messages.push("user", json!({ "type": "text", "text": memory_text(text) })),
            Entry::Notice { text } => {
                messages.push("user", json!({ "type": "text", "text": format!("[notice] {text}") }));
            }
            Entry::Assistant { parts } => assistant_blocks(parts, &mut messages),
            Entry::ToolResult { call_id, outcome } => {
                messages.push("user", tool_result(call_id, outcome, &request.images));
            }
        }
    }
    let mut body = Map::new();
    body.insert("model".into(), json!(request.model));
    body.insert("max_tokens".into(), json!(request.max_output_tokens));
    body.insert(
        "system".into(),
        json!([{ "type": "text", "text": request.system, "cache_control": { "type": "ephemeral" } }]),
    );
    body.insert("messages".into(), Value::Array(messages.into_values()));
    if let Some(effort) = &request.effort {
        body.insert("output_config".into(), json!({ "effort": effort }));
    }
    if !request.tools.is_empty() {
        let tools: Vec<Value> = request
            .tools
            .iter()
            .map(|tool| json!({ "name": tool.name, "description": tool.description, "input_schema": tool.parameters }))
            .collect();
        body.insert("tools".into(), Value::Array(tools));
    }
    Value::Object(body)
}

/// An answer the adapter cannot read; the call may have been billed, so its reservation stays.
fn bad(what: &str) -> Failure {
    Failure::permanent("bad-provider-response", what)
}

/// The provider-neutral response of a Messages API answer.
///
/// # Errors
///
/// A `bad-provider-response` failure when the answer has an unexpected shape.
pub fn parse_response(answer: &Value) -> Result<ModelResponse, Failure> {
    let content = answer["content"].as_array().ok_or_else(|| bad("no content"))?;
    let mut parts = Vec::new();
    for block in content {
        match block["type"].as_str() {
            Some("text") => parts.push(Part::Text { text: block["text"].as_str().unwrap_or_default().into() }),
            Some("tool_use") => {
                let id = block["id"].as_str().ok_or_else(|| bad("a tool use without an id"))?;
                let name = block["name"].as_str().ok_or_else(|| bad("a tool use without a name"))?;
                parts.push(Part::ToolCall { id: id.into(), name: name.into(), arguments: block["input"].clone() });
            }
            Some(kind @ ("thinking" | "redacted_thinking")) => {
                parts.push(Part::Opaque { provider: PROVIDER.into(), kind: kind.into(), payload: block.clone() });
            }
            _ => {}
        }
    }
    let stop = match answer["stop_reason"].as_str() {
        Some("end_turn" | "stop_sequence") => StopReason::EndTurn,
        Some("max_tokens") => StopReason::MaxTokens,
        Some("refusal") => StopReason::Refusal,
        Some("tool_use") => StopReason::ToolUse,
        _ => return Err(Failure::retryable("provider-failed", "the provider's answer has no known stop reason")),
    };
    let usage = &answer["usage"];
    let usage = Usage {
        input: usage["input_tokens"].as_u64().unwrap_or(0),
        output: usage["output_tokens"].as_u64().unwrap_or(0),
        cache_read: usage["cache_read_input_tokens"].as_u64().unwrap_or(0),
        cache_write: usage["cache_creation_input_tokens"].as_u64().unwrap_or(0),
    };
    Ok(ModelResponse { parts, stop, usage })
}
