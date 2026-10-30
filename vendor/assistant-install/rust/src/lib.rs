//! The neutral Assistant-install contract v1 (`.standards/assistant-install/v1`) in Rust.
//!
//! [`Contract::load`] reads the pinned schema documents (`definitions.schema.json` and
//! `resolve-response.schema.json`) with the dependency-free JSON Schema subset of `../v1/schema_validator.py`;
//! [`Contract::validate`] decides one decoded resolve response exactly as `../v1/verify.py` does, answering the same
//! stable code. `vectors.json` proves the equality (`tests/vectors.rs`).
//!
//! A consumer passes the schema bytes of its own pinned mirror, so this crate never reads a file. The one deliberate
//! difference from the Python reference: a schema `pattern`'s `$` matches only at the end of the string, as JSON
//! Schema's ECMA-262 dialect reads it, never before a final line feed.

mod json;
pub mod resolve;
pub mod schema;

use strict_json::Value;

pub use self::json::canonical;
use self::schema::Documents;

/// The origin of every contract schema `$id`.
pub const SCHEMA_ORIGIN: &str = "https://schemas.shimpz.com/assistant-install/v1/";
/// The one dialect the schemas declare.
pub const DRAFT_2020_12: &str = "https://json-schema.org/draft/2020-12/schema";
/// The resolve response entry point.
pub const RESOLVE_RESPONSE: &str = "resolve-response.schema.json";
/// The shared definitions.
pub const DEFINITIONS: &str = "definitions.schema.json";

/// Why a decoded resolve response is refused; the code never echoes the value.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Violation {
    /// The value does not satisfy the schema.
    Schema,
    /// The image reference is not the executable repository at the resolved digest.
    DigestMismatch,
    /// A catalog id is not the sorted, unique SHA-256 of its template.
    CatalogMismatch,
    /// The summary is not one parameterless catalog message within its bound.
    SummaryMismatch,
    /// A displayed static text is not one parameterless catalog message within its bound.
    DisplayMismatch,
    /// A file input is not a required exact file-id property behind exactly one authorization.
    InputFileMismatch,
    /// The Integration intents differ from the ones the Actions use.
    IntegrationMismatch,
    /// The Stored Inputs are duplicated, missing, or used without a password request.
    StoredInputMismatch,
    /// A Stored Input placement or proof is refused.
    StoredInputPlacement,
}

impl Violation {
    /// The stable code the vectors name.
    #[must_use]
    pub const fn code(self) -> &'static str {
        match self {
            Self::Schema => "schema_violation",
            Self::DigestMismatch => "resolve_digest_mismatch",
            Self::CatalogMismatch => "resolve_catalog_mismatch",
            Self::SummaryMismatch => "resolve_summary_mismatch",
            Self::DisplayMismatch => "resolve_display_mismatch",
            Self::InputFileMismatch => "resolve_input_file_mismatch",
            Self::IntegrationMismatch => "resolve_integration_mismatch",
            Self::StoredInputMismatch => "resolve_stored_input_mismatch",
            Self::StoredInputPlacement => "resolve_stored_input_placement",
        }
    }
}

impl std::fmt::Display for Violation {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.code())
    }
}

impl std::error::Error for Violation {}

/// The schema documents were not the contract's: unreadable, of another dialect or id, or outside the subset.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LoadError;

impl std::fmt::Display for LoadError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("the Assistant-install schemas are invalid")
    }
}

impl std::error::Error for LoadError {}

/// The loaded contract.
#[derive(Debug)]
pub struct Contract {
    documents: Documents,
    entry: Value,
}

impl Contract {
    /// Loads the two schema documents from their exact bytes.
    ///
    /// # Errors
    /// [`LoadError`] when either is not strict JSON, not Draft 2020-12 under its canonical `$id`, or not admitted by
    /// the subset (every node checked, every reference resolved, every pattern compiled).
    pub fn load(definitions: &[u8], resolve_response: &[u8]) -> Result<Self, LoadError> {
        let mut documents = Vec::new();
        for (name, raw) in [
            (DEFINITIONS, definitions),
            (RESOLVE_RESPONSE, resolve_response),
        ] {
            let document = strict_json::parse(raw).map_err(|_| LoadError)?;
            let id = format!("{SCHEMA_ORIGIN}{name}");
            let declared = |key: &str, expected: &str| {
                document.get(key) == Some(&Value::String(expected.to_owned()))
            };
            if !declared("$schema", DRAFT_2020_12) || !declared("$id", &id) {
                return Err(LoadError);
            }
            documents.push((id, document));
        }
        let entry = documents[1].1.clone();
        Ok(Self {
            documents: Documents::load(documents).map_err(|_| LoadError)?,
            entry,
        })
    }

    /// Whether `value` satisfies the resolve response schema.
    #[must_use]
    pub fn schema_admits(&self, value: &Value) -> bool {
        self.documents.valid(
            &self.entry,
            value,
            &format!("{SCHEMA_ORIGIN}{RESOLVE_RESPONSE}"),
        )
    }

    /// Decides one decoded resolve response: the schema, then every semantic rule of [`resolve::validate`].
    ///
    /// # Errors
    /// The first [`Violation`].
    pub fn validate(&self, value: &Value) -> Result<(), Violation> {
        if !self.schema_admits(value) {
            return Err(Violation::Schema);
        }
        resolve::validate(value)
    }
}
