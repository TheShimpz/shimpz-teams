//! Team Assistant admission (ADR-0123, plan phase R7, workstream W3): the manifest and spec, languages and message
//! catalogs, details, effects, Genesis, and Action schema admission, on `strict_json::Value` and the vendored
//! Assistant protocol crates, independently fail-closed from Developers. It replaces `assistant/` and the reviewed
//! OAuth provider registry of `integrations/providers.py`, which manifest admission reads first.

pub mod archive;
pub mod cache;
pub mod details;
pub mod effect;
pub mod language;
pub mod manifest;
pub mod provider;
pub mod spec;
pub mod text;

/// An immutable Assistant package did not expose its reviewed security intent.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ManifestError {
    /// The package was read and refused; the text names the refused part, never its content.
    Invalid(&'static str),
    /// The package could not be read from its container, so nothing about it is proven.
    Unavailable,
}

impl std::fmt::Display for ManifestError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Invalid(part) => write!(formatter, "the Assistant {part} is invalid"),
            Self::Unavailable => formatter.write_str("the Assistant package is unavailable"),
        }
    }
}

impl std::error::Error for ManifestError {}
