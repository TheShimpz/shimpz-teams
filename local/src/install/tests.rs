//! The Assistants group's lifecycle by consequence (`tests/test_local_install_service.py`,
//! `tests/test_local_assistant_lifecycle.py`): publication installs, updates, uninstall, and direct invocation over the
//! in-memory daemon, with Developers, trust, Actions, and private state faked at their seams.

use std::collections::HashMap;
use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bollard::models::{ImageConfig, ImageInspect};
use strict_json::Value;
use team_action::executor::{
    ActionBatch, ActionExecutor, ActionFailure, ActionRequest, BatchScope,
};
use team_core::future::BoxFuture;
use team_core::stop::Stop;
use team_core::testing::{CountingEntropy, FakeExec, MemoryAudit};
use team_egress::policy::PolicyStore;
use team_install::trust::{ArtifactVerifier, Resolution, ResolveError, Resolver, TrustError};

use super::admission::ImageAdmission;
use super::problem::{
    CHAT_ACTIVE, HUMAN_REQUEST_REQUIRED, MANIFEST_INVALID, NOT_INSTALLABLE, UNTRUSTED,
    UPDATE_APPROVAL_REQUIRED, UPDATE_ROLLBACK_INCOMPLETE,
};
use super::registry::Registry;
use super::snapshots::{ASSISTANT_LABEL, SOURCE_LABEL, Stage};
use super::{Assistants, Parts, PrivateState};
use crate::assistant::ContainerSpec;
use crate::assistant::containers::{self, Admission, Containers};
use crate::coordination::Coordination;
use crate::coordination::executions::ChatLimit;
use crate::docker::labels::Space;
use crate::http::service::Problem;
use crate::lifecycle::network::TeamNetworks;
use crate::testing::FakeDocker;

pub(super) const TEAM: &str = "team_1";
const ASSISTANT: &str = "hello-world";
const ICON: &[u8] = b"icon bytes";

mod staged;

pub(super) fn digest(digit: char) -> String {
    format!("sha256:{}", digit.to_string().repeat(64))
}

pub(super) fn reference(digit: char) -> String {
    format!("ghcr.io/theshimpz/shimpz-assistant@{}", digest(digit))
}

fn set(document: &mut Value, key: &str, value: Value) {
    let Value::Object(members) = document else {
        panic!("an object")
    };
    members
        .iter_mut()
        .find(|(name, _)| name == key)
        .expect("a member")
        .1 = value;
}

/// The protocol's resolution fixture, with admitted closed Action schemas, as version `version` of source `source` running image `image`.
pub(super) fn publication(version: &str, source: char, image: char) -> Value {
    let path =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../protocol/install/v1/vectors.json");
    let vectors = strict_json::parse(&fs::read(path).expect("vectors")).expect("JSON");
    let mut document = vectors
        .get("fixtures")
        .and_then(|fixtures| fixtures.get("resolve_response"))
        .and_then(|fixture| fixture.get("value"))
        .expect("the fixture")
        .clone();
    let text = |value: &str| Value::String(value.to_owned());
    set(&mut document, "assistant_version", text(version));
    set(&mut document, "source_digest", text(&digest(source)));
    set(&mut document, "image_reference", text(&reference(image)));
    set(&mut document, "oci_digest", text(&digest(image)));
    let schema = strict_json::parse(
        br#"{"type": "object", "properties": {"zone": {"type": "string", "maxLength": 40}},
            "required": ["zone"], "additionalProperties": false}"#,
    )
    .expect("a schema");
    if let Some(Value::Object(contract)) = document.get("machine_contract").cloned() {
        let mut contract = Value::Object(contract);
        if let Some(Value::Array(mut actions)) = contract.get("actions").cloned() {
            set(&mut actions[0], "input_schema", schema.clone());
            set(&mut actions[0], "output_schema", schema);
            set(&mut contract, "actions", Value::Array(actions));
        }
        set(&mut document, "machine_contract", contract);
    }
    let icon = format!("sha256:{}", assistant_protocol::python::sha256_hex(ICON));
    set(&mut document, "icon_digest", text(&icon));
    document
}

