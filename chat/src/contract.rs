//! The closed message a turn's start carries, and the committed history window it may carry beside it.

use strict_json::Value;
use team_inference::runtime::ConversationEntry;
use team_protocol::payload::canonical_conversation;
use team_protocol::python::json::dumps;
use team_protocol::python::value::{object_of, string};

use crate::orchestrator::BODY;

/// The prompt of a new turn: its selected files' metadata and the person's message, as compact JSON.
#[must_use]
pub fn build_prompt(message: &str, files: &[Value]) -> String {
    let files = files
        .iter()
        .map(|item| {
            let field = |name: &str| item.get(name).cloned().unwrap_or(Value::Null);
            object_of(vec![
                ("id", field("id")),
                ("name", field("name")),
                ("media_type", field("media_type")),
                ("size", field("size")),
            ])
        })
        .collect();
    let request = object_of(vec![
        ("files", Value::Array(files)),
        ("message", Value::String(message.to_owned())),
    ]);
    dumps(&request, BODY)
}

/// Admits one untrusted window of committed presentation history through Team's HTTP reference validator.
#[must_use]
pub fn conversation_window(value: &Value) -> Option<Vec<ConversationEntry>> {
    let Value::Array(entries) = canonical_conversation(value)? else {
        return None;
    };
    entries
        .iter()
        .map(|entry| {
            Some(ConversationEntry {
                role: string(entry.get("role")?)?.to_owned(),
                text: string(entry.get("text")?)?.to_owned(),
                truncated: matches!(entry.get("truncated")?, Value::Bool(true)),
            })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use strict_json::Value;

    use super::{build_prompt, conversation_window};

    fn json(text: &str) -> Value {
        strict_json::parse(text.as_bytes()).expect("json")
    }

    #[test]
    fn the_prompt_carries_only_file_metadata_and_the_message() {
        let files = [json(
            r#"{"id":"f1","name":"notes.md","media_type":"text/markdown","size":5,"sha256":"x","content":"secret"}"#,
        )];
        assert_eq!(
            build_prompt("Olá", &files),
            r#"{"files":[{"id":"f1","name":"notes.md","media_type":"text/markdown","size":5}],"message":"Olá"}"#
        );
    }

    #[test]
    fn a_conversation_window_is_admitted_whole_or_refused() {
        let window = conversation_window(&json(
            r#"[{"role":"user","text":"List my zones","truncated":false},{"role":"assistant","text":"Done.","truncated":true}]"#,
        ))
        .expect("window");
        assert_eq!(window.len(), 2);
        assert!(window[1].truncated);
        assert_eq!(
            conversation_window(&json(r#"[{"role":"system","text":"x","truncated":false}]"#)),
            None
        );
        assert_eq!(conversation_window(&json("{}")), None);
    }
}
