//! Team's client against a real easi.run (ADR-0126). It starts real browser sessions, so it never runs in an ordinary
//! test pass: bring up a throwaway installation whose admin answers on 127.0.0.1:39443 and name its admin container in
//! `EASI_LIVE_CONTAINER` and a directory for its password in `EASI_LIVE_STATE`, then run it with `--ignored`. Never point it at a Local's own easi.run.

use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;
use std::time::Duration;

use hyper::Method;
use strict_json::Value;
use team_browser::cua::CuaSession;
use team_core::entropy::OsEntropy;
use team_local::browser::easi::{CALL_SECONDS, Easi, LOOPBACK_PORT, PinnedDial};
use team_local::browser::runtime::{Acquire, AgentCalls, BrowserRuntime, Lease, Mode};
use team_local::docker::daemon::Daemon;
use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _};
use tokio::time::Instant;

const KEY: &str = "dGhlIHNhbXBsZSBub25jZQ==";

fn client(state: &Path) -> Arc<Easi> {
    let container = std::env::var("EASI_LIVE_CONTAINER").expect("EASI_LIVE_CONTAINER");
    let docker = Daemon::connect(Path::new("/var/run/docker.sock")).expect("the Docker socket");
    let dial = PinnedDial::at(Arc::new(docker), &container, ("127.0.0.1", LOOPBACK_PORT));
    Arc::new(Easi::over(
        Box::new(dial),
        Arc::new(OsEntropy),
        state.to_path_buf(),
    ))
}

fn soon() -> Instant {
    Instant::now() + CALL_SECONDS
}

async fn acquired(runtime: &BrowserRuntime, mode: Mode, profile: Option<&str>) -> Lease {
    let started = std::time::Instant::now();
    let lease = runtime
        .acquire(Acquire {
            team_id: "team_1",
            mode,
            profile,
            deadline: None,
            stop: None,
        })
        .await
        .expect("a Browser");
    println!("session start: {:?}", started.elapsed());
    lease
}

/// The closed Cua calls on a real page.
async fn opened_on_a_page(runtime: &Arc<BrowserRuntime>, lease: &Lease) -> CuaSession {
    let mut session = CuaSession::new(Box::new(AgentCalls::new(
        Arc::clone(runtime),
        lease.session_id.clone(),
        Arc::new(AtomicBool::new(false)),
    )));
    session.set_deadline(Some(Instant::now() + Duration::from_mins(3)));
    let started = std::time::Instant::now();
    session.open().await.expect("opened");
    session
        .navigate("https://example.com/")
        .await
        .expect("navigated");
    let page = session.observe(true).await.expect("observed");
    println!("open, navigate, observe: {:?}", started.elapsed());
    assert!(page.url.starts_with("https://example.com"), "{}", page.url);
    assert!(page.screenshot.is_some(), "the agent sees the screen");
    session
}

/// A watcher's stream beside the agent: past the time easi.run gives an idle administrator's session to a viewer, the
/// agent still sees the screen.
async fn watched(easi: &Easi, lease: &Lease, session: &mut CuaSession) {
    let path = format!(
        "/ws/sessions/{}?viewer={}",
        lease.session_id,
        "a".repeat(32)
    );
    let mut stream = easi.upgrade(&path, KEY, soon()).await.expect("a stream");
    let mut first = [0_u8; 2];
    stream.read_exact(&mut first).await.expect("a frame");
    tokio::time::sleep(Duration::from_secs(31)).await;
    let again = session.observe(true).await.expect("observed");
    assert!(
        again.screenshot.is_some(),
        "a viewer leaves the agent its screen"
    );
    let _ = stream.shutdown().await;
}

async fn new_profile(easi: &Easi) -> String {
    let body = Value::Object(vec![(
        "name".to_owned(),
        Value::String("team_1".to_owned()),
    )]);
    let created = easi
        .call(Method::POST, "/api/profiles", Some(&body), soon())
        .await
        .expect("an answer");
    assert!(created.ok(), "{}", created.status);
    match created.json().expect("json").get("id") {
        Some(Value::String(id)) => id.clone(),
        other => panic!("a profile id, not {other:?}"),
    }
}

async fn saved_durably(easi: &Easi, profile: &str) -> bool {
    let listed = easi
        .call(Method::GET, "/api/profiles", None, soon())
        .await
        .expect("an answer")
        .json()
        .expect("json");
    let Some(Value::Array(profiles)) = listed.get("profiles") else {
        return false;
    };
    profiles.iter().any(|listed| {
        matches!(listed.get("id"), Some(Value::String(id)) if id == profile)
            && matches!(listed.get("unsaved"), None | Some(Value::Null))
            && matches!(listed.get("used"), Some(Value::Number(_)))
    })
}

/// Deleting a profile answers that nothing is left, and a repeat too.
async fn deleted_twice(easi: &Easi, profile: &str) {
    let path = format!("/api/profiles/{profile}");
    for _ in 0..2 {
        let deleted = easi
            .call(Method::DELETE, &path, None, soon())
            .await
            .expect("an answer");
        assert!(deleted.ok() || deleted.status == 404, "{}", deleted.status);
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "it needs a throwaway easi.run on 127.0.0.1:39443"]
async fn team_drives_a_real_easi_run() {
    let state = std::env::var("EASI_LIVE_STATE").expect("EASI_LIVE_STATE");
    let easi = client(Path::new(&state));
    let runtime = Arc::new(BrowserRuntime::new(Arc::clone(&easi)));
    assert!(runtime.startup().await, "a clean start");
    let viewer = runtime.viewer().await.expect("the viewer");
    assert!(viewer.len() > 1000);
    let lease = acquired(&runtime, Mode::Agent, None).await;
    let mut session = opened_on_a_page(&runtime, &lease).await;
    assert!(runtime.files(&lease, None, None).await.is_ok());
    watched(&easi, &lease, &mut session).await;
    drop(session);
    runtime.release(&lease).await.expect("released");
    assert!(!runtime.dirty());
    let profile = new_profile(&easi).await;
    let person = Mode::Person("https://example.com/".to_owned());
    let lease = acquired(&runtime, person, Some(&profile)).await;
    runtime.release(&lease).await.expect("saved and released");
    assert!(saved_durably(&easi, &profile).await);
    deleted_twice(&easi, &profile).await;
}
