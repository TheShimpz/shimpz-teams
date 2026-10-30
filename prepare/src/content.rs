//! The controller's closed content branch of one attachment, built from its own text or from the helper's untrusted
//! answer, which is re-validated here before it reaches the Brain or a Supervisor's browser.
//!
//! An image answer is admitted only with exactly its fields, a known media type, sizes within the derivative's, base64
//! that is canonical and within the derivative's bytes, bytes that start with the media type's signature, and a size
//! its own header declares equal to the one answered. Anything else is `unreadable`; an opaque answer keeps its reason
//! only when the reason is a closed one.

use std::fmt::Write as _;

use base64::Engine as _;
use sha2::{Digest, Sha256};
use strict_json::{Number, Value};
use team_protocol::python::text;

use crate::answer::{MediaType, Reason, member, opaque};
use crate::limits::{
    DERIVATIVE_LONG_EDGE, DERIVATIVE_PIXELS, MAX_DERIVATIVE_BYTES, MAX_PDF_PAGES, MAX_TEXT_BYTES,
    MAX_TEXT_CHARACTERS,
};

/// A text content branch, or the opaque reason the text has none.
#[must_use]
pub fn text(found: Option<&str>, pdf: bool) -> Value {
    let Some(found) = found.filter(|found| !text::strip(found).is_empty()) else {
        return opaque(if pdf {
            Reason::NoText
        } else {
            Reason::Unreadable
        });
    };
    if text::len(found) > MAX_TEXT_CHARACTERS || found.len() > MAX_TEXT_BYTES {
        return opaque(Reason::TooLarge);
    }
    Value::Object(vec![
        member("type", "text"),
        member("text", found),
        ("pdf".to_owned(), Value::Bool(pdf)),
    ])
}

/// The content of a PDF's text answer.
#[must_use]
pub fn pdf(answer: &Value) -> Value {
    match fields(answer) {
        Some(members) if exactly(members, &["type", "text"]) && is(answer, "type", "text") => {
            text(answer.get("text").and_then(string), true)
        }
        _ => from_opaque(answer),
    }
}

/// The content of an image answer: the validated derivative and the SHA-256 of its bytes.
#[must_use]
pub fn image(answer: &Value) -> Value {
    let Some(members) = fields(answer) else {
        return opaque(Reason::Unreadable);
    };
    if !exactly(
        members,
        &["type", "media_type", "width", "height", "base64"],
    ) || !is(answer, "type", "image")
    {
        return from_opaque(answer);
    }
    match validated(answer) {
        Some((media_type, width, height, encoded, bytes)) => Value::Object(vec![
            member("type", "image"),
            member("media_type", media_type.name()),
            ("width".to_owned(), Value::Number(Number::Int(width))),
            ("height".to_owned(), Value::Number(Number::Int(height))),
            member("base64", encoded),
            member("sha256", &hex(&Sha256::digest(&bytes))),
        ]),
        None => opaque(Reason::Unreadable),
    }
}

/// One PDF page's answer: its image content and the document's page count, admitted only when the count is a whole
/// number from `page` to the page ceiling.
#[must_use]
pub fn page(answer: &Value, page: i64) -> Option<(Value, i64)> {
    let Value::Object(members) = answer else {
        return None;
    };
    let count = match answer.get("page_count") {
        Some(Value::Number(Number::Int(count))) if (page..=MAX_PDF_PAGES).contains(count) => *count,
        _ => return None,
    };
    let rest = Value::Object(
        members
            .iter()
            .filter(|(key, _)| key != "page_count")
            .cloned()
            .collect(),
    );
    Some((image(&rest), count))
}

type Validated<'a> = (MediaType, i64, i64, &'a str, Vec<u8>);

fn validated(answer: &Value) -> Option<Validated<'_>> {
    let media_type = MediaType::parse(answer.get("media_type").and_then(string)?)?;
    let side = |name| match answer.get(name) {
        Some(Value::Number(Number::Int(side))) if (1..=edge()).contains(side) => Some(*side),
        _ => None,
    };
    let (width, height) = (side("width")?, side("height")?);
    if width * height > i64::try_from(DERIVATIVE_PIXELS).ok()? {
        return None;
    }
    let encoded = answer.get("base64").and_then(string)?;
    let bytes = decoded(encoded)?;
    let declared = encoded_size(media_type, &bytes)?;
    (bytes.starts_with(media_type.signature()) && declared == (width, height))
        .then_some((media_type, width, height, encoded, bytes))
}

