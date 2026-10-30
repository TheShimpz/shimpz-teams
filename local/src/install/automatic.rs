//! Offline-safe automatic public Assistant updates for the Local Space (`local/install/automatic.py`).
//!
//! Each pass sweeps retired images and superseded snapshots, then asks Developers once per installed source digest for
//! its latest publication and installs a newer one in every Team that binds an older one, fenced on the exact binding
//! it saw. A failing binding backs off on its own; nothing a pass meets stops the other Teams' updates.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use strict_json::Value;
use team_core::audit::AuditSink;
use team_core::entropy::Entropy;
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_install::bindings::{Binding, Provenance};
use team_install::trust::ResolveError;
use tokio::time::Instant;

use super::Assistants;
use super::collector::SnapshotCollector;
use super::spec::version_triple;
use crate::audit::{Outcome, Principal};
use crate::http::activity::Activity;
use crate::http::service::Problem;

/// The pause between two successful passes, before jitter.
pub const INTERVAL: Duration = Duration::from_secs(300);
const MAX_BACKOFF: Duration = Duration::from_secs(3600);
const JITTER_SECONDS: u64 = 60;

/// What the updater reconciles: the Space's publications and their lifecycle.
pub trait Reconciled: Send + Sync {
    /// The housekeeping before each pass: retired images and superseded snapshots.
    fn sweep(&self) -> BoxFuture<'_, ()>;

    /// Every binding of the Space.
    ///
    /// # Errors
    /// The registry's problem.
    fn bindings(&self) -> BoxFuture<'_, Result<Vec<Binding>, Problem>>;

    /// The latest publication of the Assistant `source_digest` belongs to.
    ///
    /// # Errors
    /// [`ResolveError`].
    fn latest<'a>(&'a self, source_digest: &'a str) -> BoxFuture<'a, Result<Value, ResolveError>>;

    /// Installs `source_digest` over the exact binding `expected_binding_digest`.
    ///
    /// # Errors
    /// The install's problem.
    fn install<'a>(
        &'a self,
        team_id: &'a str,
        assistant_id: &'a str,
        source_digest: &'a str,
        expected_binding_digest: &'a str,
    ) -> BoxFuture<'a, Result<(), Problem>>;
}

/// The controller's lifecycle as the updater reconciles it.
pub struct Lifecycle {
    /// The Assistants group.
    pub assistants: Arc<Assistants>,
    /// The superseded snapshot collector.
    pub collector: Arc<SnapshotCollector>,
}

impl Reconciled for Lifecycle {
    fn sweep(&self) -> BoxFuture<'_, ()> {
        Box::pin(async move {
            self.assistants.sweep_residues().await;
            self.collector.collect().await;
        })
    }

    fn bindings(&self) -> BoxFuture<'_, Result<Vec<Binding>, Problem>> {
        Box::pin(self.assistants.registry.snapshot())
    }

    fn latest<'a>(&'a self, source_digest: &'a str) -> BoxFuture<'a, Result<Value, ResolveError>> {
        Box::pin(async move {
            let resolution = self.assistants.resolver.latest(source_digest).await?;
            Ok(resolution.document().clone())
        })
    }

    fn install<'a>(
        &'a self,
        team_id: &'a str,
        assistant_id: &'a str,
        source_digest: &'a str,
        expected_binding_digest: &'a str,
    ) -> BoxFuture<'a, Result<(), Problem>> {
        Box::pin(async move {
            self.assistants
                .install_publication(
                    team_id,
                    assistant_id,
                    source_digest,
                    Some(expected_binding_digest),
                )
                .await
                .map(|_| ())
        })
    }
}

/// One binding's identity, as its backoff is kept.
type Key = (String, String, String);

fn key(binding: &Binding) -> Key {
    (
        binding.team_id.clone(),
        binding.assistant_id().to_owned(),
        binding.binding_digest.clone(),
    )
}

fn member<'a>(document: &'a Value, name: &str) -> Option<&'a str> {
    match document.get(name) {
        Some(Value::String(text)) => Some(text),
        _ => None,
    }
}

#[derive(Default)]
struct Backoff {
    failures: HashMap<Key, u32>,
    retry_after: HashMap<Key, Instant>,
}

