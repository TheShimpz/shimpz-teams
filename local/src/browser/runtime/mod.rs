//! The Local Team Browser's Docker lifecycle (ADR-0117 §4 and §8).
//!
//! One Browser exists per Space at a time. Before creating one, Team proves from the labelled Docker inventory that no
//! Browser container exists and that the egress is the static Browser network's only member, and it runs every task
//! on a fresh egress generation: the egress is restarted after each task, and before a task whenever the generation an
//! earlier task used is still running. The Browser is created, inspected against its complete envelope before Chromium
//! starts, started, and inspected again; drift removes it. A failed or unproven removal keeps every later task refused
//! until a cleanup proves the Space clean again.

mod egress;
mod programs;

use std::collections::HashMap;
use std::fmt::Write;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use sha2::{Digest, Sha256};
use team_core::entropy::Entropy;
use team_core::exec::ExecEngine;
use team_core::stop::Stop;
use tokio::sync::watch;
use tokio::time::{Instant, sleep, sleep_until, timeout_at};

use super::credentials::Credentials;
use super::envelope::{self, Drift, Requested};
use crate::docker::{DockerEngine, DockerError};

pub use egress::Generation;
pub use programs::{HOST, LeaseRelays};

/// The Browser's ownership kind.
pub const KIND: &str = "browser";
/// The label a Team image names its exact Browser image with (ADR-0117 §9).
pub const IMAGE_LABEL: &str = "org.shimpz.local.browser-image";
/// The published Browser repository; a missing image of it is pulled by digest.
pub const PUBLISHED: &str = "ghcr.io/theshimpz/shimpz-browser";
const DEVELOPER: &str = "localhost/shimpz-browser";
/// Removing a Browser, proved absent, ends by this one deadline.
pub const REMOVE_SECONDS: Duration = Duration::from_secs(30);
/// How often a removed Browser's absence is checked again.
pub const REMOVE_POLL: Duration = Duration::from_millis(100);
/// How long a Browser has to answer `ready`.
pub const READY_SECONDS: Duration = Duration::from_secs(20);

const MANAGED_LABEL: &str = "com.shimpz.local.managed";
const PROFILE_LABEL: &str = "com.shimpz.local.profile";
const SPACE_LABEL: &str = "com.shimpz.local.space-id";
const KIND_LABEL: &str = "com.shimpz.local.kind";
const TEAM_LABEL: &str = "com.shimpz.local.team-id";
const OWNED_IMAGE_LABEL: &str = "com.shimpz.local.image";
const PROFILE: &str = "local-v1";

/// A closed reason no Browser task can run or continue.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BrowserError(pub &'static str);

impl BrowserError {
    /// No Browser can run, or this one drifted.
    pub const UNAVAILABLE: Self = Self("browser-unavailable");
    /// The Space's one Browser is held, or something still occupies its network.
    pub const BUSY: Self = Self("browser-busy");
    /// The Browser failed, or could not be proven removed.
    pub const LOST: Self = Self("browser-lost");
}

impl std::fmt::Display for BrowserError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.0)
    }
}

impl std::error::Error for BrowserError {}

/// Who drives a Browser (ADR-0117, Sign-ins amendment): the agent alone, a person signing in at a URL, or a task
/// restored from a Sign-in. Only the last two hold display credentials and keep a profile.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Mode {
    /// The agent alone.
    Agent,
    /// A person signing in, starting at this URL.
    Person(String),
    /// A task restored from the Team's Sign-in.
    SignedIn,
}

impl Mode {
    fn name(&self) -> &'static str {
        match self {
            Self::Agent => "agent",
            Self::Person(_) => "person",
            Self::SignedIn => "signed-in",
        }
    }
}

/// A capsule chunk that could not be read or authenticated.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CapsuleUnreadable;

/// A Sign-in's capsule, chunk by chunk, each authenticated as it is read.
pub type Capsule = Box<dyn Iterator<Item = Result<Vec<u8>, CapsuleUnreadable>> + Send>;

/// One running Browser: its Team, container, address, the egress generation it runs on, and who drives it.
#[derive(Clone, Debug)]
pub struct Lease {
    /// Its Team.
    pub team_id: String,
    /// Its container's id.
    pub container_id: String,
    /// Its address on the Browser network.
    pub address: String,
    /// The egress process it started on.
    pub egress: Generation,
    /// `agent`, `person`, or `signed-in`.
    pub mode: &'static str,
    /// The display's viewer credentials, for a Browser a person may control.
    pub credentials: Option<Arc<Credentials>>,
}

