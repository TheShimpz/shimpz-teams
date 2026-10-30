use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use team_core::entropy::OsEntropy;
use team_core::future::BoxFuture;
use team_core::testing::MemoryAudit;
use tokio::io::{AsyncReadExt, AsyncWriteExt, DuplexStream, duplex};
use tokio::sync::{Notify, mpsc};
use tokio::time::Instant;

use super::{InputRelease, Refused, RelayUnavailable, Relays, View, ViewParts};
use crate::browser::audit::{Actor, Lines};
use crate::browser::channel::Channel;
use crate::browser::channel::tests::frame;
use crate::browser::credentials::Credentials;
use crate::http::service::Relay;
use team_protocol::browser_view;

const KEY: &str = "AAAAAAAAAAAAAAAAAAAAAA==";

fn admitted_answer() -> Vec<u8> {
    format!(
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\
         Sec-WebSocket-Accept: {}\r\nSec-WebSocket-Protocol: binary\r\n\r\n",
        browser_view::accept(KEY)
    )
    .into_bytes()
}

/// Relays over in-memory displays the test answers; each opened relay's far side arrives on `displays`.
struct FakeRelays {
    displays: mpsc::UnboundedSender<DuplexStream>,
    opened: Mutex<usize>,
    unavailable: AtomicBool,
    proven: AtomicBool,
    exits: Mutex<Vec<String>>,
    // Held before a relay opens, when a test wants to race it.
    gate: Mutex<Option<Arc<Notify>>>,
}

impl Relays for FakeRelays {
    fn open_relay(&self) -> BoxFuture<'_, Result<(String, Channel), RelayUnavailable>> {
        Box::pin(async move {
            let gate = self.gate.lock().expect("usable").take();
            if let Some(gate) = gate {
                gate.notified().await;
            }
            if self.unavailable.load(Ordering::SeqCst) {
                return Err(RelayUnavailable);
            }
            let (team, far) = duplex(1024 * 1024);
            let mut opened = self.opened.lock().expect("usable");
            *opened += 1;
            let _ = self.displays.send(far);
            Ok((format!("exec-{opened}"), Channel::new(Box::new(team))))
        })
    }

    fn exited<'a>(&'a self, exec_id: &'a str, _deadline: Instant) -> BoxFuture<'a, bool> {
        Box::pin(async move {
            self.exits.lock().expect("usable").push(exec_id.to_owned());
            self.proven.load(Ordering::SeqCst)
        })
    }
}

/// A release that records its credential and answers as the test sets it, optionally held until released.
#[derive(Default)]
struct FakeRelease {
    calls: Mutex<Vec<String>>,
    fails: AtomicBool,
    hold: Mutex<Option<Arc<Notify>>>,
}

impl InputRelease for FakeRelease {
    fn release<'a>(
        &'a self,
        _relays: &'a dyn Relays,
        authorization: &'a str,
    ) -> BoxFuture<'a, bool> {
        Box::pin(async move {
            self.calls
                .lock()
                .expect("usable")
                .push(authorization.to_owned());
            let hold = self.hold.lock().expect("usable").clone();
            if let Some(hold) = hold {
                hold.notified().await;
            }
            !self.fails.load(Ordering::SeqCst)
        })
    }
}

struct Case {
    relays: Arc<FakeRelays>,
    displays: mpsc::UnboundedReceiver<DuplexStream>,
    audit: Arc<MemoryAudit>,
    release: Arc<FakeRelease>,
    credentials: Arc<Credentials>,
}

impl Case {
    fn new() -> Self {
        let (sender, displays) = mpsc::unbounded_channel();
        Self {
            relays: Arc::new(FakeRelays {
                displays: sender,
                opened: Mutex::new(0),
                unavailable: AtomicBool::new(false),
                proven: AtomicBool::new(true),
                exits: Mutex::new(Vec::new()),
                gate: Mutex::new(None),
            }),
            displays,
            audit: Arc::new(MemoryAudit::default()),
            release: Arc::new(FakeRelease::default()),
            credentials: Arc::new(Credentials::draw(&OsEntropy).expect("drawn")),
        }
    }

