use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bollard::models::{ContainerStateStatusEnum, ImageConfig, ImageInspect};
use team_core::future::BoxFuture;
use team_core::testing::{CountingEntropy, FakeExec};
use team_egress::policy::PolicyStore;

use super::{Admission, AuthorizeStart, Containers, Ensured, Parts};
use crate::assistant::isolation::Placement;
use crate::assistant::{Bindings, BindingsUnavailable, ContainerSpec, Provenance, isolation};
use crate::docker::labels::{ASSISTANT, SPACE, Space};
use crate::docker::recorded::{Events, Fault, Recorded};
use crate::docker::{DockerEngine, DockerError};
use crate::http::service::Problem;
use crate::testing::FakeDocker;

const TEAM: &str = "team_1";
const NETWORK: &str = "net";

fn reference(digit: char) -> String {
    format!(
        "ghcr.io/theshimpz/shimpz-assistant@sha256:{}",
        digit.to_string().repeat(64)
    )
}

fn image_id(digit: char) -> String {
    format!("sha256:{}", digit.to_string().repeat(64))
}

fn spec(digit: char) -> ContainerSpec {
    ContainerSpec {
        assistant_id: "notes".to_owned(),
        image: reference(digit),
        provenance: Provenance::Published,
        allowed_hosts: vec!["api.example.com".to_owned()],
        required_image_labels: Vec::new(),
        platform: None,
    }
}

/// Manifest and Genesis admission that log into the shared events and refuse when told to.
#[derive(Default)]
struct FakeAdmission {
    events: Events,
    manifest: Mutex<Option<Problem>>,
}

impl Admission for FakeAdmission {
    fn manifest<'a>(
        &'a self,
        _: &'a str,
        _: &'a ContainerSpec,
    ) -> BoxFuture<'a, Result<(), Problem>> {
        self.events.push("manifest");
        let refusal = self.manifest.lock().expect("usable").clone();
        Box::pin(async move { refusal.map_or(Ok(()), Err) })
    }

    fn genesis<'a>(
        &'a self,
        _: &'a str,
        _: &'a ContainerSpec,
    ) -> BoxFuture<'a, Result<(), Problem>> {
        self.events.push("genesis");
        Box::pin(async { Ok(()) })
    }

    fn forget(&self, _: &str) {
        self.events.push("forget");
    }
}

struct Authorized(Events);

impl AuthorizeStart for Authorized {
    fn authorize(&self) -> BoxFuture<'_, Result<(), Problem>> {
        self.0.push("authorize");
        Box::pin(async { Ok(()) })
    }
}

struct Harness {
    _directory: tempfile::TempDir,
    events: Events,
    docker: Arc<Recorded<FakeDocker>>,
    exec: Arc<FakeExec>,
    admission: Arc<FakeAdmission>,
    policies: PolicyStore,
    space: Space,
    containers: Containers,
}

const MANIFEST_INVALID: Problem = Problem::new(
    409,
    "installed Assistant manifest failed its reviewed contract",
    "assistant-manifest-invalid",
);

/// A daemon from which both releases can be pulled.
fn registry() -> FakeDocker {
    let fake = FakeDocker::default();
    for digit in ['a', 'b'] {
        fake.add_remote_image(
            &reference(digit),
            ImageInspect {
                id: Some(image_id(digit)),
                repo_digests: Some(vec![reference(digit)]),
                config: Some(ImageConfig::default()),
                ..ImageInspect::default()
            },
        );
    }
    fake
}

