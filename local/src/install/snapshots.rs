//! Team-owned admission of unpublished Assistant images staged in Local Docker (`local/install/snapshots.py`,
//! ADR-0060).
//!
//! A staged snapshot is an image tagged `shimpz-local/<assistant>:staged` whose stage labels declare its identity.
//! Its files are read from a never-started, network-less container of the exact image, and it is admitted only when its
//! canonical source package, manifest, machine contract, language pack, icon, and labels all agree. Local trust is
//! unsigned self-consistency: the record binds every digest, so the started image must carry exactly these bytes.

use std::collections::HashMap;
use std::sync::Arc;

use bollard::models::{ContainerCreateBody, HostConfig, ImageInspect, SystemInfo};
use strict_json::Value;
use team_assistant::archive::{archive_bound, container_file};
use team_assistant::details::{AssistantPage, StoredInputCopy};
use team_assistant::language::{PACK_PATH, admit_pack};
use team_assistant::manifest::{
    CONTRACT_PATH, MANIFEST_PATH, MAX_CONTRACT_BYTES, MAX_MANIFEST_BYTES,
    canonical_manifest_creators, canonical_manifest_identity, parse_machine_contract,
    parse_manifest_contract, parse_manifest_creators, parse_manifest_identity,
    parse_manifest_presentation,
};
use team_core::entropy::{Entropy, token_hex};
use team_protocol::payload::{
    CHAT_LOCALES, SOURCE_DIGEST_RE, canonical_created_at, canonical_identifier,
};

use super::source_package;
use crate::docker::labels::{Space, label_map};
use crate::docker::{DockerEngine, DockerError};

/// The stage label every staged snapshot carries.
pub const LOCAL_STAGE_LABEL: &str = "org.shimpz.local.stage";
/// Its value.
pub const LOCAL_STAGE_VALUE: &str = "assistant-v3";
/// The Assistant id label.
pub const ASSISTANT_LABEL: &str = "org.shimpz.assistant.id";
/// The source package digest label.
pub const SOURCE_LABEL: &str = "org.shimpz.source.digest";
/// The version label.
pub const VERSION_LABEL: &str = "org.shimpz.assistant.version";
const NAME_LABEL: &str = "org.shimpz.assistant.name";
const SUMMARY_LABEL: &str = "org.shimpz.assistant.summary";
const CREATORS_LABEL: &str = "org.shimpz.assistant.declared-creators";
const BUILD_LABEL: &str = "org.shimpz.local.build.digest";
const ACTIONS_LABEL: &str = "org.shimpz.assistant.actions";
const INTEGRATIONS_LABEL: &str = "org.shimpz.assistant.integrations";
/// Each Assistant's current snapshot carries exactly `shimpz-local/<assistant>:staged`.
pub const LOCAL_REPOSITORY: &str = "shimpz-local";
const LOCAL_TAG: &str = "staged";
const SOURCE_PATH: &str = "/opt/shimpz/.shimpz/source.package";
const ICON_PATH: &str = "/opt/shimpz/icon.png";
const RUNTIME_USER: &str = "10001:10001";
const RUNTIME_ENTRYPOINT: [&str; 3] = [
    "/opt/shimpz/runtime/bin/python3.14",
    "-c",
    "import signal; signal.pause()",
];
/// The most staged snapshots one listing admits.
pub const MAX_CANDIDATES: usize = 50;
const PLATFORMS: [&str; 2] = ["linux/amd64", "linux/arm64"];
/// The declaration fails the current contract.
pub const DECLARATION_INVALID: &str = "declaration-invalid";
/// The image's labels or files disagree with its own declaration.
pub const IMAGE_INCONSISTENT: &str = "image-inconsistent";
/// A staged image whose stage labels fail, though it names a valid Assistant id.
pub const LABELS_INVALID: &str = "labels-invalid";

/// Why a snapshot is refused or could not be judged.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SnapshotError {
    /// The snapshot is refused for this closed reason.
    Invalid(&'static str),
    /// Docker could not answer; nothing about the image is proven.
    Unavailable,
    /// The exact image is gone.
    Absent,
}

fn invalid<T>(_: T) -> SnapshotError {
    SnapshotError::Invalid(DECLARATION_INVALID)
}

fn inconsistent() -> SnapshotError {
    SnapshotError::Invalid(IMAGE_INCONSISTENT)
}

/// One staged image's admission verdict.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Verdict {
    /// Install admission accepts it.
    Admissible,
    /// It is refused for this reason.
    Inadmissible(&'static str),
    /// Docker could not answer this time.
    Unavailable,
}

