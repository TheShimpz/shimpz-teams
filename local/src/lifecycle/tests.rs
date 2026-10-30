use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use bollard::models::{ContainerCreateBody, ImageInspect};
use strict_json::{Number, Value};
use team_core::future::BoxFuture;
use team_core::testing::{CountingEntropy, FakeExec};
use team_egress::policy::PolicyStore;

use super::network::TeamNetworks;
use super::participants::{AssistantTeardown, EgressTeardown, Removals};
use super::{Coverage, Lifecycle, Parts, PinRemains, SupervisorPin};
use crate::assistant::containers::{Admission, Containers, Ensured};
use crate::assistant::{Bindings, BindingsUnavailable, ContainerSpec, Provenance};
use crate::authority::Absence;
use crate::coordination::Coordination;
use crate::coordination::executions::{ChatLimit, LimitUnavailable};
use crate::coordination::residue::{ResidueKind, TeamRef, TeamTeardown, TeardownFailed};
use crate::docker::DockerEngine;
use crate::docker::helpers::{Helpers, KIND as PREPARE};
use crate::docker::labels::{Space, label_map};
use crate::docker::recorded::{Events, Recorded};
use crate::http::service::Problem;
use crate::names::TeamNames;
use crate::testing::FakeDocker;

mod destroy;
mod names;
mod reset;

const IMAGE: &str = "ghcr.io/theshimpz/shimpz-assistant@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const IMAGE_ID: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

struct Limit;

impl ChatLimit for Limit {
    fn current(&self) -> usize {
        2
    }

    fn save(&self, limit: u8) -> Result<u8, LimitUnavailable> {
        Ok(limit)
    }
}

struct NoReview;

impl Admission for NoReview {
    fn manifest<'a>(
        &'a self,
        _: &'a str,
        _: &'a ContainerSpec,
    ) -> BoxFuture<'a, Result<(), Problem>> {
        Box::pin(async { Ok(()) })
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

/// The bound Assistants, by Team.
#[derive(Default)]
struct Bound(Mutex<Vec<(String, ContainerSpec)>>);

impl Bound {
    fn bind(&self, team_id: &str, spec: ContainerSpec) {
        self.0
            .lock()
            .expect("usable")
            .push((team_id.to_owned(), spec));
    }

    fn unbind_all(&self) {
        self.0.lock().expect("usable").clear();
    }
}

impl Bindings for Bound {
    fn spec<'a>(
        &'a self,
        team_id: &'a str,
        assistant_id: &'a str,
    ) -> BoxFuture<'a, Result<Option<ContainerSpec>, BindingsUnavailable>> {
        let found = self
            .0
            .lock()
            .expect("usable")
            .iter()
            .find(|(team, spec)| team == team_id && spec.assistant_id == assistant_id)
            .map(|(_, spec)| spec.clone());
        Box::pin(async move { Ok(found) })
    }

    fn identities(&self) -> BoxFuture<'_, Result<Vec<(String, String)>, BindingsUnavailable>> {
        let identities = self
            .0
            .lock()
            .expect("usable")
            .iter()
            .map(|(team, spec)| (team.clone(), spec.assistant_id.clone()))
            .collect();
        Box::pin(async move { Ok(identities) })
    }
}

/// Another owner's participant: it logs each removal and fails when told to.
struct Owner {
    kinds: &'static [ResidueKind],
    events: Events,
    failure: Mutex<Option<TeardownFailed>>,
}

impl Owner {
    fn label(&self) -> String {
        self.kinds
            .iter()
            .map(|kind| kind.name())
            .collect::<Vec<_>>()
            .join("+")
    }

    fn removed(&self, target: &str) -> Result<(), TeardownFailed> {
        self.events.push(format!("{} {target}", self.label()));
        self.failure.lock().expect("usable").map_or(Ok(()), Err)
    }
}

impl TeamTeardown for Owner {
    fn kinds(&self) -> &'static [ResidueKind] {
        self.kinds
    }

    fn remove_team<'a>(&'a self, team: &'a TeamRef) -> BoxFuture<'a, Result<(), TeardownFailed>> {
        let outcome = self.removed(&team.team_id);
        Box::pin(async move { outcome })
    }

    fn remove_space(&self) -> BoxFuture<'_, Result<(), TeardownFailed>> {
        let outcome = self.removed("space");
        Box::pin(async move { outcome })
    }
}

use ResidueKind as K;

