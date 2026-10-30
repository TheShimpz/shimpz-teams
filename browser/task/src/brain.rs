//! A Browser task's Brain calls: its four stateless operations, their answers as Team admits them, and their usage
//! (ADR-0117 §5, 2026-10-22 amendment).

use std::sync::Arc;

use strict_json::{Number, Value};
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_inference::brain::{
    BrainClient, BrainError, BrainOperation, BrainRequest, ModelCredential,
};
use team_protocol::turn::SAFE_ID_RE;
use tokio::time::Instant;

/// The most any usage count may be.
pub const MAX_COUNT: u64 = 1_000_000_000_000;

/// One Brain call's usage, as the Brain reports what each provider response said.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Usage {
    /// Model calls the operation made.
    pub model_calls: u64,
    /// Provider requests, each SDK retry one more.
    pub provider_requests: u64,
    /// Model calls that failed.
    pub failed_calls: u64,
    /// Model calls whose usage the provider did not report.
    pub unreported_calls: u64,
    /// Input tokens.
    pub input_tokens: u64,
    /// Output tokens.
    pub output_tokens: u64,
    /// Cache-read input tokens.
    pub cache_read_tokens: u64,
    /// Cache-write input tokens.
    pub cache_write_tokens: u64,
}

const FIELDS: [&str; 8] = [
    "model_calls",
    "provider_requests",
    "failed_calls",
    "unreported_calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
];

impl Usage {
    /// The Brain's closed usage shape: exactly its eight non-negative integer counts, with consistent call totals.
    #[must_use]
    pub fn parse(value: &Value) -> Option<Self> {
        let Value::Object(members) = value else {
            return None;
        };
        if members.len() != FIELDS.len() || !FIELDS.iter().all(|name| value.get(name).is_some()) {
            return None;
        }
        let count = |name: &str| match value.get(name) {
            Some(Value::Number(Number::Int(count))) => u64::try_from(*count)
                .ok()
                .filter(|count| *count <= MAX_COUNT),
            _ => None,
        };
        let usage = Self {
            model_calls: count("model_calls")?,
            provider_requests: count("provider_requests")?,
            failed_calls: count("failed_calls")?,
            unreported_calls: count("unreported_calls")?,
            input_tokens: count("input_tokens")?,
            output_tokens: count("output_tokens")?,
            cache_read_tokens: count("cache_read_tokens")?,
            cache_write_tokens: count("cache_write_tokens")?,
        };
        (usage.failed_calls + usage.unreported_calls <= usage.model_calls).then_some(usage)
    }
}

/// One Brain operation of a Browser task.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum BrowserCall {
    /// The next step.
    Step,
    /// The agent's rewritten memory as steps fold.
    Fold,
    /// The advisory completeness check.
    Check,
    /// The final answer from the thread.
    Final,
}

impl BrowserCall {
    /// The operation's name, on the wire and in usage records.
    #[must_use]
    pub const fn operation(self) -> &'static str {
        match self {
            Self::Step => "browser-step",
            Self::Fold => "browser-fold",
            Self::Check => "browser-check",
            Self::Final => "browser-final",
        }
    }

    /// The Brain operation it posts.
    #[must_use]
    pub const fn brain(self) -> BrainOperation {
        match self {
            Self::Step => BrainOperation::BrowserStep,
            Self::Fold => BrainOperation::BrowserFold,
            Self::Check => BrainOperation::BrowserCheck,
            Self::Final => BrainOperation::BrowserFinal,
        }
    }

    /// The operation [`BrowserCall::operation`] names, or `None`.
    #[must_use]
    pub fn parse(operation: &str) -> Option<Self> {
        [Self::Step, Self::Fold, Self::Check, Self::Final]
            .into_iter()
            .find(|call| call.operation() == operation)
    }

    /// The fields of its answer beside the model and its usage.
    const fn fields(self) -> &'static [&'static str] {
        match self {
            Self::Step => &["step"],
            Self::Fold => &["memory"],
            Self::Check => &["complete", "missing"],
            Self::Final => &["summary"],
        }
    }
}

/// An admitted answer: its raw value, its usage, and the Browser model that answered.
#[derive(Clone, Debug, PartialEq)]
pub struct Answered {
    /// A step's raw step, or the object of the operation's fields; the task admits only their canonical form.
    pub value: Value,
    /// The call's usage.
    pub usage: Usage,
    /// The model that answered.
    pub model: String,
}

/// Why the Brain gave no admitted answer; nothing of the call is trusted.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CallFailure {
    /// The Brain or its provider could not be reached or failed on its side: another attempt may answer.
    Unavailable,
    /// The Brain refused the call, its time ran out, the turn stopped, or the answer was outside its form.
    Refused,
}

impl From<BrainError> for CallFailure {
    fn from(error: BrainError) -> Self {
        match error {
            BrainError::Unavailable => Self::Unavailable,
            BrainError::Rejected(status) if status >= 500 => Self::Unavailable,
            BrainError::Stopped
            | BrainError::Deadline
            | BrainError::Rejected(_)
            | BrainError::Protocol => Self::Refused,
        }
    }
}