    fn view(&self, controlled: bool, private: bool) -> Arc<View> {
        View::new(ViewParts {
            relays: self.relays.clone(),
            lines: Lines::new(
                self.audit.clone(),
                Actor::team(),
                "browser-view",
                "team-a",
                "",
                ":container=0123456789ab".to_owned(),
            ),
            credentials: controlled.then(|| self.credentials.clone()),
            private,
            entropy: Arc::new(OsEntropy),
            release: self.release.clone(),
        })
        .expect("a view")
    }

    fn details(&self) -> Vec<String> {
        self.audit
            .events()
            .iter()
            .filter_map(|event| {
                event
                    .fields()
                    .iter()
                    .find_map(|(name, value)| match (name.as_str(), value) {
                        ("detail", strict_json::Value::String(detail)) => Some(detail.clone()),
                        _ => None,
                    })
            })
            .collect()
    }

    fn exits(&self) -> Vec<String> {
        self.relays.exits.lock().expect("usable").clone()
    }
}

/// Plays the display of one relay: reads the upgrade request, answers `reply`; returns the request and the far side.
async fn answer(
    displays: &mut mpsc::UnboundedReceiver<DuplexStream>,
    reply: &[u8],
) -> (String, DuplexStream) {
    let mut far = displays.recv().await.expect("a relay opened");
    let mut request = Vec::new();
    let mut buffer = [0_u8; 4096];
    while !request.windows(4).any(|window| window == b"\r\n\r\n") {
        let read = far.read(&mut buffer).await.expect("read");
        assert!(read > 0, "the request arrives");
        request.extend_from_slice(&buffer[..read]);
    }
    far.write_all(&frame(1, reply)).await.expect("answered");
    (String::from_utf8(request).expect("ascii"), far)
}

#[tokio::test]
async fn a_viewer_is_carried_until_its_browser_ends() {
    let mut case = Case::new();
    let view = case.view(false, false);
    assert_eq!(view.id().len(), 32);
    assert!(view.id().bytes().all(|byte| byte.is_ascii_hexdigit()));
    let opening = tokio::spawn({
        let view = view.clone();
        async move { view.open(KEY, false).await }
    });
    let mut reply = admitted_answer();
    reply.extend_from_slice(b"RFB");
    let (_request, mut far) = answer(&mut case.displays, &reply).await;
    let stream = opening.await.expect("joined").expect("upgraded");
    let (team_viewer, mut viewer) = duplex(1024 * 1024);
    let running = tokio::spawn(Box::new(stream).run(Box::new(team_viewer)));
    far.write_all(&frame(1, b"pixels")).await.expect("sent");
    let mut received = vec![0_u8; 9];
    viewer.read_exact(&mut received).await.expect("received");
    assert_eq!(received, b"RFBpixels");
    view.end();
    running.await.expect("the stream ended with its view");
    assert_eq!(case.exits(), ["exec-1"]);
    assert_eq!(
        case.details(),
        [
            "started:container=0123456789ab",
            "ended:proven=1:to_viewer=9:to_display=0:container=0123456789ab"
        ]
    );
    assert_eq!(
        view.open(KEY, false).await.map(|_| ()),
        Err(Refused(browser_view::NOT_RUNNING))
    );
}

#[tokio::test]
async fn a_display_that_refuses_the_upgrade_leaves_the_viewer_a_closed_refusal() {
    let mut case = Case::new();
    let view = case.view(false, false);
    let opening = tokio::spawn({
        let view = view.clone();
        async move { view.open(KEY, false).await.map(|_| ()) }
    });
    let refused = String::from_utf8(admitted_answer())
        .expect("ascii")
        .replace("101 Switching", "403 Forbidden");
    let _far = answer(&mut case.displays, refused.as_bytes()).await;
    assert_eq!(
        opening.await.expect("joined"),
        Err(Refused(browser_view::UNAVAILABLE))
    );
    assert_eq!(case.exits(), ["exec-1"]);
}

