//! Team's real Brain client against the Rust Brain (ADR-0124): every route Team calls, with a scripted provider.
//!
//! The Brain under test is the image `SHIMPZ_BRAIN_TEST_IMAGE` names, run with the host network so it reaches the
//! loopback provider origin this test serves. Team's own `HttpBrain` sends each request exactly as Local Team does and
//! parses each answer with its strict reader, so a body the Brain shapes differently fails here. The tests are ignored
//! in the workspace suite, which has no Brain image; the `brain-contract` live lane runs them:
//!
//!     SHIMPZ_BRAIN_TEST_IMAGE=<image> cargo test -p team-inference --test brain_wire -- --ignored

use std::collections::VecDeque;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use strict_json::Value;
use team_core::entropy::{Entropy, OsEntropy};
use team_core::secret::Secret;
use team_core::stop::Stop;
use team_inference::brain::ModelCredential;
use team_inference::client::HttpBrain;
use team_inference::route::{
    self, CapabilityCandidate, IntentRouteRequest, LifecycleContext, PlanStatus, RouteIntent,
};
use team_inference::runtime::{
    Brain, Runtime, RuntimeAction, RuntimeAssistant, RuntimeContext, TurnStatus,
};
use team_inference::usage::Unmetered;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

const KEY: &str = "test-provider-key-0000";
const MODEL: &str = "gpt-6-luna";
const HEALTH: Duration = Duration::from_secs(30);

fn json(text: &str) -> Value {
    strict_json::parse(text.as_bytes()).expect("test JSON")
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

/// The scripted provider origin: each request takes the next answer.
#[derive(Clone, Default)]
struct Origin(Arc<Mutex<VecDeque<String>>>);

impl Origin {
    fn script(&self, answers: impl IntoIterator<Item = String>) {
        self.0.lock().expect("script").extend(answers);
    }

    async fn serve(self) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("origin");
        let port = listener.local_addr().expect("address").port();
        tokio::spawn(async move {
            while let Ok((mut stream, _)) = listener.accept().await {
                let origin = self.clone();
                tokio::spawn(async move {
                    let _ = read_request(&mut stream).await;
                    let body = origin.0.lock().expect("script").pop_front();
                    let (status, body) = body.map_or_else(
                        || (500, r#"{"error":{"type":"x"}}"#.to_owned()),
                        |body| (200, body),
                    );
                    let reply = format!(
                        "HTTP/1.1 {status} X\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                        body.len()
                    );
                    let _ = stream.write_all(reply.as_bytes()).await;
                    let _ = stream.shutdown().await;
                });
            }
        });
        port
    }
}

/// Reads one request: its head, then exactly its declared body.
async fn read_request(stream: &mut tokio::net::TcpStream) -> std::io::Result<()> {
    let mut seen = Vec::new();
    let mut buffer = [0_u8; 8192];
    loop {
        let read = stream.read(&mut buffer).await?;
        if read == 0 {
            return Ok(());
        }
        seen.extend_from_slice(&buffer[..read]);
        if let Some(end) = seen.windows(4).position(|window| window == b"\r\n\r\n") {
            let head = String::from_utf8_lossy(&seen[..end]).to_ascii_lowercase();
            let length = head
                .lines()
                .find_map(|line| line.strip_prefix("content-length:"))
                .and_then(|value| value.trim().parse::<usize>().ok())
                .unwrap_or(0);
            while seen.len() < end + 4 + length {
                let read = stream.read(&mut buffer).await?;
                if read == 0 {
                    return Ok(());
                }
                seen.extend_from_slice(&buffer[..read]);
            }
            return Ok(());
        }
    }
}

fn openai(output: &str) -> String {
    format!(
        r#"{{"id":"resp_wire","object":"response","status":"completed","output":{output},"usage":{{"input_tokens":12,"output_tokens":3,"total_tokens":15,"input_tokens_details":{{"cached_tokens":0}}}}}}"#
    )
}

fn message(value: &str) -> String {
    let content = Value::String(value.to_owned());
    let encoded =
        team_protocol::python::json::dumps(&content, team_protocol::python::json::CANONICAL);
    openai(&format!(
        r#"[{{"type":"message","id":"msg_wire","role":"assistant","status":"completed","content":[{{"type":"output_text","text":{encoded},"annotations":[]}}]}}]"#
    ))
}

fn call(name: &str, arguments: &str) -> String {
    let arguments = Value::String(arguments.to_owned());
    let encoded =
        team_protocol::python::json::dumps(&arguments, team_protocol::python::json::CANONICAL);
    openai(&format!(
        r#"[{{"type":"function_call","id":"fc_wire","call_id":"call_wire","name":"{name}","status":"completed","arguments":{encoded}}}]"#
    ))
}

/// The Brain container under test and Team's client of it; the container is removed when this drops.
struct Running {
    name: String,
    client: HttpBrain,
    _directory: tempfile::TempDir,
}

impl Drop for Running {
    fn drop(&mut self) {
        let _ = Command::new("docker")
            .args(["rm", "--force", &self.name])
            .output();
    }
}

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .and_then(|listener| listener.local_addr())
        .expect("port")
        .port()
}