impl Verdict {
    /// The state and reason the inventory reports.
    #[must_use]
    pub const fn report(self) -> (&'static str, Option<&'static str>) {
        match self {
            Self::Admissible => ("admissible", None),
            Self::Inadmissible(reason) => ("inadmissible", Some(reason)),
            Self::Unavailable => ("unavailable", None),
        }
    }
}

/// A validly identified staged snapshot.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Candidate {
    /// The Assistant.
    pub assistant_id: String,
    /// Its version.
    pub version: String,
    /// Its name.
    pub name: String,
    /// Its summary.
    pub summary: String,
    /// Up to four self-declared Creators.
    pub declared_creators: Vec<String>,
    /// Its Action ids.
    pub actions: Vec<String>,
    /// Its Integration ids.
    pub integrations: Vec<String>,
    /// The exact image id.
    pub image_id: String,
    /// The daemon's platform.
    pub platform: String,
    /// When it was built.
    pub created_at: String,
}

/// A staged image whose identity failed: never offered; listed only under a valid Assistant id label.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Refused {
    /// The image.
    pub image_id: String,
    /// Its Assistant id label, when that label alone is valid.
    pub assistant_id: Option<String>,
}

/// Every current staged image on the daemon's platform.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Listing {
    /// The platform.
    pub platform: String,
    /// The validly identified candidates, sorted.
    pub candidates: Vec<Candidate>,
    /// The refused images, sorted.
    pub refused: Vec<Refused>,
}

/// A staged image's validated icon and its Assistant page in every interface language.
#[derive(Clone, Debug, PartialEq)]
pub struct Preview {
    /// The icon.
    pub icon: Vec<u8>,
    /// Each locale's closed details object.
    pub details: Vec<(String, Value)>,
}

impl Preview {
    /// The bytes it holds, for the cache budget.
    #[must_use]
    pub fn size(&self) -> usize {
        self.icon.len()
            + self
                .details
                .iter()
                .map(|(_, page)| team_protocol::python::json::canonical(page).len())
                .sum::<usize>()
    }

    /// One locale's page.
    #[must_use]
    pub fn page(&self, locale: &str) -> Option<&Value> {
        self.details
            .iter()
            .find(|(name, _)| name == locale)
            .map(|(_, page)| page)
    }
}

/// An admitted snapshot's closed Local record and icon.
#[derive(Clone, Debug, PartialEq)]
pub struct Admitted {
    /// The record a Local binding holds.
    pub record: Value,
    /// The icon.
    pub icon: Vec<u8>,
}

/// The tag that makes a staged image its Assistant's current snapshot.
#[must_use]
pub fn canonical_reference(assistant_id: &str) -> String {
    format!("{LOCAL_REPOSITORY}/{assistant_id}:{LOCAL_TAG}")
}

/// The daemon's platform.
///
/// # Errors
/// [`SnapshotError::Invalid`] for an unsupported architecture.
pub fn platform(info: &SystemInfo) -> Result<String, SnapshotError> {
    match info.architecture.as_deref() {
        Some("amd64" | "x86_64") => Ok("linux/amd64".to_owned()),
        Some("arm64" | "aarch64") => Ok("linux/arm64".to_owned()),
        _ => Err(invalid(())),
    }
}

fn labels(image: &ImageInspect) -> Option<&HashMap<String, String>> {
    image
        .config
        .as_ref()
        .and_then(|config| config.labels.as_ref())
}

fn capability_ids(
    value: &str,
    maximum: usize,
    required: bool,
) -> Result<Vec<String>, SnapshotError> {
    let values: Vec<String> = if value.is_empty() {
        Vec::new()
    } else {
        value.split(',').map(str::to_owned).collect()
    };
    let sorted_unique = values.windows(2).all(|pair| pair[0] < pair[1]);
    let valid = !(required && values.is_empty())
        && values.len() <= maximum
        && sorted_unique
        && values
            .iter()
            .all(|item| canonical_identifier(&Value::String(item.clone())).is_some());
    if valid { Ok(values) } else { Err(invalid(())) }
}

fn text_value(text: &str) -> Value {
    Value::String(text.to_owned())
}

