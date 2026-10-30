//! The runtime shape of one binding (`local/install/registry.py`, `runtime.py`): its reviewed runtime contract, its
//! image identity, and the [`ContainerSpec`] its container is created and admitted with.

use strict_json::Value;
use team_assistant::manifest::{ManifestContract, reviewed_manifest_contract};
use team_assistant::spec::{RuntimeContract, runtime_contract};
use team_install::bindings::{Binding, Provenance};

use super::snapshots::{
    ASSISTANT_LABEL, LOCAL_STAGE_LABEL, LOCAL_STAGE_VALUE, SOURCE_LABEL, VERSION_LABEL,
    record_valid,
};
use crate::assistant::{self, ContainerSpec};

/// One binding projected for running it.
#[derive(Clone, Debug, PartialEq)]
pub struct AssistantSpec {
    /// The Assistant.
    pub assistant_id: String,
    /// Its version.
    pub version: String,
    /// Its name.
    pub name: String,
    /// Its catalog summary.
    pub summary: String,
    /// The reviewed image: a digest reference (published) or an image id (local).
    pub image: String,
    /// The labels the image must carry.
    pub required_image_labels: Vec<(String, String)>,
    /// The reviewed runtime contract.
    pub runtime: RuntimeContract,
    /// The reviewed language pack's digest.
    pub pack_digest: String,
    /// Where it came from.
    pub provenance: Provenance,
    /// A Local snapshot's platform.
    pub platform: Option<String>,
}

fn text(document: &Value, key: &str) -> Option<String> {
    match document.get(key) {
        Some(Value::String(text)) => Some(text.clone()),
        _ => None,
    }
}

impl AssistantSpec {
    /// The binding's projection, or `None` when its document holds no valid runtime contract.
    #[must_use]
    pub fn of(binding: &Binding) -> Option<Self> {
        let document = &binding.document;
        let runtime = runtime_contract(document).ok()?;
        let assistant_id = binding.assistant_id().to_owned();
        let source = text(document, "source_digest")?;
        let version = text(document, "assistant_version")?;
        let (image, required_image_labels, platform) = match binding.provenance {
            Provenance::Published => (
                text(document, "image_reference")?,
                vec![
                    (ASSISTANT_LABEL.to_owned(), assistant_id.clone()),
                    (SOURCE_LABEL.to_owned(), source),
                ],
                None,
            ),
            Provenance::Local if record_valid(document) => (
                text(document, "image_id")?,
                vec![
                    (LOCAL_STAGE_LABEL.to_owned(), LOCAL_STAGE_VALUE.to_owned()),
                    (ASSISTANT_LABEL.to_owned(), assistant_id.clone()),
                    (SOURCE_LABEL.to_owned(), source),
                    (VERSION_LABEL.to_owned(), version.clone()),
                ],
                Some(text(document, "platform")?),
            ),
            Provenance::Local => return None,
        };
        Some(Self {
            assistant_id,
            name: text(document, "name")?,
            summary: text(document, "summary")?,
            pack_digest: text(document, "pack_digest")?,
            version,
            image,
            required_image_labels,
            runtime,
            provenance: binding.provenance,
            platform,
        })
    }

    /// What its container is created and admitted with.
    #[must_use]
    pub fn container_spec(&self) -> ContainerSpec {
        ContainerSpec {
            assistant_id: self.assistant_id.clone(),
            image: self.image.clone(),
            provenance: match self.provenance {
                Provenance::Published => assistant::Provenance::Published,
                Provenance::Local => assistant::Provenance::Local,
            },
            allowed_hosts: self.runtime.allowed_hosts.clone(),
            required_image_labels: self.required_image_labels.clone(),
            platform: self.platform.clone(),
        }
    }

    /// The reviewed security contract its image's manifest must declare exactly.
    #[must_use]
    pub fn reviewed_contract(&self) -> Option<ManifestContract> {
        let hosts = Value::Array(
            self.runtime
                .allowed_hosts
                .iter()
                .cloned()
                .map(Value::String)
                .collect(),
        );
        reviewed_manifest_contract(
            &hosts,
            &self.runtime.integrations,
            &self.runtime.stored_inputs,
        )
        .ok()
    }

    /// The version as a comparable `(major, minor, patch)`.
    #[must_use]
    pub fn version_triple(&self) -> Option<(u64, u64, u64)> {
        version_triple(&self.version)
    }
}

/// A `major.minor.patch` version as a comparable triple.
#[must_use]
pub fn version_triple(version: &str) -> Option<(u64, u64, u64)> {
    let mut parts = version.split('.').map(|part| part.parse::<u64>().ok());
    match (parts.next(), parts.next(), parts.next(), parts.next()) {
        (Some(Some(major)), Some(Some(minor)), Some(Some(patch)), None) => {
            Some((major, minor, patch))
        }
        _ => None,
    }
}

/// Whether `candidate` is a newer publication of the same Assistant than `current`.
#[must_use]
pub fn is_successor(current: &Binding, candidate: &Binding) -> bool {
    let version = |binding: &Binding| {
        text(&binding.document, "assistant_version")
            .as_deref()
            .and_then(version_triple)
    };
    current.provenance == Provenance::Published
        && candidate.provenance == Provenance::Published
        && current.assistant_id() == candidate.assistant_id()
        && matches!((version(current), version(candidate)), (Some(current), Some(candidate)) if candidate > current)
}
