use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

use team_core::exec::{ExecError, FailStop};
use team_core::future::BoxFuture;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{UnixListener, UnixStream};
use tokio::time::timeout;

use super::{ContainerStop, START_BODY, attach, fail_stop};
use crate::docker::DockerError;

const EXEC_ID: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
const UPGRADED: &[u8] =
    b"HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.raw-stream\r\n\
Connection: Upgrade\r\nUpgrade: tcp\r\n\r\n";

/// A stand-in daemon socket in a fresh directory.
fn socket() -> (tempfile::TempDir, PathBuf, UnixListener) {
    let directory = tempfile::tempdir().expect("a temporary directory");
    let path = directory.path().join("docker.sock");
    let listener = UnixListener::bind(&path).expect("the socket binds");
    (directory, path, listener)
}

/// Reads one request: its head (up to the blank line) and its `Content-Length` body.
async fn request(stream: &mut UnixStream) -> (String, Vec<u8>) {
    let mut head = Vec::new();
    while !head.ends_with(b"\r\n\r\n") {
        head.push(stream.read_u8().await.expect("the request head"));
    }
    let head = String::from_utf8(head).expect("an ASCII head");
    let length = head
        .lines()
        .find_map(|line| {
            line.to_ascii_lowercase()
                .strip_prefix("content-length: ")
                .map(|value| value.parse::<usize>().expect("a length"))
        })
        .unwrap_or(0);
    let mut body = vec![0; length];
    stream
        .read_exact(&mut body)
        .await
        .expect("the request body");
    (head, body)
}

fn frame(stream: u8, payload: &[u8]) -> Vec<u8> {
    let mut frame = vec![stream, 0, 0, 0];
    frame.extend(u32::try_from(payload.len()).expect("small").to_be_bytes());
    frame.extend(payload);
    frame
}

#[tokio::test]
async fn exec_half_close_delivers_eof() {
    let (_directory, path, listener) = socket();
    let daemon = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.expect("one connection");
        let (head, body) = request(&mut stream).await;
        // The answer and the first raw bytes arrive in one write: none may be lost to the HTTP layer.
        let mut answer = UPGRADED.to_vec();
        answer.extend(frame(1, b"ready"));
        stream.write_all(&answer).await.expect("the upgrade answer");
        let mut stdin = Vec::new();
        stream
            .read_to_end(&mut stdin)
            .await
            .expect("stdin until its end");
        stream.write_all(&frame(1, &stdin)).await.expect("the echo");
        (head, body)
    });
    let mut attached = attach(&path, EXEC_ID).await.expect("attached");
    let mut ready = [0; 13];
    attached
        .read_exact(&mut ready)
        .await
        .expect("the early frame");
    assert_eq!(ready.as_slice(), frame(1, b"ready").as_slice());
    attached.write_all(b"ping").await.expect("stdin");
    attached.shutdown().await.expect("half-closed");
    let mut echoed = Vec::new();
    timeout(Duration::from_secs(5), attached.read_to_end(&mut echoed))
        .await
        .expect("the daemon saw the end of stdin")
        .expect("read");
    assert_eq!(echoed, frame(1, b"ping"));
    let (head, body) = daemon.await.expect("the daemon");
    assert!(head.starts_with(&format!("POST /exec/{EXEC_ID}/start HTTP/1.1\r\n")));
    let lower = head.to_ascii_lowercase();
    assert!(lower.contains("\r\nupgrade: tcp\r\n"));
    assert!(lower.contains("\r\nconnection: upgrade\r\n"));
    assert_eq!(body, START_BODY);
}

#[tokio::test]
async fn an_announced_frame_beyond_any_budget_reaches_the_parser_unbuffered() {
    // The bounded parser of each consumer refuses a frame over its budget; the engine must hand it the header at
    // once instead of waiting to hold an announced 4 GiB frame.
    let (_directory, path, listener) = socket();
    let (release, released) = tokio::sync::oneshot::channel::<()>();
    let daemon = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.expect("one connection");
        let _request = request(&mut stream).await;
        let mut answer = UPGRADED.to_vec();
        answer.extend([1, 0, 0, 0, 0xff, 0xff, 0xff, 0xff, b'a', b'b', b'c']);
        stream.write_all(&answer).await.expect("the answer");
        // The connection stays open: the rest of the announced frame never comes.
        let _ = released.await;
    });
    let mut attached = attach(&path, EXEC_ID).await.expect("attached");
    let mut header = [0; 11];
    timeout(Duration::from_secs(5), attached.read_exact(&mut header))
        .await
        .expect("the header arrives without the frame")
        .expect("read");
    assert_eq!(&header[..8], &[1, 0, 0, 0, 0xff, 0xff, 0xff, 0xff]);
    assert_eq!(&header[8..], b"abc");
    drop(release);
    daemon.await.expect("the daemon");
}

