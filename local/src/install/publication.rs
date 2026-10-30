//! Installing a Developers publication, the bound Assistant's install and update, uninstall, the startup recovery of
//! update transactions, and the retired-image residue sweep (`local/install/service.py`, `local/assistant/lifecycle.py`).

use bollard::models::ImageInspect;
use strict_json::Value;
use team_assistant::manifest::automatic_update_preserves_egress;
use team_core::future::BoxFuture;
use team_install::bindings::{Binding, Provenance, from_resolution};
use team_install::icons::IconIdentity;
use team_install::trust::{ResolveError, Resolver};

use super::problem::{
    DEVELOPERS_PROTOCOL, DEVELOPERS_UNAVAILABLE, ICON_UNAVAILABLE, IMAGE_UNAVAILABLE,
    NOT_INSTALLABLE, REPLACE_FAILED, UNTRUSTED, UPDATE_APPROVAL_REQUIRED, UPDATE_CONFLICT,
    UPDATE_ROLLBACK_INCOMPLETE,
};
use super::registry::{Registry, binding_problem};
use super::snapshots::{LOCAL_STAGE_LABEL, LOCAL_STAGE_VALUE};
use super::spec::{AssistantSpec, is_successor};
use super::{Assistants, object};
use crate::assistant::containers::{AuthorizeStart, Ensured};
use crate::assistant::problem::{INSTALL_ROLLBACK_INCOMPLETE, NOT_FOUND};
use crate::docker::DockerError;
use crate::docker::problem::INTERNAL;
use crate::http::service::Problem;

/// A resolution failure as its public problem.
#[must_use]
pub const fn resolve_problem(error: ResolveError) -> Problem {
    match error {
        ResolveError::NotInstallable => NOT_INSTALLABLE,
        ResolveError::Protocol => DEVELOPERS_PROTOCOL,
        ResolveError::Unavailable => DEVELOPERS_UNAVAILABLE,
    }
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn member<'a>(document: &'a Value, key: &str) -> &'a str {
    match document.get(key) {
        Some(Value::String(text)) => text,
        _ => "",
    }
}

/// The last authorization before a container starts: Developers still resolves the publication to the same Assistant
/// and the same OCI digest.
struct PublicationStart<'a> {
    resolver: &'a dyn Resolver,
    source_digest: &'a str,
    assistant_id: &'a str,
    oci_digest: &'a str,
}

impl AuthorizeStart for PublicationStart<'_> {
    fn authorize(&self) -> BoxFuture<'_, Result<(), Problem>> {
        Box::pin(async move {
            let current = self
                .resolver
                .resolve(self.source_digest)
                .await
                .map_err(resolve_problem)?;
            let document = current.document();
            if member(document, "assistant_id") != self.assistant_id
                || member(document, "oci_digest") != self.oci_digest
            {
                return Err(NOT_INSTALLABLE);
            }
            Ok(())
        })
    }
}

/// The two generations' specs, when the successor stays within the previous outbound-host and credential-placement
/// envelope; otherwise the update needs approval.
fn preserved(
    previous: &Binding,
    successor: &Binding,
) -> Result<(AssistantSpec, AssistantSpec), Problem> {
    let (before, after) = (Registry::spec(previous)?, Registry::spec(successor)?);
    match (before.reviewed_contract(), after.reviewed_contract()) {
        (Some(left), Some(right)) if automatic_update_preserves_egress(&left, &right) => {
            Ok((before, after))
        }
        _ => Err(UPDATE_APPROVAL_REQUIRED),
    }
}

/// One update's two generations in one Team.
#[derive(Clone, Copy)]
struct Generation<'a> {
    team_id: &'a str,
    network: &'a str,
    before: &'a AssistantSpec,
    after: &'a AssistantSpec,
    previous: &'a Binding,
}

fn installed(assistant_id: &str, created: bool) -> Value {
    object(vec![
        ("assistant", text(assistant_id)),
        ("installed", Value::Bool(created)),
    ])
}

