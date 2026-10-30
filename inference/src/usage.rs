//! Observed Brain model usage (ADR-0082): the closed counts each Brain operation reports, where they are recorded, and
//! what one logical chat turn consumed.
//!
//! Brain reports what each operation's provider responses said; a failed Brain request reports nothing. Everything here
//! is metadata only: provider, model, operation counts, model-call and provider-request counts (each SDK retry is one
//! more provider request), and token counts, never a prompt, reply, or credential.

use std::collections::BTreeMap;

use strict_json::{Number, Value};
use team_protocol::payload::{
    MAX_TURN_DURATION_MS, MAX_TURN_USAGE_MODELS, MAX_TURN_USAGE_TOKENS, TURN_USAGE_ID_RE,
};
use team_protocol::python::value::{int, object_of, text};

/// The fields of Brain's closed usage shape, in its order.
pub const FIELDS: [&str; 8] = [
    "model_calls",
    "provider_requests",
    "failed_calls",
    "unreported_calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
];

/// The largest count Brain may report.
pub const MAX_COUNT: u64 = 1_000_000_000_000;

/// What one Brain operation reported.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Counts {
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

/// Admits only Brain's closed usage shape: exact non-negative integer counts with consistent call totals.
#[must_use]
pub fn parse(value: &Value) -> Option<Counts> {
    let Value::Object(members) = value else {
        return None;
    };
    if members.len() != FIELDS.len()
        || !members
            .iter()
            .all(|(name, _)| FIELDS.contains(&name.as_str()))
    {
        return None;
    }
    let count = |name: &str| match value.get(name) {
        Some(Value::Number(Number::Int(count))) => u64::try_from(*count)
            .ok()
            .filter(|count| *count <= MAX_COUNT),
        _ => None,
    };
    let counts = Counts {
        model_calls: count("model_calls")?,
        provider_requests: count("provider_requests")?,
        failed_calls: count("failed_calls")?,
        unreported_calls: count("unreported_calls")?,
        input_tokens: count("input_tokens")?,
        output_tokens: count("output_tokens")?,
        cache_read_tokens: count("cache_read_tokens")?,
        cache_write_tokens: count("cache_write_tokens")?,
    };
    (counts.failed_calls + counts.unreported_calls <= counts.model_calls).then_some(counts)
}

/// Where a Brain operation's usage is recorded: the request's audit meter and its turn's token totals.
pub trait UsageRecorder: Send + Sync {
    /// Records what `operation` (one of the metered operation names) reported for `provider` and `model`.
    fn record(&self, operation: &'static str, provider: &str, model: &str, counts: &Counts);
}

/// A recorder that keeps nothing.
#[derive(Clone, Copy, Debug, Default)]
pub struct Unmetered;

impl UsageRecorder for Unmetered {
    fn record(&self, _operation: &'static str, _provider: &str, _model: &str, _counts: &Counts) {}
}

/// Input and output tokens per provider and model that a request's model calls used; never drained.
pub type Tokens = BTreeMap<(String, String), (u64, u64)>;

/// Adds one operation's counts to `tokens` when it made a model call.
pub fn add_tokens(tokens: &mut Tokens, provider: &str, model: &str, counts: &Counts) {
    if counts.model_calls == 0 {
        return;
    }
    let entry = tokens
        .entry((provider.to_owned(), model.to_owned()))
        .or_default();
    entry.0 += counts.input_tokens;
    entry.1 += counts.output_tokens;
}

/// One model's tokens on the turn-usage wire: provider, model, input tokens, output tokens.
pub type ModelTokens = (String, String, i64, i64);

fn clamp(value: u64) -> i64 {
    i64::try_from(value)
        .unwrap_or(i64::MAX)
        .min(MAX_TURN_USAGE_TOKENS)
}

/// Per-model token totals in the turn-usage wire order and bounds: sorted, clamped, at most the wire's models.
fn bounded(totals: &Tokens) -> Vec<ModelTokens> {
    totals
        .iter()
        .filter(|((provider, model), _)| {
            TURN_USAGE_ID_RE.is_full(provider) && TURN_USAGE_ID_RE.is_full(model)
        })
        .map(|((provider, model), (inputs, outputs))| {
            (
                provider.clone(),
                model.clone(),
                clamp(*inputs),
                clamp(*outputs),
            )
        })
        .take(MAX_TURN_USAGE_MODELS)
        .collect()
}

fn added(totals: &mut Tokens, models: &[ModelTokens]) {
    for (provider, model, inputs, outputs) in models {
        let entry = totals.entry((provider.clone(), model.clone())).or_default();
        entry.0 += u64::try_from(*inputs).unwrap_or(0);
        entry.1 += u64::try_from(*outputs).unwrap_or(0);
    }
}

/// What one completed logical turn consumed, measured once: its duration and the tokens of each model it called.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Spent {
    /// The turn's duration in milliseconds.
    pub duration_ms: i64,
    /// Each model's tokens, sorted.
    pub models: Vec<ModelTokens>,
}

impl Spent {
    /// Several turns' usage summed within the wire bounds, as a Routine's creation shows its recording.
    #[must_use]
    pub fn total(spent: &[Self]) -> Self {
        let mut totals = Tokens::new();
        for item in spent {
            added(&mut totals, &item.models);
        }
        let duration = spent
            .iter()
            .map(|item| item.duration_ms)
            .fold(0_i64, i64::saturating_add)
            .min(MAX_TURN_DURATION_MS);
        Self {
            duration_ms: duration,
            models: bounded(&totals),
        }
    }