/// The candidate of one inspected staged image on `platform`.
///
/// # Errors
/// [`SnapshotError::Invalid`] for any identity, runtime, label, digest, or tag that is not exactly the staged one.
pub fn candidate(image: &ImageInspect, platform: &str) -> Result<Candidate, SnapshotError> {
    let image_id = image
        .id
        .clone()
        .filter(|id| SOURCE_DIGEST_RE.is_full(id))
        .ok_or_else(|| invalid(()))?;
    let created = image.created.clone().unwrap_or_default();
    let architecture = platform.rsplit('/').next();
    if image.architecture.as_deref() != architecture
        || canonical_created_at(&text_value(&created)).is_none()
        || !runtime_holds(image)
    {
        return Err(invalid(()));
    }
    let labels = labels(image).ok_or_else(|| invalid(()))?;
    let mut candidate = labelled(labels)?;
    let digests = image.repo_digests.clone().unwrap_or_default();
    let never_pulled = digests.is_empty()
        || digests
            == [format!(
                "{LOCAL_REPOSITORY}/{}@{image_id}",
                candidate.assistant_id
            )];
    let current =
        image.repo_tags.as_deref() == Some(&[canonical_reference(&candidate.assistant_id)][..]);
    let digest_label = |name: &str| {
        labels
            .get(name)
            .is_some_and(|value| SOURCE_DIGEST_RE.is_full(value))
    };
    let staged = labels.get(LOCAL_STAGE_LABEL).map(String::as_str) == Some(LOCAL_STAGE_VALUE);
    if !never_pulled
        || !current
        || !staged
        || !digest_label(SOURCE_LABEL)
        || !digest_label(BUILD_LABEL)
    {
        return Err(invalid(()));
    }
    candidate.image_id = image_id;
    platform.clone_into(&mut candidate.platform);
    candidate.created_at = created;
    Ok(candidate)
}

/// The image runs the fixed Assistant runtime: its user and entrypoint, and no command.
fn runtime_holds(image: &ImageInspect) -> bool {
    let entrypoint: Vec<String> = RUNTIME_ENTRYPOINT
        .iter()
        .map(|item| (*item).to_owned())
        .collect();
    image.config.as_ref().is_some_and(|config| {
        config.user.as_deref() == Some(RUNTIME_USER)
            && config.entrypoint.as_ref() == Some(&entrypoint)
            && config.cmd.as_ref().is_none_or(Vec::is_empty)
    })
}

/// The identity and capabilities the stage labels declare (the image fields are filled in by the caller).
fn labelled(labels: &HashMap<String, String>) -> Result<Candidate, SnapshotError> {
    let label = |name: &str| labels.get(name).ok_or_else(|| invalid(()));
    let identity = canonical_manifest_identity(
        &text_value(label(ASSISTANT_LABEL)?),
        &text_value(label(VERSION_LABEL)?),
        &text_value(label(NAME_LABEL)?),
        &text_value(label(SUMMARY_LABEL)?),
    )
    .map_err(invalid)?;
    let creators: Vec<Value> = label(CREATORS_LABEL)?.split(',').map(text_value).collect();
    Ok(Candidate {
        assistant_id: identity.assistant_id,
        version: identity.version,
        name: identity.name,
        summary: identity.summary,
        declared_creators: canonical_manifest_creators(&Value::Array(creators), 4)
            .map_err(invalid)?,
        actions: capability_ids(label(ACTIONS_LABEL)?, 128, true)?,
        integrations: capability_ids(label(INTEGRATIONS_LABEL)?, 16, false)?,
        image_id: String::new(),
        platform: String::new(),
        created_at: String::new(),
    })
}

fn refused(image: &ImageInspect) -> Refused {
    let assistant_id = labels(image)
        .and_then(|labels| labels.get(ASSISTANT_LABEL))
        .and_then(|id| {
            team_protocol::payload::canonical_assistant_id(&text_value(id)).map(str::to_owned)
        });
    Refused {
        image_id: image.id.clone().unwrap_or_default(),
        assistant_id,
    }
}

/// Docker's view of one exact image.
async fn exact_image(
    docker: &dyn DockerEngine,
    image_id: &str,
) -> Result<ImageInspect, SnapshotError> {
    let image = docker
        .inspect_image(image_id)
        .await
        .map_err(|error| match error {
            DockerError::NotFound => SnapshotError::Absent,
            _ => SnapshotError::Unavailable,
        })?;
    if image.id.as_deref() == Some(image_id) {
        Ok(image)
    } else {
        Err(invalid(()))
    }
}

async fn daemon_platform(docker: &dyn DockerEngine) -> Result<String, SnapshotError> {
    platform(
        &docker
            .info()
            .await
            .map_err(|_| SnapshotError::Unavailable)?,
    )
}

