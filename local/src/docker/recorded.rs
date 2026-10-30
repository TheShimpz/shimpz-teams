//! A Docker decorator for tests: it records every call into an event log the test shares with its other fakes, fails
//! the operations a test names, and can force the state the inner daemon reports.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use bollard::models::{
    ContainerCreateBody, ContainerInspectResponse, ContainerStateStatusEnum, ContainerSummary,
    ImageInspect, ImageSummary, Network, NetworkCreateRequest, NetworkInspect, SystemInfo,
};
use team_core::future::BoxFuture;

use super::{Archive, DockerEngine, DockerError, Labels};

/// One shared, ordered event log.
#[derive(Clone, Debug, Default)]
pub struct Events(Arc<Mutex<Vec<String>>>);

impl Events {
    /// Appends `event`.
    pub fn push(&self, event: impl Into<String>) {
        self.0
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(event.into());
    }

    /// Every event so far.
    #[must_use]
    pub fn all(&self) -> Vec<String> {
        self.0
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    /// Forgets every event so far.
    pub fn clear(&self) {
        self.0
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clear();
    }

    /// Whether `first` was recorded before `second` (each by its first occurrence).
    #[must_use]
    pub fn before(&self, first: &str, second: &str) -> bool {
        let all = self.all();
        let at = |wanted: &str| all.iter().position(|event| event == wanted);
        matches!((at(first), at(second)), (Some(first), Some(second)) if first < second)
    }
}

/// How one operation fails: before reaching the daemon, or after the daemon did it (a lost answer).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Fault {
    /// The daemon refuses, doing nothing.
    Before(DockerError),
    /// The daemon does it, but the answer is lost.
    After(DockerError),
}

/// A probe evaluated at each container creation, its answer recorded with the event.
pub type Probe = Box<dyn Fn() -> String + Send + Sync>;

/// The decorator.
pub struct Recorded<D> {
    inner: D,
    events: Events,
    faults: Mutex<HashMap<&'static str, Vec<Fault>>>,
    status: Mutex<Option<ContainerStateStatusEnum>>,
    statuses: Mutex<Vec<Option<ContainerStateStatusEnum>>>,
    probe: Mutex<Option<Probe>>,
}

impl<D> Recorded<D> {
    /// Records `inner`'s calls into `events`.
    pub fn new(inner: D, events: Events) -> Self {
        Self {
            inner,
            events,
            faults: Mutex::default(),
            status: Mutex::default(),
            statuses: Mutex::default(),
            probe: Mutex::default(),
        }
    }

    /// The inner daemon.
    pub fn inner(&self) -> &D {
        &self.inner
    }

    /// Makes the next call of `operation` fail with `fault` (queued in order).
    pub fn fail(&self, operation: &'static str, fault: Fault) {
        lock(&self.faults).entry(operation).or_default().push(fault);
    }

    /// Makes every inspection report `status` (and running only for `RUNNING`), or the real state again with `None`.
    pub fn force_status(&self, status: Option<ContainerStateStatusEnum>) {
        *lock(&self.status) = status;
    }

    /// Makes the next inspections report these states in order (`None`: the real one), before any forced state.
    pub fn next_statuses(&self, statuses: Vec<Option<ContainerStateStatusEnum>>) {
        *lock(&self.statuses) = statuses;
    }

    /// Records `probe`'s answer with each container creation.
    pub fn probe_on_create(&self, probe: Probe) {
        *lock(&self.probe) = Some(probe);
    }

    fn fault(&self, operation: &'static str) -> Option<Fault> {
        let mut faults = lock(&self.faults);
        let queued = faults.get_mut(operation)?;
        (!queued.is_empty()).then(|| queued.remove(0))
    }