/// Developers: each source digest resolves to its publication; with `drift`, every resolution after the first names
/// another OCI digest.
#[derive(Default)]
struct Developers {
    publications: Mutex<HashMap<String, Value>>,
    latest: Mutex<Option<Value>>,
    drift: AtomicBool,
    resolved: AtomicUsize,
}

impl Developers {
    fn publish(&self, document: Value) -> String {
        let Some(Value::String(source)) = document.get("source_digest").cloned() else {
            panic!("a source digest")
        };
        self.publications
            .lock()
            .expect("usable")
            .insert(source.clone(), document);
        source
    }
}

impl Resolver for Developers {
    fn resolve<'a>(&'a self, source: &'a str) -> BoxFuture<'a, Result<Resolution, ResolveError>> {
        let first = self.resolved.fetch_add(1, Ordering::SeqCst) == 0;
        let mut found = self
            .publications
            .lock()
            .expect("usable")
            .get(source)
            .cloned();
        if let Some(document) = found
            .as_mut()
            .filter(|_| !first && self.drift.load(Ordering::SeqCst))
        {
            set(document, "oci_digest", Value::String(digest('e')));
        }
        Box::pin(async move {
            found
                .map(Resolution::admitted)
                .ok_or(ResolveError::NotInstallable)
        })
    }

    fn latest<'a>(&'a self, _: &'a str) -> BoxFuture<'a, Result<Resolution, ResolveError>> {
        let latest = self.latest.lock().expect("usable").clone();
        Box::pin(async move {
            latest
                .map(Resolution::admitted)
                .ok_or(ResolveError::NotInstallable)
        })
    }

    fn icon<'a>(&'a self, _: &'a str, _: &'a str) -> BoxFuture<'a, Result<Vec<u8>, ResolveError>> {
        Box::pin(async { Ok(ICON.to_vec()) })
    }
}

#[derive(Default)]
struct Trust {
    refused: AtomicBool,
}

impl ArtifactVerifier for Trust {
    fn verify<'a>(&'a self, _: &'a Resolution) -> BoxFuture<'a, Result<(), TrustError>> {
        let refused = self.refused.load(Ordering::SeqCst);
        Box::pin(async move {
            if refused {
                Err(TrustError::Untrusted)
            } else {
                Ok(())
            }
        })
    }
}

/// Answers every direct invocation with `{"done": true}`, or the failure set.
#[derive(Default)]
struct Executor {
    failure: Mutex<Option<ActionFailure>>,
}

impl ActionExecutor for Executor {
    fn batch(&self, _: BatchScope) -> Box<dyn ActionBatch> {
        unreachable!("the direct route never opens a batch")
    }

    fn invoke<'a>(
        &'a self,
        _: &'a str,
        _: &'a ActionRequest,
        _: &'a Stop,
    ) -> BoxFuture<'a, Result<Value, ActionFailure>> {
        let failure = *self.failure.lock().expect("usable");
        Box::pin(async move {
            failure.map_or_else(
                || Ok(Value::Object(vec![("done".to_owned(), Value::Bool(true))])),
                Err,
            )
        })
    }
}

/// Records what the lifecycle asked of the private state.
#[derive(Default)]
struct Private(Mutex<Vec<String>>);

impl Private {
    fn record(&self, event: String) -> BoxFuture<'_, Result<(), Problem>> {
        self.0.lock().expect("usable").push(event);
        Box::pin(async { Ok(()) })
    }

    fn events(&self) -> Vec<String> {
        self.0.lock().expect("usable").clone()
    }
}

impl PrivateState for Private {
    fn retain_declared<'a>(
        &'a self,
        team_id: &'a str,
        spec: &'a super::spec::AssistantSpec,
    ) -> BoxFuture<'a, Result<(), Problem>> {
        self.record(format!(
            "retain {team_id} {} {}",
            spec.assistant_id, spec.version
        ))
    }

    fn delete_assistant<'a>(
        &'a self,
        team_id: &'a str,
        assistant_id: &'a str,
    ) -> BoxFuture<'a, Result<(), Problem>> {
        self.record(format!("delete {team_id} {assistant_id}"))
    }

    fn end_continuation<'a>(&'a self, team_id: &'a str) -> BoxFuture<'a, Result<(), Problem>> {
        self.record(format!("end {team_id}"))
    }
}