/// Every current staged snapshot: only images tagged as their Assistant's current snapshot, never the general
/// inventory. One image whose identity fails is refused on its own; Docker failing fails the listing.
///
/// # Errors
/// [`SnapshotError::Unavailable`]; [`SnapshotError::Invalid`] for more than [`MAX_CANDIDATES`] or duplicate images.
pub async fn list(docker: &dyn DockerEngine) -> Result<Listing, SnapshotError> {
    let staged = staged_images(docker).await?;
    let platform = daemon_platform(docker).await?;
    let (mut candidates, mut refusals) = (Vec::new(), Vec::new());
    for image_id in staged {
        let image = exact_image(docker, &image_id)
            .await
            .map_err(|error| match error {
                SnapshotError::Absent => SnapshotError::Unavailable,
                other => other,
            })?;
        match candidate(&image, &platform) {
            Ok(candidate) => candidates.push(candidate),
            Err(_) => refusals.push(refused(&image)),
        }
    }
    let mut ids: Vec<&str> = candidates
        .iter()
        .map(|item| item.image_id.as_str())
        .chain(refusals.iter().map(|item| item.image_id.as_str()))
        .collect();
    let count = ids.len();
    ids.sort_unstable();
    ids.dedup();
    if ids.len() != count {
        return Err(invalid(()));
    }
    candidates.sort_by(|left, right| {
        (&left.assistant_id, &left.version, &left.image_id).cmp(&(
            &right.assistant_id,
            &right.version,
            &right.image_id,
        ))
    });
    refusals.sort_by(|left, right| left.image_id.cmp(&right.image_id));
    Ok(Listing {
        platform,
        candidates,
        refused: refusals,
    })
}

/// The ids of the stage-labelled images tagged as some Assistant's current snapshot, at most [`MAX_CANDIDATES`].
async fn staged_images(docker: &dyn DockerEngine) -> Result<Vec<String>, SnapshotError> {
    let stage = [(LOCAL_STAGE_LABEL, LOCAL_STAGE_VALUE)];
    let summaries = docker
        .list_images(&stage)
        .await
        .map_err(|_| SnapshotError::Unavailable)?;
    let current = |tag: &String| {
        tag.starts_with(&format!("{LOCAL_REPOSITORY}/")) && tag.ends_with(&format!(":{LOCAL_TAG}"))
    };
    let staged: Vec<String> = summaries
        .into_iter()
        .filter(|summary| summary.repo_tags.iter().any(current))
        .map(|summary| summary.id)
        .collect();
    if staged.len() > MAX_CANDIDATES || !staged.iter().all(|id| SOURCE_DIGEST_RE.is_full(id)) {
        return Err(invalid(()));
    }
    Ok(staged)
}

/// The files a preview reads, and the source package installing reads first.
const PREVIEW_FILES: [(&str, &str, usize); 4] = [
    (MANIFEST_PATH, "shimpz.toml", MAX_MANIFEST_BYTES),
    (CONTRACT_PATH, "shimpz.contract.json", MAX_CONTRACT_BYTES),
    (
        PACK_PATH,
        "shimpz.pack.json",
        team_assistant::language::MAX_PACK_BYTES,
    ),
    (ICON_PATH, "icon.png", team_install::icons::MAX_ICON_BYTES),
];
const SOURCE_FILE: (&str, &str, usize) = (SOURCE_PATH, "source.package", 32 * 1024 * 1024);

/// The files read from a never-started, network-less container of the exact image, which is always removed.
///
/// # Errors
/// [`SnapshotError::Unavailable`] when Docker fails or the container cannot be removed; [`IMAGE_INCONSISTENT`] for a
/// missing or malformed file.
pub async fn extract(
    docker: &dyn DockerEngine,
    space: &Space,
    entropy: &dyn Entropy,
    image_id: &str,
    paths: &[(&str, &str, usize)],
) -> Result<HashMap<String, Vec<u8>>, SnapshotError> {
    let name = format!(
        "shimpz-local-{}-snapshot-{}",
        space.prefix(),
        token_hex(entropy, 8).map_err(|_| SnapshotError::Unavailable)?
    );
    let body = ContainerCreateBody {
        image: Some(image_id.to_owned()),
        labels: Some(label_map(&space.labels("snapshot-reader", None))),
        network_disabled: Some(true),
        host_config: Some(HostConfig {
            network_mode: Some("none".to_owned()),
            ..HostConfig::default()
        }),
        ..ContainerCreateBody::default()
    };
    let container = docker
        .create_container(&name, body)
        .await
        .map_err(|_| SnapshotError::Unavailable)?;
    let mut files = HashMap::new();
    let mut failure = None;
    for (path, file, maximum) in paths {
        match read_file(docker, &container, path, file, *maximum).await {
            Ok(bytes) => {
                files.insert((*path).to_owned(), bytes);
            }
            Err(error) => {
                failure = Some(error);
                break;
            }
        }
    }
    match docker.remove_container(&container).await {
        Ok(()) | Err(DockerError::NotFound) => {}
        Err(_) => return Err(SnapshotError::Unavailable),
    }
    failure.map_or(Ok(files), Err)
}