/// How a task calls the Brain.
pub trait TaskBrain: Send + Sync {
    /// Posts one operation with `request`, abandoning it at `deadline`.
    ///
    /// # Errors
    /// [`CallFailure`] for every unsuccessful outcome.
    fn call(
        &self,
        call: BrowserCall,
        request: Value,
        deadline: Instant,
    ) -> BoxFuture<'_, Result<Answered, CallFailure>>;
}

/// Told of each admitted answer: its operation, its model, and its usage.
pub type Meter = Arc<dyn Fn(BrowserCall, &str, &Usage) + Send + Sync>;

/// A task's Brain calls through the Brain runtime, under the turn's model credential and Stop.
pub struct BrainCalls {
    client: Arc<dyn BrainClient>,
    credential: ModelCredential,
    stop: Stop,
    meter: Meter,
}

impl std::fmt::Debug for BrainCalls {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("BrainCalls")
    }
}

impl BrainCalls {
    /// The calls of one task.
    #[must_use]
    pub fn new(
        client: Arc<dyn BrainClient>,
        credential: ModelCredential,
        stop: Stop,
        meter: Meter,
    ) -> Self {
        Self {
            client,
            credential,
            stop,
            meter,
        }
    }
}

/// A Browser operation's answer as Team admits it, or `None`.
#[must_use]
pub fn admit(call: BrowserCall, response: &Value) -> Option<Answered> {
    let Value::Object(members) = response else {
        return None;
    };
    let fields = call.fields();
    let expected = fields.len() + 2;
    let known = |name: &str| name == "model" || name == "usage" || fields.contains(&name);
    if members.len() != expected || !members.iter().all(|(name, _)| known(name)) {
        return None;
    }
    let Some(Value::String(model)) = response.get("model") else {
        return None;
    };
    if !SAFE_ID_RE.is_full(model) {
        return None;
    }
    let usage = Usage::parse(response.get("usage")?)?;
    let value = if call == BrowserCall::Step {
        response.get("step")?.clone()
    } else {
        Value::Object(
            fields
                .iter()
                .map(|name| Some(((*name).to_owned(), response.get(name)?.clone())))
                .collect::<Option<_>>()?,
        )
    };
    Some(Answered {
        value,
        usage,
        model: model.clone(),
    })
}

impl TaskBrain for BrainCalls {
    fn call(
        &self,
        call: BrowserCall,
        request: Value,
        deadline: Instant,
    ) -> BoxFuture<'_, Result<Answered, CallFailure>> {
        Box::pin(async move {
            let response = self
                .client
                .call(BrainRequest {
                    operation: call.brain(),
                    body: request,
                    credential: Some(&self.credential),
                    deadline,
                    stop: &self.stop,
                })
                .await
                .map_err(CallFailure::from)?;
            let answered = admit(call, &response).ok_or(CallFailure::Refused)?;
            (self.meter)(call, &answered.model, &answered.usage);
            Ok(answered)
        })
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    use team_core::secret::Secret;
    use team_core::stop::Stop;
    use team_inference::brain::{BrainError, BrainOperation, ModelCredential};
    use team_inference::testing::ScriptedBrain;
    use tokio::time::Instant;

    use super::{BrainCalls, BrowserCall, CallFailure, TaskBrain, Usage, admit};
    use crate::testing::json;

    const USAGE: &str = r#"{"model_calls": 1, "provider_requests": 1, "failed_calls": 0, "unreported_calls": 0, "input_tokens": 1200, "output_tokens": 40, "cache_read_tokens": 0, "cache_write_tokens": 0}"#;

    fn with_usage(answer: &str) -> strict_json::Value {
        let mut value = json(answer);
        if let strict_json::Value::Object(members) = &mut value {
            members.push(("usage".to_owned(), json(USAGE)));
        }
        value
    }