#[tokio::test]
async fn a_relay_that_cannot_open_is_unavailable() {
    let case = Case::new();
    case.relays.unavailable.store(true, Ordering::SeqCst);
    let view = case.view(false, false);
    assert_eq!(
        view.open(KEY, false).await.map(|_| ()),
        Err(Refused(browser_view::UNAVAILABLE))
    );
    assert_eq!(case.exits(), Vec::<String>::new());
}

#[tokio::test]
async fn an_exit_it_cannot_prove_leaves_the_view_unavailable() {
    let mut case = Case::new();
    case.relays.proven.store(false, Ordering::SeqCst);
    let view = case.view(false, false);
    let opening = tokio::spawn({
        let view = view.clone();
        async move { view.open(KEY, false).await }
    });
    let (_request, far) = answer(&mut case.displays, &admitted_answer()).await;
    let stream = opening.await.expect("joined").expect("upgraded");
    drop(far);
    let (team_viewer, _viewer) = duplex(1024);
    Box::new(stream).run(Box::new(team_viewer)).await;
    assert!(
        case.details()
            .last()
            .expect("a line")
            .starts_with("ended:proven=0:")
    );
    assert!(view.unavailable());
    assert_eq!(
        view.open(KEY, false).await.map(|_| ()),
        Err(Refused(browser_view::UNAVAILABLE))
    );
}

#[tokio::test]
async fn a_browser_that_ends_while_the_relay_opens_starts_no_stream() {
    let case = Case::new();
    let gate = Arc::new(Notify::new());
    *case.relays.gate.lock().expect("usable") = Some(gate.clone());
    let view = case.view(false, false);
    let opening = tokio::spawn({
        let view = view.clone();
        async move { view.open(KEY, false).await.map(|_| ()) }
    });
    tokio::task::yield_now().await;
    view.end();
    gate.notify_one();
    assert_eq!(
        opening.await.expect("joined"),
        Err(Refused(browser_view::NOT_RUNNING))
    );
    assert_eq!(case.exits(), ["exec-1"]);
}

#[tokio::test]
async fn an_agent_s_view_offers_no_control_and_a_private_one_no_watcher() {
    let case = Case::new();
    assert_eq!(
        case.view(false, false).open(KEY, true).await.map(|_| ()),
        Err(Refused(browser_view::UNAVAILABLE))
    );
    assert_eq!(
        case.view(true, true).open(KEY, false).await.map(|_| ()),
        Err(Refused(browser_view::NOT_RUNNING))
    );
}

#[tokio::test]
async fn control_is_private_and_ends_with_every_button_released() {
    let mut case = Case::new();
    let view = case.view(true, false);
    let watching = tokio::spawn({
        let view = view.clone();
        async move { view.open(KEY, false).await }
    });
    let (watch_request, _watch_far) = answer(&mut case.displays, &admitted_answer()).await;
    let watcher = watching.await.expect("joined").expect("upgraded");
    let (watch_team, _watch_viewer) = duplex(1024);
    let watch_run = tokio::spawn(Box::new(watcher).run(Box::new(watch_team)));
    let controlling = tokio::spawn({
        let view = view.clone();
        async move { view.open(KEY, true).await }
    });
    let (control_request, _control_far) = answer(&mut case.displays, &admitted_answer()).await;
    let controller = controlling.await.expect("joined").expect("upgraded");
    // Taking control ended the watcher's stream at once.
    tokio::time::timeout(Duration::from_secs(5), watch_run)
        .await
        .expect("the watcher ended")
        .expect("joined");
    assert_eq!(
        view.open(KEY, false).await.map(|_| ()),
        Err(Refused(browser_view::BUSY))
    );
    let (control_team, _control_viewer) = duplex(1024);
    let control_run = tokio::spawn(Box::new(controller).run(Box::new(control_team)));
    view.end();
    control_run.await.expect("joined");
    assert!(watch_request.contains(&format!(
        "Authorization: {}\r\n",
        case.credentials.authorization(false).expose()
    )));
    assert!(control_request.contains(&format!(
        "Authorization: {}\r\n",
        case.credentials.authorization(true).expose()
    )));
    assert_eq!(
        *case.release.calls.lock().expect("usable"),
        [case.credentials.authorization(true).expose().to_owned()]
    );
    assert!(
        case.details()
            .contains(&"control-ended:released=1:container=0123456789ab".to_owned())
    );
    assert!(view.quiesce(Instant::now() + Duration::from_secs(5)).await);
}

