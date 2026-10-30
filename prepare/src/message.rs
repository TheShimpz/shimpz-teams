//! One message's selected files as the Brain's private `attachments` field: literal metadata plus one closed content
//! branch each, and the per-message ceilings that refuse the whole message before dispatch.
//!
//! Text, code, CSV, JSON, and Markdown are read by the controller; images and PDFs go to the networkless helper. A file
//! beyond a per-file ceiling is opaque with a closed reason, and an original beyond every readable kind's ceiling is
//! never read.

use strict_json::{Number, Value};
use team_protocol::python::json::{self, Options};
use team_protocol::python::text;

use crate::answer::{Reason, member, opaque};
use crate::content;
use crate::detect::{self, Detected, Kind};
use crate::limits::{
    MAX_ATTACHMENTS_FIELD_BYTES, MAX_IMAGE_BYTES, MAX_MESSAGE_IMAGES, MAX_MESSAGE_TEXT_BYTES,
    MAX_MESSAGE_TEXT_CHARACTERS, MAX_PDF_BYTES, MAX_SELECTED_FILES, MAX_SELECTED_ORIGINAL_BYTES,
    MAX_TEXT_SOURCE_BYTES,
};
use crate::request;

/// The largest original any readable kind admits; a larger one is never read.
pub const MAX_SOURCE_BYTES: usize = MAX_PDF_BYTES;

/// A per-message ceiling the message exceeds; the message is refused before dispatch.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LimitError {
    /// More files than a message selects.
    TooMany,
    /// More original bytes than a message selects, or a larger encoded field than the Brain admits.
    TooLarge,
    /// More readable images than a message carries.
    TooManyImages,
    /// More readable text than a message carries.
    TooMuchText,
}

impl LimitError {
    /// The problem code the refusal answers with.
    #[must_use]
    pub const fn code(self) -> &'static str {
        match self {
            Self::TooMany => "attachments-too-many",
            Self::TooLarge => "attachments-too-large",
            Self::TooManyImages => "attachments-too-many-images",
            Self::TooMuchText => "attachments-too-much-text",
        }
    }
}

/// One prepared attachment.
#[derive(Clone, Debug, PartialEq)]
pub struct Attachment {
    /// The file's id.
    pub id: String,
    /// Its name.
    pub name: String,
    /// The media type Team determined from its bytes.
    pub media_type: String,
    /// Its original size.
    pub size: u64,
    /// The SHA-256 of its original.
    pub sha256: String,
    /// Its closed `text`, `image`, or `opaque` content.
    pub content: Value,
}

impl Attachment {
    /// The exact private Brain representation.
    #[must_use]
    pub fn wire(&self) -> Value {
        Value::Object(vec![
            member("id", &self.id),
            member("name", &self.name),
            member("media_type", &self.media_type),
            (
                "size".to_owned(),
                Value::Number(Number::Int(i64::try_from(self.size).unwrap_or(i64::MAX))),
            ),
            member("sha256", &self.sha256),
            ("content".to_owned(), self.content.clone()),
        ])
    }

    fn kind(&self) -> Option<&str> {
        match self.content.get("type") {
            Some(Value::String(kind)) => Some(kind),
            _ => None,
        }
    }
}

/// Refuses a selection with more files or original bytes than a message admits, before any original is read.
///
/// # Errors
/// [`LimitError::TooMany`] or [`LimitError::TooLarge`].
pub fn admit_selection(sizes: &[u64]) -> Result<(), LimitError> {
    if sizes.len() > MAX_SELECTED_FILES {
        return Err(LimitError::TooMany);
    }
    if sizes.iter().sum::<u64>() > MAX_SELECTED_ORIGINAL_BYTES {
        return Err(LimitError::TooLarge);
    }
    Ok(())
}

/// What one read original needs.
#[derive(Clone, Debug, PartialEq)]
pub enum Plan {
    /// Its content is decided without the helper.
    Ready(Value),
    /// The helper prepares it as this kind; its answer is validated by [`content::image`] or [`content::pdf`].
    Helper(request::Kind),
}

/// The plan for one original of `detected` kind.
#[must_use]
pub fn plan(data: &[u8], detected: Detected) -> Plan {
    let (ceiling, kind) = match detected.kind {
        Kind::Opaque => return Plan::Ready(opaque(Reason::Unsupported)),
        Kind::Text => (MAX_TEXT_SOURCE_BYTES, None),
        Kind::Pdf => (MAX_PDF_BYTES, Some(request::Kind::Pdf)),
        Kind::Image => (MAX_IMAGE_BYTES, Some(request::Kind::Image)),
    };
    if data.len() > ceiling {
        return Plan::Ready(opaque(Reason::TooLarge));
    }
    kind.map_or_else(
        || Plan::Ready(content::text(detect::decode_text(data), false)),
        Plan::Helper,
    )
}

/// The opaque attachment of an original larger than every readable kind's ceiling, never read.
#[must_use]
pub fn unread(id: &str, name: &str, size: u64, sha256: &str) -> Attachment {
    Attachment {
        id: id.to_owned(),
        name: name.to_owned(),
        media_type: detect::OCTET_STREAM.to_owned(),
        size,
        sha256: sha256.to_owned(),
        content: opaque(Reason::TooLarge),
    }
}

/// Refuses a message whose readable attachments together exceed a per-message ceiling.
///
/// # Errors
/// The [`LimitError`] the message exceeds.
pub fn admit_message(attachments: &[Attachment]) -> Result<(), LimitError> {
    let images = attachments
        .iter()
        .filter(|item| item.kind() == Some("image"))
        .count();
    if images > MAX_MESSAGE_IMAGES {
        return Err(LimitError::TooManyImages);
    }
    let texts: Vec<&str> = attachments
        .iter()
        .filter(|item| item.kind() == Some("text"))
        .filter_map(|item| match item.content.get("text") {
            Some(Value::String(found)) => Some(found.as_str()),
            _ => None,
        })
        .collect();
    let characters: usize = texts.iter().map(|found| text::len(found)).sum();
    let bytes: usize = texts.iter().map(|found| found.len()).sum();
    if characters > MAX_MESSAGE_TEXT_CHARACTERS || bytes > MAX_MESSAGE_TEXT_BYTES {
        return Err(LimitError::TooMuchText);
    }
    let field = Value::Array(attachments.iter().map(Attachment::wire).collect());
    let options = Options {
        ensure_ascii: false,
        sort_keys: false,
        compact: true,
    };
    if json::encoded_len(&field, options) > MAX_ATTACHMENTS_FIELD_BYTES {
        return Err(LimitError::TooLarge);
    }
    Ok(())
}

#[cfg(test)]
mod tests;
