//! Extract the text of one PDF with bounded decoding; runs only inside the preparation helper.
//!
//! A pure-Rust pre-pass ([`prepass`]) reads the document first: it refuses an encrypted document, more than the page
//! ceiling, and any page whose decoded content streams exceed their ceiling or together exceed the document's, each
//! stream decoded only up to its remaining budget. Only then does `PDFium` read each page's text. Pages join with a
//! blank line; a document without text, or whose text exceeds its ceiling, has none.

use lopdf::{DecompressError, Document, Error, LoadOptions};
use pdfium_render::prelude::Pdfium;
use team_protocol::python::text;

use crate::answer::Reason;
use crate::limits::{
    MAX_PDF_FILE_STREAM_BYTES, MAX_PDF_PAGE_STREAM_BYTES, MAX_PDF_PAGES, MAX_TEXT_BYTES,
    MAX_TEXT_CHARACTERS,
};
use crate::pdfium;

/// The text of every page.
///
/// # Errors
/// The closed [`Reason`] the document is refused for.
pub fn prepare(data: &[u8], pdfium: &Pdfium) -> Result<String, Reason> {
    prepass(data)?;
    let document = pdfium::open(pdfium, data)?;
    let pages = document.pages();
    if i64::from(pages.len()) > MAX_PDF_PAGES {
        return Err(Reason::TooLarge);
    }
    let mut texts = Vec::new();
    for page in pages.iter() {
        let extracted = page.text().map_err(|_| Reason::Unreadable)?.all();
        let lines = extracted.replace("\r\n", "\n");
        let stripped = text::strip(&lines);
        if !stripped.is_empty() {
            texts.push(stripped.to_owned());
        }
    }
    let joined = texts.join("\n\n");
    if text::strip(&joined).is_empty() {
        return Err(Reason::NoText);
    }
    if text::len(&joined) > MAX_TEXT_CHARACTERS || joined.len() > MAX_TEXT_BYTES {
        return Err(Reason::TooLarge);
    }
    Ok(joined)
}

/// The bounded pure-Rust pre-pass every PDF passes before `PDFium` reads it; returns its page count.
///
/// # Errors
/// [`Reason::Encrypted`] for any encryption dictionary, [`Reason::TooLarge`] beyond the page or stream ceilings, and
/// [`Reason::Unreadable`] for a document that does not parse.
pub fn prepass(data: &[u8]) -> Result<i64, Reason> {
    let options = LoadOptions {
        max_decompressed_size: Some(MAX_PDF_FILE_STREAM_BYTES),
        ..LoadOptions::default()
    };
    let document =
        Document::load_mem_with_options(data, options).map_err(|error| refused(&error))?;
    if document.trailer.get(b"Encrypt").is_ok() || document.was_encrypted() {
        return Err(Reason::Encrypted);
    }
    let pages = document.get_pages();
    let count = i64::try_from(pages.len()).map_err(|_| Reason::TooLarge)?;
    if count > MAX_PDF_PAGES {
        return Err(Reason::TooLarge);
    }
    let mut decoded = 0_usize;
    for page in pages.values() {
        let content = document
            .get_page_content_with_limit(*page, MAX_PDF_PAGE_STREAM_BYTES)
            .map_err(|error| refused(&error))?;
        decoded += content.len();
        if decoded > MAX_PDF_FILE_STREAM_BYTES {
            return Err(Reason::TooLarge);
        }
    }
    Ok(count)
}

fn refused(error: &Error) -> Reason {
    match error {
        Error::Decompress(DecompressError::MemoryLimitExceeded { .. }) => Reason::TooLarge,
        Error::Decryption(_)
        | Error::InvalidPassword
        | Error::UnsupportedSecurityHandler(_)
        | Error::AlreadyEncrypted => Reason::Encrypted,
        _ => Reason::Unreadable,
    }
}

#[cfg(test)]
mod tests;
