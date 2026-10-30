//! What one model call sees: the transcript after the host's transform, the newest tool results' images, and the
//! offered tools; and the journaled fold that keeps a long transcript bounded.

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use serde_json::Value;

use super::entry::{Entry, OfferedTool, RequestImage, ToolOutcome};
use crate::ctx::Blobs;
use crate::definition::Interrupt;
use crate::json;

/// The canonical encoded size of one transcript entry.
pub(crate) fn entry_bytes(entry: &Entry) -> u64 {
    let value = serde_json::to_value(entry).unwrap_or(Value::Null);
    u64::try_from(json::to_canonical(&value).len()).unwrap_or(u64::MAX)
}

/// The images of the tool results that follow the last assistant turn: only the call right after a tool returned an
/// image sees it, so each image has exactly one consumer.
pub(crate) fn fresh_images(entries: &[Entry]) -> Vec<(String, u64)> {
    let start = entries.iter().rposition(|entry| matches!(entry, Entry::Assistant { .. })).map_or(0, |index| index + 1);
    entries[start..]
        .iter()
        .filter_map(|entry| match entry {
            Entry::ToolResult { call_id, outcome: ToolOutcome::Ok { images, .. } } => Some((call_id, images)),
            _ => None,
        })
        .flat_map(|(call_id, images)| images.iter().map(move |id| (call_id.clone(), *id)))
        .collect()
}

/// Every image the tool results of `entries` carry.
pub(crate) fn images_of(entries: &[Entry]) -> Vec<u64> {
    entries
        .iter()
        .filter_map(|entry| match entry {
            Entry::ToolResult { outcome: ToolOutcome::Ok { images, .. }, .. } => Some(images.iter().copied()),
            _ => None,
        })
        .flatten()
        .collect()
}

/// The newest tool findings, at most [`FINDINGS`] of them, each cut to [`FINDING_BYTES`] of canonical JSON.
pub(crate) fn findings(entries: &[&Entry]) -> Vec<Value> {
    let mut found: Vec<Value> = entries
        .iter()
        .rev()
        .filter_map(|entry| match entry {
            Entry::ToolResult { outcome: ToolOutcome::Ok { value, .. }, .. } => Some(bounded(value)),
            _ => None,
        })
        .take(FINDINGS)
        .collect();
    found.reverse();
    found
}

const FINDINGS: usize = 8;
const FINDING_BYTES: usize = 2_048;
const TEXT_BYTES: usize = 4_096;

/// `text` cut to [`TEXT_BYTES`] at a character boundary.
pub(crate) fn bounded_text(text: &str) -> String {
    if text.len() <= TEXT_BYTES {
        return text.to_owned();
    }
    let mut cut = TEXT_BYTES;
    while !text.is_char_boundary(cut) {
        cut -= 1;
    }
    format!("{}…", &text[..cut])
}

pub(crate) fn bounded(value: &Value) -> Value {
    let encoded = json::to_canonical(value);
    if encoded.len() <= FINDING_BYTES {
        return value.clone();
    }
    let mut cut = FINDING_BYTES;
    while !encoded.is_char_boundary(cut) {
        cut -= 1;
    }
    Value::String(format!("{}…", &encoded[..cut]))
}

/// The image's media type from its leading bytes, or `None` when it is not a format models read.
pub(crate) fn media_type(data: &[u8]) -> Option<&'static str> {
    if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        Some("image/png")
    } else if data.starts_with(&[0xff, 0xd8, 0xff]) {
        Some("image/jpeg")
    } else if data.len() >= 12 && data.starts_with(b"RIFF") && &data[8..12] == b"WEBP" {
        Some("image/webp")
    } else if data.starts_with(b"GIF87a") || data.starts_with(b"GIF89a") {
        Some("image/gif")
    } else {
        None
    }
}

/// Reads the fresh images for a request; a released or unreadable image is left out.
pub(crate) fn images(blobs: &Blobs<'_>, fresh: &[(String, u64)]) -> Result<Vec<RequestImage>, Interrupt> {
    let mut images = Vec::new();
    for (call_id, id) in fresh {
        let Some(data) = blobs.get(*id)? else {
            continue;
        };
        if let Some(media_type) = media_type(&data) {
            images.push(RequestImage {
                call_id: call_id.clone(),
                media_type: media_type.into(),
                data: STANDARD.encode(&data),
            });
        }
    }
    Ok(images)
}