/// The containers' review of a running image: the manifest's own admission is team-assistant's, proved there. A
/// test may refuse exact images.
#[derive(Default)]
struct Reviewed {
    refused: std::sync::Mutex<Vec<String>>,
}

impl Reviewed {
    fn refuse(&self, images: &[String]) {
        *self.refused.lock().expect("refused") = images.to_vec();
    }
}

impl Admission for Reviewed {
    fn manifest<'a>(
        &'a self,
        _: &'a str,
        spec: &'a ContainerSpec,
    ) -> BoxFuture<'a, Result<(), Problem>> {
        let refused = self.refused.lock().expect("refused").contains(&spec.image);
        Box::pin(async move {
            if refused {
                Err(MANIFEST_INVALID)
            } else {
                Ok(())
            }
        })
    }

    fn genesis<'a>(
        &'a self,
        _: &'a str,
        _: &'a ContainerSpec,
    ) -> BoxFuture<'a, Result<(), Problem>> {
        Box::pin(async { Ok(()) })
    }

    fn forget(&self, _: &str) {}
}

struct Limit;

impl ChatLimit for Limit {
    fn current(&self) -> usize {
        2
    }

    fn save(&self, limit: u8) -> Result<u8, crate::coordination::executions::LimitUnavailable> {
        Ok(limit)
    }
}

struct Harness {
    _directory: tempfile::TempDir,
    docker: Arc<FakeDocker>,
    developers: Arc<Developers>,
    trust: Arc<Trust>,
    executor: Arc<Executor>,
    private: Arc<Private>,
    coordination: Coordination,
    registry: Arc<Registry>,
    admission: Arc<ImageAdmission>,
    reviewed: Arc<Reviewed>,
    assistants: Arc<Assistants>,
}

/// Makes the published image `image` of `source` pullable, carrying its reviewed labels.
fn release(docker: &FakeDocker, source: char, image: char) {
    let labels = HashMap::from([
        (ASSISTANT_LABEL.to_owned(), ASSISTANT.to_owned()),
        (SOURCE_LABEL.to_owned(), digest(source)),
    ]);
    let inspection = ImageInspect {
        id: Some(digest(image)),
        repo_digests: Some(vec![reference(image)]),
        config: Some(ImageConfig {
            labels: Some(labels),
            ..ImageConfig::default()
        }),
        ..ImageInspect::default()
    };
    docker.add_remote_image(&reference(image), inspection.clone());
    // The daemon also answers the pulled image by its id, as the residue sweep inspects it.
    docker.add_image(&digest(image), inspection);
}

/// A daemon from which both test publications' images, `b` and `d`, can be pulled.
fn released() -> Arc<FakeDocker> {
    let docker = Arc::new(FakeDocker::default());
    release(&docker, 'a', 'b');
    release(&docker, 'c', 'd');
    docker
}

/// The Assistant containers over `docker`, their egress policies under `directory`.
fn containers(
    directory: &std::path::Path,
    docker: &Arc<FakeDocker>,
    space: &Space,
    entropy: &Arc<CountingEntropy>,
    admission: Arc<dyn Admission>,
) -> Containers {
    let egress = directory.join("assistant-egress");
    fs::create_dir(&egress).expect("created");
    fs::set_permissions(&egress, fs::Permissions::from_mode(0o750)).expect("mode");
    let gid = fs::metadata(&egress).expect("stat").gid();
    Containers::new(containers::Parts {
        docker: docker.clone(),
        exec: Arc::new(FakeExec::default()),
        admission,
        policies: PolicyStore::new(egress, gid),
        entropy: entropy.clone(),
        space: space.clone(),
        cpuset: "0".to_owned(),
    })
    .with_ready_wait(Duration::ZERO)
}