/// What one Browser is acquired for.
pub struct Acquire<'a> {
    /// The Team.
    pub team_id: &'a str,
    /// The pages' language (`en` or a two-letter language and region).
    pub language: &'a str,
    /// Who drives it.
    pub mode: Mode,
    /// The capsule restored before Chromium starts.
    pub capsule: Option<Capsule>,
    /// When a missing image's pull, and the whole start, stop being waited for.
    pub deadline: Option<Instant>,
    /// The task's Stop, which ends every wait at once.
    pub stop: Option<&'a Stop>,
}

/// Whether a task may still wait for its Browser: not stopped, and its deadline not passed.
#[derive(Clone, Copy)]
struct Waiting<'a> {
    deadline: Option<Instant>,
    stop: Option<&'a Stop>,
}

impl Waiting<'_> {
    fn may(&self) -> bool {
        !self.stop.is_some_and(Stop::is_stopped)
            && self
                .deadline
                .is_none_or(|deadline| Instant::now() < deadline)
    }

    /// Resolves once the task may no longer wait.
    async fn ended(&self) {
        let stopped = async {
            match self.stop {
                Some(stop) => stop.stopped().await,
                None => std::future::pending().await,
            }
        };
        let passed = async {
            match self.deadline {
                Some(deadline) => sleep_until(deadline).await,
                None => std::future::pending().await,
            }
        };
        tokio::select! {
            () = stopped => {},
            () = passed => {},
        }
    }
}

/// Where the runtime runs.
#[derive(Clone)]
pub struct Host {
    /// The Docker daemon.
    pub docker: Arc<dyn DockerEngine>,
    /// Exec into the Browser.
    pub exec: Arc<dyn ExecEngine>,
    /// The random source of display credentials.
    pub entropy: Arc<dyn Entropy>,
}

/// Admission, creation, the exec programs, and proven removal of the Space's one Browser.
pub struct BrowserRuntime {
    host: Host,
    space_id: String,
    cpuset: String,
    image: Option<String>,
    // The Space's one slot: held from a lease's acquisition until its release.
    held: AtomicBool,
    // Set when a removal failed or was not proven: the next holder cleans first.
    dirty: AtomicBool,
    // Set once this process proved that no Browser of an earlier Team process is left.
    cleaned: AtomicBool,
    // The egress generation an earlier task ran on, which no later task may share; unknown after a Team start.
    used: Mutex<Option<Generation>>,
    // The one pull of the pinned image under way, if any: its outcome once known.
    pull: Mutex<Option<watch::Receiver<Option<bool>>>>,
}

impl std::fmt::Debug for BrowserRuntime {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("BrowserRuntime")
            .field("image", &self.image)
            .finish_non_exhaustive()
    }
}

/// The exact Browser image a Team image's labels name, or `None` (ADR-0117 §9).
#[must_use]
pub fn browser_image<S: std::hash::BuildHasher>(
    labels: Option<&HashMap<String, String, S>>,
) -> Option<String> {
    let value = labels?.get(IMAGE_LABEL)?;
    let (repository, digest) = value.split_once("@sha256:")?;
    let exact = (repository == DEVELOPER || repository == PUBLISHED)
        && digest.len() == 64
        && digest
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));
    exact.then(|| value.clone())
}

/// The Browser image this Team container's own image names, or `None`: Team then answers browser-unavailable.
pub async fn own_browser_image(docker: &dyn DockerEngine, hostname: &str) -> Option<String> {
    let container = docker.inspect_container(hostname).await.ok()?;
    let image = docker
        .inspect_image(container.image.as_deref()?)
        .await
        .ok()?;
    browser_image(image.config.as_ref()?.labels.as_ref())
}

fn language_admitted(language: &str) -> bool {
    let bytes = language.as_bytes();
    let lower = |part: &[u8]| part.len() == 2 && part.iter().all(u8::is_ascii_lowercase);
    let upper = |part: &[u8]| part.len() == 2 && part.iter().all(u8::is_ascii_uppercase);
    match bytes.len() {
        2 => lower(bytes),
        5 => lower(&bytes[..2]) && bytes[2] == b'-' && upper(&bytes[3..]),
        _ => false,
    }
}