/// The automatic updater.
pub struct AutomaticUpdates {
    reconciled: Arc<dyn Reconciled>,
    audit: Arc<dyn AuditSink>,
    activity: Arc<Activity>,
    entropy: Arc<dyn Entropy>,
    interval: Duration,
    backoff: Mutex<Backoff>,
}

impl std::fmt::Debug for AutomaticUpdates {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("AutomaticUpdates")
    }
}

/// The pause after a pass: the interval plus jitter after a success; otherwise the previous pause doubled, bounded.
#[must_use]
pub fn next_delay(
    previous: Duration,
    succeeded: bool,
    interval: Duration,
    jitter: Duration,
) -> Duration {
    if succeeded {
        return interval + jitter;
    }
    let previous = if previous.is_zero() {
        interval
    } else {
        previous
    };
    (previous * 2).min(MAX_BACKOFF)
}

impl AutomaticUpdates {
    /// The updater of `reconciled`, auditing to `audit` and counting its installs as Team work in `activity`.
    #[must_use]
    pub fn new(
        reconciled: Arc<dyn Reconciled>,
        audit: Arc<dyn AuditSink>,
        activity: Arc<Activity>,
        entropy: Arc<dyn Entropy>,
    ) -> Self {
        Self::with_interval(reconciled, audit, activity, entropy, INTERVAL)
    }

    /// The updater with another pass interval.
    #[must_use]
    pub fn with_interval(
        reconciled: Arc<dyn Reconciled>,
        audit: Arc<dyn AuditSink>,
        activity: Arc<Activity>,
        entropy: Arc<dyn Entropy>,
        interval: Duration,
    ) -> Self {
        Self {
            reconciled,
            audit,
            activity,
            entropy,
            interval,
            backoff: Mutex::new(Backoff::default()),
        }
    }

    fn backoff(&self) -> std::sync::MutexGuard<'_, Backoff> {
        self.backoff.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Runs passes until `stop`, the first at once.
    pub async fn run(&self, stop: &Stop) {
        let mut delay = Duration::ZERO;
        loop {
            tokio::select! {
                biased;
                () = stop.stopped() => return,
                () = tokio::time::sleep(delay) => {}
            }
            let succeeded = self.run_once().await;
            delay = next_delay(delay, succeeded, self.interval, self.jitter());
        }
    }

    fn jitter(&self) -> Duration {
        let mut bytes = [0_u8; 8];
        let draw = self
            .entropy
            .fill(&mut bytes)
            .map_or(0, |()| u64::from_le_bytes(bytes) % (JITTER_SECONDS + 1));
        Duration::from_secs(draw)
    }

    /// One pass; `false` when the bindings could not be read.
    pub async fn run_once(&self) -> bool {
        self.reconciled.sweep().await;
        let Ok(installed) = self.reconciled.bindings().await else {
            self.record(None, None, Outcome::Error, "cycle:bindings-unavailable")
                .await;
            return false;
        };
        let publications: Vec<&Binding> = installed
            .iter()
            .filter(|binding| binding.provenance == Provenance::Published)
            .collect();
        let active: Vec<Key> = publications.iter().map(|binding| key(binding)).collect();
        {
            let mut backoff = self.backoff();
            backoff.failures.retain(|key, _| active.contains(key));
            backoff.retry_after.retain(|key, _| active.contains(key));
        }
        let now = Instant::now();
        let mut candidates = HashMap::new();
        for binding in publications {
            self.reconcile(binding, now, &mut candidates).await;
        }
        true
    }

    async fn reconcile(
        &self,
        binding: &Binding,
        now: Instant,
        candidates: &mut HashMap<String, Result<Value, ResolveError>>,
    ) {
        let key = key(binding);
        if self
            .backoff()
            .retry_after
            .get(&key)
            .is_some_and(|after| now < *after)
        {
            return;
        }
        let (team, assistant) = (binding.team_id.as_str(), binding.assistant_id());
        let installed = member(&binding.document, "assistant_version");
        let (Some(current), Some(version), Some(source)) = (
            installed.and_then(version_triple),
            installed,
            member(&binding.document, "source_digest"),
        ) else {
            self.record(
                Some(team),
                Some(assistant),
                Outcome::Error,
                "binding:invalid",
            )
            .await;
            return;
        };
        let Some(target) = self.candidate(binding, source, candidates).await else {
            self.defer(&key, now);
            return;
        };
        self.apply(binding, &key, now, (current, version), &target)
            .await;
    }

