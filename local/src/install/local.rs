//! Staged Local snapshots: the inventory with each one's admission verdict, the preview (icon and localized pages),
//! and installing an exact snapshot (`local/install/service.py`, `admission.py`, `preview.py`).

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use strict_json::Value;
use team_assistant::cache::ReadCache;
use team_core::audit::AuditSink;
use team_install::bindings::{Binding, Provenance, from_local_record};
use team_install::icons::IconIdentity;
use tokio::sync::{Mutex, Semaphore};

use super::problem::{
    ICON_UNAVAILABLE, LOCAL_BINDING_CONFLICT, PREVIEW_BUSY, PREVIEW_INVALID, PREVIEW_UNAVAILABLE,
    SNAPSHOT_INVALID, SNAPSHOT_UNAVAILABLE, SNAPSHOTS_INVALID, SNAPSHOTS_UNAVAILABLE,
};
use super::registry::{Registry, binding_problem};
use super::snapshots::{
    self, Candidate, LABELS_INVALID, MAX_CANDIDATES, Preview, Refused, SnapshotError, Stage,
    Verdict,
};
use super::{Assistants, object};
use crate::audit::Outcome;
use crate::browser::audit::Actor;
use crate::docker::problem::INTERNAL;
use crate::http::service::Problem;

const MAX_PREVIEW_BYTES: usize = 8 * 1024 * 1024;
const MAX_CONCURRENT_EXTRACTIONS: usize = 2;

/// The staged snapshot inventory and its bounded caches.
pub struct LocalSnapshots {
    stage: Stage,
    audit: Arc<dyn AuditSink>,
    verdicts: ReadCache<Verdict, ()>,
    previews: ReadCache<Preview, Problem>,
    slots: Semaphore,
    sightings: Mutex<Vec<Refused>>,
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn optional(value: Option<&str>) -> Value {
    value.map_or(Value::Null, text)
}

fn strings(items: &[String]) -> Value {
    Value::Array(items.iter().map(|item| text(item)).collect())
}

fn preview_problem(error: SnapshotError) -> Problem {
    match error {
        SnapshotError::Invalid(_) => PREVIEW_INVALID,
        SnapshotError::Unavailable | SnapshotError::Absent => PREVIEW_UNAVAILABLE,
    }
}

impl LocalSnapshots {
    /// The inventory of `stage`, auditing refusals to `audit`.
    #[must_use]
    pub fn new(stage: Stage, audit: Arc<dyn AuditSink>) -> Self {
        Self {
            stage,
            audit,
            verdicts: ReadCache::new(2 * MAX_CANDIDATES, None),
            previews: ReadCache::new(MAX_CANDIDATES, Some(MAX_PREVIEW_BYTES)),
            slots: Semaphore::new(MAX_CONCURRENT_EXTRACTIONS),
            sightings: Mutex::new(Vec::new()),
        }
    }

    async fn record(
        &self,
        actor: &Actor,
        operation: &str,
        result: Outcome,
        assistant: Option<&str>,
        detail: &str,
    ) -> Result<(), Problem> {
        let event = actor
            .event(operation, result)
            .with("assistant", optional(assistant))
            .with("detail", text(detail));
        self.audit.record(event).await.map_err(|_| INTERNAL)
    }

    /// One candidate's verdict, judged once per image; an unavailable judgment is never kept. `true` when this read
    /// settled it.
    async fn judge(&self, candidate: &Candidate) -> (Verdict, bool) {
        let fresh = AtomicBool::new(false);
        let key = format!(
            "{}\0{}",
            candidate.image_id,
            snapshots::canonical_reference(&candidate.assistant_id)
        );
        let verdict = self
            .verdicts
            .get(
                &key,
                |_| 1,
                || async {
                    let _slot = self.slots.acquire().await;
                    fresh.store(true, Ordering::Relaxed);
                    match snapshots::verdict(
                        self.stage.docker.as_ref(),
                        &self.stage.space,
                        self.stage.entropy.as_ref(),
                        &candidate.image_id,
                    )
                    .await
                    {
                        Verdict::Unavailable => Err(()),
                        verdict => Ok(verdict),
                    }
                },
            )
            .await
            .unwrap_or(Verdict::Unavailable);
        (verdict, fresh.load(Ordering::Relaxed))
    }