fn harness() -> Harness {
    let directory = tempfile::tempdir().expect("a temporary directory");
    let root = directory.path().join("assistant-egress");
    fs::create_dir(&root).expect("created");
    fs::set_permissions(&root, fs::Permissions::from_mode(0o750)).expect("mode");
    let gid = fs::metadata(&root).expect("stat").gid();
    let policies = PolicyStore::new(root, gid);
    let events = Events::default();
    let docker = Arc::new(Recorded::new(registry(), events.clone()));
    let exec = Arc::new(FakeExec::default());
    let admission = Arc::new(FakeAdmission {
        events: events.clone(),
        ..FakeAdmission::default()
    });
    let space = Space::new("local-space").expect("a Space");
    let containers = Containers::new(Parts {
        docker: docker.clone(),
        exec: exec.clone(),
        admission: admission.clone(),
        policies: policies.clone(),
        entropy: Arc::new(CountingEntropy::default()),
        space: space.clone(),
        cpuset: "0".to_owned(),
    })
    .with_ready_wait(Duration::ZERO);
    let probe_store = policies.clone();
    docker.probe_on_create(Box::new(move || {
        let identity = format!("local-space\0{TEAM}\0notes");
        let held = probe_store
            .admitted(&identity, &CountingEntropy::default())
            .ok()
            .flatten()
            .is_some();
        if held {
            "with-policy"
        } else {
            "without-policy"
        }
        .to_owned()
    }));
    Harness {
        _directory: directory,
        events,
        docker,
        exec,
        admission,
        policies,
        space,
        containers,
    }
}

impl Harness {
    fn name(&self) -> String {
        self.space.container_name(TEAM, "notes")
    }

    fn has_policy(&self) -> bool {
        self.containers.has_policy(TEAM, "notes").expect("readable")
    }

    fn exists(&self) -> bool {
        self.docker.inner().container(&self.name()).is_some()
    }

    /// Creates `spec`'s container as an earlier install left it: admitted, stopped, its policy written.
    async fn installed(&self, spec: &ContainerSpec) {
        assert_eq!(
            self.containers.ensure(TEAM, NETWORK, spec, None).await,
            Ok(Ensured::Created)
        );
        self.events.clear();
    }

    fn code(result: Result<impl std::fmt::Debug, Problem>) -> Option<&'static str> {
        result.err().and_then(|problem| problem.code)
    }
}

#[tokio::test]
async fn policy_written_before_container_create() {
    let harness = harness();
    let authorize = Authorized(harness.events.clone());
    assert_eq!(
        harness
            .containers
            .ensure(TEAM, NETWORK, &spec('b'), Some(&authorize))
            .await,
        Ok(Ensured::Created)
    );
    let events = harness.events.all();
    // The route exists before the container; the image's manifest and the start authorization come before start; the
    // complete profile is admitted after it, and the Genesis only once it runs.
    assert!(
        events.contains(&"create with-policy".to_owned()),
        "{events:?}"
    );
    assert!(harness.events.before("create with-policy", "manifest"));
    assert!(harness.events.before("manifest", "authorize"));
    assert!(harness.events.before("authorize", "start"));
    assert!(harness.events.before("start", "genesis"));
    assert_eq!(harness.docker.inner().pulled(), [reference('b')]);
    assert!(harness.has_policy());
    let inspection = harness
        .containers
        .find(TEAM, "notes")
        .await
        .expect("found")
        .expect("present");
    let admitted = harness
        .containers
        .admit(&inspection, TEAM, NETWORK, &spec('b'), true)
        .await
        .expect("admitted");
    assert!(admitted.running);
    assert_eq!(admitted.image_id, Some(image_id('b')));
}

#[tokio::test]
async fn manifest_mismatch_removes_the_unstarted_container_and_its_egress() {
    let harness = harness();
    *harness.admission.manifest.lock().expect("usable") = Some(MANIFEST_INVALID);
    let result = harness
        .containers
        .ensure(TEAM, NETWORK, &spec('b'), None)
        .await;
    assert_eq!(Harness::code(result), Some("assistant-manifest-invalid"));
    assert!(!harness.events.all().contains(&"start".to_owned()));
    assert!(harness.events.before("manifest", "remove id"));
    assert!(!harness.exists());
    assert!(!harness.has_policy());
}

#[tokio::test]
async fn a_failed_install_removal_fail_stops_and_keeps_the_route_for_the_next_install() {
    let harness = harness();
    *harness.admission.manifest.lock().expect("usable") = Some(MANIFEST_INVALID);
    harness
        .docker
        .fail("remove_container", Fault::Before(DockerError::Unavailable));
    let result = harness
        .containers
        .ensure(TEAM, NETWORK, &spec('b'), None)
        .await;
    assert_eq!(
        Harness::code(result),
        Some("assistant-install-rollback-incomplete")
    );
    assert_eq!(harness.exec.stopped().len(), 1);
    assert!(harness.exists());
    assert!(harness.has_policy());
}