/// The Team networks, with [`TEAM`]'s created.
async fn networks(docker: &Arc<FakeDocker>, space: &Space) -> Arc<TeamNetworks> {
    let networks = Arc::new(TeamNetworks::new(docker.clone(), space.clone()));
    networks
        .create(TEAM, "Team One")
        .await
        .expect("a Team network");
    networks
}

fn registry(directory: &std::path::Path) -> Arc<Registry> {
    let publications = directory.join("publications");
    fs::create_dir(&publications).expect("created");
    Arc::new(Registry::new(&publications))
}

async fn harness() -> Harness {
    build(false).await
}

/// A harness whose containers are admitted by the real image review: the image's manifest against its binding.
async fn reviewing_harness() -> Harness {
    build(true).await
}

async fn build(review_images: bool) -> Harness {
    let directory = tempfile::tempdir().expect("a temporary directory");
    let docker = released();
    let space = Space::new("local-space").expect("a Space");
    let entropy = Arc::new(CountingEntropy::default());
    let registry = registry(directory.path());
    let admission = Arc::new(ImageAdmission::new(docker.clone(), registry.clone()));
    let reviewed = Arc::new(Reviewed::default());
    let review: Arc<dyn Admission> = if review_images {
        admission.clone()
    } else {
        reviewed.clone()
    };
    let containers = containers(directory.path(), &docker, &space, &entropy, review);
    let networks = networks(&docker, &space).await;
    let coordination = Coordination::new(Arc::new(Limit));
    let (developers, trust) = (Arc::new(Developers::default()), Arc::new(Trust::default()));
    let (executor, private) = (Arc::new(Executor::default()), Arc::new(Private::default()));
    let assistants = Arc::new(Assistants::new(Parts {
        coordination: coordination.clone(),
        networks,
        containers: Arc::new(containers),
        registry: registry.clone(),
        admission: admission.clone(),
        resolver: developers.clone(),
        verifier: trust.clone(),
        executor: executor.clone(),
        private: private.clone(),
        stage: Stage {
            docker: docker.clone(),
            space,
            entropy,
        },
        audit: Arc::new(MemoryAudit::default()),
    }));
    Harness {
        _directory: directory,
        docker,
        developers,
        trust,
        executor,
        private,
        coordination,
        registry,
        admission,
        reviewed,
        assistants,
    }
}

impl Harness {
    async fn install(&self, document: Value) -> Result<Value, Problem> {
        let source = self.developers.publish(document);
        self.assistants
            .install_publication(TEAM, ASSISTANT, &source, None)
            .await
    }

    async fn version(&self) -> Option<String> {
        let binding = self
            .registry
            .binding(TEAM, ASSISTANT)
            .await
            .expect("readable")?;
        match binding.document.get("assistant_version") {
            Some(Value::String(version)) => Some(version.clone()),
            _ => None,
        }
    }

    fn running(&self) -> bool {
        self.docker
            .container_names()
            .iter()
            .any(|name| name.contains(ASSISTANT))
    }
}

fn member<'a>(value: &'a Value, key: &str) -> &'a Value {
    value.get(key).expect("a member")
}

#[tokio::test]
async fn a_verified_publication_is_bound_run_and_listed_once() {
    let harness = harness().await;
    let first = harness
        .install(publication("0.1.0", 'a', 'b'))
        .await
        .expect("installed");
    assert_eq!(member(&first, "installed"), &Value::Bool(true));
    assert!(harness.running());
    assert_eq!(harness.version().await.as_deref(), Some("0.1.0"));

    let again = harness
        .install(publication("0.1.0", 'a', 'b'))
        .await
        .expect("idempotent");
    assert_eq!(member(&again, "installed"), &Value::Bool(false));

    let catalog = harness.assistants.list_registry().await.expect("listed");
    let Value::Array(entries) = member(&catalog, "assistants") else {
        panic!("a list")
    };
    assert_eq!(entries.len(), 1);
    assert_eq!(
        member(&entries[0], "title"),
        &Value::String("Hello World".to_owned())
    );
    assert_eq!(
        harness
            .assistants
            .icon(TEAM, ASSISTANT)
            .await
            .expect("an icon"),
        ICON
    );
    let summary = harness
        .assistants
        .summary(TEAM, ASSISTANT, "en")
        .await
        .expect("en");
    assert_eq!(
        member(&summary, "summary"),
        &Value::String("Complete one reviewed greeting Action.".to_owned())
    );
}

