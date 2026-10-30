//! The closed set of errors Holdpoint's API returns.
//!
//! A message never carries a credential or a payload: it names the refused contract, the run, or the step. A
//! storage error carries SQLite's own message, which names tables and constraints but never bound values.

use serde::{Deserialize, Serialize};

/// Every error the public API returns.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum Error {
    /// An input broke a documented contract (shape, size, identifier, or schema).
    #[error("contract: {0}")]
    Contract(String),
    /// A repeated request differed from the one already recorded under the same identity.
    #[error("conflict: {0}")]
    Conflict(String),
    /// The request does not apply to the run's current state (unknown, ended, or acknowledged).
    #[error("state: {0}")]
    State(String),
    /// Another drive session currently holds the run.
    #[error("busy: {0}")]
    Busy(String),
    /// A configured bound refused the request.
    #[error("limit: {0}")]
    Limit(LimitKind),
    /// This drive session's lease ended; another session may own the run now.
    #[error("lease lost: {0}")]
    LeaseLost(String),
    /// The ledger could not be read or written, or holds content that fails validation.
    #[error("storage: {0}")]
    Storage(String),
}

/// Which bound was reached.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum LimitKind {
    /// The run's maximum number of model steps.
    Steps,
    /// The maximum number of tool calls in one model response.
    ToolCalls,
    /// The run's transcript or history size.
    History,
    /// The run's spend ceiling.
    Spend,
    /// A model response truncated by its output limit.
    Output,
    /// The store-wide number of runs or bytes.
    Store,
    /// One value (an input, a result, or a blob) over its size bound.
    Value,
}

impl std::fmt::Display for LimitKind {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Steps => "steps",
            Self::ToolCalls => "tool-calls",
            Self::History => "history",
            Self::Spend => "spend",
            Self::Output => "output",
            Self::Store => "store",
            Self::Value => "value",
        })
    }
}

impl From<rusqlite::Error> for Error {
    fn from(error: rusqlite::Error) -> Self {
        Self::Storage(error.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn limit_kinds_display_as_their_wire_names() {
        for kind in [
            LimitKind::Steps,
            LimitKind::ToolCalls,
            LimitKind::History,
            LimitKind::Spend,
            LimitKind::Output,
            LimitKind::Store,
            LimitKind::Value,
        ] {
            let wire = serde_json::to_value(kind).unwrap();
            assert_eq!(wire, serde_json::Value::String(kind.to_string()));
        }
    }

    #[test]
    fn storage_errors_keep_sqlite_messages() {
        let error = Error::from(rusqlite::Error::QueryReturnedNoRows);
        assert_eq!(error, Error::Storage("Query returned no rows".into()));
        assert_eq!(Error::Limit(LimitKind::Spend).to_string(), "limit: spend");
    }
}