#[tokio::test]
async fn a_create_whose_answer_was_lost_is_adopted_by_the_next_install() {
    let harness = harness();
    harness
        .docker
        .fail("create_container", Fault::After(DockerError::Unavailable));
    let result = harness
        .containers
        .ensure(TEAM, NETWORK, &spec('b'), None)
        .await;
    assert_eq!(
        Harness::code(result),
        Some("assistant-install-rollback-incomplete")
    );
    // Docker may still be creating it: nothing is removed, and the route stays for the container.
    assert!(harness.exists());
    assert!(harness.has_policy());
    harness.events.clear();
    assert_eq!(
        harness
            .containers
            .ensure(TEAM, NETWORK, &spec('b'), None)
            .await,
        Ok(Ensured::Existing)
    );
    assert!(harness.events.before("start", "genesis"));
}

#[tokio::test]
async fn a_refused_create_rolls_back_only_when_no_container_is_left() {
    // Refused and proven absent: the policy it wrote goes, and the install failed.
    let harness = harness();
    harness
        .docker
        .fail("create_container", Fault::Before(DockerError::Conflict));
    let result = harness
        .containers
        .ensure(TEAM, NETWORK, &spec('b'), None)
        .await;
    assert_eq!(Harness::code(result), Some("docker-install-failed"));
    assert!(!harness.has_policy());
    // Refused, but the lookup cannot prove absence: everything stays.
    let harness = self::harness();
    harness
        .docker
        .fail("create_container", Fault::Before(DockerError::Conflict));
    harness
        .docker
        .fail("inspect_container", Fault::Before(DockerError::Unavailable));
    let resolved = crate::assistant::image::Resolved { id: image_id('b') };
    let result = harness
        .containers
        .create(TEAM, NETWORK, &spec('b'), &resolved, None)
        .await;
    assert_eq!(
        Harness::code(result),
        Some("assistant-install-rollback-incomplete")
    );
    assert!(harness.has_policy());
}

#[tokio::test]
async fn an_unexpected_image_or_a_failed_start_removes_the_container() {
    let harness = harness();
    let spec = spec('b');
    let resolved = crate::assistant::image::Resolved { id: image_id('a') };
    let result = harness
        .containers
        .create(TEAM, NETWORK, &spec, &resolved, None)
        .await;
    assert_eq!(Harness::code(result), Some("image-resolution-mismatch"));
    assert!(!harness.exists() && !harness.has_policy());
    harness
        .docker
        .fail("start_container", Fault::Before(DockerError::Unavailable));
    let result = harness.containers.ensure(TEAM, NETWORK, &spec, None).await;
    assert_eq!(Harness::code(result), Some("docker-install-failed"));
    assert!(!harness.exists() && !harness.has_policy());
}

#[tokio::test]
async fn an_outdated_release_is_replaced_from_its_trusted_image_without_its_manifest() {
    let harness = harness();
    harness.installed(&spec('a')).await;
    *harness.admission.manifest.lock().expect("usable") = Some(MANIFEST_INVALID);
    // The outdated container's manifest is never admitted; the successor's is.
    let result = harness
        .containers
        .ensure(TEAM, NETWORK, &spec('b'), None)
        .await;
    assert_eq!(Harness::code(result), Some("assistant-manifest-invalid"));
    assert!(harness.events.before("pull", "remove id"));
    assert!(harness.events.before("remove id", "create with-policy"));
    *harness.admission.manifest.lock().expect("usable") = None;
    harness.installed(&spec('a')).await;
    assert_eq!(
        harness
            .containers
            .ensure(TEAM, NETWORK, &spec('b'), None)
            .await,
        Ok(Ensured::Replaced)
    );
    let inspection = harness
        .containers
        .find(TEAM, "notes")
        .await
        .expect("found")
        .expect("present");
    let admitted = harness
        .containers
        .admit_profile(&inspection, TEAM, NETWORK, &spec('b'))
        .expect("admitted");
    assert_eq!(admitted.image, reference('b'));
}

