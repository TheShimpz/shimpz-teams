//! One preparation segment's helper: started only when a file needs it, inspected before any file byte reaches it,
//! serving each file with a fresh worker process, replaced after a failed file, and removed when the segment ends.
//!
//! A helper is owned until its removal is proved (an absent one counts as removed): a failed removal keeps it owned,
//! a later close retries, and no replacement starts while it remains. Before a new helper is created, every earlier
//! helper of the same Team is removed, or the segment is refused.

use std::sync::Arc;

use strict_json::Value;
use team_core::entropy::{Entropy, token_hex};
use team_core::exec::ExecEngine;
use team_prepare::answer::{Reason, opaque};
use team_prepare::request::{self, Kind};

use super::exchange::exchange;
use super::helper::{self, Requested};
use crate::docker::DockerEngine;

/// What every helper of this controller shares.
#[derive(Clone)]
pub struct Helpers {
    /// The Docker daemon.
    pub docker: Arc<dyn DockerEngine>,
    /// Workload exec over the same daemon.
    pub exec: Arc<dyn ExecEngine>,
    /// The source of each helper's name suffix.
    pub entropy: Arc<dyn Entropy>,
    /// The Space every helper is labelled with.
    pub space_id: String,
    /// The controller's own container (its hostname), whose exact image the helper runs.
    pub controller: String,
    /// The CPUs the controller's workloads run on.
    pub cpuset: Option<String>,
}

impl std::fmt::Debug for Helpers {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Helpers")
            .field("space_id", &self.space_id)
            .finish_non_exhaustive()
    }
}

/// How a segment's owner follows its helper: a chat turn's Stop or a preview's teardown.
pub trait Hooks: Send + Sync {
    /// The helper `id` was created and inspected and is about to start; refuses when the segment already ended.
    ///
    /// # Errors
    /// [`Ended`] when it ended; the helper is then removed.
    fn started(&self, id: &str) -> Result<(), Ended>;

    /// The helper `id` is gone.
    fn stopped(&self, id: &str);
}

/// The segment ended (its turn was stopped or its preview ended).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Ended;

/// A segment failure.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SessionError {
    /// The segment ended.
    Ended,
    /// A helper could not be started or removed, or the controller's image is unknown.
    Unavailable,
}

/// One segment's helper.
pub struct Session<'a> {
    helpers: &'a Helpers,
    team_id: &'a str,
    hooks: &'a dyn Hooks,
    container: Option<String>,
}

impl std::fmt::Debug for Session<'_> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Session")
            .field("container", &self.container)
            .finish_non_exhaustive()
    }
}

impl<'a> Session<'a> {
    /// A segment of `team_id` that has not started a helper yet.
    #[must_use]
    pub const fn new(helpers: &'a Helpers, team_id: &'a str, hooks: &'a dyn Hooks) -> Self {
        Self {
            helpers,
            team_id,
            hooks,
            container: None,
        }
    }

    /// The helper's unvalidated answer for one file (or one page of a PDF); a failed file answers `unreadable` and
    /// its helper is replaced before the next file.
    ///
    /// # Errors
    /// [`SessionError`] when the segment ended or a helper could not be started or removed.
    pub async fn prepare(
        &mut self,
        kind: Kind,
        data: &[u8],
        page: Option<i64>,
    ) -> Result<Value, SessionError> {
        let Some(container) = self.running().await? else {
            return Ok(opaque(Reason::Unreadable));
        };
        let framed = request::encode(kind, data, page);
        if let Ok(answer) = exchange(self.helpers.exec.as_ref(), &container, &framed).await {
            Ok(answer)
        } else {
            self.close().await?;
            Ok(opaque(Reason::Unreadable))
        }
    }

    /// Removes the helper, keeping it owned until its removal or absence is proved.
    ///
    /// # Errors
    /// [`SessionError::Unavailable`] when it may still exist.
    pub async fn close(&mut self) -> Result<(), SessionError> {
        let Some(container) = self.container.clone() else {
            return Ok(());
        };
        helper::remove(self.helpers.docker.as_ref(), &container)
            .await
            .map_err(|_| SessionError::Unavailable)?;
        self.container = None;
        self.hooks.stopped(&container);
        Ok(())
    }

    /// The running helper; `None` when a new one drifted from its envelope and was removed.
    async fn running(&mut self) -> Result<Option<String>, SessionError> {
        if let Some(container) = &self.container {
            return Ok(Some(container.clone()));
        }
        let helpers = self.helpers;
        let docker = helpers.docker.as_ref();
        helper::remove_all(docker, &helpers.space_id, Some(self.team_id))
            .await
            .map_err(|_| SessionError::Unavailable)?;
        let requested = self.requested().await?;
        let id = docker
            .create_container(&requested.name, helper::body(&requested))
            .await
            .map_err(|_| SessionError::Unavailable)?;
        self.container = Some(id.clone());
        let inspected = docker.inspect_container(&id).await;
        if !inspected.is_ok_and(|found| helper::inspect(&found, &requested).is_ok()) {
            self.close().await?;
            return Ok(None);
        }
        if self.hooks.started(&id).is_err() {
            self.close().await?;
            return Err(SessionError::Ended);
        }
        if docker.start_container(&id).await.is_err() {
            self.close().await?;
            return Err(SessionError::Unavailable);
        }
        Ok(Some(id))
    }

    async fn requested(&self) -> Result<Requested, SessionError> {
        let helpers = self.helpers;
        let image = helpers
            .docker
            .inspect_container(&helpers.controller)
            .await
            .ok()
            .and_then(|own| own.image)
            .filter(|image| image.starts_with("sha256:"))
            .ok_or(SessionError::Unavailable)?;
        let suffix =
            token_hex(helpers.entropy.as_ref(), 4).map_err(|_| SessionError::Unavailable)?;
        Ok(Requested {
            image,
            name: format!("shimpz-prepare-{}-{suffix}", self.team_id),
            labels: helper::labels(&helpers.space_id, Some(self.team_id))
                .into_iter()
                .map(|(name, value)| (name.to_owned(), value))
                .collect(),
            cpuset: helpers.cpuset.clone(),
        })
    }
}

#[cfg(all(test, feature = "testing"))]
mod tests;