impl Assistants {
    /// Resolves, verifies, binds, and runs one exact publication; a bound older one is updated to it.
    ///
    /// # Errors
    /// The resolution's, trust's, registry's, icon's, and lifecycle's problems.
    pub async fn install_publication(
        &self,
        team_id: &str,
        assistant_id: &str,
        source_digest: &str,
        expected_binding_digest: Option<&str>,
    ) -> Result<Value, Problem> {
        if team_protocol::payload::canonical_assistant_id(&text(assistant_id)).is_none() {
            return Err(super::problem::INVALID_ASSISTANT_ID);
        }
        let existing = self.registry.binding(team_id, assistant_id).await?;
        if expected_binding_digest.is_some_and(|expected| {
            existing
                .as_ref()
                .is_none_or(|binding| binding.binding_digest != expected)
        }) {
            return Err(UPDATE_CONFLICT);
        }
        let (document, icon) = self
            .verified_publication(assistant_id, source_digest)
            .await?;
        let identity = IconIdentity::publication(&document).map_err(|_| ICON_UNAVAILABLE)?;
        let retained = self
            .registry
            .icons()
            .retain(identity, &icon)
            .map_err(|_| ICON_UNAVAILABLE)?;
        let oci_digest = member(&document, "oci_digest").to_owned();
        let start = PublicationStart {
            resolver: self.resolver.as_ref(),
            source_digest,
            assistant_id,
            oci_digest: &oci_digest,
        };
        let result = self
            .apply_publication(team_id, existing.as_ref(), document, &start)
            .await;
        retained
            .release(&self.registry.references())
            .map_err(|_| ICON_UNAVAILABLE)?;
        let result = result?;
        if let Some(existing) = existing {
            let previous =
                IconIdentity::publication(&existing.document).map_err(|_| ICON_UNAVAILABLE)?;
            self.registry
                .icons()
                .discard(&previous, &self.registry.references())
                .map_err(|_| ICON_UNAVAILABLE)?;
        }
        Ok(result)
    }

    /// The resolution of `source_digest` for `assistant_id` with its proved artifact and its verified icon.
    async fn verified_publication(
        &self,
        assistant_id: &str,
        source_digest: &str,
    ) -> Result<(Value, Vec<u8>), Problem> {
        let resolution = self
            .resolver
            .resolve(source_digest)
            .await
            .map_err(resolve_problem)?;
        let document = resolution.document().clone();
        if member(&document, "assistant_id") != assistant_id {
            return Err(NOT_INSTALLABLE);
        }
        let icon_digest = member(&document, "icon_digest");
        let (trust, icon) = tokio::join!(
            self.verifier.verify(&resolution),
            self.resolver.icon(source_digest, icon_digest)
        );
        trust.map_err(|_| UNTRUSTED)?;
        Ok((document, icon.map_err(resolve_problem)?))
    }

    async fn apply_publication(
        &self,
        team_id: &str,
        existing: Option<&Binding>,
        document: Value,
        start: &PublicationStart<'_>,
    ) -> Result<Value, Problem> {
        let Some(existing) = existing else {
            let team = team_id.to_owned();
            let (binding, created) = self
                .registry
                .store(move |store| store.put(&team, document))
                .await?
                .map_err(binding_problem)?;
            return self
                .install_new(team_id, &binding, created, Some(start))
                .await;
        };
        let candidate = from_resolution(team_id, document.clone()).map_err(binding_problem)?;
        if candidate == *existing {
            let _held = self.hold(team_id).await?;
            return self
                .install_bound(team_id, &Registry::spec(existing)?, Some(start))
                .await;
        }
        if !is_successor(existing, &candidate) {
            return Err(NOT_INSTALLABLE);
        }
        let _held = self.hold(team_id).await?;
        self.update_held(team_id, existing, &candidate, document, Some(start))
            .await
    }

    /// Runs a binding this install may just have created; a failure deletes exactly that binding, unless the failed
    /// install could not remove what it created.
    pub(crate) async fn install_new(
        &self,
        team_id: &str,
        binding: &Binding,
        created: bool,
        start: Option<&dyn AuthorizeStart>,
    ) -> Result<Value, Problem> {
        let result = match self.hold(team_id).await {
            Ok(_held) => {
                self.install_bound(team_id, &Registry::spec(binding)?, start)
                    .await
            }
            Err(problem) => Err(problem),
        };
        if let Err(problem) = &result
            && created
            && *problem != INSTALL_ROLLBACK_INCOMPLETE
        {
            let (team, assistant, digest) = (
                team_id.to_owned(),
                binding.assistant_id().to_owned(),
                binding.binding_digest.clone(),
            );
            self.registry
                .store(move |store| store.delete_if_matches(&team, &assistant, &digest))
                .await?
                .map_err(|_| INTERNAL)?;
        }
        result
    }