fn hex_of(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    bytes.iter().fold(String::new(), |mut out, byte| {
        let _ = write!(out, "{byte:02x}");
        out
    })
}

fn hex(entropy: &dyn Entropy) -> String {
    let mut bytes = [0_u8; 6];
    entropy.fill(&mut bytes).expect("entropy");
    hex_of(&bytes)
}

/// Team's token in its custody for the client, and the same bytes readable by the Brain's identity for its mount.
fn tokens(root: &Path) -> (PathBuf, u32, PathBuf) {
    let path = root.join("runtime").join("token");
    let group = rustix::process::getegid().as_raw();
    let token = team_inference::token::ensure(&path, group, &OsEntropy).expect("token");
    let mounted = root.join("brain-token");
    std::fs::write(&mounted, token.expose()).expect("mounted token");
    std::fs::set_permissions(&mounted, std::fs::Permissions::from_mode(0o644)).expect("mode");
    (path, group, mounted)
}

/// Starts the Brain image under test as `name` on `listen`, with `mounted` as its token and `origin` as its provider.
fn start(image: &str, name: &str, listen: &str, (mounted, origin): (&Path, u16)) {
    let started = Command::new("docker")
        .args([
            "run",
            "--rm",
            "--detach",
            "--name",
            name,
            "--network",
            "host",
        ])
        .args([
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
        ])
        .args(["--user", "10001:10001"])
        .args([
            "--tmpfs",
            "/var/lib/shimpz-brain-runtime:uid=10001,gid=10001,mode=0700",
        ])
        .args([
            "--volume",
            &format!("{}:/run/shimpz-brain-runtime/token:ro", mounted.display()),
        ])
        .args([image, "serve", "--listen", listen, "--provider-origin"])
        .arg(format!("http://127.0.0.1:{origin}"))
        .output()
        .expect("docker run");
    assert!(started.status.success(), "the Brain did not start");
}