    /// Installs `target` when it is the same Assistant and newer than the bound `installed` version.
    async fn apply(
        &self,
        binding: &Binding,
        key: &Key,
        now: Instant,
        installed: ((u64, u64, u64), &str),
        target: &Value,
    ) {
        let (team, assistant) = (binding.team_id.as_str(), binding.assistant_id());
        if member(target, "assistant_id") != Some(assistant) {
            self.refuse(binding, key, now, "candidate:identity-mismatch")
                .await;
            return;
        }
        let target_version = member(target, "assistant_version").unwrap_or_default();
        let (Some(newer), Some(source)) = (
            version_triple(target_version),
            member(target, "source_digest"),
        ) else {
            self.refuse(binding, key, now, "deferred:developers-protocol-invalid")
                .await;
            return;
        };
        if newer <= installed.0 {
            self.clear(key);
            return;
        }
        let updated = {
            let _working = self.activity.working();
            self.reconciled
                .install(team, assistant, source, &binding.binding_digest)
                .await
        };
        if let Err(problem) = updated {
            let code = problem.code.unwrap_or("runtime-unavailable");
            self.refuse(binding, key, now, &format!("deferred:{code}"))
                .await;
            return;
        }
        self.clear(key);
        let detail = format!("updated:{}:{target_version}", installed.1);
        self.record(Some(team), Some(assistant), Outcome::Ok, &detail)
            .await;
    }

    /// The latest publication of `source`, asked of Developers once per pass; a failure is audited.
    async fn candidate(
        &self,
        binding: &Binding,
        source: &str,
        candidates: &mut HashMap<String, Result<Value, ResolveError>>,
    ) -> Option<Value> {
        if !candidates.contains_key(source) {
            let latest = self.reconciled.latest(source).await;
            candidates.insert(source.to_owned(), latest);
        }
        let (outcome, detail) = match candidates.get(source)? {
            Ok(target) => return Some(target.clone()),
            Err(ResolveError::NotInstallable) => (Outcome::Ok, "deferred:no-candidate"),
            Err(ResolveError::Protocol) => (Outcome::Error, "deferred:developers-protocol-invalid"),
            Err(ResolveError::Unavailable) => (Outcome::Error, "deferred:developers-unavailable"),
        };
        self.record(
            Some(&binding.team_id),
            Some(binding.assistant_id()),
            outcome,
            detail,
        )
        .await;
        None
    }

    async fn refuse(&self, binding: &Binding, key: &Key, now: Instant, detail: &str) {
        self.record(
            Some(&binding.team_id),
            Some(binding.assistant_id()),
            Outcome::Error,
            detail,
        )
        .await;
        self.defer(key, now);
    }

    fn defer(&self, key: &Key, now: Instant) {
        let mut backoff = self.backoff();
        let failures = backoff.failures.entry(key.clone()).or_insert(0);
        *failures += 1;
        let delay = (self.interval * 2_u32.pow((*failures - 1).min(4))).min(MAX_BACKOFF);
        backoff.retry_after.insert(key.clone(), now + delay);
    }

    fn clear(&self, key: &Key) {
        let mut backoff = self.backoff();
        backoff.failures.remove(key);
        backoff.retry_after.remove(key);
    }

    /// Audits one result; an unavailable audit log never stops the updater.
    async fn record(
        &self,
        team_id: Option<&str>,
        assistant_id: Option<&str>,
        result: Outcome,
        detail: &str,
    ) {
        let text = |value: Option<&str>| {
            value.map_or(Value::Null, |value| Value::String(value.to_owned()))
        };
        let event = Principal::team()
            .event("assistant-update", result)
            .with("team_id", text(team_id))
            .with("assistant", text(assistant_id))
            .with("detail", text(Some(detail)));
        let _ = self.audit.record(event).await;
    }
}

#[cfg(all(test, feature = "testing"))]
mod tests;