    /// Makes the bound Assistant's container exist, admitted, running, and ready. The Team is held.
    pub(crate) async fn install_bound(
        &self,
        team_id: &str,
        spec: &AssistantSpec,
        start: Option<&dyn AuthorizeStart>,
    ) -> Result<Value, Problem> {
        let network = self.networks.require(team_id).await?;
        let ensured = self
            .containers
            .ensure(team_id, &network.name, &spec.container_spec(), start)
            .await?;
        if ensured == Ensured::Replaced {
            self.private.retain_declared(team_id, spec).await?;
        }
        Ok(installed(&spec.assistant_id, ensured == Ensured::Created))
    }

    /// Replaces `previous` with `successor` in one durable transaction: the previous generation is restored on any
    /// failure, and the binding changes only once the successor runs. The Team is held.
    pub(crate) async fn update_held(
        &self,
        team_id: &str,
        previous: &Binding,
        successor: &Binding,
        successor_document: Value,
        start: Option<&dyn AuthorizeStart>,
    ) -> Result<Value, Problem> {
        let (before, after) = preserved(previous, successor)?;
        let network = self.networks.require(team_id).await?;
        let previous_image = self.current_image(team_id, &network.name, &before).await?;
        let (record, image) = (previous.clone(), previous_image.clone());
        let update = self
            .registry
            .updates(move |updates| updates.begin(&record, successor_document, &image))
            .await?
            .map_err(binding_problem)?;
        let generation = Generation {
            team_id,
            network: &network.name,
            before: &before,
            after: &after,
            previous,
        };
        if let Err(problem) = self.switch_generation(&generation, successor, start).await {
            self.clear_update(update).await;
            return Err(problem);
        }
        self.settle(team_id, &after, previous, &previous_image, update)
            .await?;
        Ok(object(vec![
            ("assistant", text(&after.assistant_id)),
            ("installed", Value::Bool(false)),
            ("updated", Value::Bool(true)),
        ]))
    }

    /// Keeps the successor's declared private state, queues the previous published image, and ends the transaction.
    async fn settle(
        &self,
        team_id: &str,
        after: &AssistantSpec,
        previous: &Binding,
        previous_image: &str,
        update: team_install::update::AssistantUpdate,
    ) -> Result<(), Problem> {
        self.private.retain_declared(team_id, after).await?;
        if previous.provenance == Provenance::Published {
            self.queue_residue(previous_image).await;
        }
        self.clear_update(update).await;
        self.sweep_residues().await;
        Ok(())
    }

    /// Replaces the running generation and commits the binding; on any failure the previous generation is restored.
    async fn switch_generation(
        &self,
        generation: &Generation<'_>,
        successor: &Binding,
        start: Option<&dyn AuthorizeStart>,
    ) -> Result<(), Problem> {
        if let Err(problem) = self.replace_generation(generation, start).await {
            self.restore(generation).await?;
            return Err(if problem == INSTALL_ROLLBACK_INCOMPLETE {
                REPLACE_FAILED
            } else {
                problem
            });
        }
        let Generation {
            team_id,
            network,
            after,
            previous,
            ..
        } = *generation;
        if self
            .commit_successor(team_id, previous, successor)
            .await?
            .is_err()
        {
            self.containers
                .uninstall(
                    team_id,
                    network,
                    &after.assistant_id,
                    Some(&after.container_spec()),
                )
                .await?;
            self.restore(generation).await?;
            return Err(UPDATE_CONFLICT);
        }
        Ok(())
    }

    /// The image the admitted current container of `before` runs.
    async fn current_image(
        &self,
        team_id: &str,
        network: &str,
        before: &AssistantSpec,
    ) -> Result<String, Problem> {
        let spec = before.container_spec();
        let existing = self
            .containers
            .find(team_id, &before.assistant_id)
            .await?
            .ok_or(NOT_FOUND)?;
        let admitted = self
            .containers
            .admit_profile(&existing, team_id, network, &spec)?;
        self.containers.admit_egress(&admitted, team_id, &spec)?;
        admitted.image_id.ok_or(IMAGE_UNAVAILABLE)
    }

    /// Removes the previous generation's container and policy, then creates the successor's.
    async fn replace_generation(
        &self,
        generation: &Generation<'_>,
        start: Option<&dyn AuthorizeStart>,
    ) -> Result<(), Problem> {
        let Generation {
            team_id,
            network,
            before,
            after,
            ..
        } = *generation;
        self.containers
            .uninstall(
                team_id,
                network,
                &before.assistant_id,
                Some(&before.container_spec()),
            )
            .await?;
        self.containers
            .ensure(team_id, network, &after.container_spec(), start)
            .await
            .map(|_| ())
    }