fn edge() -> i64 {
    i64::try_from(DERIVATIVE_LONG_EDGE).unwrap_or(i64::MAX)
}

/// The bytes of canonical padded base64 text within the derivative's ceiling.
#[must_use]
pub fn decoded(encoded: &str) -> Option<Vec<u8>> {
    if encoded.len() > MAX_DERIVATIVE_BYTES.div_ceil(3) * 4 {
        return None;
    }
    let engine = base64::engine::general_purpose::STANDARD;
    let bytes = engine.decode(encoded).ok()?;
    (bytes.len() <= MAX_DERIVATIVE_BYTES && engine.encode(&bytes) == encoded).then_some(bytes)
}

/// A JPEG frame header (SOF0 to SOF15, except the DHT, JPG, and DAC markers) carries the image's encoded size.
fn jpeg_frame(marker: u8) -> bool {
    (0xc0..=0xcf).contains(&marker) && ![0xc4, 0xc8, 0xcc].contains(&marker)
}

/// Markers that stand alone, without a length: TEM and RST0 to RST7.
fn jpeg_standalone(marker: u8) -> bool {
    marker == 0x01 || (0xd0..=0xd7).contains(&marker)
}

/// The width and height a JPEG or PNG declares in its own header, read without decoding it.
///
/// The helper's answer is untrusted, so its reported size must be the size its bytes make a decoder allocate.
#[must_use]
pub fn encoded_size(media_type: MediaType, data: &[u8]) -> Option<(i64, i64)> {
    let be = |bytes: &[u8]| {
        bytes
            .iter()
            .fold(0_i64, |total, byte| (total << 8) | i64::from(*byte))
    };
    if media_type == MediaType::Png {
        return (data.len() >= 24 && &data[8..16] == b"\x00\x00\x00\x0dIHDR")
            .then(|| (be(&data[16..20]), be(&data[20..24])));
    }
    let mut offset = 2;
    while offset + 4 <= data.len() && data[offset] == 0xff {
        let marker = data[offset + 1];
        if marker == 0xff {
            offset += 1;
        } else if jpeg_standalone(marker) {
            offset += 2;
        } else if marker == 0xda {
            return None;
        } else if jpeg_frame(marker) {
            return (offset + 9 <= data.len()).then(|| {
                (
                    be(&data[offset + 7..offset + 9]),
                    be(&data[offset + 5..offset + 7]),
                )
            });
        } else {
            offset += 2 + usize::from(u16::from_be_bytes([data[offset + 2], data[offset + 3]]));
        }
    }
    None
}

/// The closed opaque content of an answer that claims to be opaque; anything else is `unreadable`.
#[must_use]
pub fn from_opaque(answer: &Value) -> Value {
    let reason = fields(answer)
        .filter(|members| exactly(members, &["type", "reason"]) && is(answer, "type", "opaque"))
        .and_then(|_| answer.get("reason").and_then(string))
        .and_then(Reason::parse);
    opaque(reason.unwrap_or(Reason::Unreadable))
}

fn fields(value: &Value) -> Option<&[(String, Value)]> {
    match value {
        Value::Object(members) => Some(members),
        _ => None,
    }
}

/// Whether the object has exactly the named members (its names are unique).
fn exactly(members: &[(String, Value)], names: &[&str]) -> bool {
    members.len() == names.len()
        && members
            .iter()
            .all(|(name, _)| names.contains(&name.as_str()))
}

fn is(value: &Value, key: &str, expected: &str) -> bool {
    value.get(key).and_then(string) == Some(expected)
}

fn string(value: &Value) -> Option<&str> {
    match value {
        Value::String(found) => Some(found),
        _ => None,
    }
}

/// Lowercase hexadecimal.
#[must_use]
pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().fold(String::new(), |mut text, byte| {
        let _ = write!(text, "{byte:02x}");
        text
    })
}

#[cfg(test)]
mod tests;