    #[tokio::test]
    async fn a_step_returns_the_raw_step_its_usage_and_model_and_meters_the_browser_model() {
        let client = Arc::new(ScriptedBrain::default());
        client.answer(Ok(with_usage(
            r#"{"step": {"kind": "done", "summary": "ok"}, "model": "gpt-6-luna"}"#,
        )));
        let metered = Arc::new(Mutex::new(Vec::new()));
        let seen = Arc::clone(&metered);
        let credential = ModelCredential {
            provider: "openai".to_owned(),
            model: "gpt-6.1-sol".to_owned(),
            api_key: Secret::new("sk-test".to_owned()),
        };
        let calls = BrainCalls::new(
            client.clone(),
            credential,
            Stop::new(),
            Arc::new(move |call, model: &str, usage: &Usage| {
                seen.lock()
                    .expect("meter")
                    .push((call, model.to_owned(), usage.input_tokens));
            }),
        );
        let request = json(r#"{"objective": "x", "language": "en", "observation": "url: x"}"#);
        let answered = calls
            .call(
                BrowserCall::Step,
                request.clone(),
                Instant::now() + Duration::from_secs(60),
            )
            .await
            .expect("answered");
        assert_eq!(answered.value, json(r#"{"kind": "done", "summary": "ok"}"#));
        assert_eq!(
            (answered.model.as_str(), answered.usage.input_tokens),
            ("gpt-6-luna", 1200)
        );
        assert_eq!(
            *metered.lock().expect("meter"),
            [(BrowserCall::Step, "gpt-6-luna".to_owned(), 1200)]
        );
        let received = client.received();
        assert_eq!(received[0].operation, BrainOperation::BrowserStep);
        assert_eq!(received[0].body, request);
        assert_eq!(received[0].provider.as_deref(), Some("openai"));
        assert_eq!(metered.lock().expect("meter").len(), 1);
    }

    #[tokio::test]
    async fn an_unreachable_brain_may_answer_later_and_a_refusal_never() {
        let client = Arc::new(ScriptedBrain::default());
        let credential = ModelCredential {
            provider: "openai".to_owned(),
            model: "gpt-6.1-sol".to_owned(),
            api_key: Secret::new("test-key".to_owned()),
        };
        let calls = BrainCalls::new(
            client.clone(),
            credential,
            Stop::new(),
            Arc::new(|_, _: &str, _: &Usage| {}),
        );
        let request = json(r#"{"objective": "x"}"#);
        client.answer(Err(BrainError::Unavailable));
        client.answer(Err(BrainError::Rejected(503)));
        client.answer(Err(BrainError::Rejected(422)));
        client.answer(Ok(with_usage(r#"{"step": {}, "model": 7}"#)));
        for expected in [
            CallFailure::Unavailable,
            CallFailure::Unavailable,
            CallFailure::Refused,
            CallFailure::Refused,
        ] {
            let deadline = Instant::now() + Duration::from_secs(60);
            assert_eq!(
                calls
                    .call(BrowserCall::Step, request.clone(), deadline)
                    .await,
                Err(expected)
            );
        }
        assert_eq!(BrowserCall::parse("browser-fold"), Some(BrowserCall::Fold));
        assert_eq!(BrowserCall::parse("browser-walk"), None);
    }

    #[test]
    fn each_finishing_operation_answers_its_own_fields() {
        for (call, answer) in [
            (BrowserCall::Fold, r#"{"memory": "V101: R$ 9.500"}"#),
            (
                BrowserCall::Check,
                r#"{"complete": false, "missing": "Faltam vagas."}"#,
            ),
            (BrowserCall::Final, r#"{"summary": "Encontrei 7 vagas."}"#),
        ] {
            let response = with_usage(&answer.replace('}', r#", "model": "gpt-6-luna"}"#));
            assert_eq!(
                admit(call, &response).map(|answered| answered.value),
                Some(json(answer))
            );
        }
        assert_eq!(BrowserCall::Check.operation(), "browser-check");
        assert_eq!(BrowserCall::Final.brain(), BrainOperation::BrowserFinal);
    }

    #[test]
    fn a_malformed_answer_is_refused() {
        let bad_usage = r#"{"step": {}, "model": "gpt-6-luna", "usage": {"model_calls": "one"}}"#;
        for (call, response) in [
            (
                BrowserCall::Step,
                with_usage(r#"{"step": {}, "model": "gpt-6-luna", "extra": 1}"#),
            ),
            (
                BrowserCall::Step,
                with_usage(r#"{"step": {}, "model": "not a model!"}"#),
            ),
            (BrowserCall::Step, with_usage(r#"{"step": {}, "model": 7}"#)),
            (BrowserCall::Step, json(bad_usage)),
            (BrowserCall::Step, json(r#"["step"]"#)),
            (
                BrowserCall::Step,
                json(r#"{"step": {}, "model": "gpt-6-luna"}"#),
            ),
            (
                BrowserCall::Fold,
                with_usage(r#"{"step": {}, "model": "gpt-6-luna"}"#),
            ),
            (
                BrowserCall::Check,
                with_usage(r#"{"complete": true, "model": "gpt-6-luna"}"#),
            ),
        ] {
            assert_eq!(admit(call, &response), None);
        }
    }

    #[test]
    fn usage_admits_only_its_closed_consistent_shape() {
        assert_eq!(
            Usage::parse(&json(USAGE)).map(|usage| usage.output_tokens),
            Some(40)
        );
        for broken in [
            USAGE.replace(r#""failed_calls": 0"#, r#""failed_calls": 1, "x": 0"#),
            USAGE.replace(r#""failed_calls": 0"#, r#""failed_calls": 2"#),
            USAGE.replace(r#""input_tokens": 1200"#, r#""input_tokens": -1"#),
            USAGE.replace(r#""input_tokens": 1200"#, r#""input_tokens": 1200.0"#),
            USAGE.replace(r#""input_tokens": 1200"#, r#""input_tokens": true"#),
            USAGE.replace(
                r#""input_tokens": 1200"#,
                r#""input_tokens": 1000000000001"#,
            ),
            USAGE.replace(r#", "cache_write_tokens": 0"#, ""),
        ] {
            assert_eq!(Usage::parse(&json(&broken)), None);
        }
    }
}