async fn brain(origin: u16) -> Running {
    let image = std::env::var("SHIMPZ_BRAIN_TEST_IMAGE")
        .expect("SHIMPZ_BRAIN_TEST_IMAGE must name the Brain image under test");
    let directory = tempfile::tempdir().expect("tempdir");
    let (path, group, mounted) = tokens(directory.path());
    let listen = format!("127.0.0.1:{}", free_port());
    let name = format!("brain-wire-{}", hex(&OsEntropy));
    start(&image, &name, &listen, (&mounted, origin));
    let deadline = Instant::now() + HEALTH;
    while Instant::now() < deadline && tokio::net::TcpStream::connect(&listen).await.is_err() {
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let client = HttpBrain::new(&format!("http://{listen}"), path, group).expect("client");
    Running {
        name,
        client,
        _directory: directory,
    }
}

fn credential() -> ModelCredential {
    ModelCredential {
        provider: "openai".to_owned(),
        model: MODEL.to_owned(),
        api_key: Secret::new(KEY.to_owned()),
    }
}

fn context(thread: &str) -> RuntimeContext {
    let schema = json(r#"{"type":"object","properties":{},"additionalProperties":false}"#);
    let action = RuntimeAction {
        id: "list-zones".to_owned(),
        summary: "List the user's DNS zones.".to_owned(),
        input_schema: schema.clone(),
        authorization: false,
        input_files: Vec::new(),
        output_schema: schema,
    };
    RuntimeContext {
        thread_id: thread.to_owned(),
        team_name: "Fixture Team".to_owned(),
        assistants: vec![RuntimeAssistant {
            id: "dns".to_owned(),
            genesis: "DNS manages the user's zones.".to_owned(),
            actions: vec![action],
        }],
        credential: credential(),
        memories: None,
        routines: None,
        routine_question: None,
        routine_mode: false,
        routine_rerun: None,
        routine_schedule: None,
        routine_output: None,
        routine_settings_answer: false,
        routine_selected: None,
        locale: None,
        turn_clock: None,
        attachments: Vec::new(),
        browser: false,
    }
}

fn envelope(message: &str) -> String {
    format!(r#"{{"files":[],"message":"{message}"}}"#)
}

/// The Brain's tool name of the `dns` Assistant's `list-zones` Action.
fn tool() -> String {
    use sha2::{Digest, Sha256};
    let digest = Sha256::digest(b"dns\0list-zones");
    let short = hex_of(&digest[..8]);
    format!("a_dns__a_list-zones__{short}")
}

fn runtime(running: &Running) -> Brain<'_> {
    Brain {
        client: &running.client,
        usage: &Unmetered,
    }
}

#[tokio::test]
#[ignore = "needs the Brain image under test (SHIMPZ_BRAIN_TEST_IMAGE)"]
async fn a_turn_suspends_on_an_action_and_completes_on_its_result() {
    let origin = Origin::default();
    origin.script([call(&tool(), "{}"), message("You have one zone.")]);
    let running = brain(origin.clone().serve().await).await;
    let brain = runtime(&running);
    let stop = Stop::new();
    let context = context("wire-1");
    let started = brain
        .start(&context, &envelope("List my zones"), &[], &stop)
        .await
        .expect("turn");
    assert_eq!(started.status, TurnStatus::ActionRequired);
    let request = &started.actions[0];
    assert_eq!(
        (request.assistant_id.as_str(), request.action.as_str()),
        ("dns", "list-zones")
    );
    let results = Value::Object(vec![(
        request.interrupt_id.clone(),
        json(r#"{"zones":["a"]}"#),
    )]);
    let resumed = brain
        .resume(&context, results, &stop)
        .await
        .expect("resumed");
    assert_eq!(
        (resumed.status, resumed.reply.as_str()),
        (TurnStatus::Completed, "You have one zone.")
    );
    brain.delete_thread("wire-1", &stop).await.expect("deleted");
}

#[tokio::test]
#[ignore = "needs the Brain image under test (SHIMPZ_BRAIN_TEST_IMAGE)"]
async fn a_clarification_ends_the_turn_in_its_closed_shape() {
    let origin = Origin::default();
    let question = r#"{"question":"Which zone?","options":[{"label":"Production","description":""},{"label":"Staging","description":""}],"default_index":0}"#;
    origin.script([call("shimpz_clarify", question)]);
    let running = brain(origin.clone().serve().await).await;
    let turn = runtime(&running)
        .start(
            &context("wire-2"),
            &envelope("Change the zone"),
            &[],
            &Stop::new(),
        )
        .await
        .expect("turn");
    assert_eq!(turn.status, TurnStatus::Completed);
    let clarification = turn.clarification.expect("clarification");
    assert_eq!(clarification.get("question"), Some(&text("Which zone?")));
}

#[tokio::test]
#[ignore = "needs the Brain image under test (SHIMPZ_BRAIN_TEST_IMAGE)"]
async fn a_memory_proposal_returns_its_confirmed_change() {
    let origin = Origin::default();
    let proposal = r#"{"op":"remember","topic":"tone","quote":"always answer briefly"}"#;
    origin.script([
        call("shimpz_memory", proposal),
        message("Noted."),
        message(r#"{"lasting":[true]}"#),
    ]);
    let running = brain(origin.clone().serve().await).await;
    let context = RuntimeContext {
        memories: Some(Vec::new()),
        ..context("wire-3")
    };
    let turn = runtime(&running)
        .start(
            &context,
            &envelope("From now on, always answer briefly"),
            &[],
            &Stop::new(),
        )
        .await
        .expect("turn");
    assert_eq!(
        turn.memory,
        vec![json(
            r#"{"op":"remember","topic":"tone","preference":"always answer briefly"}"#
        )]
    );
}

#[tokio::test]
#[ignore = "needs the Brain image under test (SHIMPZ_BRAIN_TEST_IMAGE)"]
async fn a_browser_call_suspends_alone() {
    let origin = Origin::default();
    origin.script([call(
        "browser",
        r#"{"objective":"Find the release notes of example.com"}"#,
    )]);
    let running = brain(origin.clone().serve().await).await;
    let context = RuntimeContext {
        browser: true,
        ..context("wire-4")
    };
    let turn = runtime(&running)
        .start(&context, &envelope("Search the web"), &[], &Stop::new())
        .await
        .expect("turn");
    assert_eq!(turn.status, TurnStatus::BrowserRequired);
    assert_eq!(
        turn.browser.expect("browser").objective,
        "Find the release notes of example.com"
    );
}

#[tokio::test]
#[ignore = "needs the Brain image under test (SHIMPZ_BRAIN_TEST_IMAGE)"]
async fn a_purpose_is_written_for_the_pending_action() {
    let origin = Origin::default();
    origin.script([
        call(&tool(), "{}"),
        message(r#"{"purpose":"To list your zones."}"#),
    ]);
    let running = brain(origin.clone().serve().await).await;
    let brain = runtime(&running);
    let (context, stop) = (context("wire-5"), Stop::new());
    let started = brain
        .start(&context, &envelope("List my zones"), &[], &stop)
        .await
        .expect("turn");
    let purpose = brain
        .purpose(
            &context,
            &started.actions[0],
            "DNS",
            "List the user's DNS zones.",
            &stop,
        )
        .await;
    assert_eq!(purpose.as_deref(), Some("To list your zones."));
}

#[tokio::test]
#[ignore = "needs the Brain image under test (SHIMPZ_BRAIN_TEST_IMAGE)"]
async fn a_capability_plan_and_an_intent_route_answer_in_their_shapes() {
    let origin = Origin::default();
    origin.script([
        message(r#"{"status":"sufficient","assistant_ids":[]}"#),
        message(r#"{"intent":"ordinary-task","query":"","assistant_ids":[],"reply":"","task_follows":false}"#),
    ]);
    let running = brain(origin.clone().serve().await).await;
    let (stop, credential) = (Stop::new(), credential());
    let candidate = CapabilityCandidate {
        id: "dns".to_owned(),
        name: "DNS".to_owned(),
        summary: "Manage zones.".to_owned(),
        actions: vec!["list-zones".to_owned()],
        integrations: Vec::new(),
    };
    let plan = route::capability_plan(
        &running.client,
        &Unmetered,
        &credential,
        "Say hello",
        &[candidate],
        &stop,
    )
    .await
    .expect("plan");
    assert_eq!(plan.status, PlanStatus::Sufficient);
    let request = IntentRouteRequest {
        objective: "Say hello".to_owned(),
        expected: None,
        candidates: Vec::new(),
        context: LifecycleContext {
            locale: Some("en".to_owned()),
            ..LifecycleContext::default()
        },
        installable: Vec::new(),
    };
    let routed = route::intent_route(
        &running.client,
        &Unmetered,
        &credential,
        None,
        &request,
        &stop,
    )
    .await
    .expect("route");
    assert_eq!(routed.intent, RouteIntent::OrdinaryTask);
}