    /// Every staged identity with its verdict; only an admissible snapshot may be offered (ADR-0060).
    ///
    /// # Errors
    /// `local-assistant-snapshots-unavailable` or `-invalid`, and the audit log's failure.
    pub async fn list(&self, actor: &Actor) -> Result<Value, Problem> {
        let listing =
            snapshots::list(self.stage.docker.as_ref())
                .await
                .map_err(|error| match error {
                    SnapshotError::Invalid(_) => SNAPSHOTS_INVALID,
                    _ => SNAPSHOTS_UNAVAILABLE,
                })?;
        let mut entries = Vec::new();
        for candidate in &listing.candidates {
            entries.push(self.judged_entry(candidate, actor).await?);
        }
        for refused in &listing.refused {
            if let Some(entry) = self
                .refused_entry(refused, &listing.platform, actor)
                .await?
            {
                entries.push(entry);
            }
        }
        Ok(object(vec![("assistants", Value::Array(entries))]))
    }

    /// One candidate's entry; an unavailable verdict, and a refusal this read settled, are audited.
    async fn judged_entry(&self, candidate: &Candidate, actor: &Actor) -> Result<Value, Problem> {
        let (verdict, fresh) = self.judge(candidate).await;
        let assistant = Some(candidate.assistant_id.as_str());
        match verdict {
            Verdict::Unavailable => {
                self.record(
                    actor,
                    "local-assistant-admission-unavailable",
                    Outcome::Error,
                    assistant,
                    &candidate.image_id,
                )
                .await?;
            }
            Verdict::Inadmissible(reason) if fresh => {
                self.record(
                    actor,
                    "local-assistant-inadmissible",
                    Outcome::Denied,
                    assistant,
                    &format!("{reason}:{}", candidate.image_id),
                )
                .await?;
            }
            _ => {}
        }
        Ok(entry(candidate, verdict))
    }

    /// A refused image's shadow entry under its valid Assistant id, its refusal audited once per sighting.
    async fn refused_entry(
        &self,
        refused: &Refused,
        platform: &str,
        actor: &Actor,
    ) -> Result<Option<Value>, Problem> {
        if self.first_sighting(refused).await {
            let reason = if refused.assistant_id.is_some() {
                LABELS_INVALID
            } else {
                "identity-invalid"
            };
            self.record(
                actor,
                "local-assistant-inadmissible",
                Outcome::Denied,
                refused.assistant_id.as_deref(),
                &format!("{reason}:{}", refused.image_id),
            )
            .await?;
        }
        Ok(refused
            .assistant_id
            .as_ref()
            .map(|assistant_id| shadow_entry(assistant_id, &refused.image_id, platform)))
    }

    async fn first_sighting(&self, refused: &Refused) -> bool {
        let mut sightings = self.sightings.lock().await;
        if let Some(index) = sightings.iter().position(|seen| seen == refused) {
            let seen = sightings.remove(index);
            sightings.push(seen);
            return false;
        }
        sightings.push(refused.clone());
        if sightings.len() > 2 * MAX_CANDIDATES {
            sightings.remove(0);
        }
        true
    }

    /// One staged image's validated preview; a cached one is kept only while the exact staged image remains.
    async fn preview(&self, image_id: &str) -> Result<Preview, Problem> {
        let extracted = AtomicBool::new(false);
        let preview = self
            .previews
            .get(image_id, Preview::size, || async {
                let _slot = self.slots.try_acquire().map_err(|_| PREVIEW_BUSY)?;
                extracted.store(true, Ordering::Relaxed);
                snapshots::preview(
                    self.stage.docker.as_ref(),
                    &self.stage.space,
                    self.stage.entropy.as_ref(),
                    image_id,
                )
                .await
                .map_err(preview_problem)
            })
            .await?;
        if !extracted.load(Ordering::Relaxed) {
            match snapshots::require_candidate(self.stage.docker.as_ref(), image_id).await {
                Ok(_) => {}
                Err(SnapshotError::Absent) => {
                    self.previews.discard(image_id);
                    return Err(PREVIEW_UNAVAILABLE);
                }
                Err(error) => return Err(preview_problem(error)),
            }
        }
        Ok(preview)
    }