/// Every kind another workstream owns, one participant per group.
const OTHERS: [&[ResidueKind]; 9] = [
    &[K::ChatContinuations],
    &[K::BrainCheckpoints, K::ActionCheckpoints],
    &[K::Browsers],
    &[K::SignIns],
    &[K::Routines],
    &[K::PublicationBindings],
    &[K::InferenceConfiguration],
    &[K::TeamStorage],
    &[K::IntegrationCredentials, K::StoredInputs],
];

#[derive(Default)]
struct Pin {
    established: AtomicBool,
    forgotten: AtomicBool,
}

impl SupervisorPin for Pin {
    fn require_absent(&self) -> Result<(), Absence> {
        if self.established.load(Ordering::SeqCst) {
            Err(Absence::Established)
        } else {
            Ok(())
        }
    }

    fn forget(&self) -> Result<(), PinRemains> {
        self.forgotten.store(true, Ordering::SeqCst);
        Ok(())
    }
}

struct Harness {
    _directory: tempfile::TempDir,
    events: Events,
    docker: Arc<Recorded<FakeDocker>>,
    space: Space,
    bound: Arc<Bound>,
    containers: Arc<Containers>,
    owners: Vec<Arc<Owner>>,
    names: TeamNames,
    pin: Arc<Pin>,
    coordination: Coordination,
    lifecycle: Lifecycle,
    storage: std::path::PathBuf,
}

fn policies(root: &std::path::Path) -> PolicyStore {
    fs::create_dir(root).expect("created");
    fs::set_permissions(root, fs::Permissions::from_mode(0o750)).expect("mode");
    PolicyStore::new(root.to_owned(), fs::metadata(root).expect("stat").gid())
}

fn registry() -> FakeDocker {
    let fake = FakeDocker::default();
    fake.add_image(
        IMAGE,
        ImageInspect {
            id: Some(IMAGE_ID.to_owned()),
            repo_digests: Some(vec![IMAGE.to_owned()]),
            ..ImageInspect::default()
        },
    );
    fake
}

fn containers(
    docker: Arc<Recorded<FakeDocker>>,
    space: &Space,
    root: &std::path::Path,
) -> Arc<Containers> {
    Arc::new(Containers::new(crate::assistant::containers::Parts {
        docker,
        exec: Arc::new(FakeExec::default()),
        admission: Arc::new(NoReview),
        policies: policies(&root.join("assistant-egress")),
        entropy: Arc::new(CountingEntropy::default()),
        space: space.clone(),
        cpuset: "0".to_owned(),
    }))
}

fn owners(events: &Events) -> Vec<Arc<Owner>> {
    OTHERS
        .iter()
        .map(|kinds| {
            Arc::new(Owner {
                kinds,
                events: events.clone(),
                failure: Mutex::default(),
            })
        })
        .collect()
}

/// Every other owner's participant, and this workstream's egress policies.
fn others(
    owners: &[Arc<Owner>],
    containers: &Arc<Containers>,
    bound: &Arc<Bound>,
    removals: &Removals,
) -> Vec<Arc<dyn TeamTeardown>> {
    let mut participants: Vec<Arc<dyn TeamTeardown>> = owners
        .iter()
        .map(|owner| owner.clone() as Arc<dyn TeamTeardown>)
        .collect();
    participants.push(Arc::new(EgressTeardown::new(
        containers.clone(),
        bound.clone(),
        removals.clone(),
    )));
    participants
}

fn harness() -> Harness {
    let directory = tempfile::tempdir().expect("a temporary directory");
    let events = Events::default();
    let docker = Arc::new(Recorded::new(registry(), events.clone()));
    let space = Space::new("local-space").expect("a Space");
    let containers = containers(docker.clone(), &space, directory.path());
    let bound = Arc::new(Bound::default());
    let removals = Removals::default();
    let owners = owners(&events);
    let participants = others(&owners, &containers, &bound, &removals);
    let names = TeamNames::new(directory.path().join("inference"));
    let pin = Arc::new(Pin::default());
    let coordination = Coordination::new(Arc::new(Limit));
    let storage = directory.path().join("storage");
    let lifecycle = Lifecycle::new(Parts {
        coordination: coordination.clone(),
        networks: TeamNetworks::new(docker.clone(), space.clone()),
        names: names.clone(),
        assistants: Arc::new(AssistantTeardown::new(
            docker.clone(),
            containers.clone(),
            bound.clone(),
            space.clone(),
            removals,
        )),
        helpers: Arc::new(Helpers::new(docker.clone(), space.clone())),
        participants,
        storage: storage.clone(),
        pin: pin.clone(),
    })
    .expect("every kind is covered once");
    Harness {
        _directory: directory,
        events,
        docker,
        space,
        bound,
        containers,
        owners,
        names,
        pin,
        coordination,
        lifecycle,
        storage,
    }
}