async fn read_file(
    docker: &dyn DockerEngine,
    container: &str,
    path: &str,
    name: &str,
    maximum: usize,
) -> Result<Vec<u8>, SnapshotError> {
    let archive = docker
        .get_archive(container, path, archive_bound(maximum))
        .await
        .map_err(|error| match error {
            DockerError::NotFound => inconsistent(),
            _ => SnapshotError::Unavailable,
        })?;
    container_file(&archive.metadata, &archive.tar, name, maximum).map_err(|_| inconsistent())
}

fn file<'a>(files: &'a HashMap<String, Vec<u8>>, path: &str) -> Result<&'a [u8], SnapshotError> {
    files.get(path).map(Vec::as_slice).ok_or_else(inconsistent)
}

/// Every check a preview makes on extracted bytes: the declaration, the icon, the pages, and the labels' agreement.
fn checked_preview(
    candidate: &Candidate,
    files: &HashMap<String, Vec<u8>>,
) -> Result<Preview, SnapshotError> {
    let manifest = file(files, MANIFEST_PATH)?;
    let identity = parse_manifest_identity(manifest).map_err(invalid)?;
    let creators: Vec<String> = parse_manifest_creators(manifest)
        .map_err(invalid)?
        .into_iter()
        .take(4)
        .collect();
    let icon = file(files, ICON_PATH)?;
    source_package::validate_icon(icon).map_err(invalid)?;
    let details = pages(&identity, &creators, manifest, files)?;
    let agrees = identity.assistant_id == candidate.assistant_id
        && identity.version == candidate.version
        && identity.name == candidate.name
        && identity.summary == candidate.summary
        && creators == candidate.declared_creators;
    if !agrees {
        return Err(inconsistent());
    }
    Ok(Preview {
        icon: icon.to_vec(),
        details,
    })
}

/// The Assistant page in every interface language, each text read only from the image's own admitted pack.
fn pages(
    identity: &team_assistant::manifest::ManifestIdentity,
    creators: &[String],
    manifest: &[u8],
    files: &HashMap<String, Vec<u8>>,
) -> Result<Vec<(String, Value)>, SnapshotError> {
    let presentation = parse_manifest_presentation(manifest).map_err(invalid)?;
    let contract = parse_manifest_contract(manifest).map_err(invalid)?;
    let machine = parse_machine_contract(
        file(files, CONTRACT_PATH)?,
        &contract,
        &identity.summary,
        &presentation.description,
    )
    .map_err(invalid)?;
    let raw_pack = file(files, PACK_PATH)?;
    let pack_digest = format!(
        "sha256:{}",
        assistant_protocol::python::sha256_hex(raw_pack)
    );
    let pack = admit_pack(
        raw_pack,
        machine.get("messages").unwrap_or(&Value::Null),
        &pack_digest,
    )
    .map_err(invalid)?;
    let page = AssistantPage {
        assistant_id: identity.assistant_id.clone(),
        version: identity.version.clone(),
        name: identity.name.clone(),
        creators: creators.to_vec(),
        summary: identity.summary.clone(),
        description: presentation.description,
        links: presentation.links,
        machine_contract: machine,
        integrations: contract
            .integrations
            .iter()
            .map(|item| (item.id.clone(), item.provider.clone()))
            .collect(),
        stored_inputs: contract.stored_inputs.iter().map(copy).collect(),
    };
    let mut locales: Vec<&str> = CHAT_LOCALES.to_vec();
    locales.sort_unstable();
    locales
        .into_iter()
        .map(|locale| {
            page.localized(locale, Some(&pack))
                .map(|details| (locale.to_owned(), details))
        })
        .collect::<Result<Vec<_>, _>>()
        .map_err(invalid)
}

