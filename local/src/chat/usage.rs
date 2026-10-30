//! The Brain model usage of each request (ADR-0082): the server's [`ModelUsageSink`], and the per-request recorder a
//! chat turn's Brain calls report through.
//!
//! Each Brain operation's reported counts are kept under the request's trace id until the request's next audit event
//! drains them; the request's own model tokens are also tallied for its turn's `usage`, which is never drained.

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use team_inference::usage::{Counts, Tokens, UsageRecorder, add_tokens};

use crate::http::server::usage::{ModelUsageSink, OPERATIONS, UsageCounts, UsageEntry};

/// How many ended requests are remembered, so a late record of one is not kept.
const ENDED: usize = 256;

#[derive(Default)]
struct Book {
    live: HashMap<String, Vec<UsageEntry>>,
    ended: VecDeque<String>,
}

/// Every request's undrained Brain usage.
#[derive(Default)]
pub struct UsageBook {
    book: Mutex<Book>,
}

impl std::fmt::Debug for UsageBook {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("UsageBook")
    }
}

impl UsageBook {
    fn book(&self) -> MutexGuard<'_, Book> {
        self.book.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

impl ModelUsageSink for UsageBook {
    fn record(&self, trace_id: &str, entry: UsageEntry) {
        let mut book = self.book();
        if book.ended.iter().any(|ended| ended == trace_id) {
            return;
        }
        book.live
            .entry(trace_id.to_owned())
            .or_default()
            .push(entry);
    }

    fn drain(&self, trace_id: &str) -> Vec<UsageEntry> {
        self.book().live.remove(trace_id).unwrap_or_default()
    }

    fn end(&self, trace_id: &str) {
        let mut book = self.book();
        book.live.remove(trace_id);
        if book.ended.len() == ENDED {
            book.ended.pop_front();
        }
        book.ended.push_back(trace_id.to_owned());
    }
}

/// One request's recorder: every Brain operation's usage goes to the request's audit meter, and its model tokens to
/// the request's tally.
pub struct RequestUsage {
    sink: Arc<dyn ModelUsageSink>,
    trace_id: String,
    tokens: Mutex<Tokens>,
}

impl std::fmt::Debug for RequestUsage {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RequestUsage")
            .field("trace_id", &self.trace_id)
            .finish_non_exhaustive()
    }
}

impl RequestUsage {
    /// The recorder of request `trace_id`.
    #[must_use]
    pub fn new(sink: Arc<dyn ModelUsageSink>, trace_id: &str) -> Self {
        Self {
            sink,
            trace_id: trace_id.to_owned(),
            tokens: Mutex::new(Tokens::new()),
        }
    }

    /// The input and output tokens per provider and model this request's model calls used so far.
    #[must_use]
    pub fn tokens(&self) -> Tokens {
        self.tokens
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }
}

impl UsageRecorder for RequestUsage {
    fn record(&self, operation: &'static str, provider: &str, model: &str, counts: &Counts) {
        add_tokens(
            &mut self.tokens.lock().unwrap_or_else(PoisonError::into_inner),
            provider,
            model,
            counts,
        );
        let Some(operation) = OPERATIONS.iter().copied().find(|known| *known == operation) else {
            return;
        };
        self.sink.record(
            &self.trace_id,
            UsageEntry {
                operation,
                provider: provider.to_owned(),
                model: model.to_owned(),
                counts: UsageCounts {
                    model_calls: counts.model_calls,
                    provider_requests: counts.provider_requests,
                    failed_calls: counts.failed_calls,
                    unreported_calls: counts.unreported_calls,
                    input_tokens: counts.input_tokens,
                    output_tokens: counts.output_tokens,
                    cache_read_tokens: counts.cache_read_tokens,
                    cache_write_tokens: counts.cache_write_tokens,
                },
            },
        );
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use team_inference::usage::{Counts, UsageRecorder};

    use super::{RequestUsage, UsageBook};
    use crate::http::server::usage::ModelUsageSink;

    fn counts(inputs: u64) -> Counts {
        Counts {
            model_calls: 1,
            provider_requests: 1,
            input_tokens: inputs,
            output_tokens: 2,
            ..Counts::default()
        }
    }

    #[test]
    fn each_request_drains_its_own_usage_and_keeps_its_token_tally() {
        let book = Arc::new(UsageBook::default());
        let first = RequestUsage::new(Arc::clone(&book) as Arc<dyn ModelUsageSink>, "t1");
        let second = RequestUsage::new(Arc::clone(&book) as Arc<dyn ModelUsageSink>, "t2");
        first.record("turn", "openai", "gpt-6-luna", &counts(5));
        first.record("turn-resume", "openai", "gpt-6-luna", &counts(7));
        second.record("purpose", "openai", "gpt-6-luna", &counts(1));
        let drained = book.drain("t1");
        assert_eq!(drained.len(), 2);
        assert_eq!(drained[1].operation, "turn-resume");
        assert_eq!(book.drain("t1").len(), 0);
        assert_eq!(
            first
                .tokens()
                .get(&("openai".to_owned(), "gpt-6-luna".to_owned())),
            Some(&(12, 4))
        );
        // An ended request keeps nothing recorded after its answer.
        book.end("t2");
        second.record("turn", "openai", "gpt-6-luna", &counts(1));
        assert_eq!(book.drain("t2").len(), 0);
        // An unmetered operation name never reaches the audit meter.
        first.record("chat", "openai", "gpt-6-luna", &counts(1));
        assert_eq!(book.drain("t1").len(), 0);
    }
}
