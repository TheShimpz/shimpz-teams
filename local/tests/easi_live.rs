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
use team_local::browser::runtime::{Acquire, AgentCalls, BrowserRuntime, Mode};
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

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "it needs a throwaway easi.run on 127.0.0.1:39443"]
async fn team_drives_a_real_easi_run() {
    let state = std::env::var("EASI_LIVE_STATE").expect("EASI_LIVE_STATE");
    let easi = client(Path::new(&state));
    let runtime = Arc::new(BrowserRuntime::new(Arc::clone(&easi)));
    assert!(runtime.startup().await, "a clean start");

    // The viewer the page draws with.
    let viewer = runtime.viewer().await.expect("the viewer");
    assert!(viewer.len() > 1000);

    // An agent's task: the closed Cua calls, on a real page.
    let started = std::time::Instant::now();
    let lease = runtime
        .acquire(Acquire {
            team_id: "team_1",
            mode: Mode::Agent,
            profile: None,
            deadline: None,
            stop: None,
        })
        .await
        .expect("a Browser");
    println!("session start: {:?}", started.elapsed());
    let uncertain = Arc::new(AtomicBool::new(false));
    let mut session = CuaSession::new(Box::new(AgentCalls::new(
        Arc::clone(&runtime),
        lease.session_id.clone(),
        uncertain,
    )));
    session.set_deadline(Some(Instant::now() + Duration::from_mins(3)));
    let opened = std::time::Instant::now();
    session.open().await.expect("opened");
    println!("open: {:?}", opened.elapsed());
    let navigated = std::time::Instant::now();
    session
        .navigate("https://example.com/")
        .await
        .expect("navigated");
    println!("navigate: {:?}", navigated.elapsed());
    let observed = std::time::Instant::now();
    let page = session.observe(true).await.expect("observed");
    println!("observe: {:?}", observed.elapsed());
    assert!(page.url.starts_with("https://example.com"), "{}", page.url);
    assert!(page.screenshot.is_some(), "the agent sees the screen");
    let files = runtime.files(&lease, None, None).await.expect("a listing");
    assert!(files.get("files").is_some());

    // A watcher's stream on Team's login, and the agent keeps working beside it.
    let mut stream = easi
        .upgrade(
            &format!(
                "/ws/sessions/{}?viewer={}",
                lease.session_id,
                "a".repeat(32)
            ),
            KEY,
            soon(),
        )
        .await
        .expect("a stream");
    let mut first = [0_u8; 2];
    stream.read_exact(&mut first).await.expect("a frame");
    let again = session
        .observe(true)
        .await
        .expect("observed beside a viewer");
    assert!(
        again.screenshot.is_some(),
        "a viewer leaves the agent its screen"
    );
    let _ = stream.shutdown().await;
    drop(session);
    runtime.release(&lease).await.expect("released");
    assert!(!runtime.dirty());

    // A person's Sign-in: a profile, a session on it, and the save easi.run reports when it stops.
    let body = Value::Object(vec![(
        "name".to_owned(),
        Value::String("team_1".to_owned()),
    )]);
    let created = easi
        .call(Method::POST, "/api/profiles", Some(&body), soon())
        .await
        .expect("an answer");
    assert!(created.ok(), "{}", created.status);
    let profile = match created.json().expect("json").get("id") {
        Some(Value::String(id)) => id.clone(),
        other => panic!("a profile id, not {other:?}"),
    };
    let person = runtime
        .acquire(Acquire {
            team_id: "team_1",
            mode: Mode::Person("https://example.com/".to_owned()),
            profile: Some(&profile),
            deadline: None,
            stop: None,
        })
        .await
        .expect("a person's Browser");
    let saving = std::time::Instant::now();
    runtime.release(&person).await.expect("saved and released");
    println!("stop and save: {:?}", saving.elapsed());
    let listed = easi
        .call(Method::GET, "/api/profiles", None, soon())
        .await
        .expect("an answer")
        .json()
        .expect("json");
    println!("profiles: {listed:?}");
    let Some(Value::Array(profiles)) = listed.get("profiles") else {
        panic!("a list of profiles")
    };
    let saved = profiles
        .iter()
        .find(|listed| matches!(listed.get("id"), Some(Value::String(id)) if *id == profile))
        .expect("the profile");
    assert!(matches!(saved.get("unsaved"), None | Some(Value::Null)));
    assert!(matches!(saved.get("used"), Some(Value::Number(_))));

    // Deleting it answers that nothing is left, and a repeat too.
    for _ in 0..2 {
        let deleted = easi
            .call(
                Method::DELETE,
                &format!("/api/profiles/{profile}"),
                None,
                soon(),
            )
            .await
            .expect("an answer");
        assert!(deleted.ok() || deleted.status == 404, "{}", deleted.status);
    }
}
