//! The fallible audit sink: every authority records what it is about to do before it does it.
//!
//! The Local writer appends each event as one line of the hash-chained audit log (`/var/log/shimpz-local/audit.jsonl`:
//! compact JSON with sorted keys, ASCII escapes, `prev_sha256` over the previous line). A record resolves only once its
//! line is durably committed; a failure means it was not recorded, and the caller refuses the operation it was about to
//! perform (for example, no provider call is dispatched without its dispatch record).

use strict_json::Value;

use crate::future::BoxFuture;

/// One metadata-only audit event: its fields, never a body, a secret, or provider data.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct AuditEvent {
    fields: Vec<(String, Value)>,
}

impl AuditEvent {
    /// An event without fields.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// The event with `name` set to `value`, replacing an earlier value of the same name.
    #[must_use]
    pub fn with(mut self, name: &str, value: Value) -> Self {
        self.fields.retain(|(existing, _)| existing != name);
        self.fields.push((name.to_owned(), value));
        self
    }

    /// The fields in the order they were set; the writer sorts them.
    #[must_use]
    pub fn fields(&self) -> &[(String, Value)] {
        &self.fields
    }
}

/// The event was not recorded; the operation that needed it must not proceed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AuditUnavailable;

impl std::fmt::Display for AuditUnavailable {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("the audit log is unavailable")
    }
}

impl std::error::Error for AuditUnavailable {}

/// Where audit events are recorded.
pub trait AuditSink: Send + Sync {
    /// Records `event`, resolving once it is durably committed.
    ///
    /// # Errors
    /// [`AuditUnavailable`] when the event was not recorded.
    fn record(&self, event: AuditEvent) -> BoxFuture<'_, Result<(), AuditUnavailable>>;
}

#[cfg(test)]
mod tests {
    use strict_json::Value;

    use super::AuditEvent;

    #[test]
    fn a_field_set_twice_keeps_its_last_value_once() {
        let event = AuditEvent::new()
            .with("phase", Value::String("dispatch".to_owned()))
            .with("call", Value::Bool(true))
            .with("phase", Value::String("outcome".to_owned()));
        assert_eq!(
            event.fields(),
            [
                ("call".to_owned(), Value::Bool(true)),
                ("phase".to_owned(), Value::String("outcome".to_owned())),
            ]
        );
    }
}