    async fn call<T: Send>(
        &self,
        operation: &'static str,
        event: String,
        run: BoxFuture<'_, Result<T, DockerError>>,
    ) -> Result<T, DockerError> {
        self.events.push(event);
        match self.fault(operation) {
            Some(Fault::Before(error)) => Err(error),
            Some(Fault::After(error)) => run.await.and(Err(error)),
            None => run.await,
        }
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// A short name for an id or name in the log: a container or network name as given, a 64-hex id as `id`.
fn short(id: &str) -> &str {
    if id.len() == 64 { "id" } else { id }
}

impl<D: DockerEngine> DockerEngine for Recorded<D> {
    fn info(&self) -> BoxFuture<'_, Result<SystemInfo, DockerError>> {
        Box::pin(self.call("info", "info".to_owned(), self.inner.info()))
    }

    fn list_containers<'a>(
        &'a self,
        labels: Labels<'a>,
    ) -> BoxFuture<'a, Result<Vec<ContainerSummary>, DockerError>> {
        Box::pin(self.call(
            "list_containers",
            "list-containers".to_owned(),
            self.inner.list_containers(labels),
        ))
    }

    fn inspect_container<'a>(
        &'a self,
        id: &'a str,
    ) -> BoxFuture<'a, Result<ContainerInspectResponse, DockerError>> {
        Box::pin(async move {
            let mut inspection = self
                .call(
                    "inspect_container",
                    format!("inspect {}", short(id)),
                    self.inner.inspect_container(id),
                )
                .await?;
            let queued = {
                let mut statuses = lock(&self.statuses);
                (!statuses.is_empty()).then(|| statuses.remove(0))
            };
            let forced = queued.unwrap_or(*lock(&self.status));
            if let Some(status) = forced
                && let Some(state) = inspection.state.as_mut()
            {
                state.status = Some(status);
                state.running = Some(status == ContainerStateStatusEnum::RUNNING);
            }
            Ok(inspection)
        })
    }

    fn create_container<'a>(
        &'a self,
        name: &'a str,
        body: ContainerCreateBody,
    ) -> BoxFuture<'a, Result<String, DockerError>> {
        let probed = lock(&self.probe).as_ref().map(|probe| probe());
        let event = probed.map_or_else(|| "create".to_owned(), |probed| format!("create {probed}"));
        Box::pin(self.call(
            "create_container",
            event,
            self.inner.create_container(name, body),
        ))
    }

    fn start_container<'a>(&'a self, id: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(self.call(
            "start_container",
            "start".to_owned(),
            self.inner.start_container(id),
        ))
    }

    fn restart_container<'a>(
        &'a self,
        id: &'a str,
        grace_seconds: i32,
    ) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(self.call(
            "restart_container",
            "restart".to_owned(),
            self.inner.restart_container(id, grace_seconds),
        ))
    }

    fn remove_container<'a>(&'a self, id: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(self.call(
            "remove_container",
            format!("remove {}", short(id)),
            self.inner.remove_container(id),
        ))
    }

    fn list_networks<'a>(
        &'a self,
        labels: Labels<'a>,
    ) -> BoxFuture<'a, Result<Vec<Network>, DockerError>> {
        Box::pin(self.call(
            "list_networks",
            "list-networks".to_owned(),
            self.inner.list_networks(labels),
        ))
    }

    fn inspect_network<'a>(
        &'a self,
        id: &'a str,
    ) -> BoxFuture<'a, Result<NetworkInspect, DockerError>> {
        Box::pin(self.call(
            "inspect_network",
            "inspect-network".to_owned(),
            self.inner.inspect_network(id),
        ))
    }

    fn create_network(
        &self,
        request: NetworkCreateRequest,
    ) -> BoxFuture<'_, Result<String, DockerError>> {
        Box::pin(self.call(
            "create_network",
            "create-network".to_owned(),
            self.inner.create_network(request),
        ))
    }

    fn remove_network<'a>(&'a self, id: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(self.call(
            "remove_network",
            "remove-network".to_owned(),
            self.inner.remove_network(id),
        ))
    }

    fn inspect_image<'a>(
        &'a self,
        reference: &'a str,
    ) -> BoxFuture<'a, Result<ImageInspect, DockerError>> {
        Box::pin(self.call(
            "inspect_image",
            "inspect-image".to_owned(),
            self.inner.inspect_image(reference),
        ))
    }

    fn get_archive<'a>(
        &'a self,
        container: &'a str,
        path: &'a str,
        bound: usize,
    ) -> BoxFuture<'a, Result<Archive, DockerError>> {
        Box::pin(self.call(
            "get_archive",
            format!("archive {path}"),
            self.inner.get_archive(container, path, bound),
        ))
    }

    fn list_images<'a>(
        &'a self,
        labels: Labels<'a>,
    ) -> BoxFuture<'a, Result<Vec<ImageSummary>, DockerError>> {
        Box::pin(self.call(
            "list_images",
            "list-images".to_owned(),
            self.inner.list_images(labels),
        ))
    }

    fn remove_image<'a>(&'a self, image: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(self.call(
            "remove_image",
            format!("remove-image {}", short(image)),
            self.inner.remove_image(image),
        ))
    }

    fn pull_image<'a>(&'a self, reference: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(self.call(
            "pull_image",
            "pull".to_owned(),
            self.inner.pull_image(reference),
        ))
    }
}