#[tokio::test]
async fn isolation_drift_is_refused_never_repaired_or_removed() {
    let harness = harness();
    harness.installed(&spec('b')).await;
    harness
        .docker
        .inner()
        .tamper(&harness.name(), |inspection| {
            inspection.host_config.as_mut().expect("host").privileged = Some(true);
        });
    let result = harness
        .containers
        .ensure(TEAM, NETWORK, &spec('b'), None)
        .await;
    assert_eq!(Harness::code(result), Some("assistant-isolation-drift"));
    let result = harness
        .containers
        .uninstall(TEAM, NETWORK, "notes", Some(&spec('b')))
        .await;
    assert_eq!(Harness::code(result), Some("assistant-isolation-drift"));
    assert!(harness.exists());
    assert!(
        !harness
            .events
            .all()
            .iter()
            .any(|event| event.starts_with("remove") || event == "pull")
    );
}

#[tokio::test]
async fn a_workload_proxy_or_a_drifted_route_is_refused() {
    let harness = harness();
    harness.installed(&spec('b')).await;
    let inspection = harness
        .containers
        .find(TEAM, "notes")
        .await
        .expect("found")
        .expect("present");
    let admitted = harness
        .containers
        .admit_profile(&inspection, TEAM, NETWORK, &spec('b'))
        .expect("admitted");
    let mut proxied = admitted.clone();
    proxied.environment.push((
        "https_proxy".to_owned(),
        "http://proxy.example:1".to_owned(),
    ));
    assert_eq!(
        Harness::code(harness.containers.admit_egress(&proxied, TEAM, &spec('b'))),
        Some("assistant-isolation-drift")
    );
    let unsorted = ContainerSpec {
        allowed_hosts: vec!["b.example.com".to_owned(), "a.example.com".to_owned()],
        ..spec('b')
    };
    assert_eq!(
        Harness::code(harness.containers.admit_egress(&admitted, TEAM, &unsorted)),
        Some("assistant-registry-drift")
    );
    let expanded = ContainerSpec {
        allowed_hosts: vec!["api.example.com".to_owned(), "other.example.com".to_owned()],
        ..spec('b')
    };
    assert_eq!(
        Harness::code(harness.containers.admit_egress(&admitted, TEAM, &expanded)),
        Some("egress-policy-drift")
    );
    harness
        .containers
        .remove_policy(TEAM, "notes")
        .expect("removed");
    assert_eq!(
        Harness::code(harness.containers.admit_egress(&admitted, TEAM, &spec('b'))),
        Some("egress-policy-drift")
    );
}

#[tokio::test]
async fn a_failed_policy_preparation_is_cleaned_up_before_any_container_exists() {
    let harness = harness();
    // Non-canonical hosts fail the write: the token the write created goes with it, and Docker is never called.
    let unsorted = ContainerSpec {
        allowed_hosts: vec!["b.example.com".to_owned(), "a.example.com".to_owned()],
        ..spec('b')
    };
    let resolved = crate::assistant::image::Resolved { id: image_id('b') };
    let result = harness
        .containers
        .create(TEAM, NETWORK, &unsorted, &resolved, None)
        .await;
    assert_eq!(Harness::code(result), Some("egress-policy-drift"));
    assert!(!harness.has_policy());
    // A policy that already existed may belong to a container not yet proven absent: it stays.
    harness.installed(&spec('b')).await;
    harness
        .docker
        .inner()
        .remove_container(&harness.name())
        .await
        .expect("removed");
    let result = harness
        .containers
        .create(TEAM, NETWORK, &unsorted, &resolved, None)
        .await;
    assert_eq!(Harness::code(result), Some("egress-policy-drift"));
    assert!(harness.has_policy());
    assert!(
        !harness
            .events
            .all()
            .iter()
            .any(|event| event.starts_with("create"))
    );
}

struct OneBinding(Option<ContainerSpec>);

impl Bindings for OneBinding {
    fn spec<'a>(
        &'a self,
        _: &'a str,
        assistant_id: &'a str,
    ) -> BoxFuture<'a, Result<Option<ContainerSpec>, BindingsUnavailable>> {
        let found = self
            .0
            .clone()
            .filter(|spec| spec.assistant_id == assistant_id);
        Box::pin(async move { Ok(found) })
    }

    fn identities(&self) -> BoxFuture<'_, Result<Vec<(String, String)>, BindingsUnavailable>> {
        let identities = self
            .0
            .iter()
            .map(|spec| (TEAM.to_owned(), spec.assistant_id.clone()))
            .collect();
        Box::pin(async move { Ok(identities) })
    }
}