#[tokio::test]
async fn an_untrusted_publication_leaves_nothing_bound_or_running() {
    let harness = harness().await;
    harness.trust.refused.store(true, Ordering::SeqCst);
    assert_eq!(
        harness.install(publication("0.1.0", 'a', 'b')).await,
        Err(UNTRUSTED)
    );
    assert_eq!(harness.version().await, None);
    assert!(!harness.running());
}

#[tokio::test]
async fn a_publication_of_another_assistant_is_refused() {
    let harness = harness().await;
    let source = harness.developers.publish(publication("0.1.0", 'a', 'b'));
    let refused = harness
        .assistants
        .install_publication(TEAM, "other", &source, None)
        .await;
    assert_eq!(refused, Err(NOT_INSTALLABLE));
}

#[tokio::test]
async fn the_start_re_resolves_and_a_drifted_digest_deletes_the_new_binding() {
    let harness = harness().await;
    harness.developers.drift.store(true, Ordering::SeqCst);
    assert_eq!(
        harness.install(publication("0.1.0", 'a', 'b')).await,
        Err(NOT_INSTALLABLE)
    );
    assert_eq!(harness.version().await, None);
}

#[tokio::test]
async fn a_lifecycle_change_during_a_chat_turn_is_refused() {
    let harness = harness().await;
    harness
        .install(publication("0.1.0", 'a', 'b'))
        .await
        .expect("installed");
    let _turn = harness
        .coordination
        .executions
        .admit(
            TEAM,
            crate::coordination::executions::ExecutionKind::Chat,
            "turn".to_owned(),
        )
        .expect("a chat turn");
    assert_eq!(
        harness.assistants.uninstall(TEAM, ASSISTANT).await,
        Err(CHAT_ACTIVE)
    );
    assert_eq!(harness.version().await.as_deref(), Some("0.1.0"));
}

#[tokio::test]
async fn uninstall_removes_the_container_binding_icon_private_state_and_image() {
    let harness = harness().await;
    harness
        .install(publication("0.1.0", 'a', 'b'))
        .await
        .expect("installed");
    let removed = harness
        .assistants
        .uninstall(TEAM, ASSISTANT)
        .await
        .expect("uninstalled");
    assert_eq!(member(&removed, "uninstalled"), &Value::Bool(true));
    assert!(!harness.running());
    assert_eq!(harness.version().await, None);
    assert!(harness.assistants.icon(TEAM, ASSISTANT).await.is_err());
    assert_eq!(harness.docker.removed_images(), [digest('b')]);
    let events = harness.private.events();
    assert!(events.contains(&format!("end {TEAM}")));
    assert!(events.contains(&format!("delete {TEAM} {ASSISTANT}")));

    let absent = harness
        .assistants
        .uninstall(TEAM, ASSISTANT)
        .await
        .expect("absent is success");
    assert_eq!(member(&absent, "uninstalled"), &Value::Bool(false));
}

#[tokio::test]
async fn a_successor_replaces_the_running_generation_and_retires_its_image() {
    let harness = harness().await;
    harness
        .install(publication("0.1.0", 'a', 'b'))
        .await
        .expect("installed");
    let updated = harness
        .install(publication("0.2.0", 'c', 'd'))
        .await
        .expect("updated");
    assert_eq!(member(&updated, "updated"), &Value::Bool(true));
    assert_eq!(harness.version().await.as_deref(), Some("0.2.0"));
    assert!(
        harness
            .private
            .events()
            .contains(&format!("retain {TEAM} {ASSISTANT} 0.2.0"))
    );
    assert_eq!(harness.docker.removed_images(), [digest('b')]);
    let transactions = harness.registry.list_updates().await.expect("readable");
    assert_eq!(transactions.len(), 0);
}

