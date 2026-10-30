//! Retry policies, timeouts, and the classification of an attempt's failure, after Temporal's activity options.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::json as canonical;

/// Why an attempt failed, as the step records it.
///
/// `code` is a short machine name; `message` is for people and must never carry a credential or a payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Failure {
    /// A short machine name, such as `timeout` or `http-503`.
    pub code: String,
    /// A human-readable explanation without secrets or payloads.
    pub message: String,
    /// Whether another attempt may succeed.
    pub retryable: bool,
    /// Whether the executor confirmed that it stopped and will not act for this attempt (see [`Containment`]).
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub ended: bool,
}

impl Failure {
    /// A failure another attempt may fix.
    #[must_use]
    pub fn retryable(code: &str, message: &str) -> Self {
        Self { code: code.into(), message: message.into(), retryable: true, ended: false }
    }

    /// A failure no further attempt can fix.
    #[must_use]
    pub fn permanent(code: &str, message: &str) -> Self {
        Self { code: code.into(), message: message.into(), retryable: false, ended: false }
    }

    /// The same failure, with the executor's confirmation that it stopped.
    #[must_use]
    pub fn with_ended(mut self) -> Self {
        self.ended = true;
        self
    }
}

/// When and how often a failed attempt is retried.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RetryPolicy {
    /// The delay before the second attempt.
    pub initial_ms: u64,
    /// Each later delay is the previous one times `backoff_percent / 100`.
    pub backoff_percent: u32,
    /// The longest delay between two attempts.
    pub max_interval_ms: u64,
    /// The most attempts in total; `None` retries until the step's or the run's deadline.
    pub max_attempts: Option<u32>,
    /// Failure codes that are never retried, whatever the failure says.
    pub non_retryable: Vec<String>,
}

impl Default for RetryPolicy {
    fn default() -> Self {
        Self {
            initial_ms: 1_000,
            backoff_percent: 200,
            max_interval_ms: 60_000,
            max_attempts: None,
            non_retryable: vec![],
        }
    }
}

impl RetryPolicy {
    /// A policy that never retries.
    #[must_use]
    pub fn never() -> Self {
        Self { max_attempts: Some(1), ..Self::default() }
    }

    /// The delay before attempt `attempt + 1`, after attempt `attempt` (1-based) failed with `failure`, or `None`
    /// when no further attempt is allowed.
    #[must_use]
    pub fn next_delay(&self, attempt: u32, failure: &Failure) -> Option<u64> {
        if !failure.retryable || self.non_retryable.contains(&failure.code) {
            return None;
        }
        if self.max_attempts.is_some_and(|max| attempt >= max) {
            return None;
        }
        let mut delay = self.initial_ms;
        for _ in 1..attempt {
            delay = delay.saturating_mul(u64::from(self.backoff_percent)) / 100;
            if delay >= self.max_interval_ms {
                break;
            }
        }
        Some(delay.min(self.max_interval_ms))
    }
}

/// The canonical description of a retry policy and its time bounds, digested into every scheduled step.
pub(crate) fn describe(retry: &RetryPolicy, timeouts: &Timeouts) -> Value {
    json!({
        "retry": {
            "initial_ms": retry.initial_ms,
            "backoff_percent": retry.backoff_percent,
            "max_interval_ms": retry.max_interval_ms,
            "max_attempts": retry.max_attempts,
            "non_retryable": retry.non_retryable,
        },
        "timeouts": {
            "start_to_close_ms": timeouts.start_to_close_ms,
            "schedule_to_close_ms": timeouts.schedule_to_close_ms,
            "heartbeat_ms": timeouts.heartbeat_ms,
        },
    })
}

/// Time bounds of an activity or a model step.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Timeouts {
    /// The longest one attempt may run.
    pub start_to_close_ms: Option<u64>,
    /// The longest all attempts together may take, from the first dispatch.
    pub schedule_to_close_ms: Option<u64>,
    /// The longest an attempt may go without a heartbeat.
    pub heartbeat_ms: Option<u64>,
}

/// Where an activity's effects happen, which decides what proves that an abandoned attempt stopped.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Containment {
    /// All of the attempt's work happens on its own thread: once that thread returns, nothing more can happen.
    InThread,
    /// The attempt drives an executor outside the thread (a browser, a remote call); only the host can prove the
    /// executor stopped, unless the attempt returned `Ok` or a failure marked [`Failure::ended`].
    External,
}

