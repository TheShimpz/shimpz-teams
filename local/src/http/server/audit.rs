//! Per-request audit state: who the request proved to be, under one trace id, recorded metadata only.

use std::sync::Arc;

use strict_json::Value;
use team_core::audit::{AuditEvent, AuditSink, AuditUnavailable};

/// The principal and credential state of one request, and its trace id.
#[derive(Clone)]
pub struct RequestAudit {
    sink: Arc<dyn AuditSink>,
    /// The request's trace id: 32 lowercase hex digits.
    pub trace_id: String,
    /// The resolved operation's name, `request` until the route resolves.
    pub operation: &'static str,
    /// The principal: `admin`, `admin-routine`, a Supervisor's opaque id, or none.
    pub principal_id: Option<String>,
    /// `absent`, `machine`, or `human`.
    pub principal_class: &'static str,
    /// The credential state the request was judged in.
    pub credential_state: &'static str,
    /// The validated Team the request concerns, once known.
    pub team_id: Option<String>,
    /// Whether a failed record still lets the operation proceed (ADR-0120).
    pub tolerant: bool,
}

impl std::fmt::Debug for RequestAudit {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RequestAudit")
            .field("trace_id", &self.trace_id)
            .field("operation", &self.operation)
            .finish_non_exhaustive()
    }
}

impl RequestAudit {
    /// A request that presented the controller bearer, before anything else is known.
    pub fn new(sink: Arc<dyn AuditSink>, trace_id: String) -> Self {
        Self {
            sink,
            trace_id,
            operation: "request",
            principal_id: None,
            principal_class: "absent",
            credential_state: "machine_bearer_present",
            team_id: None,
            tolerant: false,
        }
    }

    /// Admin under the controller bearer alone.
    pub fn machine(&mut self) {
        self.principal_id = Some("admin".to_owned());
        self.principal_class = "machine";
        self.credential_state = "machine_bearer_present";
    }

    /// Admin's Routine identity, proven by a Routine assertion for one leased run.
    pub fn routine(&mut self) {
        self.principal_id = Some("admin-routine".to_owned());
        self.principal_class = "machine";
        self.credential_state = "assertion_present";
    }

    /// A Supervisor, proven by a session (or host-reset) assertion.
    pub fn human(&mut self, supervisor_id: &str) {
        self.principal_id = Some(supervisor_id.to_owned());
        self.principal_class = "human";
        self.credential_state = "assertion_present";
    }

    /// No proven principal, in `credential_state`.
    pub fn absent(&mut self, credential_state: &'static str) {
        self.principal_id = None;
        self.principal_class = "absent";
        self.credential_state = credential_state;
    }

    /// The metadata-only event of `operation` (the resolved operation for `None`) with `result`.
    #[must_use]
    pub fn event(
        &self,
        operation: Option<&str>,
        result: &str,
        assistant: Option<&str>,
        detail: Option<&str>,
    ) -> AuditEvent {
        let text = |value: &str| Value::String(value.to_owned());
        let mut event = AuditEvent::new()
            .with("trace_id", text(&self.trace_id))
            .with("operation", text(operation.unwrap_or(self.operation)))
            .with("result", text(result))
            .with(
                "principal_id",
                self.principal_id.as_deref().map_or(Value::Null, text),
            )
            .with("principal_class", text(self.principal_class))
            .with("credential_state", text(self.credential_state));
        for (name, value) in [
            ("team_id", self.team_id.as_deref()),
            ("assistant", assistant),
            ("detail", detail),
        ] {
            if let Some(value) = value {
                event = event.with(name, text(value));
            }
        }
        event
    }

    /// Records one event; a tolerant operation proceeds when the journal cannot record it.
    ///
    /// # Errors
    /// [`AuditUnavailable`] when the event was not recorded and the operation is not tolerant.
    pub async fn record(
        &self,
        operation: Option<&str>,
        result: &str,
        assistant: Option<&str>,
        detail: Option<&str>,
    ) -> Result<(), AuditUnavailable> {
        let recorded = self
            .sink
            .record(self.event(operation, result, assistant, detail))
            .await;
        if self.tolerant { Ok(()) } else { recorded }
    }
}