impl BrowserRuntime {
    /// The runtime of `space_id`'s Browser on `cpuset`, running `image` (only on amd64; `None` where it cannot run).
    #[must_use]
    pub fn new(host: Host, space_id: &str, cpuset: &str, image: Option<String>) -> Self {
        Self {
            host,
            space_id: space_id.to_owned(),
            cpuset: cpuset.to_owned(),
            image,
            held: AtomicBool::new(false),
            dirty: AtomicBool::new(true),
            cleaned: AtomicBool::new(false),
            used: Mutex::new(None),
            pull: Mutex::new(None),
        }
    }

    /// The exact Browser image this Team runs, which a Sign-in records as its writer.
    #[must_use]
    pub fn image(&self) -> Option<&str> {
        self.image.as_deref()
    }

    /// Whether this Team image names a Browser it can run.
    #[must_use]
    pub fn available(&self) -> bool {
        self.image.is_some()
    }

    fn take_slot(&self) -> bool {
        self.held
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
    }

    fn free_slot(&self) {
        self.held.store(false, Ordering::SeqCst);
    }

    /// Whether this process proved that no Browser an earlier Team process left remains, so none of its Driver
    /// sessions can act again.
    #[must_use]
    pub fn previous_gone(&self) -> bool {
        self.cleaned.load(Ordering::SeqCst)
    }

    /// Whether the last cleanup or release left residue unproven.
    #[must_use]
    pub fn dirty(&self) -> bool {
        self.dirty.load(Ordering::SeqCst)
    }

    /// The labels every Browser of this Space carries, optionally for one Team.
    fn ownership(&self, team_id: Option<&str>) -> Vec<(&'static str, String)> {
        let mut labels = vec![
            (MANAGED_LABEL, "1".to_owned()),
            (PROFILE_LABEL, PROFILE.to_owned()),
            (SPACE_LABEL, self.space_id.clone()),
            (KIND_LABEL, KIND.to_owned()),
        ];
        if let Some(team_id) = team_id {
            labels.push((TEAM_LABEL, team_id.to_owned()));
        }
        labels
    }

    /// The container name of `team_id`'s Browser.
    #[must_use]
    pub fn container_name(&self, team_id: &str) -> String {
        let digest = Sha256::digest(self.space_id.as_bytes());
        let prefix = digest
            .iter()
            .take(6)
            .fold(String::new(), |mut prefix, byte| {
                // Writing to a String cannot fail.
                let _ = write!(prefix, "{byte:02x}");
                prefix
            });
        format!("shimpz-local-{prefix}-{team_id}-browser")
    }

    // Admission and creation

    /// Creates this task's Browser on a fresh egress generation; the caller releases the returned lease.
    ///
    /// # Errors
    /// [`BrowserError`]: `browser-unavailable` when it cannot run or drifted, `browser-busy` while the Space's Browser
    /// is held or its network is occupied.
    pub async fn acquire(&self, request: Acquire<'_>) -> Result<Lease, BrowserError> {
        let admitted = self.image.is_some()
            && language_admitted(request.language)
            && match request.mode {
                Mode::SignedIn => request.capsule.is_some(),
                Mode::Agent => request.capsule.is_none(),
                Mode::Person(_) => true,
            };
        if !admitted {
            return Err(BrowserError::UNAVAILABLE);
        }
        if !self.take_slot() {
            return Err(BrowserError::BUSY);
        }
        match self.acquired(request).await {
            Ok((lease, egress)) => {
                *self.used.lock().unwrap_or_else(PoisonError::into_inner) = Some(egress);
                Ok(lease)
            }
            Err(error) => {
                self.dirty.store(true, Ordering::SeqCst);
                self.free_slot();
                Err(error)
            }
        }
    }

    async fn acquired(&self, request: Acquire<'_>) -> Result<(Lease, Generation), BrowserError> {
        if self.dirty() {
            self.clean().await?;
        }
        self.prove_clean().await?;
        let egress = self.fresh_egress().await?;
        let waiting = Waiting {
            deadline: request.deadline,
            stop: request.stop,
        };
        let lease = self.create(request, egress.clone(), waiting).await?;
        Ok((lease, egress))
    }