/// How a registered activity is dispatched.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ActivityOptions {
    /// The host's declaration that its executor deduplicates by the effect key, so an interrupted attempt may be
    /// repeated. Never inferred.
    pub idempotent: bool,
    /// Where the activity's effects happen.
    pub containment: Containment,
    /// How failed attempts are retried.
    pub retry: RetryPolicy,
    /// The activity's time bounds.
    pub timeouts: Timeouts,
}

impl ActivityOptions {
    /// The digest of everything that decides how the activity's attempts run.
    #[must_use]
    pub fn digest(&self) -> String {
        let mut description = describe(&self.retry, &self.timeouts);
        description["idempotent"] = json!(self.idempotent);
        description["containment"] = json!(match self.containment {
            Containment::InThread => "in-thread",
            Containment::External => "external",
        });
        canonical::digest(&description)
    }

    /// A non-idempotent external activity that is never retried and has no time bound: the safest default.
    #[must_use]
    pub fn external() -> Self {
        Self {
            idempotent: false,
            containment: Containment::External,
            retry: RetryPolicy::never(),
            timeouts: Timeouts::default(),
        }
    }

    /// An idempotent in-thread activity retried with the default policy.
    #[must_use]
    pub fn idempotent() -> Self {
        Self {
            idempotent: true,
            containment: Containment::InThread,
            retry: RetryPolicy::default(),
            timeouts: Timeouts::default(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn delays_grow_by_the_backoff_and_stop_at_the_interval() {
        let policy =
            RetryPolicy { initial_ms: 100, backoff_percent: 300, max_interval_ms: 1_000, ..RetryPolicy::default() };
        let failure = Failure::retryable("http-503", "unavailable");
        let delays: Vec<_> = (1..=5).map(|attempt| policy.next_delay(attempt, &failure)).collect();
        assert_eq!(delays, [Some(100), Some(300), Some(900), Some(1_000), Some(1_000)]);
    }

    #[test]
    fn a_huge_attempt_number_saturates_at_the_interval() {
        let policy = RetryPolicy { initial_ms: u64::MAX / 2, ..RetryPolicy::default() };
        assert_eq!(policy.next_delay(200, &Failure::retryable("x", "y")), Some(60_000));
    }

    #[test]
    fn permanent_listed_and_exhausted_failures_stop() {
        let policy =
            RetryPolicy { max_attempts: Some(2), non_retryable: vec!["denied".into()], ..RetryPolicy::default() };
        assert_eq!(policy.next_delay(1, &Failure::permanent("bad-request", "no")), None);
        assert_eq!(policy.next_delay(1, &Failure::retryable("denied", "no")), None);
        assert_eq!(policy.next_delay(1, &Failure::retryable("timeout", "slow")), Some(1_000));
        assert_eq!(policy.next_delay(2, &Failure::retryable("timeout", "slow")), None);
        assert_eq!(RetryPolicy::never().next_delay(1, &Failure::retryable("timeout", "slow")), None);
    }

    #[test]
    fn ended_is_omitted_when_false() {
        let encoded = serde_json::to_value(Failure::permanent("a", "b")).unwrap();
        assert_eq!(encoded, serde_json::json!({"code": "a", "message": "b", "retryable": false}));
        let ended = serde_json::to_value(Failure::permanent("a", "b").with_ended()).unwrap();
        assert_eq!(ended["ended"], serde_json::json!(true));
        assert!(
            serde_json::from_value::<Failure>(
                serde_json::json!({"code": "a", "message": "b", "retryable": true, "x": 1})
            )
            .is_err()
        );
    }

    #[test]
    fn digests_change_with_every_semantic_option() {
        let base = ActivityOptions::external();
        let idempotent = ActivityOptions { idempotent: true, ..base.clone() };
        let in_thread = ActivityOptions { containment: Containment::InThread, ..base.clone() };
        let retried = ActivityOptions { retry: RetryPolicy::default(), ..base.clone() };
        let bounded =
            ActivityOptions { timeouts: Timeouts { heartbeat_ms: Some(5), ..Timeouts::default() }, ..base.clone() };
        let digests: std::collections::HashSet<String> =
            [&base, &idempotent, &in_thread, &retried, &bounded].iter().map(|options| options.digest()).collect();
        assert_eq!(digests.len(), 5);
        assert_eq!(base.digest(), ActivityOptions::external().digest());
    }

    #[test]
    fn option_presets_are_safe() {
        let external = ActivityOptions::external();
        assert!(!external.idempotent);
        assert_eq!(external.containment, Containment::External);
        assert_eq!(external.retry.max_attempts, Some(1));
        let idempotent = ActivityOptions::idempotent();
        assert!(idempotent.idempotent);
        assert_eq!(idempotent.containment, Containment::InThread);
    }
}
