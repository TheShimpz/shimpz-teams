//! The Browser network and its egress: each task runs on a fresh egress process, named by the random generation its
//! healthcheck prints, and the egress is restarted after each task.

use std::time::Duration;

use bollard::models::{ContainerInspectResponse, HealthStatusEnum, NetworkInspect};
use tokio::time::{Instant, sleep};

use super::{BrowserError, BrowserRuntime, KIND_LABEL, MANAGED_LABEL, SPACE_LABEL};
use crate::browser::envelope::NETWORK;

/// The egress container's fixed name.
pub const EGRESS: &str = "shimpz-browser-egress";
/// The egress's ownership kind.
pub const EGRESS_KIND: &str = "browser-egress";
/// How long a restarted egress has to show a new healthy process.
pub const EGRESS_SECONDS: Duration = Duration::from_secs(30);
const RESTART_GRACE: i32 = 5;
const RESTART_POLL: Duration = Duration::from_millis(200);

/// One egress process: its container, the start Docker recorded, and the random id its audit lines carry.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Generation {
    /// The egress container's id.
    pub container_id: String,
    /// When Docker started this process.
    pub started: String,
    /// The 32-hex generation its latest passing probe printed.
    pub generation: String,
}

fn printed(output: &str) -> Option<&str> {
    let generation = output.strip_suffix('\n').unwrap_or(output);
    (generation.len() == 32
        && generation
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)))
    .then_some(generation)
}

/// The running, healthy egress process, named by the generation its latest passing probe printed.
///
/// A probe that failed since, a slow one on a loaded host, names no other process: Docker keeps the egress healthy
/// until its retries run out, and a restart changes its start time.
///
/// # Errors
/// [`BrowserError::UNAVAILABLE`] when it is not running, not healthy, or printed no generation.
pub fn generation(egress: &ContainerInspectResponse) -> Result<Generation, BrowserError> {
    let state = egress.state.as_ref().ok_or(BrowserError::UNAVAILABLE)?;
    let health = state.health.as_ref().ok_or(BrowserError::UNAVAILABLE)?;
    let latest = health
        .log
        .as_deref()
        .unwrap_or_default()
        .iter()
        .rev()
        .find(|probe| probe.exit_code == Some(0))
        .and_then(|probe| probe.output.as_deref())
        .and_then(printed);
    match (
        state.running,
        health.status,
        &state.started_at,
        latest,
        &egress.id,
    ) {
        (
            Some(true),
            Some(HealthStatusEnum::HEALTHY),
            Some(started),
            Some(generation),
            Some(id),
        ) => Ok(Generation {
            container_id: id.clone(),
            started: started.clone(),
            generation: generation.to_owned(),
        }),
        _ => Err(BrowserError::UNAVAILABLE),
    }
}

impl BrowserRuntime {
    /// The static Browser network, which must be internal, IPv4 only, and isolated from the host's gateway.
    pub(super) async fn network(&self) -> Result<NetworkInspect, BrowserError> {
        let network = self
            .host
            .docker
            .inspect_network(NETWORK)
            .await
            .map_err(|_| BrowserError::UNAVAILABLE)?;
        let isolated = network.internal == Some(true)
            && network.enable_ipv6 != Some(true)
            && network
                .options
                .as_ref()
                .and_then(|options| options.get("com.docker.network.bridge.gateway_mode_ipv4"))
                .is_some_and(|mode| mode == "isolated");
        if isolated {
            Ok(network)
        } else {
            Err(BrowserError::UNAVAILABLE)
        }
    }

    /// The Space's own egress: managed, of this Space, and of the egress kind.
    pub(super) async fn egress(&self) -> Result<ContainerInspectResponse, BrowserError> {
        let egress = self
            .host
            .docker
            .inspect_container(EGRESS)
            .await
            .map_err(|_| BrowserError::UNAVAILABLE)?;
        let labels = egress
            .config
            .as_ref()
            .and_then(|config| config.labels.as_ref());
        let owned = labels.is_some_and(|labels| {
            labels.get(MANAGED_LABEL).is_some_and(|value| value == "1")
                && labels.get(SPACE_LABEL) == Some(&self.space_id)
                && labels
                    .get(KIND_LABEL)
                    .is_some_and(|value| value == EGRESS_KIND)
        });
        if owned {
            Ok(egress)
        } else {
            Err(BrowserError::UNAVAILABLE)
        }
    }

    /// Restarts the egress and waits for a new process's own healthy probe: a probe of the old one never counts.
    pub(super) async fn restart_egress(&self) -> Result<Generation, BrowserError> {
        let before = self.egress().await?;
        let earlier = generation(&before).ok();
        let id = before.id.ok_or(BrowserError::UNAVAILABLE)?;
        self.host
            .docker
            .restart_container(&id, RESTART_GRACE)
            .await
            .map_err(|_| BrowserError::UNAVAILABLE)?;
        let deadline = Instant::now() + EGRESS_SECONDS;
        loop {
            if let Ok(current) = self.egress().await.and_then(|egress| generation(&egress)) {
                let fresh = earlier.as_ref().is_none_or(|earlier| {
                    current.started != earlier.started && current.generation != earlier.generation
                });
                if fresh {
                    return Ok(current);
                }
            }
            if Instant::now() >= deadline {
                return Err(BrowserError::UNAVAILABLE);
            }
            sleep(RESTART_POLL).await;
        }
    }

    /// The egress process this task runs on: the current one, unless an earlier task used it.
    pub(super) async fn fresh_egress(&self) -> Result<Generation, BrowserError> {
        let current = generation(&self.egress().await?)?;
        let used = self
            .used
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone();
        if used.as_ref() == Some(&current) {
            self.restart_egress().await
        } else {
            Ok(current)
        }
    }
}
