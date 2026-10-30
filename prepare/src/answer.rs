//! The worker's closed answer: one image derivative, one PDF page's derivative with its page count, one PDF's text, or
//! an opaque reason. Standard output carries exactly its compact ASCII JSON.

use strict_json::{Number, Value};
use team_protocol::python::json::{self, Options};

/// Why a file yields no derivative (the Team HTTP protocol's closed reasons).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Reason {
    /// An animated image.
    Animated,
    /// An encrypted PDF.
    Encrypted,
    /// A PDF without text.
    NoText,
    /// Beyond a ceiling.
    TooLarge,
    /// Malformed, or refused by a decoder.
    Unreadable,
    /// A kind outside the closed v1 rules.
    Unsupported,
}

impl Reason {
    /// The reason's wire name.
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Animated => "animated",
            Self::Encrypted => "encrypted",
            Self::NoText => "no_text",
            Self::TooLarge => "too_large",
            Self::Unreadable => "unreadable",
            Self::Unsupported => "unsupported",
        }
    }

    /// The reason a wire name names.
    #[must_use]
    pub fn parse(name: &str) -> Option<Self> {
        [
            Self::Animated,
            Self::Encrypted,
            Self::NoText,
            Self::TooLarge,
            Self::Unreadable,
            Self::Unsupported,
        ]
        .into_iter()
        .find(|reason| reason.name() == name)
    }
}

/// A derivative's media type.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MediaType {
    /// `image/jpeg`.
    Jpeg,
    /// `image/png`.
    Png,
}

impl MediaType {
    /// The media type's name.
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Jpeg => "image/jpeg",
            Self::Png => "image/png",
        }
    }

    /// The bytes every encoding of this media type starts with.
    #[must_use]
    pub const fn signature(self) -> &'static [u8] {
        match self {
            Self::Jpeg => b"\xff\xd8\xff",
            Self::Png => b"\x89PNG\r\n\x1a\n",
        }
    }

    /// The media type a name names.
    #[must_use]
    pub fn parse(name: &str) -> Option<Self> {
        [Self::Jpeg, Self::Png]
            .into_iter()
            .find(|media_type| media_type.name() == name)
    }
}

/// Fresh pixels encoded without metadata.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Derivative {
    /// Its media type.
    pub media_type: MediaType,
    /// Its width in pixels.
    pub width: u32,
    /// Its height in pixels.
    pub height: u32,
    /// Its encoded bytes.
    pub bytes: Vec<u8>,
}

/// One file's answer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Answer {
    /// An image's derivative.
    Image(Derivative),
    /// One PDF page's derivative and its document's page count.
    Page(Derivative, i64),
    /// A PDF's text.
    Text(String),
    /// No derivative, and why.
    Opaque(Reason),
}

impl Answer {
    /// The answer as the JSON object the worker writes.
    #[must_use]
    pub fn to_value(&self) -> Value {
        match self {
            Self::Image(derivative) => Value::Object(image_members(derivative)),
            Self::Page(derivative, page_count) => {
                let mut members = image_members(derivative);
                members.push(("page_count".to_owned(), int(*page_count)));
                Value::Object(members)
            }
            Self::Text(text) => Value::Object(vec![
                member("type", "text"),
                ("text".to_owned(), Value::String(text.clone())),
            ]),
            Self::Opaque(reason) => opaque(*reason),
        }
    }

    /// The exact bytes the worker writes: compact JSON with every non-ASCII character escaped.
    #[must_use]
    pub fn to_json(&self) -> String {
        json::dumps(
            &self.to_value(),
            Options {
                ensure_ascii: true,
                sort_keys: false,
                compact: true,
            },
        )
    }
}

/// `{"type": "opaque", "reason": …}`.
#[must_use]
pub fn opaque(reason: Reason) -> Value {
    Value::Object(vec![
        member("type", "opaque"),
        member("reason", reason.name()),
    ])
}

fn image_members(derivative: &Derivative) -> Vec<(String, Value)> {
    vec![
        member("type", "image"),
        member("media_type", derivative.media_type.name()),
        ("width".to_owned(), int(i64::from(derivative.width))),
        ("height".to_owned(), int(i64::from(derivative.height))),
        (
            "base64".to_owned(),
            Value::String(base64_text(&derivative.bytes)),
        ),
    ]
}

/// The padded standard base64 text of `bytes`, as Python's `base64.b64encode` writes it.
#[must_use]
pub fn base64_text(bytes: &[u8]) -> String {
    use base64::Engine as _;
    base64::engine::general_purpose::STANDARD.encode(bytes)
}

pub(crate) fn member(name: &str, text: &str) -> (String, Value) {
    (name.to_owned(), Value::String(text.to_owned()))
}

pub(crate) const fn int(value: i64) -> Value {
    Value::Number(Number::Int(value))
}

#[cfg(test)]
mod tests {
    use super::{Answer, Derivative, MediaType, Reason};

    #[test]
    fn each_answer_writes_its_exact_ascii_object() {
        assert_eq!(
            Answer::Opaque(Reason::NoText).to_json(),
            r#"{"type":"opaque","reason":"no_text"}"#
        );
        assert_eq!(
            Answer::Text("Ação\n".to_owned()).to_json(),
            r#"{"type":"text","text":"A\u00e7\u00e3o\n"}"#
        );
        let derivative = Derivative {
            media_type: MediaType::Png,
            width: 2,
            height: 1,
            bytes: b"png".to_vec(),
        };
        assert_eq!(
            Answer::Page(derivative, 3).to_json(),
            r#"{"type":"image","media_type":"image/png","width":2,"height":1,"base64":"cG5n","page_count":3}"#
        );
        for name in super::super::limits::OPAQUE_REASONS {
            assert_eq!(Reason::parse(name).map(Reason::name), Some(name));
        }
    }
}