fn spec() -> ContainerSpec {
    ContainerSpec {
        assistant_id: "notes".to_owned(),
        image: IMAGE.to_owned(),
        provenance: Provenance::Published,
        allowed_hosts: vec!["api.example.com".to_owned()],
        required_image_labels: Vec::new(),
        platform: None,
    }
}

fn code<T: std::fmt::Debug>(result: Result<T, Problem>) -> Option<&'static str> {
    result.err().and_then(|problem| problem.code)
}

fn field<'a>(value: &'a Value, name: &str) -> &'a Value {
    value.get(name).unwrap_or(&Value::Null)
}

fn int(value: i64) -> Value {
    Value::Number(Number::Int(value))
}

impl Harness {
    fn owner(&self, kind: ResidueKind) -> &Owner {
        self.owners
            .iter()
            .find(|owner| owner.kinds.contains(&kind))
            .expect("an owner of the kind")
    }

    /// A Team with one bound, running Assistant whose route exists, a preparation helper, and Team files.
    async fn populated(&self, team_id: &str, name: &str) {
        self.lifecycle.create(team_id, name).await.expect("created");
        self.bound.bind(team_id, spec());
        let network = self.space.network_name(team_id);
        assert_eq!(
            self.containers
                .ensure(team_id, &network, &spec(), None)
                .await,
            Ok(Ensured::Created)
        );
        let helper = ContainerCreateBody {
            labels: Some(label_map(&self.space.labels(PREPARE, Some(team_id)))),
            ..ContainerCreateBody::default()
        };
        self.docker
            .inner()
            .create_container(&format!("helper-{team_id}"), helper)
            .await
            .expect("a helper");
        fs::create_dir_all(self.storage.join(team_id)).expect("Team files");
        self.events.clear();
    }

    fn assistant_exists(&self, team_id: &str) -> bool {
        self.docker
            .inner()
            .container(&self.space.container_name(team_id, "notes"))
            .is_some()
    }

    fn has_policy(&self, team_id: &str) -> bool {
        self.containers
            .has_policy(team_id, "notes")
            .expect("readable")
    }

    fn network_exists(&self, team_id: &str) -> bool {
        self.docker
            .inner()
            .network_names()
            .contains(&self.space.network_name(team_id))
    }
}

#[test]
fn the_participants_must_cover_every_kind_once_within_one_step() {
    let harness = harness();
    let parts = |participants: Vec<Arc<dyn TeamTeardown>>| Parts {
        coordination: harness.coordination.clone(),
        networks: TeamNetworks::new(harness.docker.clone(), harness.space.clone()),
        names: harness.names.clone(),
        assistants: Arc::new(AssistantTeardown::new(
            harness.docker.clone(),
            harness.containers.clone(),
            harness.bound.clone(),
            harness.space.clone(),
            Removals::default(),
        )),
        helpers: Arc::new(Helpers::new(harness.docker.clone(), harness.space.clone())),
        participants,
        storage: harness.storage.clone(),
        pin: harness.pin.clone(),
    };
    let owner = |kinds: &'static [ResidueKind]| -> Arc<dyn TeamTeardown> {
        Arc::new(Owner {
            kinds,
            events: Events::default(),
            failure: Mutex::default(),
        })
    };
    // One kind missing (the egress policies).
    let missing: Vec<Arc<dyn TeamTeardown>> = OTHERS.iter().map(|kinds| owner(kinds)).collect();
    assert_eq!(Lifecycle::new(parts(missing)).err(), Some(Coverage));
    // One kind twice.
    let mut repeated: Vec<Arc<dyn TeamTeardown>> =
        OTHERS.iter().map(|kinds| owner(kinds)).collect();
    repeated.push(owner(&[K::EgressPolicies]));
    repeated.push(owner(&[K::TeamStorage]));
    assert_eq!(Lifecycle::new(parts(repeated)).err(), Some(Coverage));
    // One participant whose kinds span two reset steps (inference and Team files go in separate reset steps).
    let mut spanning: Vec<Arc<dyn TeamTeardown>> = OTHERS
        .iter()
        .filter(|kinds| {
            !kinds.contains(&K::TeamStorage) && !kinds.contains(&K::InferenceConfiguration)
        })
        .map(|kinds| owner(kinds))
        .collect();
    spanning.push(owner(&[K::InferenceConfiguration, K::TeamStorage]));
    spanning.push(owner(&[K::EgressPolicies]));
    assert_eq!(Lifecycle::new(parts(spanning)).err(), Some(Coverage));
}
