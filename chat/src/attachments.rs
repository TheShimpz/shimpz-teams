//! Chat attachment rules for every Team turn (ADR-0093).
//!
//! Team alone reads the selected files, prepares them for the message, and rehydrates them for every segment of the
//! logical turn; Brain receives them only as request-local content. While any attachment's text or image is in the
//! turn, only Actions that declare an authorization capability are admitted, so file content can leave only through an
//! Action a person authorizes.

use std::collections::BTreeMap;

use strict_json::Value;
use team_inference::runtime::{RuntimeAction, RuntimeContext};
use team_protocol::python::value::string;

/// The prepared contents a provider reads.
pub const READABLE: [&str; 2] = ["text", "image"];

/// Whether any attachment's text or image content is in the turn's provider projection.
#[must_use]
pub fn reads_content(attachments: &[Value]) -> bool {
    attachments.iter().any(|item| {
        matches!(item.get("content"), Some(content @ Value::Object(_))
            if content.get("type").and_then(string).is_some_and(|kind| READABLE.contains(&kind)))
    })
}

/// The Actions a turn may run, by Assistant and Action: every declared Action, or only authorizing ones while
/// attachment content is in it.
#[must_use]
pub fn admitted_actions(context: &RuntimeContext) -> BTreeMap<(String, String), RuntimeAction> {
    let restricted = reads_content(&context.attachments);
    context
        .assistants
        .iter()
        .flat_map(|assistant| {
            assistant
                .actions
                .iter()
                .filter(move |action| !restricted || action.authorization)
                .map(move |action| ((assistant.id.clone(), action.id.clone()), action.clone()))
        })
        .collect()
}

/// The ids of the files a turn selected.
#[must_use]
pub fn selected_ids(context: &RuntimeContext) -> Vec<String> {
    context
        .attachments
        .iter()
        .filter_map(|item| item.get("id").and_then(string).map(str::to_owned))
        .collect()
}

#[cfg(test)]
mod tests {
    use strict_json::Value;

    use super::reads_content;

    fn json(text: &str) -> Value {
        strict_json::parse(text.as_bytes()).expect("json")
    }

    #[test]
    fn only_text_or_image_content_counts_as_read() {
        assert!(reads_content(&[json(
            r#"{"content":{"type":"text","text":"x","pdf":false}}"#
        )]));
        assert!(reads_content(&[json(r#"{"content":{"type":"image"}}"#)]));
        assert!(!reads_content(&[json(
            r#"{"content":{"type":"unreadable","reason":"encrypted"}}"#
        )]));
        assert!(!reads_content(&[json(r#"{"content":"text"}"#)]));
        assert!(!reads_content(&[]));
    }
}
