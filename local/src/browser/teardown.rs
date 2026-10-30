//! The Browser's part of a Team's destruction and of the Space reset: its Browsers and its Sign-ins.
//!
//! A Team's destruction ends its sign-in session (keeping nothing, and fencing one still starting) and removes its
//! Browser, proving it absent; then it deletes its Sign-in, fencing a Save under way. The Space reset ends every
//! session and removes every Browser, restarting the egress; then it deletes every Sign-in, keeping only the fenced
//! records of Supervisor deletions under way until their completions are recorded.

use std::sync::Arc;

use team_core::future::BoxFuture;

use super::service::BrowserService;
use super::sign_in::SignInSessions;
use super::sign_ins::SignInStore;
use crate::coordination::residue::{ResidueKind, TeamRef, TeamTeardown, TeardownFailed};

/// The Browsers' owner in a teardown.
#[derive(Clone, Debug)]
pub struct Browsers {
    /// The Browser service.
    pub service: Arc<BrowserService>,
    /// The sign-in sessions.
    pub sessions: Arc<SignInSessions>,
}

impl TeamTeardown for Browsers {
    fn kinds(&self) -> &'static [ResidueKind] {
        &[ResidueKind::Browsers]
    }

    fn remove_team<'a>(&'a self, team: &'a TeamRef) -> BoxFuture<'a, Result<(), TeardownFailed>> {
        Box::pin(async move {
            self.sessions.end_team(&team.team_id).await;
            self.service
                .runtime()
                .remove_team(&team.team_id)
                .await
                .map_err(|_| TeardownFailed::Failed)
        })
    }

    fn remove_space(&self) -> BoxFuture<'_, Result<(), TeardownFailed>> {
        Box::pin(async move {
            self.sessions.end_all().await;
            self.service
                .runtime()
                .clean()
                .await
                .map_err(|_| TeardownFailed::Failed)
        })
    }
}

/// The Sign-ins' owner in a teardown.
#[derive(Clone, Debug)]
pub struct SignIns {
    /// The Sign-ins.
    pub store: Arc<SignInStore>,
}

impl TeamTeardown for SignIns {
    fn kinds(&self) -> &'static [ResidueKind] {
        &[ResidueKind::SignIns]
    }

    fn remove_team<'a>(&'a self, team: &'a TeamRef) -> BoxFuture<'a, Result<(), TeardownFailed>> {
        Box::pin(async move {
            match self.store.delete_team(&team.team_id) {
                Ok(true) => Ok(()),
                Ok(false) => Err(TeardownFailed::Failed),
                Err(_) => Err(TeardownFailed::StateUnavailable),
            }
        })
    }

    fn remove_space(&self) -> BoxFuture<'_, Result<(), TeardownFailed>> {
        Box::pin(async move {
            self.store
                .delete_all()
                .map_err(|_| TeardownFailed::StateUnavailable)
        })
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::{Browsers, SignIns};
    use crate::browser::sign_in::tests::Case;
    use crate::coordination::residue::{ResidueKind, TeamRef, TeamTeardown};

    fn team() -> TeamRef {
        TeamRef {
            team_id: "neo".to_owned(),
            generation: None,
        }
    }

    #[tokio::test(start_paused = true)]
    async fn a_team_s_destruction_ends_its_session_and_takes_its_browser_and_sign_in() {
        let case = Case::new().await;
        let browsers = Browsers {
            service: Arc::clone(case.sessions.service()),
            sessions: Arc::clone(&case.sessions),
        };
        let sign_ins = SignIns {
            store: Arc::clone(&case.store),
        };
        assert_eq!(browsers.kinds(), [ResidueKind::Browsers]);
        assert_eq!(sign_ins.kinds(), [ResidueKind::SignIns]);
        let started = case
            .sessions
            .start(
                "neo",
                &case.actor,
                &strict_json::Value::String("https://www.linkedin.com/login".to_owned()),
                None,
                "https://admin.example",
            )
            .await
            .expect("started");
        assert!(started.get("view_id").is_some());
        browsers.remove_team(&team()).await.expect("removed");
        assert_eq!(case.fixture.daemon.browsers(), Vec::<String>::new());
        sign_ins.remove_team(&team()).await.expect("deleted");
        assert_eq!(case.store.current("neo", None), Ok(None));
        browsers.remove_space().await.expect("reset");
        sign_ins.remove_space().await.expect("reset");
    }
}