    /// The staged image's icon.
    ///
    /// # Errors
    /// The preview's problems.
    pub async fn icon(&self, image_id: &str) -> Result<Vec<u8>, Problem> {
        Ok(self.preview(image_id).await?.icon)
    }

    /// The staged image's summary in one interface language, from its own pack.
    ///
    /// # Errors
    /// The preview's problems.
    pub async fn summary(&self, image_id: &str, locale: &str) -> Result<Value, Problem> {
        let preview = self.preview(image_id).await?;
        let summary = preview
            .page(locale)
            .and_then(|page| page.get("summary"))
            .cloned()
            .ok_or(PREVIEW_INVALID)?;
        Ok(object(vec![("locale", text(locale)), ("summary", summary)]))
    }

    /// The staged image's Assistant page in one interface language, from its own pack.
    ///
    /// # Errors
    /// The preview's problems.
    pub async fn details(&self, image_id: &str, locale: &str) -> Result<Value, Problem> {
        self.preview(image_id)
            .await?
            .page(locale)
            .cloned()
            .ok_or(PREVIEW_INVALID)
    }
}

fn entry(candidate: &Candidate, verdict: Verdict) -> Value {
    let (state, reason) = verdict.report();
    object(vec![
        ("assistant_id", text(&candidate.assistant_id)),
        ("assistant_version", text(&candidate.version)),
        ("name", text(&candidate.name)),
        ("summary", text(&candidate.summary)),
        ("declared_creators", strings(&candidate.declared_creators)),
        ("actions", strings(&candidate.actions)),
        ("integrations", strings(&candidate.integrations)),
        ("image_id", text(&candidate.image_id)),
        ("platform", text(&candidate.platform)),
        ("created_at", text(&candidate.created_at)),
        ("provenance", text("local")),
        ("unpublished", Value::Bool(true)),
        ("admission", text(state)),
        ("reason", optional(reason)),
    ])
}

fn shadow_entry(assistant_id: &str, image_id: &str, platform: &str) -> Value {
    let none = || Value::Null;
    object(vec![
        ("assistant_id", text(assistant_id)),
        ("assistant_version", none()),
        ("name", none()),
        ("summary", none()),
        ("declared_creators", none()),
        ("actions", none()),
        ("integrations", none()),
        ("image_id", text(image_id)),
        ("platform", text(platform)),
        ("created_at", none()),
        ("provenance", text("local")),
        ("unpublished", Value::Bool(true)),
        ("admission", text("inadmissible")),
        ("reason", text(LABELS_INVALID)),
    ])
}

fn record_id(record: &Value) -> String {
    match record.get("assistant_id") {
        Some(Value::String(id)) => id.clone(),
        _ => String::new(),
    }
}

impl Assistants {
    /// Installs one exact staged snapshot; `fresh_only` installs it only while the Assistant is unbound. A published
    /// binding is replaced by the Local one; a Local binding of another image is updated to it.
    ///
    /// # Errors
    /// The snapshot's admission, the registry's, the icon's, and the lifecycle's problems.
    pub async fn install_local(
        &self,
        team_id: &str,
        image_id: &str,
        fresh_only: bool,
    ) -> Result<Value, Problem> {
        let admitted = snapshots::admit(
            self.stage.docker.as_ref(),
            &self.stage.space,
            self.stage.entropy.as_ref(),
            image_id,
        )
        .await
        .map_err(|error| match error {
            SnapshotError::Invalid(_) => SNAPSHOT_INVALID,
            SnapshotError::Unavailable | SnapshotError::Absent => SNAPSHOT_UNAVAILABLE,
        })?;
        let assistant_id = record_id(&admitted.record);
        let existing = self.registry.binding(team_id, &assistant_id).await?;
        let identity = IconIdentity::local(&admitted.record).map_err(|_| ICON_UNAVAILABLE)?;
        let retained = self
            .registry
            .icons()
            .retain(identity, &admitted.icon)
            .map_err(|_| ICON_UNAVAILABLE)?;
        let result = self
            .apply_snapshot(team_id, existing.as_ref(), admitted.record, fresh_only)
            .await;
        retained
            .release(&self.registry.references())
            .map_err(|_| ICON_UNAVAILABLE)?;
        let mut result = result?;
        if let Some(existing) = existing {
            let previous = IconIdentity::binding(&existing).map_err(|_| ICON_UNAVAILABLE)?;
            self.registry
                .icons()
                .discard(&previous, &self.registry.references())
                .map_err(|_| ICON_UNAVAILABLE)?;
        }
        if let Value::Object(members) = &mut result {
            members.push(("provenance".to_owned(), text("local")));
            members.push(("image_id".to_owned(), text(image_id)));
            members.push(("unpublished".to_owned(), Value::Bool(true)));
        }
        Ok(result)
    }

