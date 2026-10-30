//! The agent's transcript entries and its provider-neutral model request and response.
//!
//! Entries are distinct from any provider's messages; adapters translate both ways. An [`Part::Opaque`] carries a
//! provider's own continuation (OpenAI encrypted reasoning, Anthropic thinking with its signature) and is replayed
//! byte for byte to the same provider only.

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// One entry of the agent's transcript.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "role", rename_all = "snake_case", deny_unknown_fields)]
pub enum Entry {
    /// A person's message.
    User {
        /// The message.
        text: String,
    },
    /// The model's turn.
    Assistant {
        /// What the model produced, in order.
        parts: Vec<Part>,
    },
    /// The result of one tool call.
    ToolResult {
        /// The call this answers.
        call_id: String,
        /// The result.
        outcome: ToolOutcome,
    },
    /// A trusted runtime or host notice (a bound reached, a lost resource), rendered with instruction priority.
    Notice {
        /// The notice.
        text: String,
    },
    /// The model's own summary of earlier work, from tool results it read: untrusted data, never instructions.
    Memory {
        /// The summary.
        text: String,
    },
}

/// A part of the model's turn.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum Part {
    /// Text for the person.
    Text {
        /// The text.
        text: String,
    },
    /// A tool call.
    ToolCall {
        /// The provider's call identifier.
        id: String,
        /// The tool's name.
        name: String,
        /// The call's arguments.
        arguments: Value,
    },
    /// A provider continuation replayed only to the same provider.
    Opaque {
        /// The provider that produced it.
        provider: String,
        /// Its kind, such as `reasoning` or `thinking`.
        kind: String,
        /// Its content, exactly as received.
        payload: Value,
    },
}

/// The result of a tool call, after Pydantic AI's closed outcome kinds.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "outcome", rename_all = "snake_case", deny_unknown_fields)]
pub enum ToolOutcome {
    /// The tool's result.
    Ok {
        /// The result value.
        value: Value,
        /// Transient images the result carries (blob ids), shown to the next model call only.
        #[serde(default, skip_serializing_if = "Vec::is_empty")]
        images: Vec<u64>,
    },
    /// The tool failed.
    Error {
        /// A short machine name.
        code: String,
        /// A message for the model.
        message: String,
    },
    /// The host or a person refused the call.
    Denied {
        /// Why.
        reason: String,
    },
    /// The model should call again differently.
    Retry {
        /// What to change.
        message: String,
    },
    /// An interrupted effect: it may or may not have happened.
    Uncertain,
}

impl ToolOutcome {
    /// An error outcome.
    #[must_use]
    pub fn error(code: &str, message: &str) -> Self {
        Self::Error { code: code.into(), message: message.into() }
    }
}

/// What a tool does when called.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ToolKind {
    /// A pure computation registered on the [`crate::Agent`].
    Local,
    /// An activity registered on the worker under the tool's name.
    Activity,
    /// A request the host answers later (a person, or work another system runs).
    Deferred,
    /// The run's structured result; it must be the response's only call.
    Final,
}

/// A tool offered to the model.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolSpec {
    /// The tool's name.
    pub name: String,
    /// What it does, for the model.
    pub description: String,
    /// The JSON Schema of its arguments.
    pub parameters: Value,
    /// How a call is carried out.
    pub kind: ToolKind,
    /// Whether the provider enforces the schema strictly (it must then be in the strict subset).
    #[serde(default)]
    pub strict: bool,
    /// Whether Holdpoint validates the arguments against `parameters` (the schema must be in its subset); when
    /// `false` the host validates them itself.
    #[serde(default = "yes")]
    pub validate: bool,
}

fn yes() -> bool {
    true
}

/// A tool as the model sees it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OfferedTool {
    /// The tool's name.
    pub name: String,
    /// What it does.
    pub description: String,
    /// Its argument schema.
    pub parameters: Value,
    /// Whether the provider enforces the schema strictly.
    pub strict: bool,
}

