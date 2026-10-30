use std::sync::Arc;
use std::sync::atomic::Ordering;

use strict_json::Value;
use team_core::testing::FakeExec;
use team_prepare::answer::{Reason, opaque};
use team_prepare::fixtures::{self, Format};
use team_prepare::request::Kind;

use super::{Session, SessionError};
use crate::prepare::fakes::{Daemon, Recorded, Worker, helpers, script};

fn text(answer: &Value) -> Option<&str> {
    match answer.get("text") {
        Some(Value::String(text)) => Some(text),
        _ => None,
    }
}

#[tokio::test]
async fn one_helper_serves_each_file_with_a_fresh_process_and_is_removed() {
    let (daemon, exec) = (
        Daemon::running_controller().await,
        Arc::new(FakeExec::default()),
    );
    let (helpers, hooks) = (helpers(&daemon, &exec), Recorded::default());
    let mut session = Session::new(&helpers, "team_1", &hooks);
    script(&exec, Worker::Real);
    script(&exec, Worker::Real);
    let png = fixtures::image(Format::Png, 8, 8, false);
    let first = session
        .prepare(Kind::Image, &png, None)
        .await
        .expect("prepared");
    assert_eq!(first.get("type"), Some(&Value::String("image".to_owned())));
    let second = session
        .prepare(Kind::Pdf, b"%PDF-1", None)
        .await
        .expect("prepared");
    assert_eq!(text(&second), Some("PDF"));
    let [created] = &daemon.helpers().await[..] else {
        panic!("one helper")
    };
    assert!(created.starts_with("/shimpz-prepare-team_1-"));
    session.close().await.expect("removed");
    assert_eq!(daemon.helpers().await, [] as [std::string::String; 0]);
    let started = exec.started();
    assert_eq!(started.len(), 2);
    assert!(
        started
            .iter()
            .all(|spec| spec.container == started[0].container)
    );
    let id = &started[0].container;
    assert_eq!(
        hooks.seen(),
        [format!("started {id}"), format!("stopped {id}")]
    );
    // The helper started only after it was inspected and its owner admitted it.
    let events = daemon.events();
    assert_eq!(
        events
            .iter()
            .filter(|event| event.starts_with("create"))
            .count(),
        1
    );
    assert_eq!(events.last(), Some(&format!("remove {id}")));
}

#[tokio::test]
async fn a_failed_file_is_unreadable_and_its_helper_is_replaced() {
    let (daemon, exec) = (
        Daemon::running_controller().await,
        Arc::new(FakeExec::default()),
    );
    let (helpers, hooks) = (helpers(&daemon, &exec), Recorded::default());
    let mut session = Session::new(&helpers, "team_1", &hooks);
    script(&exec, Worker::Failing);
    script(&exec, Worker::Real);
    assert_eq!(
        session.prepare(Kind::Pdf, b"%PDF-1", None).await,
        Ok(opaque(Reason::Unreadable))
    );
    assert_eq!(daemon.helpers().await, [] as [std::string::String; 0]);
    let second = session
        .prepare(Kind::Pdf, b"%PDF-2", None)
        .await
        .expect("prepared");
    assert_eq!(text(&second), Some("PDF"));
    let started = exec.started();
    assert_ne!(started[0].container, started[1].container);
    session.close().await.expect("removed");
}

#[tokio::test]
async fn a_failed_removal_keeps_ownership_and_refuses_a_replacement() {
    let (daemon, exec) = (
        Daemon::running_controller().await,
        Arc::new(FakeExec::default()),
    );
    let (helpers, hooks) = (helpers(&daemon, &exec), Recorded::default());
    let mut session = Session::new(&helpers, "team_1", &hooks);
    script(&exec, Worker::Failing);
    daemon.fail_remove.store(true, Ordering::SeqCst);
    assert_eq!(
        session.prepare(Kind::Pdf, b"%PDF", None).await,
        Err(SessionError::Unavailable)
    );
    assert_eq!(session.close().await, Err(SessionError::Unavailable));
    assert_eq!(daemon.helpers().await.len(), 1);
    assert!(
        hooks
            .seen()
            .iter()
            .all(|event| !event.starts_with("stopped"))
    );
    daemon.fail_remove.store(false, Ordering::SeqCst);
    session.close().await.expect("removed at last");
    assert_eq!(daemon.helpers().await, [] as [std::string::String; 0]);
    assert_eq!(
        hooks
            .seen()
            .iter()
            .filter(|event| event.starts_with("stopped"))
            .count(),
        1
    );
    assert_eq!(
        daemon
            .events()
            .iter()
            .filter(|event| event.starts_with("create"))
            .count(),
        1
    );
}