    /// Replaces the binding, only while it is still `previous`.
    async fn commit_successor(
        &self,
        team_id: &str,
        previous: &Binding,
        successor: &Binding,
    ) -> Result<Result<Binding, team_install::bindings::BindingError>, Problem> {
        let (team, expected, document) = (
            team_id.to_owned(),
            previous.binding_digest.clone(),
            successor.document.clone(),
        );
        let provenance = previous.provenance;
        self.registry
            .store(move |store| match provenance {
                Provenance::Published => store.replace(&team, &expected, document),
                Provenance::Local => store.replace_local(&team, &expected, document),
            })
            .await
    }

    /// Recreates the previous generation after a failed update, and queues the successor's pulled image.
    async fn restore(&self, generation: &Generation<'_>) -> Result<(), Problem> {
        let Generation {
            team_id,
            network,
            before,
            after,
            previous,
        } = *generation;
        self.containers
            .ensure(team_id, network, &before.container_spec(), None)
            .await
            .map_err(|_| UPDATE_ROLLBACK_INCOMPLETE)?;
        if previous.provenance == Provenance::Published
            && let Some(image) = self.image_id(&after.image).await
        {
            self.queue_residue(&image).await;
        }
        Ok(())
    }

    async fn image_id(&self, reference: &str) -> Option<String> {
        self.stage
            .docker
            .inspect_image(reference)
            .await
            .ok()
            .and_then(|image| image.id)
    }

    async fn clear_update(&self, update: team_install::update::AssistantUpdate) {
        let _ = self
            .registry
            .updates(move |updates| updates.clear(&update))
            .await;
    }

    /// Confirms an Assistant absent from the Team: its container and policy, its private state, its binding and
    /// unreferenced icon. An Assistant already absent is success.
    ///
    /// # Errors
    /// `assistant-registry-drift` for a container without its binding, and Docker's, the stores', and the icon's.
    pub async fn uninstall(&self, team_id: &str, assistant_id: &str) -> Result<Value, Problem> {
        let binding = self.registry.binding(team_id, assistant_id).await?;
        let _held = self.hold(team_id).await?;
        self.private.end_continuation(team_id).await?;
        let removed = self
            .uninstall_held(team_id, assistant_id, binding.as_ref())
            .await?;
        Ok(object(vec![
            ("assistant", text(assistant_id)),
            ("uninstalled", Value::Bool(removed)),
        ]))
    }

    pub(crate) async fn uninstall_held(
        &self,
        team_id: &str,
        assistant_id: &str,
        binding: Option<&Binding>,
    ) -> Result<bool, Problem> {
        let network = self.networks.require(team_id).await?;
        let spec = binding
            .map(Registry::spec)
            .transpose()?
            .map(|spec| spec.container_spec());
        let removed = self
            .containers
            .uninstall(team_id, &network.name, assistant_id, spec.as_ref())
            .await?;
        if let Some(image) = removed
            .as_ref()
            .and_then(|removed| removed.image_id.clone())
            && binding.is_none_or(|binding| binding.provenance == Provenance::Published)
        {
            self.queue_residue(&image).await;
        }
        self.private.delete_assistant(team_id, assistant_id).await?;
        self.retire(team_id, assistant_id, binding).await?;
        self.sweep_residues().await;
        Ok(removed.is_some())
    }

    /// Retires the binding and its unreferenced icon together; a failed icon removal keeps the binding to retry.
    pub(crate) async fn retire(
        &self,
        team_id: &str,
        assistant_id: &str,
        binding: Option<&Binding>,
    ) -> Result<(), Problem> {
        let (team, assistant) = (team_id.to_owned(), assistant_id.to_owned());
        let Some(binding) = binding else {
            return self
                .registry
                .store(move |store| store.delete(&team, &assistant).map(|_| ()))
                .await?
                .map_err(|_| INTERNAL);
        };
        let (registry, retiring) = (std::sync::Arc::clone(&self.registry), binding.clone());
        tokio::task::spawn_blocking(move || registry.retire(&retiring))
            .await
            .map_err(|_| INTERNAL)?
            .map_err(|_| ICON_UNAVAILABLE)
    }

    /// Queues one retired image; when it cannot be queued, removes it at once.
    pub(crate) async fn queue_residue(&self, image_id: &str) {
        let image = image_id.to_owned();
        let queued = self
            .registry
            .residues(move |residues| residues.add(&image))
            .await;
        if !matches!(queued, Ok(Ok(()))) {
            let _ = self.remove_retired(image_id).await;
        }
    }

