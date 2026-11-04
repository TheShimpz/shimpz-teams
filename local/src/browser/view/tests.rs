use std::sync::Arc;
use std::time::Duration;

use team_core::audit::AuditSink;
use team_core::entropy::OsEntropy;
use team_core::future::BoxFuture;
use team_core::testing::MemoryAudit;
use team_protocol::browser_view::{BUSY, NOT_RUNNING, UNAVAILABLE};
use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _, duplex};
use tokio::time::Instant;

use super::{Refused, RelayUnavailable, Relays, View, ViewParts};
use crate::browser::audit::{Actor, Lines};
use crate::browser::easi::Upgraded as EasiStream;
use crate::browser::fake::Fake;
use crate::http::service::{Principal, Relay};

const KEY: &str = "AAAAAAAAAAAAAAAAAAAAAA==";

struct Streams {
    fake: Fake,
    client: crate::browser::easi::Easi,
}

impl Relays for Streams {
    fn open_relay<'a>(
        &'a self,
        key: &'a str,
        viewer: &'a str,
        deadline: Instant,
    ) -> BoxFuture<'a, Result<EasiStream, RelayUnavailable>> {
        Box::pin(async move {
            let _ = &self.fake;
            self.client
                .upgrade(&format!("/ws/sessions/s1?viewer={viewer}"), key, deadline)
                .await
                .map_err(|_| RelayUnavailable)
        })
    }
}

struct Case {
    view: Arc<View>,
    _state: tempfile::TempDir,
}

fn case(private: bool, controllable: bool) -> Case {
    let state = tempfile::tempdir().expect("a directory");
    let fake = Fake::default();
    fake.leave_session("s1");
    let client = fake.client(state.path());
    let audit: Arc<dyn AuditSink> = Arc::new(MemoryAudit::default());
    let actor: Actor = crate::browser::audit::request_actor(&Principal::Machine, &"a".repeat(32));
    let view = View::new(ViewParts {
        relays: Arc::new(Streams { fake, client }),
        lines: Lines::new(audit, actor, "browser-view", "team_1", "", String::new()),
        controllable,
        private,
        entropy: Arc::new(OsEntropy),
    })
    .expect("a view");
    Case {
        view,
        _state: state,
    }
}

#[tokio::test]
async fn a_watcher_s_bytes_reach_easi_run_and_come_back_untouched() {
    let case = case(false, false);
    let stream = case.view.open(KEY, false).await.expect("a stream");
    let (mut viewer, far) = duplex(4096);
    let carried = tokio::spawn(Box::new(stream).run(Box::new(far)));
    viewer.write_all(b"frame").await.expect("written");
    let mut read = [0_u8; 5];
    viewer.read_exact(&mut read).await.expect("read");
    assert_eq!(&read, b"frame");
    case.view.end();
    carried.await.expect("ended");
    assert!(
        case.view
            .quiesce(Instant::now() + Duration::from_secs(5))
            .await
    );
}

#[tokio::test]
async fn a_view_that_ended_admits_nothing_and_a_private_one_no_watcher() {
    let case_ended = case(false, false);
    case_ended.view.end();
    assert_eq!(
        case_ended
            .view
            .open(KEY, false)
            .await
            .err()
            .map(|r: Refused| r.0),
        Some(NOT_RUNNING)
    );
    let private = case(true, true);
    assert_eq!(
        private
            .view
            .open(KEY, false)
            .await
            .err()
            .map(|r: Refused| r.0),
        Some(NOT_RUNNING)
    );
}

#[tokio::test]
async fn only_a_browser_a_person_may_control_offers_control_and_to_one_person_at_a_time() {
    let watched = case(false, false);
    assert_eq!(
        watched
            .view
            .open(KEY, true)
            .await
            .err()
            .map(|r: Refused| r.0),
        Some(UNAVAILABLE)
    );
    let driven = case(true, true);
    let held = driven.view.open(KEY, true).await.expect("control");
    assert_eq!(driven.view.quiet_for(), Duration::ZERO);
    assert_eq!(
        driven
            .view
            .open(KEY, true)
            .await
            .err()
            .map(|r: Refused| r.0),
        Some(BUSY)
    );
    drop(held);
    tokio::time::timeout(Duration::from_secs(5), async {
        while driven.view.quiet_for() == Duration::ZERO {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .expect("control ended");
}
