//! The bounded canonical JSON an Action result must be before Team keeps it durably or acts on it (`action/result.py`).
//!
//! The journal stores exactly these bytes, and Team refuses any other result before a follow-up side effect, so a
//! result the journal could not persist is never accepted.

use strict_json::Value;
use team_protocol::python::json;

/// The largest result, matching the RPC frame bound.
pub const MAX_RESULT_BYTES: usize = 512 * 1024;
/// The deepest nesting a result may have.
pub const MAX_JSON_DEPTH: usize = 32;
/// The most values a result may hold.
pub const MAX_JSON_NODES: usize = 4096;

/// A result is not bounded canonical JSON.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ResultError {
    /// It nests deeper than [`MAX_JSON_DEPTH`] or holds more than [`MAX_JSON_NODES`] values.
    Structure,
    /// Its canonical bytes exceed the bound.
    Size,
}

impl std::fmt::Display for ResultError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Structure => "the Action result exceeds the JSON structure limit",
            Self::Size => "the Action result exceeds the durable size limit",
        })
    }
}

impl std::error::Error for ResultError {}

/// Admits only values within the depth and node bounds.
///
/// # Errors
/// [`ResultError::Structure`] beyond either bound.
pub fn walk(value: &Value) -> Result<(), ResultError> {
    let mut budget = MAX_JSON_NODES;
    let mut pending: Vec<(&Value, usize)> = vec![(value, 0)];
    while let Some((value, depth)) = pending.pop() {
        budget = budget.checked_sub(1).ok_or(ResultError::Structure)?;
        if depth > MAX_JSON_DEPTH {
            return Err(ResultError::Structure);
        }
        match value {
            Value::Array(items) => pending.extend(items.iter().map(|item| (item, depth + 1))),
            Value::Object(members) => {
                pending.extend(members.iter().map(|(_, item)| (item, depth + 1)));
            }
            _ => {}
        }
    }
    Ok(())
}

/// The result's canonical UTF-8 JSON bytes (sorted, compact), refused beyond `max_bytes`.
///
/// # Errors
/// [`ResultError`] beyond the structure or size bound.
pub fn canonical(value: &Value, max_bytes: usize) -> Result<Vec<u8>, ResultError> {
    walk(value)?;
    let encoded = json::canonical(value);
    if encoded.len() > max_bytes {
        return Err(ResultError::Size);
    }
    Ok(encoded)
}

/// Refuses, before any follow-up side effect, a result the journal could not persist.
///
/// # Errors
/// As [`canonical`] at [`MAX_RESULT_BYTES`].
pub fn require_durable(value: &Value) -> Result<(), ResultError> {
    canonical(value, MAX_RESULT_BYTES).map(|_| ())
}

#[cfg(test)]
mod tests {
    use strict_json::Value;

    use super::{
        MAX_JSON_DEPTH, MAX_JSON_NODES, MAX_RESULT_BYTES, ResultError, canonical, require_durable,
        walk,
    };

    fn nested(depth: usize) -> Value {
        (0..depth).fold(Value::Array(Vec::new()), |value, _| {
            Value::Array(vec![value])
        })
    }

    #[test]
    fn only_bounded_json_values_are_admitted() {
        assert_eq!(walk(&nested(MAX_JSON_DEPTH)), Ok(()));
        assert_eq!(
            walk(&nested(MAX_JSON_DEPTH + 1)),
            Err(ResultError::Structure)
        );
        let wide = Value::Array(vec![Value::Null; MAX_JSON_NODES]);
        assert_eq!(walk(&wide), Err(ResultError::Structure));
        let fits = Value::Array(vec![Value::Null; MAX_JSON_NODES - 1]);
        assert_eq!(walk(&fits), Ok(()));
    }

    #[test]
    fn canonical_bytes_are_sorted_compact_utf8_and_bounded() {
        let value = strict_json::parse("{\"b\":\"ã\",\"a\":[1]}".as_bytes()).expect("json");
        assert_eq!(
            canonical(&value, 100),
            Ok("{\"a\":[1],\"b\":\"ã\"}".as_bytes().to_vec())
        );
        assert_eq!(
            canonical(&Value::String("x".repeat(10)), 5),
            Err(ResultError::Size)
        );
    }

    #[test]
    fn the_durable_bound_is_the_rpc_frame_bound() {
        let overhead = canonical(
            &strict_json::parse(br#"{"text":""}"#).expect("json"),
            MAX_RESULT_BYTES,
        )
        .expect("small")
        .len();
        let fits = Value::Object(vec![(
            "text".to_owned(),
            Value::String("x".repeat(MAX_RESULT_BYTES - overhead)),
        )]);
        assert_eq!(require_durable(&fits), Ok(()));
        let over = Value::Object(vec![(
            "text".to_owned(),
            Value::String("x".repeat(MAX_RESULT_BYTES - overhead + 1)),
        )]);
        assert_eq!(require_durable(&over), Err(ResultError::Size));
    }
}