    /// The closed `usage` of a completed turn, or `None` when no model call of the turn reported usage.
    #[must_use]
    pub fn wire(&self) -> Option<Value> {
        if self.models.is_empty() {
            return None;
        }
        let models = self
            .models
            .iter()
            .map(|(provider, model, inputs, outputs)| {
                object_of(vec![
                    ("provider", text(provider)),
                    ("model", text(model)),
                    ("input_tokens", int(*inputs)),
                    ("output_tokens", int(*outputs)),
                ])
            })
            .collect();
        Some(object_of(vec![
            ("duration_ms", int(self.duration_ms)),
            ("models", Value::Array(models)),
        ]))
    }
}

/// What one logical chat turn consumed so far, carried with its pending state across human and Integration resumes.
///
/// `started_ms` is the wall-clock admission of the turn, so its duration survives a resume in a later request.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct TurnUsage {
    /// The turn's admission, in Unix milliseconds.
    pub started_ms: i64,
    /// The sorted tokens of calls that reported usage.
    pub models: Vec<ModelTokens>,
}

impl TurnUsage {
    /// A turn admitted at `now_ms`.
    #[must_use]
    pub const fn start(now_ms: i64) -> Self {
        Self {
            started_ms: now_ms,
            models: Vec::new(),
        }
    }

    /// This turn's usage plus what the current request's model calls used (`used`), within the wire bounds.
    #[must_use]
    pub fn joined(&self, used: &Tokens) -> Self {
        let mut totals = Tokens::new();
        added(&mut totals, &self.models);
        for (key, (inputs, outputs)) in used {
            let entry = totals.entry(key.clone()).or_default();
            entry.0 += inputs;
            entry.1 += outputs;
        }
        Self {
            started_ms: self.started_ms,
            models: bounded(&totals),
        }
    }

    /// This turn's usage as it stands at `now_ms`: its duration so far and its models.
    #[must_use]
    pub fn measured(&self, now_ms: i64) -> Spent {
        Spent {
            duration_ms: (now_ms - self.started_ms).clamp(0, MAX_TURN_DURATION_MS),
            models: self.models.clone(),
        }
    }

    /// The completed turn's whole usage, the current request's model calls included, measured once at `now_ms`.
    #[must_use]
    pub fn spent(&self, used: &Tokens, now_ms: i64) -> Spent {
        self.joined(used).measured(now_ms)
    }
}

#[cfg(test)]
mod tests;