#[tokio::test]
async fn a_failed_update_keeps_its_transaction_until_its_rollback_is_proven() {
    let harness = harness().await;
    harness
        .install(publication("0.1.0", 'a', 'b'))
        .await
        .expect("installed");
    // The successor is refused and the previous generation restored: the transaction ends with the rollback.
    harness.reviewed.refuse(&[reference('d')]);
    assert!(
        harness
            .install(publication("0.2.0", 'c', 'd'))
            .await
            .is_err()
    );
    let transactions = harness.registry.list_updates().await.expect("readable");
    assert_eq!(transactions.len(), 0);
    // The previous generation cannot be restored either: the transaction stays for recovery.
    harness.reviewed.refuse(&[reference('b'), reference('d')]);
    let failed = harness.install(publication("0.2.0", 'c', 'd')).await;
    assert_eq!(failed, Err(UPDATE_ROLLBACK_INCOMPLETE));
    let transactions = harness.registry.list_updates().await.expect("readable");
    assert_eq!(transactions.len(), 1);
    assert!(!harness.running());
    harness.reviewed.refuse(&[]);
    harness.assistants.recover_updates().await;
    let transactions = harness.registry.list_updates().await.expect("readable");
    assert_eq!(transactions.len(), 0);
    assert_eq!(harness.version().await.as_deref(), Some("0.1.0"));
    assert!(harness.running());
}

#[tokio::test]
async fn a_successor_widening_egress_needs_approval_and_keeps_the_previous_generation() {
    let harness = harness().await;
    harness
        .install(publication("0.1.0", 'a', 'b'))
        .await
        .expect("installed");
    let mut widened = publication("0.2.0", 'c', 'd');
    let Some(Value::Array(mut hosts)) = widened.get("allowed_hosts").cloned() else {
        panic!("hosts")
    };
    hosts.push(Value::String("upload.example.org".to_owned()));
    set(&mut widened, "allowed_hosts", Value::Array(hosts));
    assert_eq!(
        harness.install(widened).await,
        Err(UPDATE_APPROVAL_REQUIRED)
    );
    assert_eq!(harness.version().await.as_deref(), Some("0.1.0"));
    assert_eq!(harness.docker.removed_images(), Vec::<String>::new());
}

#[tokio::test]
async fn direct_invocation_answers_the_result_and_refuses_a_human_request() {
    let harness = harness().await;
    let input = Value::Object(Vec::new());
    let answered = harness
        .assistants
        .invoke(TEAM, ASSISTANT, "hello", input.clone())
        .await
        .expect("answered");
    assert_eq!(
        member(&answered, "result"),
        &Value::Object(vec![("done".to_owned(), Value::Bool(true))])
    );
    *harness.executor.failure.lock().expect("usable") = Some(ActionFailure::HumanRequestRequired);
    assert_eq!(
        harness
            .assistants
            .invoke(TEAM, ASSISTANT, "hello", input)
            .await,
        Err(HUMAN_REQUEST_REQUIRED)
    );
}

#[tokio::test]
async fn the_automatic_updater_installs_the_latest_publication_over_the_bound_one() {
    let harness = harness().await;
    harness
        .install(publication("0.1.0", 'a', 'b'))
        .await
        .expect("installed");
    let successor = publication("0.2.0", 'c', 'd');
    harness.developers.publish(successor.clone());
    *harness.developers.latest.lock().expect("usable") = Some(successor);
    let lifecycle = super::automatic::Lifecycle {
        assistants: harness.assistants.clone(),
        collector: Arc::new(super::collector::SnapshotCollector::new(
            harness.docker.clone(),
            harness.registry.clone(),
        )),
    };
    let updates = super::automatic::AutomaticUpdates::new(
        Arc::new(lifecycle),
        Arc::new(MemoryAudit::default()),
        Arc::new(crate::http::activity::Activity::default()),
        Arc::new(CountingEntropy::default()),
    );
    assert!(updates.run_once().await);
    assert_eq!(harness.version().await.as_deref(), Some("0.2.0"));
    assert_eq!(harness.docker.removed_images(), [digest('b')]);
}
