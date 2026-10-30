//! Determine an attachment's kind and media type from its bytes; names and uploaded types grant nothing.

use team_protocol::python::text;

use crate::limits::MAX_TEXT_SOURCE_BYTES;

/// The media type of a file Team cannot read.
pub const OCTET_STREAM: &str = "application/octet-stream";

const UTF8_BOM: &[u8] = b"\xef\xbb\xbf";
// A Markdown or CSV extension only names a text subtype; the bytes must still be valid text.
const TEXT_EXTENSIONS: [(&str, &str); 4] = [
    (".csv", "text/csv"),
    (".json", "application/json"),
    (".md", "text/markdown"),
    (".markdown", "text/markdown"),
];

/// A file's kind.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// UTF-8 text, read by the controller.
    Text,
    /// A PDF, read by the helper.
    Pdf,
    /// A JPEG, PNG, or WebP, read by the helper.
    Image,
    /// Anything else.
    Opaque,
}

/// A file's kind and the media type Team determined from its bytes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Detected {
    /// Its kind.
    pub kind: Kind,
    /// Its media type.
    pub media_type: &'static str,
}

/// Classifies bytes as an image, a PDF, UTF-8 text, or opaque.
#[must_use]
pub fn detect(name: &str, data: &[u8]) -> Detected {
    let found = |kind, media_type| Detected { kind, media_type };
    if data.starts_with(b"\xff\xd8\xff") {
        return found(Kind::Image, "image/jpeg");
    }
    if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        return found(Kind::Image, "image/png");
    }
    if data.len() >= 12 && data.starts_with(b"RIFF") && &data[8..12] == b"WEBP" {
        return found(Kind::Image, "image/webp");
    }
    if data.starts_with(b"%PDF-") {
        return found(Kind::Pdf, "application/pdf");
    }
    if text_prefix(data) {
        return found(Kind::Text, text_media_type(name, data));
    }
    found(Kind::Opaque, OCTET_STREAM)
}

/// Whether the bytes read as text, deciding from at most the text source ceiling without decoding more.
///
/// A longer file classifies as text from its prefix and is then refused for size, so no oversized text is decoded; a
/// multi-byte character the ceiling splits still counts as text.
fn text_prefix(data: &[u8]) -> bool {
    let complete = data.len() <= MAX_TEXT_SOURCE_BYTES;
    let prefix = &data[..data.len().min(MAX_TEXT_SOURCE_BYTES)];
    let body = prefix.strip_prefix(UTF8_BOM).unwrap_or(prefix);
    if body.is_empty() {
        return false;
    }
    match std::str::from_utf8(body) {
        Ok(text) => printable(text),
        Err(error) if !complete && error.error_len().is_none() => {
            std::str::from_utf8(&body[..error.valid_up_to()]).is_ok_and(printable)
        }
        Err(_) => false,
    }
}

/// The text of strict UTF-8 bytes, with an optional BOM, or `None` when the bytes are not text or exceed the ceiling.
#[must_use]
pub fn decode_text(data: &[u8]) -> Option<&str> {
    let body = data.strip_prefix(UTF8_BOM).unwrap_or(data);
    if body.is_empty() || data.len() > MAX_TEXT_SOURCE_BYTES {
        return None;
    }
    std::str::from_utf8(body)
        .ok()
        .filter(|text| printable(text))
}

/// Tab, line feed, form feed, and carriage return are the only control characters admitted in text.
fn printable(text: &str) -> bool {
    !text.chars().any(|character| {
        (character < ' ' && !"\t\n\x0c\r".contains(character)) || character == '\x7f'
    })
}

fn text_media_type(name: &str, data: &[u8]) -> &'static str {
    let suffix = text::lower(suffix(name));
    let media_type = TEXT_EXTENSIONS
        .iter()
        .find(|(extension, _)| *extension == suffix)
        .map_or("text/plain", |(_, media_type)| *media_type);
    if media_type == "application/json" && !valid_json(data) {
        return "text/plain";
    }
    media_type
}

/// `PurePosixPath(name).suffix`: from the last dot of the last component, unless that dot starts or ends it.
fn suffix(name: &str) -> &str {
    let last = name.rsplit('/').next().unwrap_or(name);
    match last.rfind('.') {
        Some(dot) if dot > 0 && dot + 1 < last.len() => &last[dot..],
        _ => "",
    }
}

fn valid_json(data: &[u8]) -> bool {
    data.len() <= MAX_TEXT_SOURCE_BYTES
        && strict_json::parse(data.strip_prefix(UTF8_BOM).unwrap_or(data)).is_ok()
}

#[cfg(test)]
mod tests;