    /// Removes the task's Browser, proves it absent, and ends its egress generation; then frees the slot.
    ///
    /// # Errors
    /// [`BrowserError::LOST`] or `browser-unavailable` when either was not proven; the runtime stays dirty.
    pub async fn release(&self, lease: &Lease) -> Result<(), BrowserError> {
        let outcome = async {
            self.remove(&lease.container_id).await?;
            self.restart_egress().await.map(drop)
        }
        .await;
        self.dirty.store(outcome.is_err(), Ordering::SeqCst);
        self.free_slot();
        outcome
    }

    /// Cleans up after a removal that failed; whether no residue is left, proven. While a task holds the slot, only
    /// its own cleanup can prove that.
    pub async fn recover(&self) -> bool {
        if !self.take_slot() {
            return !self.dirty();
        }
        let outcome = if self.dirty() {
            self.clean().await.is_ok()
        } else {
            true
        };
        self.free_slot();
        outcome
    }

    /// Removes every Browser a previous Team process left; whether it proved none is left. The egress is not
    /// restarted here, and the runtime stays dirty, so the first task restarts it on a generation no task used.
    pub async fn startup(&self) -> bool {
        if !self.take_slot() {
            return false;
        }
        let outcome = self.remove_all(None).await.is_ok();
        if outcome {
            self.cleaned.store(true, Ordering::SeqCst);
        }
        self.free_slot();
        outcome
    }

    /// Removes every Browser of this Space and restarts the egress, proving both; where no Browser can run, proving
    /// none is left is all.
    ///
    /// # Errors
    /// [`BrowserError`] when either was not proven.
    pub async fn clean(&self) -> Result<(), BrowserError> {
        self.remove_all(None).await?;
        self.cleaned.store(true, Ordering::SeqCst);
        if self.available() {
            self.restart_egress().await?;
        }
        self.dirty.store(false, Ordering::SeqCst);
        Ok(())
    }

    /// Removes one Team's Browser, as its teardown requires; an absent Browser is success.
    ///
    /// # Errors
    /// [`BrowserError`] when it may remain.
    pub async fn remove_team(&self, team_id: &str) -> Result<(), BrowserError> {
        self.remove_all(Some(team_id)).await
    }

    async fn remove_all(&self, team_id: Option<&str>) -> Result<(), BrowserError> {
        for id in self.browsers(team_id).await? {
            self.remove(&id).await?;
        }
        Ok(())
    }

    async fn browsers(&self, team_id: Option<&str>) -> Result<Vec<String>, BrowserError> {
        let owned = self.ownership(team_id);
        let labels: Vec<(&str, &str)> = owned
            .iter()
            .map(|(name, value)| (*name, value.as_str()))
            .collect();
        let listed = self
            .host
            .docker
            .list_containers(&labels)
            .await
            .map_err(|_| BrowserError::UNAVAILABLE)?;
        listed
            .into_iter()
            .map(|summary| summary.id.ok_or(BrowserError::UNAVAILABLE))
            .collect()
    }

    async fn prove_clean(&self) -> Result<(), BrowserError> {
        if !self.browsers(None).await?.is_empty() {
            return Err(BrowserError::BUSY);
        }
        let network = self.network().await?;
        let egress = self.egress().await?;
        let members = network.containers.unwrap_or_default();
        let only_egress = members.len() == 1
            && egress
                .id
                .as_ref()
                .is_some_and(|id| members.contains_key(id));
        if only_egress {
            Ok(())
        } else {
            Err(BrowserError::BUSY)
        }
    }

    /// Whether the lease's Browser still runs on the very egress process it started on (ADR-0117 §4.3).
    pub async fn intact(&self, lease: &Lease) -> bool {
        let Ok(egress) = self.egress().await else {
            return false;
        };
        let Ok(current) = egress::generation(&egress) else {
            return false;
        };
        let Ok(container) = self
            .host
            .docker
            .inspect_container(&lease.container_id)
            .await
        else {
            return false;
        };
        current == lease.egress && container.state.and_then(|state| state.running) == Some(true)
    }

    /// Removes the lease's Browser at once, for Stop; its task's release proves the removal and frees the slot.
    ///
    /// # Errors
    /// [`BrowserError::LOST`] when the removal was not proven.
    pub async fn kill(&self, lease: &Lease) -> Result<(), BrowserError> {
        self.remove(&lease.container_id).await
    }

