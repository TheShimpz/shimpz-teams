use std::sync::Arc;
use std::sync::atomic::Ordering;

use super::HelperTeardown;
use crate::coordination::previews::{HelperRemover, RemoveFailed};
use crate::coordination::residue::{ResidueKind, TeamRef, TeamTeardown, TeardownFailed};
use crate::prepare::fakes::Daemon;
use crate::prepare::helper::{Requested, body, labels};

async fn helper(daemon: &Daemon, name: &str, team: &str) {
    let requested = Requested {
        image: "sha256:c".to_owned(),
        name: name.to_owned(),
        labels: labels("space", Some(team))
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
        cpuset: None,
    };
    daemon.create_container_named(name, body(&requested)).await;
}

impl Daemon {
    async fn create_container_named(
        &self,
        name: &str,
        created: bollard::models::ContainerCreateBody,
    ) {
        use crate::docker::DockerEngine as _;
        self.inner
            .create_container(name, created)
            .await
            .expect("created");
    }
}

#[tokio::test]
async fn a_team_teardown_removes_its_helpers_and_the_reset_every_helper() {
    let daemon = Daemon::running_controller().await;
    for (name, team) in [("a", "team_1"), ("b", "team_2")] {
        helper(&daemon, name, team).await;
    }
    let teardown = HelperTeardown {
        docker: Arc::clone(&daemon) as Arc<dyn crate::docker::DockerEngine>,
        space_id: "space".to_owned(),
    };
    assert_eq!(teardown.kinds(), [ResidueKind::PreparationHelpers]);
    let team = TeamRef {
        team_id: "team_1".to_owned(),
        generation: None,
    };
    teardown.remove_team(&team).await.expect("removed");
    assert_eq!(daemon.helpers().await, ["/b"]);
    // Removal is idempotent: nothing left is a success.
    teardown.remove_team(&team).await.expect("still removed");
    teardown.remove_space().await.expect("removed");
    assert_eq!(daemon.helpers().await, [] as [std::string::String; 0]);
    assert!(daemon.inner.container("controller").is_some());

    helper(&daemon, "c", "team_1").await;
    daemon.fail_remove.store(true, Ordering::SeqCst);
    assert_eq!(
        teardown.remove_team(&team).await,
        Err(TeardownFailed::Failed)
    );
    assert_eq!(teardown.remove_space().await, Err(TeardownFailed::Failed));
    assert_eq!(teardown.remove("c").await, Err(RemoveFailed));
    daemon.fail_remove.store(false, Ordering::SeqCst);
    assert_eq!(teardown.remove("c").await, Ok(()));
    assert_eq!(teardown.remove("c").await, Ok(()));
}