/// The copy of one declared Stored Input an Assistant page shows.
#[must_use]
pub fn copy(declaration: &team_assistant::manifest::StoredInputDeclaration) -> StoredInputCopy {
    StoredInputCopy {
        id: declaration.id.clone(),
        label: declaration.label.clone(),
        description: declaration.description.clone(),
        help_url: declaration.help_url.clone(),
    }
}

/// The validated preview of an exact staged image, read without starting it.
///
/// # Errors
/// As [`extract`], and the preview's refusals.
pub async fn preview(
    docker: &dyn DockerEngine,
    space: &Space,
    entropy: &dyn Entropy,
    image_id: &str,
) -> Result<Preview, SnapshotError> {
    let candidate = require_candidate(docker, image_id).await?;
    let files = extract(docker, space, entropy, image_id, &PREVIEW_FILES).await?;
    checked_preview(&candidate, &files)
}

/// One exact staged candidate, its identity validated again.
///
/// # Errors
/// [`SnapshotError`] for an invalid id, an absent image, or a refused identity.
pub async fn require_candidate(
    docker: &dyn DockerEngine,
    image_id: &str,
) -> Result<Candidate, SnapshotError> {
    if !SOURCE_DIGEST_RE.is_full(image_id) {
        return Err(invalid(()));
    }
    let image = exact_image(docker, image_id).await?;
    candidate(&image, &daemon_platform(docker).await?)
}

/// Derives the closed Local record of an exact staged image without starting it; admission also runs every preview
/// check on the same bytes.
///
/// # Errors
/// [`SnapshotError`] for any refusal or Docker failure.
pub async fn admit(
    docker: &dyn DockerEngine,
    space: &Space,
    entropy: &dyn Entropy,
    image_id: &str,
) -> Result<Admitted, SnapshotError> {
    let candidate = require_candidate(docker, image_id).await?;
    admit_candidate(docker, space, entropy, &candidate).await
}

async fn admit_candidate(
    docker: &dyn DockerEngine,
    space: &Space,
    entropy: &dyn Entropy,
    candidate: &Candidate,
) -> Result<Admitted, SnapshotError> {
    let image_id = candidate.image_id.as_str();
    let image = exact_image(docker, image_id).await?;
    let mut paths = vec![SOURCE_FILE];
    paths.extend(PREVIEW_FILES);
    let files = extract(docker, space, entropy, image_id, &paths).await?;
    let package = source_package::admit(file(&files, SOURCE_PATH)?).map_err(invalid)?;
    let source_label = labels(&image).and_then(|labels| labels.get(SOURCE_LABEL));
    if source_label != Some(&package.digest) {
        return Err(inconsistent());
    }
    if package.manifest != file(&files, MANIFEST_PATH)? || package.icon != file(&files, ICON_PATH)?
    {
        return Err(inconsistent());
    }
    let record = record(
        candidate,
        &package,
        file(&files, CONTRACT_PATH)?,
        file(&files, PACK_PATH)?,
    )?;
    checked_preview(candidate, &files)?;
    if !record_valid(&record) {
        return Err(invalid(()));
    }
    Ok(Admitted {
        record,
        icon: package.icon,
    })
}

/// The one verdict for a listed staged image: full install admission, which includes every preview check.
pub async fn verdict(
    docker: &dyn DockerEngine,
    space: &Space,
    entropy: &dyn Entropy,
    image_id: &str,
) -> Verdict {
    // An image that no longer carries the identity it was listed with says nothing about its content.
    let Ok(candidate) = require_candidate(docker, image_id).await else {
        return Verdict::Unavailable;
    };
    match admit_candidate(docker, space, entropy, &candidate).await {
        Ok(_) => Verdict::Admissible,
        Err(SnapshotError::Invalid(reason)) => Verdict::Inadmissible(reason),
        Err(SnapshotError::Unavailable | SnapshotError::Absent) => Verdict::Unavailable,
    }
}

/// A shared handle the Local install service keeps to the daemon and the Space.
#[derive(Clone)]
pub struct Stage {
    /// The daemon.
    pub docker: Arc<dyn DockerEngine>,
    /// The Space.
    pub space: Space,
    /// Fresh reader container names.
    pub entropy: Arc<dyn Entropy>,
}

impl std::fmt::Debug for Stage {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Stage")
    }
}

mod record;
#[cfg(test)]
pub(crate) mod tests;

use self::record::record;
pub use self::record::record_valid;
