//! The OpenAI Responses API: unstored calls (`store: false`) that carry reasoning between calls as encrypted content
//! the adapter replays, strict function tools, and `incomplete` answers reported as truncated.

use serde_json::{Map, Value, json};

use super::{Client, Pricing, cost, key, memory_text, reserve};
use crate::agent::entry::{Entry, ModelRequest, ModelResponse, Part, StopReason, ToolOutcome, Usage};
use crate::handler::{Model, ModelCall, ModelReply};
use crate::json as canonical;
use crate::policy::Failure;

const PROVIDER: &str = "openai";

/// An OpenAI Responses model.
#[derive(Debug)]
pub struct OpenAi {
    client: Client,
    base_url: String,
    pricing: Pricing,
}

impl OpenAi {
    /// A model served at `base_url` (`https://api.openai.com` in production), priced by `pricing`.
    #[must_use]
    pub fn new(base_url: &str, pricing: Pricing) -> Self {
        Self { client: Client::new(), base_url: base_url.trim_end_matches('/').to_owned(), pricing }
    }
}

impl Model for OpenAi {
    fn reserve(&self, request: &Value) -> Option<u64> {
        Some(reserve(&self.pricing, request))
    }

    fn call(&self, call: ModelCall<'_>) -> Result<ModelReply, Failure> {
        let request: ModelRequest = serde_json::from_value(call.request.clone())
            .map_err(|_| Failure::permanent("bad-request", "the request is not a model request").with_ended())?;
        let authorization = format!("Bearer {}", key(call.credentials)?);
        let url = format!("{}/v1/responses", self.base_url);
        let answer = self.client.post(
            &url,
            &[("authorization", &authorization)],
            &wire_request(&request),
            call.attempt.deadline_ms,
        )?;
        let response = parse_response(&answer)?;
        let reported = answer["usage"]["input_tokens"].is_u64() && answer["usage"]["output_tokens"].is_u64();
        let cost = cost(&self.pricing, reported.then_some(&response.usage), call.request);
        let response =
            serde_json::to_value(response).map_err(|_| Failure::permanent("bad-provider-response", "unencodable"))?;
        Ok(ModelReply { response, cost })
    }
}

fn text_item(role: &str, kind: &str, text: &str) -> Value {
    json!({ "role": role, "content": [{ "type": kind, "text": text }] })
}

fn assistant_items(parts: &[Part], items: &mut Vec<Value>) {
    for part in parts {
        match part {
            Part::Text { text } => {
                items.push(json!({ "type": "message", "role": "assistant", "content": [{ "type": "output_text", "text": text }] }));
            }
            Part::ToolCall { id, name, arguments } => {
                let arguments = canonical::to_canonical(arguments);
                items.push(json!({ "type": "function_call", "call_id": id, "name": name, "arguments": arguments }));
            }
            Part::Opaque { provider, payload, .. } if provider == PROVIDER => items.push(payload.clone()),
            Part::Opaque { .. } => {}
        }
    }
}

fn outcome_text(outcome: &ToolOutcome) -> String {
    canonical::to_canonical(&serde_json::to_value(outcome).unwrap_or(Value::Null))
}

/// The Responses API body for `request`.
#[must_use]
pub fn wire_request(request: &ModelRequest) -> Value {
    let mut items = Vec::new();
    for entry in &request.entries {
        match entry {
            Entry::User { text } => items.push(text_item("user", "input_text", text)),
            Entry::Notice { text } => items.push(text_item("developer", "input_text", text)),
            Entry::Memory { text } => items.push(text_item("user", "input_text", &memory_text(text))),
            Entry::Assistant { parts } => assistant_items(parts, &mut items),
            Entry::ToolResult { call_id, outcome } => {
                items.push(
                    json!({ "type": "function_call_output", "call_id": call_id, "output": outcome_text(outcome) }),
                );
                let images: Vec<Value> = request
                    .images
                    .iter()
                    .filter(|image| &image.call_id == call_id)
                    .map(|image| json!({ "type": "input_image", "image_url": format!("data:{};base64,{}", image.media_type, image.data) }))
                    .collect();
                if !images.is_empty() {
                    items.push(json!({ "role": "user", "content": images }));
                }
            }
        }
    }
    let mut body = Map::new();
    body.insert("model".into(), json!(request.model));
    body.insert("instructions".into(), json!(request.system));
    body.insert("input".into(), Value::Array(items));
    body.insert("max_output_tokens".into(), json!(request.max_output_tokens));
    body.insert("store".into(), json!(false));
    body.insert("include".into(), json!(["reasoning.encrypted_content"]));
    if let Some(effort) = &request.effort {
        body.insert("reasoning".into(), json!({ "effort": effort }));
    }
    if !request.tools.is_empty() {
        let tools: Vec<Value> = request
            .tools
            .iter()
            .map(|tool| json!({ "type": "function", "name": tool.name, "description": tool.description, "parameters": tool.parameters, "strict": tool.strict }))
            .collect();
        body.insert("tools".into(), Value::Array(tools));
    }
    Value::Object(body)
}