/// Where a fold may cut the transcript: the newest boundary that leaves at least `keep_last` entries and never
/// separates a tool call from its result.
pub(crate) fn fold_cut(entries: &[Entry], keep_last: usize) -> Option<usize> {
    let latest = entries.len().checked_sub(keep_last)?;
    (1..=latest).rev().find(|&cut| !matches!(entries.get(cut), Some(Entry::ToolResult { .. })))
}

/// The tools as the model sees them.
pub(crate) fn offered(specs: &[super::entry::ToolSpec]) -> Vec<OfferedTool> {
    specs
        .iter()
        .map(|spec| OfferedTool {
            name: spec.name.clone(),
            description: spec.description.clone(),
            parameters: spec.parameters.clone(),
            strict: spec.strict,
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agent::entry::Part;
    use serde_json::json;

    fn result(call_id: &str, images: Vec<u64>) -> Entry {
        Entry::ToolResult { call_id: call_id.into(), outcome: ToolOutcome::Ok { value: json!(1), images } }
    }

    fn assistant() -> Entry {
        Entry::Assistant { parts: vec![Part::Text { text: "t".into() }] }
    }

    #[test]
    fn only_results_after_the_last_assistant_turn_show_images() {
        let entries = vec![result("old", vec![1]), assistant(), result("new", vec![2, 3]), result("plain", vec![])];
        assert_eq!(fresh_images(&entries), [("new".to_owned(), 2), ("new".to_owned(), 3)]);
        assert_eq!(fresh_images(&[result("first", vec![4])]), [("first".to_owned(), 4)]);
    }

    #[test]
    fn findings_are_the_newest_bounded_results() {
        let big = Entry::ToolResult {
            call_id: "big".into(),
            outcome: ToolOutcome::Ok { value: json!("é".repeat(3_000)), images: vec![] },
        };
        let mut entries: Vec<Entry> = (0..10).map(|index| result(&format!("c{index}"), vec![index])).collect();
        entries.push(big);
        let found = findings(&entries.iter().collect::<Vec<_>>());
        assert_eq!(found.len(), FINDINGS);
        assert!(found.last().unwrap().as_str().unwrap().ends_with('…'));
        assert!(found.last().unwrap().as_str().unwrap().len() <= FINDING_BYTES + 4);
        assert_eq!(images_of(&entries[..3]), [0, 1, 2]);
    }

    #[test]
    fn texts_are_bounded_at_character_boundaries() {
        assert_eq!(bounded_text("short"), "short");
        let long = bounded_text(&format!("a{}", "é".repeat(3_000)));
        assert!(long.ends_with('…') && long.len() <= TEXT_BYTES + 3);
    }

    #[test]
    fn media_types_are_sniffed() {
        assert_eq!(media_type(b"\x89PNG\r\n\x1a\nrest"), Some("image/png"));
        assert_eq!(media_type(&[0xff, 0xd8, 0xff, 0xe0]), Some("image/jpeg"));
        assert_eq!(media_type(b"RIFF\0\0\0\0WEBPVP8 "), Some("image/webp"));
        assert_eq!(media_type(b"GIF89a"), Some("image/gif"));
        assert_eq!(media_type(b"GIF87a"), Some("image/gif"));
        assert_eq!(media_type(b"RIFF\0\0\0\0WAVE"), None);
        assert_eq!(media_type(b"%PDF"), None);
        assert_eq!(media_type(b"RIFF"), None);
        assert_eq!(media_type(b"%PDF-1.7 and more bytes"), None);
    }

    #[test]
    fn folds_never_separate_a_call_from_its_result() {
        let entries =
            vec![Entry::User { text: "u".into() }, assistant(), result("c", vec![]), assistant(), result("d", vec![])];
        assert_eq!(fold_cut(&entries, 1), Some(3));
        assert_eq!(fold_cut(&entries, 2), Some(3));
        assert_eq!(fold_cut(&entries, 3), Some(1));
        assert_eq!(fold_cut(&entries, 4), Some(1));
        assert_eq!(fold_cut(&entries, 5), None);
        assert_eq!(fold_cut(&entries, 9), None);
    }

    #[test]
    fn entries_are_measured_canonically() {
        let entry = Entry::User { text: "x".repeat(100) };
        let encoded = json::to_canonical(&serde_json::to_value(&entry).unwrap());
        assert_eq!(entry_bytes(&entry), u64::try_from(encoded.len()).unwrap());
    }
}
