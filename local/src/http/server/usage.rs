//! The Brain model usage of one request, carried on that request's next audit event (ADR-0082).
//!
//! The Brain client records what each Brain operation's provider responses reported under the request's trace id; the
//! server drains everything recorded since the request's previous event into the next one as `model_usage`, and ends
//! the request's meter when its answer is complete. The summary is metadata only: provider, model, operation counts,
//! model-call and provider-request counts, and token counts, never a prompt, reply, or credential. A failed Brain
//! request reports nothing.

use std::collections::BTreeMap;

use strict_json::Value;
use team_protocol::python::value::{int, object_of, text};

/// The Brain operations whose usage is metered.
pub const OPERATIONS: [&str; 9] = [
    "browser-check",
    "browser-final",
    "browser-fold",
    "browser-step",
    "capability-plan",
    "intent-route",
    "purpose",
    "turn",
    "turn-resume",
];

/// The counts one Brain operation reported, in Brain's closed usage shape.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct UsageCounts {
    /// Model calls made.
    pub model_calls: u64,
    /// Provider requests made (each SDK retry is one more).
    pub provider_requests: u64,
    /// Model calls that failed.
    pub failed_calls: u64,
    /// Model calls whose provider reported no usage.
    pub unreported_calls: u64,
    /// Input tokens.
    pub input_tokens: u64,
    /// Output tokens.
    pub output_tokens: u64,
    /// Cache-read tokens.
    pub cache_read_tokens: u64,
    /// Cache-write tokens.
    pub cache_write_tokens: u64,
}

impl UsageCounts {
    fn fields(&self) -> [(&'static str, u64); 8] {
        [
            ("model_calls", self.model_calls),
            ("provider_requests", self.provider_requests),
            ("failed_calls", self.failed_calls),
            ("unreported_calls", self.unreported_calls),
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("cache_read_tokens", self.cache_read_tokens),
            ("cache_write_tokens", self.cache_write_tokens),
        ]
    }
}

/// One Brain operation's usage, as the Brain client records it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UsageEntry {
    /// One of [`OPERATIONS`].
    pub operation: &'static str,
    /// The provider.
    pub provider: String,
    /// The model.
    pub model: String,
    /// What the operation reported.
    pub counts: UsageCounts,
}

/// Where each request's Brain usage accumulates until its next audit event (implemented by the inference service).
pub trait ModelUsageSink: Send + Sync {
    /// Records one Brain operation's usage for the request `trace_id`.
    fn record(&self, trace_id: &str, entry: UsageEntry);

    /// Returns and forgets every entry recorded for `trace_id` since the last drain.
    fn drain(&self, trace_id: &str) -> Vec<UsageEntry>;

    /// Forgets the request `trace_id`: its answer is complete, so nothing it records later is reported.
    fn end(&self, trace_id: &str);
}

/// The `model_usage` value of an audit event: one entry per operation, provider, and model, sorted by them, with how
/// many operations it sums (`count`) and the summed counts; `None` when no Brain operation ran.
#[must_use]
pub fn model_usage(entries: &[UsageEntry]) -> Option<Value> {
    let mut summed: BTreeMap<(&str, &str, &str), (u64, UsageCounts)> = BTreeMap::new();
    for entry in entries {
        let (count, counts) = summed
            .entry((entry.operation, &entry.provider, &entry.model))
            .or_default();
        *count += 1;
        let add = entry.counts;
        counts.model_calls += add.model_calls;
        counts.provider_requests += add.provider_requests;
        counts.failed_calls += add.failed_calls;
        counts.unreported_calls += add.unreported_calls;
        counts.input_tokens += add.input_tokens;
        counts.output_tokens += add.output_tokens;
        counts.cache_read_tokens += add.cache_read_tokens;
        counts.cache_write_tokens += add.cache_write_tokens;
    }
    let number = |value: u64| int(i64::try_from(value).unwrap_or(i64::MAX));
    let rows: Vec<Value> = summed
        .into_iter()
        .map(|((operation, provider, model), (count, counts))| {
            let mut members = vec![
                ("operation", text(operation)),
                ("provider", text(provider)),
                ("model", text(model)),
                ("count", number(count)),
            ];
            members.extend(counts.fields().map(|(name, value)| (name, number(value))));
            object_of(members)
        })
        .collect();
    (!rows.is_empty()).then_some(Value::Array(rows))
}

#[cfg(test)]
mod tests {
    use team_protocol::python::json::{CANONICAL, dumps};

    use super::{UsageCounts, UsageEntry, model_usage};

    fn entry(operation: &'static str, model: &str, input_tokens: u64) -> UsageEntry {
        UsageEntry {
            operation,
            provider: "openai".to_owned(),
            model: model.to_owned(),
            counts: UsageCounts {
                model_calls: 1,
                provider_requests: 2,
                input_tokens,
                ..UsageCounts::default()
            },
        }
    }

    #[test]
    fn usage_is_summed_per_operation_provider_and_model_in_their_order() {
        assert_eq!(model_usage(&[]), None);
        let usage = model_usage(&[
            entry("turn", "m2", 5),
            entry("purpose", "m1", 1),
            entry("turn", "m2", 7),
        ])
        .expect("usage");
        assert_eq!(
            dumps(&usage, CANONICAL),
            "[{\"cache_read_tokens\":0,\"cache_write_tokens\":0,\"count\":1,\"failed_calls\":0,\"input_tokens\":1,\
             \"model\":\"m1\",\"model_calls\":1,\"operation\":\"purpose\",\"output_tokens\":0,\"provider\":\"openai\",\
             \"provider_requests\":2,\"unreported_calls\":0},\
             {\"cache_read_tokens\":0,\"cache_write_tokens\":0,\"count\":2,\"failed_calls\":0,\"input_tokens\":12,\
             \"model\":\"m2\",\"model_calls\":2,\"operation\":\"turn\",\"output_tokens\":0,\"provider\":\"openai\",\
             \"provider_requests\":4,\"unreported_calls\":0}]"
        );
    }
}