/// An answer the adapter cannot read; the call may have been billed, so its reservation stays.
fn bad(what: &str) -> Failure {
    Failure::permanent("bad-provider-response", what)
}

/// The provider-neutral response of a Responses API answer.
///
/// # Errors
///
/// A `bad-provider-response` failure when the answer has an unexpected shape.
pub fn parse_response(answer: &Value) -> Result<ModelResponse, Failure> {
    let output = answer["output"].as_array().ok_or_else(|| bad("no output"))?;
    let mut parts = Vec::new();
    let mut refused = false;
    for item in output {
        match item["type"].as_str() {
            Some("reasoning") => {
                parts.push(Part::Opaque { provider: PROVIDER.into(), kind: "reasoning".into(), payload: item.clone() });
            }
            Some("function_call") => parts.push(function_call(item)?),
            Some("message") => refused |= message_parts(item, &mut parts)?,
            _ => {}
        }
    }
    let stop = match (answer["status"].as_str(), answer["incomplete_details"]["reason"].as_str()) {
        (Some("incomplete"), Some("content_filter")) => StopReason::Refusal,
        (Some("incomplete"), _) => StopReason::MaxTokens,
        (Some("completed"), _) if refused => StopReason::Refusal,
        (Some("completed"), _) if parts.iter().any(|part| matches!(part, Part::ToolCall { .. })) => StopReason::ToolUse,
        (Some("completed"), _) => StopReason::EndTurn,
        // `failed`, `cancelled`, an unfinished status, or none: never a successful answer.
        _ => return Err(Failure::retryable("provider-failed", "the provider did not complete the answer")),
    };
    Ok(ModelResponse { parts, stop, usage: usage(&answer["usage"]) })
}

fn function_call(item: &Value) -> Result<Part, Failure> {
    let id = item["call_id"].as_str().ok_or_else(|| bad("a function call without an id"))?;
    let name = item["name"].as_str().ok_or_else(|| bad("a function call without a name"))?;
    let raw = item["arguments"].as_str().unwrap_or("");
    // Arguments that are not JSON are kept as text, so the tool's validation answers the model.
    let arguments = canonical::parse(raw.as_bytes(), 1 << 20).unwrap_or_else(|_| Value::String(raw.to_owned()));
    Ok(Part::ToolCall { id: id.into(), name: name.into(), arguments })
}

fn message_parts(item: &Value, parts: &mut Vec<Part>) -> Result<bool, Failure> {
    let mut refused = false;
    for content in item["content"].as_array().ok_or_else(|| bad("a message without content"))? {
        match content["type"].as_str() {
            Some("output_text") => parts.push(Part::Text { text: content["text"].as_str().unwrap_or_default().into() }),
            Some("refusal") => {
                refused = true;
                parts.push(Part::Text { text: content["refusal"].as_str().unwrap_or_default().into() });
            }
            _ => {}
        }
    }
    Ok(refused)
}

fn usage(usage: &Value) -> Usage {
    let input = usage["input_tokens"].as_u64().unwrap_or(0);
    let cached = usage["input_tokens_details"]["cached_tokens"].as_u64().unwrap_or(0);
    Usage {
        input: input.saturating_sub(cached),
        output: usage["output_tokens"].as_u64().unwrap_or(0),
        cache_read: cached,
        cache_write: 0,
    }
}