#[tokio::test]
async fn registry_drift_unbound_container_never_removed() {
    let harness = harness();
    harness.installed(&spec('b')).await;
    let result = harness
        .containers
        .uninstall(TEAM, NETWORK, "notes", None)
        .await;
    assert_eq!(Harness::code(result), Some("assistant-registry-drift"));
    assert!(harness.exists());
    let result = harness.containers.bound(TEAM, &OneBinding(None)).await;
    assert_eq!(Harness::code(result), Some("assistant-registry-drift"));
    let bound = harness
        .containers
        .bound(TEAM, &OneBinding(Some(spec('b'))))
        .await
        .expect("bound");
    assert_eq!(bound.len(), 1);
    assert!(
        !harness
            .events
            .all()
            .iter()
            .any(|event| event.starts_with("remove"))
    );
}

#[tokio::test]
async fn foreign_or_partly_labelled_resources_never_adopted() {
    let harness = harness();
    harness.installed(&spec('b')).await;
    let bindings = OneBinding(Some(spec('b')));
    // A labelled container under another name, a second one of the same Assistant, or a foreign Space's label.
    let mut body = isolation::create_body(
        Placement {
            space: &harness.space,
            team_id: TEAM,
            network: NETWORK,
            cpuset: "0",
        },
        &spec('b'),
    );
    harness
        .docker
        .inner()
        .create_container("foreign-container", body.clone())
        .await
        .expect("created");
    let result = harness.containers.bound(TEAM, &bindings).await;
    assert_eq!(Harness::code(result), Some("assistant-isolation-drift"));
    harness
        .docker
        .inner()
        .remove_container("foreign-container")
        .await
        .expect("removed");
    // A container missing one ownership label is not listed as the Team's at all.
    let labels = body.labels.as_mut().expect("labels");
    labels.remove(SPACE);
    harness
        .docker
        .inner()
        .create_container("partly-labelled", body)
        .await
        .expect("created");
    assert_eq!(
        harness
            .containers
            .bound(TEAM, &bindings)
            .await
            .map(|bound| bound.len()),
        Ok(1)
    );
    assert!(
        harness
            .docker
            .inner()
            .container("partly-labelled")
            .is_some()
    );
}

impl Harness {
    fn set_label(&self, name: &'static str, value: &'static str) {
        self.docker.inner().tamper(&self.name(), |inspection| {
            let labels = inspection
                .config
                .as_mut()
                .and_then(|config| config.labels.as_mut())
                .expect("labels");
            labels.insert(name.to_owned(), value.to_owned());
        });
    }
}

#[tokio::test]
async fn a_container_with_a_foreign_label_is_never_removed() {
    let harness = harness();
    harness.installed(&spec('b')).await;
    for (name, value) in [(SPACE, "other-space"), (ASSISTANT, "other")] {
        harness.set_label(name, value);
        let result = harness
            .containers
            .uninstall(TEAM, NETWORK, "notes", Some(&spec('b')))
            .await;
        assert_eq!(Harness::code(result), Some("assistant-isolation-drift"));
        harness.set_label(SPACE, "local-space");
        harness.set_label(ASSISTANT, "notes");
    }
    assert!(harness.exists());
}

#[tokio::test]
async fn uninstall_confirms_absence_and_removes_the_route_after_the_container() {
    let harness = harness();
    harness.installed(&spec('b')).await;
    let removed = harness
        .containers
        .uninstall(TEAM, NETWORK, "notes", Some(&spec('b')))
        .await
        .expect("uninstalled")
        .expect("a container was removed");
    assert_eq!(removed.image_id, Some(image_id('b')));
    assert!(harness.events.before("forget", "remove id"));
    assert!(!harness.exists() && !harness.has_policy());
    // Repeated: already absent is success, and a route left without a container goes too.
    assert_eq!(
        harness
            .containers
            .uninstall(TEAM, NETWORK, "notes", None)
            .await,
        Ok(None)
    );
    let identity = format!("local-space\0{TEAM}\0notes");
    let entropy = CountingEntropy::default();
    let token = harness
        .policies
        .token(&identity, true, &entropy)
        .expect("written")
        .expect("token");
    harness
        .policies
        .write(&token, &spec('b').allowed_hosts)
        .expect("written");
    assert_eq!(
        harness
            .containers
            .uninstall(TEAM, NETWORK, "notes", None)
            .await,
        Ok(None)
    );
    assert!(!harness.has_policy());
    harness.docker.inner().set_unavailable(true);
    assert_eq!(
        Harness::code(
            harness
                .containers
                .uninstall(TEAM, NETWORK, "notes", None)
                .await
        ),
        Some("docker-unavailable")
    );
}

