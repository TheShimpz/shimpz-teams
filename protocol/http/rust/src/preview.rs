//! A Team file's preview: the framing of the sanitized rendition Team sends a Supervisor's browser.
//!
//! The rendition is the bounded image derivative a chat turn sends the model, never the original: a JPEG or PNG of at
//! most the derivative's bytes, of an image or of one page of a PDF. Team and Admin each admit its exact media type and
//! length, and its bytes start with that media type's signature; a page's rendition also names its page count.

use std::sync::LazyLock;

use strict_json::Value;

use crate::python::regex::{Pattern, declared};
use crate::python::value::{exact_object, field, object_of, small_int, string, whole};
use crate::turn::MAX_ATTACHMENT_IMAGE_BYTES;

/// The largest rendition.
pub const MAX_PREVIEW_BYTES: usize = MAX_ATTACHMENT_IMAGE_BYTES;
/// Each admitted media type and the bytes its rendition starts with.
pub const SIGNATURES: [(&str, &[u8]); 2] = [
    ("image/jpeg", b"\xff\xd8\xff"),
    ("image/png", b"\x89PNG\r\n\x1a\n"),
];
/// A PDF has a page preview only within its page ceiling; pages count from 1.
pub const MAX_PDF_PAGES: i64 = 50;
/// A page number as a preview path names it, matched whole.
pub static PAGE_RE: LazyLock<Pattern> = LazyLock::new(|| declared("[1-9][0-9]?"));
/// The header a page rendition names its document's page count in.
pub const PAGE_COUNT_HEADER: &str = "X-Page-Count";

/// `canonical_framing`: exactly `{"media_type", "length"}` of an admissible preview response, or `None`.
#[must_use]
pub fn canonical_framing(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &["media_type", "length"])?;
    framed(field(members, "media_type"), field(members, "length")).then_some(value)
}

fn framed(media_type: &Value, length: &Value) -> bool {
    string(media_type)
        .is_some_and(|media_type| SIGNATURES.iter().any(|(known, _)| *known == media_type))
        && small_int(length)
            .and_then(|length| usize::try_from(length).ok())
            .is_some_and(|length| (1..=MAX_PREVIEW_BYTES).contains(&length))
}

/// `canonical_page`: the page number a page preview path names, from 1 to [`MAX_PDF_PAGES`], or `None`.
#[must_use]
pub fn canonical_page(value: &Value) -> Option<i64> {
    let digits = string(value).filter(|text| PAGE_RE.is_full(text))?;
    let page = digits
        .bytes()
        .fold(0, |page, digit| page * 10 + i64::from(digit - b'0'));
    (page <= MAX_PDF_PAGES).then_some(page)
}

/// `canonical_page_framing`: exactly `{"media_type", "length", "page_count"}` of an admissible page preview, or
/// `None`.
#[must_use]
pub fn canonical_page_framing(value: &Value) -> Option<&Value> {
    let members = exact_object(value, &["media_type", "length", "page_count"])?;
    let framing = object_of(vec![
        ("media_type", field(members, "media_type").clone()),
        ("length", field(members, "length").clone()),
    ]);
    (whole(field(members, "page_count"), 1, MAX_PDF_PAGES) && canonical_framing(&framing).is_some())
        .then_some(value)
}