async fn answering(head: &'static [u8]) -> Result<(), ExecError> {
    let (_directory, path, listener) = socket();
    let daemon = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.expect("one connection");
        let _request = request(&mut stream).await;
        stream.write_all(head).await.expect("the answer");
    });
    let attached = attach(&path, EXEC_ID).await.map(|_| ());
    daemon.await.expect("the daemon");
    attached
}

#[tokio::test]
async fn only_a_101_attaches_and_an_unknown_exec_is_not_found() {
    assert_eq!(answering(UPGRADED).await, Ok(()));
    assert_eq!(
        answering(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n").await,
        Err(ExecError::NotFound)
    );
    assert_eq!(
        answering(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n").await,
        Err(ExecError::Unavailable)
    );
    assert_eq!(
        answering(b"HTTP/1.1 409 Conflict\r\nContent-Length: 0\r\n\r\n").await,
        Err(ExecError::Unavailable)
    );
    // A malformed id never reaches a request path, and an absent daemon is unavailable.
    let missing = Path::new("/nonexistent/docker.sock");
    assert_eq!(
        attach(missing, "../containers/x").await.err(),
        Some(ExecError::NotFound)
    );
    assert_eq!(
        attach(missing, EXEC_ID).await.err(),
        Some(ExecError::Unavailable)
    );
}

/// A scripted daemon for fail-stop: each call takes its next scripted answer and is recorded.
#[derive(Default)]
struct Scripted {
    stops: Mutex<VecDeque<Result<(), DockerError>>>,
    kills: Mutex<VecDeque<Result<(), DockerError>>>,
    states: Mutex<VecDeque<Result<Option<bool>, DockerError>>>,
    calls: Mutex<Vec<String>>,
}

impl Scripted {
    fn new(
        stop: Result<(), DockerError>,
        kill: Result<(), DockerError>,
        states: &[Result<Option<bool>, DockerError>],
    ) -> Self {
        Self {
            stops: Mutex::new(VecDeque::from([stop])),
            kills: Mutex::new(VecDeque::from([kill])),
            states: Mutex::new(states.iter().copied().collect()),
            calls: Mutex::default(),
        }
    }

    fn calls(&self) -> Vec<String> {
        self.calls.lock().expect("usable").clone()
    }

    fn record(&self, call: String) {
        self.calls.lock().expect("usable").push(call);
    }
}

impl ContainerStop for Scripted {
    fn stop<'a>(
        &'a self,
        container: &'a str,
        grace_seconds: i32,
    ) -> BoxFuture<'a, Result<(), DockerError>> {
        self.record(format!("stop {container} {grace_seconds}"));
        let answer = self.stops.lock().expect("usable").pop_front();
        Box::pin(async move { answer.expect("a scripted stop") })
    }

    fn kill<'a>(&'a self, container: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        self.record(format!("kill {container}"));
        let answer = self.kills.lock().expect("usable").pop_front();
        Box::pin(async move { answer.expect("a scripted kill") })
    }

    fn running<'a>(
        &'a self,
        container: &'a str,
    ) -> BoxFuture<'a, Result<Option<bool>, DockerError>> {
        self.record(format!("inspect {container}"));
        let answer = self.states.lock().expect("usable").pop_front();
        Box::pin(async move { answer.expect("a scripted inspection") })
    }
}

