//! Strict JSON admission for Shimpz backends.
//!
//! [`parse`] admits exactly the documents that the neutral Python oracle `.standards/strict_json.py` admits and
//! decodes them to the same values: UTF-8 without a byte-order mark, no duplicate object fields, no non-finite
//! numbers, no unpaired surrogate escapes, no raw control characters in strings, and arrays and objects nested at
//! most [`MAX_DEPTH`] deep. `vectors.json` and the seeded differential harness prove the equality.
//!
//! ```
//! use strict_json::{Number, Value};
//!
//! let value = strict_json::parse(br#"{"name": "Team", "size": 3}"#)?;
//! assert_eq!(value.get("size"), Some(&Value::Number(Number::Int(3))));
//! assert!(strict_json::parse(br#"{"a": 1, "a": 2}"#).is_err());
//! # Ok::<(), strict_json::Error>(())
//! ```

mod error;
mod parser;
mod value;

pub use error::Error;
pub use value::{Number, Value};

/// The deepest nesting of arrays and objects a document may have; exactly this depth is admitted.
pub const MAX_DEPTH: usize = 512;

/// The most decimal digits an integer literal may have, not counting its sign (Python's `sys.int_max_str_digits`).
pub const MAX_INTEGER_DIGITS: usize = 4300;

const BYTE_ORDER_MARK: &[u8] = b"\xef\xbb\xbf";

/// Decodes one JSON document with surrounding whitespace.
///
/// # Errors
///
/// Returns the first [`Error`] that refuses the document; any refusal means the oracle refuses it too.
pub fn parse(input: &[u8]) -> Result<Value, Error> {
    if input.starts_with(BYTE_ORDER_MARK) {
        return Err(Error::ByteOrderMark);
    }
    let text = std::str::from_utf8(input).map_err(|error| Error::InvalidUtf8 {
        offset: error.valid_up_to(),
    })?;
    parser::Parser::new(text).document()
}