#[tokio::test]
async fn a_watcher_whose_relay_opened_as_control_began_is_refused_before_any_frame() {
    let mut case = Case::new();
    let view = case.view(true, false);
    let gate = Arc::new(Notify::new());
    *case.relays.gate.lock().expect("usable") = Some(gate.clone());
    let watching = tokio::spawn({
        let view = view.clone();
        async move { view.open(KEY, false).await.map(|_| ()) }
    });
    tokio::task::yield_now().await;
    let controlling = tokio::spawn({
        let view = view.clone();
        async move { view.open(KEY, true).await }
    });
    let (_request, _far) = answer(&mut case.displays, &admitted_answer()).await;
    let controller = controlling.await.expect("joined").expect("upgraded");
    gate.notify_one();
    assert_eq!(
        watching.await.expect("joined"),
        Err(Refused(browser_view::BUSY))
    );
    view.end();
    let (team, _viewer) = duplex(1024);
    Box::new(controller).run(Box::new(team)).await;
    assert!(view.quiesce(Instant::now() + Duration::from_secs(5)).await);
}

#[tokio::test(start_paused = true)]
async fn a_private_view_quiesces_only_once_its_person_s_input_is_released() {
    let mut case = Case::new();
    let hold = Arc::new(Notify::new());
    *case.release.hold.lock().expect("usable") = Some(hold.clone());
    let view = case.view(true, true);
    let controlling = tokio::spawn({
        let view = view.clone();
        async move { view.open(KEY, true).await }
    });
    let (_request, _far) = answer(&mut case.displays, &admitted_answer()).await;
    let controller = controlling.await.expect("joined").expect("upgraded");
    let (team, _viewer) = duplex(1024);
    let control_run = tokio::spawn(Box::new(controller).run(Box::new(team)));
    view.end();
    // Control is still being released: the view is not quiet yet.
    assert!(
        !view
            .quiesce(Instant::now() + Duration::from_millis(200))
            .await
    );
    hold.notify_one();
    assert!(view.quiesce(Instant::now() + Duration::from_secs(5)).await);
    control_run.await.expect("joined");
}

#[tokio::test]
async fn a_release_that_fails_leaves_the_view_unavailable() {
    let mut case = Case::new();
    case.release.fails.store(true, Ordering::SeqCst);
    let view = case.view(true, false);
    let controlling = tokio::spawn({
        let view = view.clone();
        async move { view.open(KEY, true).await }
    });
    let (_request, far) = answer(&mut case.displays, &admitted_answer()).await;
    let controller = controlling.await.expect("joined").expect("upgraded");
    drop(far);
    let (team, _viewer) = duplex(1024);
    Box::new(controller).run(Box::new(team)).await;
    assert!(
        case.details()
            .contains(&"control-ended:released=0:container=0123456789ab".to_owned())
    );
    assert_eq!(
        view.open(KEY, false).await.map(|_| ()),
        Err(Refused(browser_view::UNAVAILABLE))
    );
    view.end();
    assert!(!view.quiesce(Instant::now() + Duration::from_secs(1)).await);
}

#[tokio::test]
async fn a_stream_never_run_is_still_finished() {
    let mut case = Case::new();
    let view = case.view(false, false);
    let opening = tokio::spawn({
        let view = view.clone();
        async move { view.open(KEY, false).await }
    });
    let _far = answer(&mut case.displays, &admitted_answer()).await;
    drop(opening.await.expect("joined").expect("upgraded"));
    assert!(view.quiesce(Instant::now() + Duration::from_secs(5)).await);
    assert_eq!(case.exits(), ["exec-1"]);
}