#[tokio::test]
async fn an_earlier_helper_of_the_team_is_cleared_first_or_refuses_the_segment() {
    let (daemon, exec) = (
        Daemon::running_controller().await,
        Arc::new(FakeExec::default()),
    );
    let (helpers, hooks) = (helpers(&daemon, &exec), Recorded::default());
    let mut leftover = Session::new(&helpers, "team_1", &hooks);
    script(&exec, Worker::Real);
    leftover
        .prepare(Kind::Pdf, b"%PDF", None)
        .await
        .expect("prepared");
    // The leftover helper is never closed; the next segment removes it before it creates its own.
    let mut session = Session::new(&helpers, "team_1", &hooks);
    script(&exec, Worker::Real);
    session
        .prepare(Kind::Pdf, b"%PDF", None)
        .await
        .expect("prepared");
    assert_eq!(daemon.helpers().await.len(), 1);
    session.close().await.expect("removed");

    let mut stale = Session::new(&helpers, "team_1", &hooks);
    script(&exec, Worker::Real);
    stale
        .prepare(Kind::Pdf, b"%PDF", None)
        .await
        .expect("prepared");
    daemon.fail_remove.store(true, Ordering::SeqCst);
    let creates = daemon
        .events()
        .iter()
        .filter(|event| event.starts_with("create"))
        .count();
    let mut refused = Session::new(&helpers, "team_1", &hooks);
    assert_eq!(
        refused.prepare(Kind::Pdf, b"%PDF", None).await,
        Err(SessionError::Unavailable)
    );
    assert_eq!(
        daemon
            .events()
            .iter()
            .filter(|event| event.starts_with("create"))
            .count(),
        creates
    );
}

#[tokio::test]
async fn a_drifted_helper_is_removed_before_any_file_byte_reaches_it() {
    let (daemon, exec) = (
        Daemon::running_controller().await,
        Arc::new(FakeExec::default()),
    );
    let (helpers, hooks) = (helpers(&daemon, &exec), Recorded::default());
    let mut session = Session::new(&helpers, "team_1", &hooks);
    daemon.drift.store(true, Ordering::SeqCst);
    assert_eq!(
        session.prepare(Kind::Pdf, b"%PDF", None).await,
        Ok(opaque(Reason::Unreadable))
    );
    assert_eq!(exec.started(), [] as [team_core::exec::ExecSpec; 0]);
    assert!(
        daemon
            .events()
            .iter()
            .all(|event| !event.starts_with("start"))
    );
    assert_eq!(daemon.helpers().await, [] as [std::string::String; 0]);
    assert!(
        hooks
            .seen()
            .iter()
            .all(|event| !event.starts_with("started"))
    );
}

#[tokio::test]
async fn start_failures_ended_segments_and_an_unknown_image_are_explicit() {
    let (daemon, exec) = (
        Daemon::running_controller().await,
        Arc::new(FakeExec::default()),
    );
    let (helpers, hooks) = (helpers(&daemon, &exec), Recorded::default());
    daemon.fail_start.store(true, Ordering::SeqCst);
    let mut session = Session::new(&helpers, "team_1", &hooks);
    assert_eq!(
        session.prepare(Kind::Image, b"x", None).await,
        Err(SessionError::Unavailable)
    );
    assert_eq!(daemon.helpers().await, [] as [std::string::String; 0]);
    daemon.fail_start.store(false, Ordering::SeqCst);

    hooks.ended.store(true, Ordering::SeqCst);
    let mut ended = Session::new(&helpers, "team_1", &hooks);
    assert_eq!(
        ended.prepare(Kind::Image, b"x", None).await,
        Err(SessionError::Ended)
    );
    assert_eq!(daemon.helpers().await, [] as [std::string::String; 0]);
    hooks.ended.store(false, Ordering::SeqCst);

    daemon.fail_create.store(true, Ordering::SeqCst);
    let mut uncreated = Session::new(&helpers, "team_1", &hooks);
    assert_eq!(
        uncreated.prepare(Kind::Image, b"x", None).await,
        Err(SessionError::Unavailable)
    );
    daemon.fail_create.store(false, Ordering::SeqCst);

    let mut unknown = helpers.clone();
    unknown.controller = "missing".to_owned();
    let mut orphan = Session::new(&unknown, "team_1", &hooks);
    assert_eq!(
        orphan.prepare(Kind::Image, b"x", None).await,
        Err(SessionError::Unavailable)
    );
    daemon.fail_list.store(true, Ordering::SeqCst);
    let mut unlisted = Session::new(&helpers, "team_1", &hooks);
    assert_eq!(
        unlisted.prepare(Kind::Image, b"x", None).await,
        Err(SessionError::Unavailable)
    );
    assert_eq!(exec.started(), [] as [team_core::exec::ExecSpec; 0]);
}
