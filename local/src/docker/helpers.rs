//! Removal of preparation helpers: one by id when its preview ends, and every helper of a Team or of the whole Space
//! (its `prepare` ownership labels) when the Team is destroyed or the Space reset. An absent helper is a success.

use std::sync::Arc;

use team_core::future::BoxFuture;

use super::labels::{Space, filter};
use super::{DockerEngine, DockerError};
use crate::coordination::previews::{HelperRemover, RemoveFailed};
use crate::coordination::residue::{ResidueKind, TeamRef, TeamTeardown, TeardownFailed};

/// The ownership kind of a preparation helper.
pub const KIND: &str = "prepare";

/// The Space's preparation helpers.
#[derive(Clone)]
pub struct Helpers {
    docker: Arc<dyn DockerEngine>,
    space: Space,
}

impl std::fmt::Debug for Helpers {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.debug_struct("Helpers").finish_non_exhaustive()
    }
}

impl Helpers {
    /// The helpers of `space` on `docker`.
    #[must_use]
    pub fn new(docker: Arc<dyn DockerEngine>, space: Space) -> Self {
        Self { docker, space }
    }

    async fn remove_one(&self, container: &str) -> Result<(), DockerError> {
        match self.docker.remove_container(container).await {
            Ok(()) | Err(DockerError::NotFound) => Ok(()),
            Err(error) => Err(error),
        }
    }

    /// Removes every helper of `team_id`, or of the whole Space with `None`.
    ///
    /// # Errors
    /// [`DockerError`] when they could not be listed or one could not be removed.
    pub async fn remove_all(&self, team_id: Option<&str>) -> Result<(), DockerError> {
        let labels = self.space.labels(KIND, team_id);
        let helpers = self.docker.list_containers(&filter(&labels)).await?;
        for helper in helpers {
            let id = helper.id.ok_or(DockerError::Unavailable)?;
            self.remove_one(&id).await?;
        }
        Ok(())
    }
}

impl HelperRemover for Helpers {
    fn remove<'a>(&'a self, container: &'a str) -> BoxFuture<'a, Result<(), RemoveFailed>> {
        Box::pin(async move { self.remove_one(container).await.map_err(|_| RemoveFailed) })
    }
}

impl TeamTeardown for Helpers {
    fn kinds(&self) -> &'static [ResidueKind] {
        &[ResidueKind::PreparationHelpers]
    }

    fn remove_team<'a>(&'a self, team: &'a TeamRef) -> BoxFuture<'a, Result<(), TeardownFailed>> {
        Box::pin(async move {
            self.remove_all(Some(&team.team_id))
                .await
                .map_err(|_| TeardownFailed::Failed)
        })
    }

    fn remove_space(&self) -> BoxFuture<'_, Result<(), TeardownFailed>> {
        Box::pin(async move {
            self.remove_all(None)
                .await
                .map_err(|_| TeardownFailed::Failed)
        })
    }
}

#[cfg(all(test, feature = "testing"))]
mod tests {
    use std::sync::Arc;

    use bollard::models::ContainerCreateBody;

    use super::{Helpers, KIND};
    use crate::coordination::previews::HelperRemover;
    use crate::coordination::residue::{TeamRef, TeamTeardown, TeardownFailed};
    use crate::docker::DockerEngine;
    use crate::docker::labels::{Space, label_map};
    use crate::testing::FakeDocker;

    async fn helper(docker: &FakeDocker, space: &Space, name: &str, team_id: &str) {
        let body = ContainerCreateBody {
            labels: Some(label_map(&space.labels(KIND, Some(team_id)))),
            ..ContainerCreateBody::default()
        };
        docker.create_container(name, body).await.expect("created");
    }

    #[tokio::test]
    async fn prepare_helpers_are_removed_by_team_or_space_and_absence_is_success() {
        let docker = Arc::new(FakeDocker::default());
        let space = Space::new("local-space").expect("a Space");
        let other = Space::new("other-space").expect("a Space");
        helper(&docker, &space, "one", "team_1").await;
        helper(&docker, &space, "two", "team_2").await;
        helper(&docker, &other, "foreign", "team_1").await;
        let helpers = Helpers::new(docker.clone(), space);
        let team = TeamRef {
            team_id: "team_1".to_owned(),
            generation: None,
        };
        assert_eq!(helpers.remove_team(&team).await, Ok(()));
        assert!(docker.container("one").is_none());
        assert!(docker.container("two").is_some());
        assert_eq!(helpers.remove("absent").await, Ok(()));
        assert_eq!(helpers.remove_space().await, Ok(()));
        assert!(docker.container("two").is_none());
        // Another Space's helper is never touched.
        assert!(docker.container("foreign").is_some());
        docker.set_unavailable(true);
        assert_eq!(
            helpers.remove_team(&team).await,
            Err(TeardownFailed::Failed)
        );
    }
}
