//! What the install workstream proves of an Assistant container's image beyond its isolation: its manifest and
//! machine contract equal the binding's review, its Genesis is admitted, and its language pack is the reviewed one.
//! Each verdict is read once per container generation (`assistant/manifest.py` caches, `assistant/language.py`).

use std::sync::Arc;

use strict_json::Value;
use team_assistant::archive::{archive_bound, container_file};
use team_assistant::cache::{DEFAULT_ENTRIES, ReadCache, container_id};
use team_assistant::language::{
    LanguagePack, MAX_PACK_BYTES, PACK_PATH, admit_pack, require_binding,
};
use team_assistant::manifest::{
    CONTRACT_PATH, MANIFEST_PATH, MAX_CONTRACT_BYTES, MAX_MANIFEST_BYTES, parse_machine_contract,
    parse_manifest_contract, parse_manifest_genesis,
};
use team_core::future::BoxFuture;
use team_protocol::python::value::py_eq;

use super::problem::{MANIFEST_INVALID, MANIFEST_UNAVAILABLE};
use super::registry::Registry;
use super::spec::AssistantSpec;
use crate::assistant::ContainerSpec;
use crate::assistant::containers::Admission;
use crate::docker::DockerEngine;
use crate::http::service::Problem;

const PACK_ENTRIES: usize = 256;
const PACK_BYTES: usize = 32 * 1024 * 1024;

/// Manifest, Genesis, and language-pack admission of Assistant containers.
pub struct ImageAdmission {
    docker: Arc<dyn DockerEngine>,
    registry: Arc<Registry>,
    manifests: ReadCache<(), Problem>,
    genesis: ReadCache<(), Problem>,
    packs: ReadCache<LanguagePack, Problem>,
}

impl std::fmt::Debug for ImageAdmission {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("ImageAdmission")
    }
}

impl ImageAdmission {
    /// Admission over `docker`, reviewed against `registry`.
    #[must_use]
    pub fn new(docker: Arc<dyn DockerEngine>, registry: Arc<Registry>) -> Self {
        Self {
            docker,
            registry,
            manifests: ReadCache::new(DEFAULT_ENTRIES, None),
            genesis: ReadCache::new(DEFAULT_ENTRIES, None),
            packs: ReadCache::new(PACK_ENTRIES, Some(PACK_BYTES)),
        }
    }

    /// One fixed file of a container, admitted as the single read-only regular file it must be.
    async fn read(
        &self,
        container: &str,
        path: &str,
        name: &str,
        maximum: usize,
    ) -> Result<Vec<u8>, Problem> {
        let archive = self
            .docker
            .get_archive(container, path, archive_bound(maximum))
            .await
            .map_err(|_| MANIFEST_UNAVAILABLE)?;
        container_file(&archive.metadata, &archive.tar, name, maximum).map_err(|_| MANIFEST_INVALID)
    }

    /// The binding whose artifact `spec` runs.
    async fn reviewed(&self, spec: &ContainerSpec) -> Result<AssistantSpec, Problem> {
        let bindings = self
            .registry
            .snapshot()
            .await
            .map_err(|_| MANIFEST_UNAVAILABLE)?;
        bindings
            .iter()
            .filter(|binding| binding.assistant_id() == spec.assistant_id)
            .filter_map(AssistantSpec::of)
            .find(|candidate| candidate.image == spec.image)
            .ok_or(MANIFEST_INVALID)
    }

    async fn check_manifest(&self, container: &str, spec: &ContainerSpec) -> Result<(), Problem> {
        let reviewed = self.reviewed(spec).await?;
        let raw = self
            .read(container, MANIFEST_PATH, "shimpz.toml", MAX_MANIFEST_BYTES)
            .await?;
        let declared = parse_manifest_contract(&raw).map_err(|_| MANIFEST_INVALID)?;
        if reviewed.reviewed_contract().as_ref() != Some(&declared) {
            return Err(MANIFEST_INVALID);
        }
        let raw_contract = self
            .read(
                container,
                CONTRACT_PATH,
                "shimpz.contract.json",
                MAX_CONTRACT_BYTES,
            )
            .await?;
        let machine = parse_machine_contract(
            &raw_contract,
            &declared,
            &reviewed.summary,
            &reviewed.runtime.presentation.description,
        )
        .map_err(|_| MANIFEST_INVALID)?;
        if py_eq(&machine, &reviewed.runtime.machine_contract) {
            Ok(())
        } else {
            Err(MANIFEST_INVALID)
        }
    }

    /// The reviewed language pack of `spec`'s running container `container`.
    ///
    /// # Errors
    /// `assistant-manifest-unavailable` or `assistant-manifest-invalid`.
    pub async fn pack(
        &self,
        container: &str,
        spec: &AssistantSpec,
    ) -> Result<LanguagePack, Problem> {
        let key = container_id(container).ok_or(MANIFEST_INVALID)?;
        let messages = spec
            .runtime
            .machine_contract
            .get("messages")
            .cloned()
            .unwrap_or(Value::Null);
        let digest = spec.pack_digest.clone();
        let pack = self
            .packs
            .get(
                key,
                |pack| pack.size,
                || async {
                    let raw = self
                        .read(container, PACK_PATH, "shimpz.pack.json", MAX_PACK_BYTES)
                        .await?;
                    admit_pack(&raw, &messages, &digest).map_err(|_| MANIFEST_INVALID)
                },
            )
            .await?;
        require_binding(&pack, &spec.runtime.machine_contract, &spec.pack_digest)
            .map_err(|_| MANIFEST_INVALID)?;
        Ok(pack)
    }
}

impl Admission for ImageAdmission {
    fn manifest<'a>(
        &'a self,
        container: &'a str,
        spec: &'a ContainerSpec,
    ) -> BoxFuture<'a, Result<(), Problem>> {
        Box::pin(async move {
            let key = container_id(container).ok_or(MANIFEST_INVALID)?;
            self.manifests
                .get(key, |()| 1, || self.check_manifest(container, spec))
                .await
        })
    }

    fn genesis<'a>(
        &'a self,
        container: &'a str,
        _spec: &'a ContainerSpec,
    ) -> BoxFuture<'a, Result<(), Problem>> {
        Box::pin(async move {
            let key = container_id(container).ok_or(MANIFEST_INVALID)?;
            self.genesis
                .get(
                    key,
                    |()| 1,
                    || async {
                        let raw = self
                            .read(container, MANIFEST_PATH, "shimpz.toml", MAX_MANIFEST_BYTES)
                            .await?;
                        parse_manifest_genesis(&raw)
                            .map(|_| ())
                            .map_err(|_| MANIFEST_INVALID)
                    },
                )
                .await
        })
    }

    fn forget(&self, container: &str) {
        // A container never changes its image, so each verdict is its generation's.
        self.packs.discard(container);
        self.manifests.discard(container);
        self.genesis.discard(container);
    }
}