    async fn create(
        &self,
        request: Acquire<'_>,
        egress: Generation,
        waiting: Waiting<'_>,
    ) -> Result<Lease, BrowserError> {
        let image = self.image.clone().ok_or(BrowserError::UNAVAILABLE)?;
        // The egress has its outbound network too; the Browser reaches it only on the Browser network.
        let proxy =
            envelope::networks_address(&self.egress().await?).ok_or(BrowserError::UNAVAILABLE)?;
        let image_id = self.image_id(&image, waiting).await?;
        if !waiting.may() {
            return Err(BrowserError::UNAVAILABLE);
        }
        let requested = self.requested(&request, &image, &proxy);
        let id = self
            .host
            .docker
            .create_container(&requested.name, envelope::body(&requested))
            .await
            .map_err(|_| BrowserError::UNAVAILABLE)?;
        match self
            .started(&id, &requested, &image_id, &egress, request, waiting)
            .await
        {
            Ok(lease) => Ok(lease),
            Err(error) => {
                // Removal is proven or the runtime stays dirty: either way the Browser never serves this task.
                let _ = self.remove(&id).await;
                Err(error)
            }
        }
    }

    fn requested(&self, request: &Acquire<'_>, image: &str, proxy: &str) -> Requested {
        let mut labels: HashMap<String, String> = self
            .ownership(Some(request.team_id))
            .into_iter()
            .map(|(name, value)| (name.to_owned(), value))
            .collect();
        labels.insert(OWNED_IMAGE_LABEL.to_owned(), image.to_owned());
        let mut env = vec![
            format!("BROWSER_PROXY={proxy}"),
            format!("BROWSER_LANG={}", request.language),
            format!("BROWSER_MODE={}", request.mode.name()),
        ];
        if let Mode::Person(url) = &request.mode {
            env.push(format!("BROWSER_URL={url}"));
            if request.capsule.is_some() {
                env.push("BROWSER_RESTORE=1".to_owned());
            }
        }
        Requested {
            image: image.to_owned(),
            name: self.container_name(request.team_id),
            labels,
            env,
            cpuset: self.cpuset.clone(),
        }
    }

    /// Inspects the created Browser against its envelope, starts it, inspects it again, hands it its credentials and
    /// capsule, and waits for it to be ready.
    async fn started(
        &self,
        id: &str,
        requested: &Requested,
        image_id: &str,
        egress: &Generation,
        request: Acquire<'_>,
        waiting: Waiting<'_>,
    ) -> Result<Lease, BrowserError> {
        let drifted = |_: Drift| BrowserError::UNAVAILABLE;
        let created = self.inspect(id).await?;
        envelope::before_start(&created, requested, image_id).map_err(drifted)?;
        // Stop or the deadline came while the container was created: Chromium never starts.
        if !waiting.may() {
            return Err(BrowserError::UNAVAILABLE);
        }
        self.host
            .docker
            .start_container(id)
            .await
            .map_err(|_| BrowserError::UNAVAILABLE)?;
        let running = self.inspect(id).await?;
        let network = self.network().await?;
        let address = envelope::after_start(
            &running,
            id,
            requested,
            image_id,
            &network,
            &egress.container_id,
        )
        .map_err(drifted)?;
        let mode = request.mode.name();
        let credentials = self.provision(id, &request.mode, request.capsule).await?;
        self.wait_ready(id, waiting).await?;
        Ok(Lease {
            team_id: request.team_id.to_owned(),
            container_id: id.to_owned(),
            address,
            egress: egress.clone(),
            mode,
            credentials,
        })
    }

    async fn provision(
        &self,
        id: &str,
        mode: &Mode,
        capsule: Option<Capsule>,
    ) -> Result<Option<Arc<Credentials>>, BrowserError> {
        let credentials = if *mode == Mode::Agent {
            None
        } else {
            let drawn = Credentials::draw(self.host.entropy.as_ref())
                .map_err(|_| BrowserError::UNAVAILABLE)?;
            let file = drawn.file();
            self.feed(id, &["credentials"], Box::new(std::iter::once(Ok(file))))
                .await?;
            Some(Arc::new(drawn))
        };
        if let Some(capsule) = capsule {
            self.feed(id, &["profile", "import"], capsule).await?;
        }
        Ok(credentials)
    }