    /// Removes every queued retired image no binding uses; one still in use stays queued.
    pub async fn sweep_residues(&self) {
        let Ok(Ok(images)) = self
            .registry
            .residues(team_install::update::ResidueStore::list)
            .await
        else {
            return;
        };
        for image in images {
            if self.remove_retired(&image).await {
                let _ = self
                    .registry
                    .residues(move |residues| residues.clear(&image))
                    .await;
            }
        }
    }

    /// Whether `image_id` is gone or was removed: never an image a binding resolves to or a staged Local snapshot.
    async fn remove_retired(&self, image_id: &str) -> bool {
        let Ok(bindings) = self.registry.snapshot().await else {
            return false;
        };
        for spec in bindings.iter().filter_map(AssistantSpec::of) {
            match self.stage.docker.inspect_image(&spec.image).await {
                Ok(image) if image.id.as_deref() == Some(image_id) => return false,
                Ok(_) | Err(DockerError::NotFound) => {}
                Err(_) => return false,
            }
        }
        match self.stage.docker.inspect_image(image_id).await {
            Err(DockerError::NotFound) => return true,
            Err(_) => return false,
            Ok(image) if staged(&image) => return true,
            Ok(_) => {}
        }
        matches!(
            self.stage.docker.remove_image(image_id).await,
            Ok(()) | Err(DockerError::NotFound)
        )
    }

    /// Finishes every update transaction a crash interrupted, toward the binding that holds: the previous generation
    /// or the successor. One that cannot be finished stays for the next start.
    pub async fn recover_updates(&self) {
        let Ok(updates) = self.registry.list_updates().await else {
            return;
        };
        for update in updates {
            let Ok(_held) = self.hold(&update.team_id).await else {
                continue;
            };
            if self.recover(&update).await.is_ok() {
                self.clear_update(update).await;
            }
        }
        self.sweep_residues().await;
    }

    async fn recover(&self, update: &team_install::update::AssistantUpdate) -> Result<(), Problem> {
        let binding = self
            .registry
            .binding(&update.team_id, &update.assistant_id)
            .await?
            .ok_or(UPDATE_CONFLICT)?;
        let committed = binding == update.successor;
        if !committed && binding != update.previous {
            return Err(UPDATE_CONFLICT);
        }
        let (previous, successor) = (
            Registry::spec(&update.previous)?,
            Registry::spec(&update.successor)?,
        );
        let (target, other) = if committed {
            (&successor, &previous)
        } else {
            (&previous, &successor)
        };
        self.converge(&update.team_id, target, other).await?;
        if committed {
            self.private
                .retain_declared(&update.team_id, &successor)
                .await?;
            if update.previous.provenance == Provenance::Published {
                self.queue_residue(&update.previous_image_id).await;
            }
        }
        Ok(())
    }

    /// Makes `target`'s generation run, removing a container of the `other` generation first.
    async fn converge(
        &self,
        team_id: &str,
        target: &AssistantSpec,
        other: &AssistantSpec,
    ) -> Result<(), Problem> {
        let network = self.networks.require(team_id).await?;
        let existing = self.containers.find(team_id, &target.assistant_id).await?;
        let image = existing
            .as_ref()
            .and_then(|existing| existing.config.as_ref())
            .and_then(|config| config.image.clone());
        if image.as_deref() == Some(other.image.as_str()) {
            self.containers
                .uninstall(
                    team_id,
                    &network.name,
                    &other.assistant_id,
                    Some(&other.container_spec()),
                )
                .await?;
        }
        self.containers
            .ensure(team_id, &network.name, &target.container_spec(), None)
            .await
            .map(|_| ())
    }

    /// Makes every bound Assistant run again after a start; one that cannot is left for its next use.
    pub async fn resume(&self) {
        let Ok(bindings) = self.registry.snapshot().await else {
            return;
        };
        for binding in bindings {
            let Ok(spec) = Registry::spec(&binding) else {
                continue;
            };
            if let Ok(_held) = self.hold(&binding.team_id).await {
                let _ = self.install_bound(&binding.team_id, &spec, None).await;
            }
        }
    }
}

fn staged(image: &ImageInspect) -> bool {
    image
        .config
        .as_ref()
        .and_then(|config| config.labels.as_ref())
        .and_then(|labels| labels.get(LOCAL_STAGE_LABEL))
        .is_some_and(|stage| stage == LOCAL_STAGE_VALUE)
}