#[tokio::test]
async fn exec_fail_stop_proves_not_running_or_blocks() {
    use DockerError::{NotFound, Unavailable};

    // An absent container is stopped; a container proven stopped is never killed.
    let gone = Scripted::new(Err(NotFound), Ok(()), &[]);
    assert_eq!(fail_stop(&gone, "c").await, Ok(FailStop::Stopped));
    assert_eq!(gone.calls(), ["stop c 3"]);
    let stopped = Scripted::new(Ok(()), Ok(()), &[Ok(Some(false))]);
    assert_eq!(fail_stop(&stopped, "c").await, Ok(FailStop::Stopped));
    assert_eq!(stopped.calls(), ["stop c 3", "inspect c"]);

    // A failed stop proves nothing: the container is killed, then proven stopped (or absent).
    for proof in [Ok(Some(false)), Err(NotFound)] {
        let killed = Scripted::new(Err(Unavailable), Ok(()), &[Ok(Some(true)), proof]);
        assert_eq!(fail_stop(&killed, "c").await, Ok(FailStop::Stopped));
        assert_eq!(
            killed.calls(),
            ["stop c 3", "inspect c", "kill c", "inspect c"]
        );
    }
    let vanished = Scripted::new(Ok(()), Err(NotFound), &[Err(Unavailable)]);
    assert_eq!(fail_stop(&vanished, "c").await, Ok(FailStop::Stopped));

    // Still running, or a state Docker does not report, after the kill is blocked; an unreachable daemon at the
    // last proof is a failure, never a stop.
    for state in [Ok(Some(true)), Ok(None)] {
        let running = Scripted::new(Ok(()), Ok(()), &[Ok(Some(true)), state]);
        assert_eq!(fail_stop(&running, "c").await, Ok(FailStop::Blocked));
    }
    let failed_kill = Scripted::new(Ok(()), Err(Unavailable), &[Ok(None), Ok(Some(true))]);
    assert_eq!(fail_stop(&failed_kill, "c").await, Ok(FailStop::Blocked));
    let unreachable = Scripted::new(
        Err(Unavailable),
        Err(Unavailable),
        &[Err(Unavailable), Err(Unavailable)],
    );
    assert_eq!(
        fail_stop(&unreachable, "c").await,
        Err(ExecError::Unavailable)
    );
}

/// The real daemon's half-close, run by `cargo test -- --ignored docker_` (the umbrella's live controller-snapshot
/// lane) on a host with Docker and the image named by `SHIMPZ_TEST_EXEC_IMAGE` (any local image with `cat` and
/// `sleep`; `python:3-alpine` by default).
#[tokio::test]
#[ignore = "needs a Docker daemon"]
async fn docker_exec_half_close_delivers_eof() {
    use team_core::exec::{ExecEngine, ExecSpec};

    use crate::docker::daemon::{Daemon, SOCKET};

    let daemon = Daemon::connect(Path::new(SOCKET)).expect("the daemon");
    let (id, _cleanup) = sleeping_container(&daemon).await;
    let spec = ExecSpec {
        container: id.clone(),
        argv: vec!["cat".to_owned()],
        user: "65534:65534".to_owned(),
        working_dir: Some("/tmp".to_owned()),
        env: Vec::new(),
        stdin: true,
    };
    let mut session = daemon.start_exec(&spec).await.expect("exec started");
    session.stream.write_all(b"ping").await.expect("stdin");
    session.stream.shutdown().await.expect("half-closed");
    let mut output = Vec::new();
    timeout(
        Duration::from_secs(10),
        session.stream.read_to_end(&mut output),
    )
    .await
    .expect("cat saw the end of its stdin and exited")
    .expect("read");
    assert_eq!(output, frame(1, b"ping"));
    let mut exit = daemon.inspect_exec(&session.id).await.expect("inspected");
    for _ in 0..50 {
        if !exit.running {
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
        exit = daemon.inspect_exec(&session.id).await.expect("inspected");
    }
    assert_eq!(exit.exit_code, Some(0));
    assert_eq!(daemon.fail_stop(&id).await, Ok(FailStop::Stopped));
}

/// A started container that sleeps, networkless and unprivileged, and its guard.
async fn sleeping_container(daemon: &crate::docker::daemon::Daemon) -> (String, Cleanup) {
    use bollard::models::{ContainerCreateBody, HostConfig};

    use crate::docker::DockerEngine;

    let image =
        std::env::var("SHIMPZ_TEST_EXEC_IMAGE").unwrap_or_else(|_| "python:3-alpine".to_owned());
    let name = format!("shimpz-team-exec-test-{}", std::process::id());
    let body = ContainerCreateBody {
        image: Some(image),
        cmd: Some(vec!["sleep".to_owned(), "300".to_owned()]),
        user: Some("65534:65534".to_owned()),
        host_config: Some(HostConfig {
            network_mode: Some("none".to_owned()),
            ..HostConfig::default()
        }),
        ..ContainerCreateBody::default()
    };
    let id = daemon.create_container(&name, body).await.expect("created");
    // Removed even when an assertion fails.
    let cleanup = Cleanup(name);
    daemon.start_container(&id).await.expect("started");
    (id, cleanup)
}

struct Cleanup(String);

impl Drop for Cleanup {
    fn drop(&mut self) {
        let _ = std::process::Command::new("docker")
            .args(["rm", "-f", &self.0])
            .output();
    }
}