/// A part of a request entry: the transcript, with images resolved for this call.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RequestImage {
    /// The call the image belongs to.
    pub call_id: String,
    /// The image's media type.
    pub media_type: String,
    /// The image, base64-encoded.
    pub data: String,
}

/// What the agent asks a model.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelRequest {
    /// The provider model name.
    pub model: String,
    /// The system prompt.
    pub system: String,
    /// The transcript as this call sees it.
    pub entries: Vec<Entry>,
    /// Images shown with this call, attached to their tool results.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub images: Vec<RequestImage>,
    /// The tools offered to this call (none for a final answer or a fold).
    pub tools: Vec<OfferedTool>,
    /// The output bound.
    pub max_output_tokens: u32,
    /// The provider's reasoning effort, if any.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub effort: Option<String>,
}

/// Why the model stopped.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StopReason {
    /// It finished its turn.
    EndTurn,
    /// It called tools.
    ToolUse,
    /// Its output bound cut it off: never accepted (R10).
    MaxTokens,
    /// It refused.
    Refusal,
}

/// Token counts of one call.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Usage {
    /// Uncached input tokens.
    pub input: u64,
    /// Output tokens (reasoning included).
    pub output: u64,
    /// Input tokens read from the provider's cache.
    #[serde(default)]
    pub cache_read: u64,
    /// Input tokens written to the provider's cache.
    #[serde(default)]
    pub cache_write: u64,
}

/// A model's answer, provider-neutral.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelResponse {
    /// What the model produced, in order.
    pub parts: Vec<Part>,
    /// Why it stopped.
    pub stop: StopReason,
    /// Its token counts.
    #[serde(default)]
    pub usage: Usage,
}

impl ModelResponse {
    /// The response's text parts, joined.
    #[must_use]
    pub fn text(&self) -> String {
        text_of(&self.parts)
    }
}

/// The text parts of `parts`, joined.
#[must_use]
pub fn text_of(parts: &[Part]) -> String {
    parts
        .iter()
        .filter_map(|part| match part {
            Part::Text { text } => Some(text.as_str()),
            Part::ToolCall { .. } | Part::Opaque { .. } => None,
        })
        .collect::<Vec<_>>()
        .join("")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn entries_round_trip_and_refuse_unknown_fields() {
        let entries = vec![
            Entry::User { text: "hi".into() },
            Entry::Assistant {
                parts: vec![
                    Part::Text { text: "a".into() },
                    Part::ToolCall { id: "c1".into(), name: "t".into(), arguments: json!({}) },
                    Part::Opaque { provider: "openai".into(), kind: "reasoning".into(), payload: json!("x") },
                ],
            },
            Entry::ToolResult { call_id: "c1".into(), outcome: ToolOutcome::Ok { value: json!(1), images: vec![2] } },
            Entry::ToolResult { call_id: "c2".into(), outcome: ToolOutcome::Uncertain },
            Entry::Notice { text: "n".into() },
            Entry::Memory { text: "m".into() },
        ];
        let value = serde_json::to_value(&entries).unwrap();
        assert_eq!(serde_json::from_value::<Vec<Entry>>(value).unwrap(), entries);
        assert!(serde_json::from_value::<Entry>(json!({"role": "user", "text": "x", "extra": 1})).is_err());
    }

    #[test]
    fn tool_specs_validate_by_default() {
        let spec: ToolSpec = serde_json::from_value(json!({
            "name": "t", "description": "d", "parameters": {}, "kind": "deferred"
        }))
        .unwrap();
        assert!(spec.validate);
        assert!(!spec.strict);
    }

    #[test]
    fn response_text_joins_text_parts() {
        let response = ModelResponse {
            parts: vec![
                Part::Text { text: "a".into() },
                Part::ToolCall { id: "c".into(), name: "t".into(), arguments: json!({}) },
                Part::Text { text: "b".into() },
            ],
            stop: StopReason::ToolUse,
            usage: Usage::default(),
        };
        assert_eq!(response.text(), "ab");
        assert_eq!(ToolOutcome::error("x", "y"), ToolOutcome::Error { code: "x".into(), message: "y".into() });
    }
}