    async fn inspect(
        &self,
        id: &str,
    ) -> Result<bollard::models::ContainerInspectResponse, BrowserError> {
        self.host
            .docker
            .inspect_container(id)
            .await
            .map_err(|_| BrowserError::UNAVAILABLE)
    }

    async fn wait_ready(&self, id: &str, waiting: Waiting<'_>) -> Result<(), BrowserError> {
        let deadline = Instant::now() + READY_SECONDS;
        while self
            .program(id, &["ready"], Instant::now() + Duration::from_secs(2))
            .await
            != Some(0)
        {
            if Instant::now() >= deadline || !waiting.may() {
                return Err(BrowserError::UNAVAILABLE);
            }
            sleep(Duration::from_millis(200)).await;
        }
        Ok(())
    }

    /// The image's id: present, or the published image pulled by digest while the task may wait.
    async fn image_id(&self, image: &str, waiting: Waiting<'_>) -> Result<String, BrowserError> {
        match self.host.docker.inspect_image(image).await {
            Ok(found) => return found.id.ok_or(BrowserError::UNAVAILABLE),
            Err(DockerError::NotFound) if image.starts_with(&format!("{PUBLISHED}@")) => {}
            Err(_) => return Err(BrowserError::UNAVAILABLE),
        }
        self.pulled(image, waiting).await?;
        let pulled = self
            .host
            .docker
            .inspect_image(image)
            .await
            .map_err(|_| BrowserError::UNAVAILABLE)?;
        pulled.id.ok_or(BrowserError::UNAVAILABLE)
    }

    /// Waits for the one pull of the pinned image, starting it if none runs, while the task may wait. A pull still
    /// running when the wait ends goes on alone and is never used by this task.
    async fn pulled(&self, image: &str, waiting: Waiting<'_>) -> Result<(), BrowserError> {
        if !waiting.may() {
            return Err(BrowserError::UNAVAILABLE);
        }
        let mut outcome = {
            let mut pull = self.pull.lock().unwrap_or_else(PoisonError::into_inner);
            let running = pull
                .as_ref()
                .is_some_and(|receiver| receiver.borrow().is_none());
            if !running {
                let (sender, receiver) = watch::channel(None);
                let docker = Arc::clone(&self.host.docker);
                let reference = image.to_owned();
                tokio::spawn(async move {
                    let pulled = docker.pull_image(&reference).await.is_ok();
                    sender.send_replace(Some(pulled));
                });
                *pull = Some(receiver);
            }
            pull.clone().ok_or(BrowserError::UNAVAILABLE)?
        };
        let finished = async {
            outcome
                .wait_for(Option::is_some)
                .await
                .ok()
                .and_then(|value| *value)
        };
        tokio::select! {
            pulled = finished => match pulled {
                Some(true) => Ok(()),
                _ => Err(BrowserError::UNAVAILABLE),
            },
            () = waiting.ended() => Err(BrowserError::UNAVAILABLE),
        }
    }

    /// Removes one Browser and proves it absent, all by one deadline that no stalled Docker call extends.
    ///
    /// A removal another caller already has under way (Stop and the task's release race) is no failure: its end is
    /// proved like any other. Only Docker answering that the container is gone proves it.
    async fn remove(&self, id: &str) -> Result<(), BrowserError> {
        let deadline = Instant::now() + REMOVE_SECONDS;
        match timeout_at(deadline, self.host.docker.remove_container(id)).await {
            Ok(Ok(()) | Err(DockerError::NotFound | DockerError::Conflict)) => {}
            Ok(Err(DockerError::Unavailable)) | Err(_) => return Err(BrowserError::LOST),
        }
        loop {
            match timeout_at(deadline, self.host.docker.inspect_container(id)).await {
                Ok(Err(DockerError::NotFound)) => return Ok(()),
                Ok(Ok(_)) => {}
                Ok(Err(_)) | Err(_) => return Err(BrowserError::LOST),
            }
            if Instant::now() + REMOVE_POLL > deadline {
                return Err(BrowserError::LOST);
            }
            sleep(REMOVE_POLL).await;
        }
    }
}

#[cfg(test)]
mod tests;