#[tokio::test]
async fn an_unproven_stop_blocks_input_until_the_container_is_removed() {
    let harness = harness();
    harness.installed(&spec('b')).await;
    let ready = harness
        .containers
        .ready_for_input(TEAM, NETWORK, &spec('b'))
        .await
        .expect("ready");
    assert_eq!(harness.containers.fail_stop(&ready.id).await, Ok(()));
    harness.exec.block_fail_stop();
    assert_eq!(
        Harness::code(harness.containers.fail_stop(&ready.id).await),
        Some("assistant-action-blocked")
    );
    assert_eq!(
        Harness::code(
            harness
                .containers
                .ready_for_input(TEAM, NETWORK, &spec('b'))
                .await
        ),
        Some("assistant-action-blocked")
    );
    harness.containers.remove(&ready).await.expect("removed");
    assert!(!harness.containers.is_blocked(&ready.id));
    assert_eq!(
        Harness::code(
            harness
                .containers
                .ready_for_input(TEAM, NETWORK, &spec('b'))
                .await
        ),
        Some("assistant-not-found")
    );
}

#[tokio::test]
async fn input_needs_the_current_running_admitted_container() {
    let harness = harness();
    harness.installed(&spec('a')).await;
    assert_eq!(
        Harness::code(
            harness
                .containers
                .ready_for_input(TEAM, NETWORK, &spec('b'))
                .await
        ),
        Some("assistant-update-required")
    );
    harness
        .docker
        .force_status(Some(ContainerStateStatusEnum::EXITED));
    assert_eq!(
        Harness::code(
            harness
                .containers
                .ready_for_input(TEAM, NETWORK, &spec('a'))
                .await
        ),
        Some("assistant-not-running")
    );
}

#[tokio::test]
async fn a_stopped_assistant_is_started_and_only_an_unready_one_is_replaced() {
    let harness = harness();
    harness.installed(&spec('b')).await;
    harness.docker.inner().exit(&harness.name());
    let authorize = Authorized(harness.events.clone());
    assert_eq!(
        harness
            .containers
            .ensure(TEAM, NETWORK, &spec('b'), Some(&authorize))
            .await,
        Ok(Ensured::Existing)
    );
    assert!(harness.events.before("authorize", "start"));
    assert!(harness.events.before("start", "genesis"));
    assert!(
        !harness
            .events
            .all()
            .iter()
            .any(|event| event.starts_with("remove"))
    );
}

#[tokio::test]
async fn only_an_unready_assistant_is_replaced() {
    let harness = harness();
    harness.installed(&spec('b')).await;
    // Still only created at the readiness bound: replaced from its trusted image, once its ownership is proved again.
    harness.events.clear();
    harness
        .docker
        .next_statuses(vec![None, Some(ContainerStateStatusEnum::CREATED)]);
    assert_eq!(
        harness
            .containers
            .ensure(TEAM, NETWORK, &spec('b'), None)
            .await,
        Ok(Ensured::Replaced)
    );
    assert!(harness.events.before("remove id", "create with-policy"));
    // Exited at once is not ready either; a failure that is not readiness is never repaired by replacement.
    harness.events.clear();
    harness
        .docker
        .force_status(Some(ContainerStateStatusEnum::DEAD));
    let result = harness
        .containers
        .ensure(TEAM, NETWORK, &spec('b'), None)
        .await;
    assert_eq!(Harness::code(result), Some("assistant-not-ready"));
    harness.docker.force_status(None);
    harness.installed(&spec('b')).await;
    harness
        .docker
        .fail("start_container", Fault::Before(DockerError::Unavailable));
    harness.docker.inner().exit(&harness.name());
    let result = harness
        .containers
        .ensure(TEAM, NETWORK, &spec('b'), None)
        .await;
    assert_eq!(Harness::code(result), Some("docker-start-failed"));
}