    async fn apply_snapshot(
        &self,
        team_id: &str,
        existing: Option<&Binding>,
        record: Value,
        fresh_only: bool,
    ) -> Result<Value, Problem> {
        match existing {
            Some(_) if fresh_only => Err(LOCAL_BINDING_CONFLICT),
            Some(binding) if binding.provenance == Provenance::Published => {
                self.replace_published(team_id, binding, record).await
            }
            None => self.install_new_local(team_id, record).await,
            Some(binding) => self.apply_local(team_id, binding, record).await,
        }
    }

    async fn install_new_local(&self, team_id: &str, record: Value) -> Result<Value, Problem> {
        let team = team_id.to_owned();
        let (binding, created) = self
            .registry
            .store(move |store| store.put_local(&team, record))
            .await?
            .map_err(local_binding_problem)?;
        self.install_new(team_id, &binding, created, None).await
    }

    /// Replaces a published binding with a Local one: the published Assistant is uninstalled, then the snapshot
    /// installed, under one hold of the Team.
    async fn replace_published(
        &self,
        team_id: &str,
        previous: &Binding,
        record: Value,
    ) -> Result<Value, Problem> {
        {
            let _held = self.hold(team_id).await?;
            let current = self
                .registry
                .binding(team_id, previous.assistant_id())
                .await?;
            if current.as_ref() != Some(previous) {
                return Err(LOCAL_BINDING_CONFLICT);
            }
            self.private.end_continuation(team_id).await?;
            self.uninstall_held(team_id, previous.assistant_id(), Some(previous))
                .await?;
        }
        self.install_new_local(team_id, record).await
    }

    async fn apply_local(
        &self,
        team_id: &str,
        existing: &Binding,
        record: Value,
    ) -> Result<Value, Problem> {
        let validator: team_install::bindings::LocalRecordValidator =
            Arc::new(snapshots::record_valid);
        let candidate = from_local_record(team_id, record.clone(), &validator)
            .map_err(local_binding_problem)?;
        let _held = self.hold(team_id).await?;
        if candidate == *existing {
            return self
                .install_bound(team_id, &Registry::spec(existing)?, None)
                .await;
        }
        if existing.document.get("image_id") == record.get("image_id") {
            return Err(LOCAL_BINDING_CONFLICT);
        }
        self.update_held(team_id, existing, &candidate, record, None)
            .await
    }
}

fn local_binding_problem(error: team_install::bindings::BindingError) -> Problem {
    match binding_problem(error) {
        problem if problem == super::problem::BINDING_CONFLICT => LOCAL_BINDING_CONFLICT,
        problem => problem,
    }
}
