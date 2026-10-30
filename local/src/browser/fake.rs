//! An in-memory Docker daemon and exec engine shaped like the Space's Browser network, for the Browser's tests.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use bollard::models::{
    ContainerConfig, ContainerCreateBody, ContainerInspectResponse, ContainerState,
    ContainerSummary, EndpointResource, EndpointSettings, Health, HealthStatusEnum,
    HealthcheckResult, ImageConfig, ImageInspect, Network, NetworkCreateRequest, NetworkInspect,
    NetworkSettings, SystemInfo,
};
use strict_json::Value;
use team_core::entropy::OsEntropy;
use team_core::exec::{ExecEngine, ExecError, ExecExit, ExecId, ExecSession, ExecSpec, FailStop};
use team_core::future::BoxFuture;
use tokio::io::{AsyncReadExt, AsyncWriteExt, DuplexStream, duplex};
use tokio::sync::mpsc;

use super::channel::tests::frame;
use super::envelope::NETWORK;
use super::runtime::{BrowserRuntime, Host};
use crate::docker::{DockerEngine, DockerError, Labels};

pub(crate) const SPACE: &str = "space-1";
pub(crate) const EGRESS_ID: &str = "egress-id";

pub(crate) fn image() -> String {
    format!("localhost/shimpz-browser@sha256:{}", "a".repeat(64))
}

pub(crate) fn published() -> String {
    format!("ghcr.io/theshimpz/shimpz-browser@sha256:{}", "b".repeat(64))
}

/// The generation the egress's `n`th process prints.
pub(crate) fn generation(n: u32) -> String {
    format!("{n:032x}")
}

/// A container inspection a test changes.
pub(crate) type Change = fn(&mut ContainerInspectResponse);

#[derive(Debug)]
struct Container {
    id: String,
    name: String,
    body: ContainerCreateBody,
    running: bool,
}

#[derive(Debug, Default)]
pub(crate) struct State {
    containers: Vec<Container>,
    next: u32,
    /// How often the egress was restarted.
    pub restarts: u32,
    /// Inspections after a restart that still show the old process.
    pub probe_lag: u32,
    lagging: u32,
    /// The egress's health log, when a test sets it.
    pub health_log: Option<Vec<HealthcheckResult>>,
    /// The egress's health once restarted.
    pub health: Option<HealthStatusEnum>,
    /// The egress's labels' Space.
    pub egress_space: Option<String>,
    /// The egress has no address on the Browser network.
    pub egress_unaddressed: bool,
    /// Members that join the Browser network once a Browser runs.
    pub strangers: Vec<String>,
    /// The Browser network's inspection.
    pub network: NetworkInspect,
    /// More members of the Browser network.
    pub extra_members: Vec<String>,
    /// Image ids by reference.
    pub images: HashMap<String, String>,
    /// Every reference pulled.
    pub pulled: Vec<String>,
    /// Failing operations: `list`, `network`, `egress`, `restart`, `create`, `start`, `remove`, `inspect`,
    /// `pull`, `image`, `survive` (a removed Browser stays).
    pub fail: HashSet<&'static str>,
    /// Operations that never answer: `remove`, `inspect`, `pull`.
    pub stall: HashSet<&'static str>,
    /// How a created Browser's inspection drifts.
    pub drift: Option<Change>,
    /// How a started Browser's inspection drifts.
    pub on_start: Option<Change>,
    /// Inspections that still show a removed Browser.
    pub linger: u32,
    /// Answer a removal with Docker's 409, as when another one is under way.
    pub conflict: bool,
    /// Every created Browser's body.
    pub created: Vec<ContainerCreateBody>,
    /// Every removal asked for.
    pub removed: Vec<String>,
}

/// The Space's Docker daemon: the egress, the Browser network, its images, and the Browsers created.
#[derive(Debug)]
pub(crate) struct Daemon(Mutex<State>);

impl Daemon {
    pub(crate) fn new() -> Self {
        let network = NetworkInspect {
            id: Some("network-id".to_owned()),
            internal: Some(true),
            enable_ipv6: Some(false),
            options: Some(HashMap::from([(
                "com.docker.network.bridge.gateway_mode_ipv4".to_owned(),
                "isolated".to_owned(),
            )])),
            ..NetworkInspect::default()
        };
        let images = HashMap::from([
            (image(), "sha256:browser".to_owned()),
            (published(), "sha256:published".to_owned()),
        ]);
        Self(Mutex::new(State {
            network,
            images,
            ..State::default()
        }))
    }

    pub(crate) fn state(&self) -> MutexGuard<'_, State> {
        self.0.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The ids of every Browser container.
    pub(crate) fn browsers(&self) -> Vec<String> {
        self.state()
            .containers
            .iter()
            .map(|container| container.id.clone())
            .collect()
    }

    /// Marks a Browser not running.
    pub(crate) fn halt(&self, id: &str) {
        for container in &mut self.state().containers {
            if container.id == id {
                container.running = false;
            }
        }
    }

    fn egress(state: &mut State) -> ContainerInspectResponse {
        let shown = if state.lagging > 0 {
            state.lagging -= 1;
            state.restarts.saturating_sub(1)
        } else {
            state.restarts
        };
        let log = state.health_log.clone().unwrap_or_else(|| {
            vec![HealthcheckResult {
                exit_code: Some(0),
                output: Some(format!("{}\n", generation(shown))),
                ..HealthcheckResult::default()
            }]
        });
        let address = (!state.egress_unaddressed).then(|| "172.30.0.2".to_owned());
        ContainerInspectResponse {
            id: Some(EGRESS_ID.to_owned()),
            state: Some(ContainerState {
                running: Some(true),
                started_at: Some(format!("started-{shown}")),
                health: Some(Health {
                    status: Some(state.health.unwrap_or(HealthStatusEnum::HEALTHY)),
                    log: Some(log),
                    ..Health::default()
                }),
                ..ContainerState::default()
            }),
            config: Some(Self::egress_config(state)),
            network_settings: Some(NetworkSettings {
                networks: Some(HashMap::from([
                    (
                        NETWORK.to_owned(),
                        EndpointSettings {
                            ip_address: address,
                            ..EndpointSettings::default()
                        },
                    ),
                    ("shimpz-egress".to_owned(), EndpointSettings::default()),
                ])),
                ..NetworkSettings::default()
            }),
            ..ContainerInspectResponse::default()
        }
    }

    fn egress_config(state: &State) -> ContainerConfig {
        ContainerConfig {
            labels: Some(HashMap::from([
                ("com.shimpz.local.managed".to_owned(), "1".to_owned()),
                (
                    "com.shimpz.local.space-id".to_owned(),
                    state
                        .egress_space
                        .clone()
                        .unwrap_or_else(|| SPACE.to_owned()),
                ),
                (
                    "com.shimpz.local.kind".to_owned(),
                    "browser-egress".to_owned(),
                ),
            ])),
            ..ContainerConfig::default()
        }
    }

    fn browser(state: &State, container: &Container) -> ContainerInspectResponse {
        let body = &container.body;
        let mut env = vec!["PATH=/usr/bin".to_owned()];
        env.extend(body.env.clone().unwrap_or_default());
        let image_id = body
            .image
            .as_ref()
            .and_then(|image| state.images.get(image))
            .cloned();
        let mut inspected = ContainerInspectResponse {
            id: Some(container.id.clone()),
            name: Some(format!("/{}", container.name)),
            image: image_id,
            state: Some(ContainerState {
                running: Some(container.running),
                ..ContainerState::default()
            }),
            host_config: body.host_config.clone(),
            mounts: Some(Vec::new()),
            config: Some(ContainerConfig {
                image: body.image.clone(),
                user: body.user.clone(),
                env: Some(env),
                labels: body.labels.clone(),
                ..ContainerConfig::default()
            }),
            network_settings: Some(NetworkSettings {
                networks: Some(HashMap::from([(
                    NETWORK.to_owned(),
                    EndpointSettings {
                        ip_address: container.running.then(|| "172.30.0.9".to_owned()),
                        ..EndpointSettings::default()
                    },
                )])),
                ..NetworkSettings::default()
            }),
            ..ContainerInspectResponse::default()
        };
        let change = if container.running {
            state.on_start
        } else {
            state.drift
        };
        if let Some(change) = change {
            change(&mut inspected);
        }
        inspected
    }

    fn reachable(&self, operation: &'static str) -> Result<MutexGuard<'_, State>, DockerError> {
        let state = self.state();
        if state.fail.contains(operation) {
            Err(DockerError::Unavailable)
        } else {
            Ok(state)
        }
    }

    fn stalled(&self, operation: &'static str) -> bool {
        self.state().stall.contains(operation)
    }
}

fn carries(labels: Option<&HashMap<String, String>>, wanted: Labels<'_>) -> bool {
    wanted.iter().all(|(name, value)| {
        labels
            .and_then(|labels| labels.get(*name))
            .is_some_and(|found| found == value)
    })
}

impl DockerEngine for Daemon {
    fn info(&self) -> BoxFuture<'_, Result<SystemInfo, DockerError>> {
        Box::pin(async { Ok(SystemInfo::default()) })
    }

    fn list_containers<'a>(
        &'a self,
        labels: Labels<'a>,
    ) -> BoxFuture<'a, Result<Vec<ContainerSummary>, DockerError>> {
        Box::pin(async move {
            let state = self.reachable("list")?;
            Ok(state
                .containers
                .iter()
                .filter(|container| carries(container.body.labels.as_ref(), labels))
                .map(|container| ContainerSummary {
                    id: Some(container.id.clone()),
                    labels: container.body.labels.clone(),
                    ..ContainerSummary::default()
                })
                .collect())
        })
    }

    fn inspect_container<'a>(
        &'a self,
        id: &'a str,
    ) -> BoxFuture<'a, Result<ContainerInspectResponse, DockerError>> {
        Box::pin(async move {
            if id == "shimpz-browser-egress" || id == EGRESS_ID {
                let mut state = self.reachable("egress")?;
                return Ok(Self::egress(&mut state));
            }
            if self.stalled("inspect") {
                std::future::pending::<()>().await;
            }
            let mut state = self.reachable("inspect")?;
            if let Some(container) = state
                .containers
                .iter()
                .find(|container| container.id == id || container.name == id)
            {
                return Ok(Self::browser(&state, container));
            }
            if state.linger > 0 {
                state.linger -= 1;
                return Ok(ContainerInspectResponse::default());
            }
            Err(DockerError::NotFound)
        })
    }

    fn create_container<'a>(
        &'a self,
        name: &'a str,
        body: ContainerCreateBody,
    ) -> BoxFuture<'a, Result<String, DockerError>> {
        Box::pin(async move {
            let mut state = self.reachable("create")?;
            state.next += 1;
            let id = format!("browser-{}", state.next);
            state.created.push(body.clone());
            state.containers.push(Container {
                id: id.clone(),
                name: name.to_owned(),
                body,
                running: false,
            });
            Ok(id)
        })
    }

    fn start_container<'a>(&'a self, id: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            let mut state = self.reachable("start")?;
            let container = state
                .containers
                .iter_mut()
                .find(|container| container.id == id)
                .ok_or(DockerError::NotFound)?;
            container.running = true;
            Ok(())
        })
    }

    fn restart_container<'a>(
        &'a self,
        _id: &'a str,
        _grace_seconds: i32,
    ) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            let mut state = self.reachable("restart")?;
            state.restarts += 1;
            state.lagging = state.probe_lag;
            Ok(())
        })
    }

    fn remove_container<'a>(&'a self, id: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            if self.stalled("remove") {
                std::future::pending::<()>().await;
            }
            let mut state = self.reachable("remove")?;
            state.removed.push(id.to_owned());
            if state.conflict {
                state.containers.retain(|container| container.id != id);
                return Err(DockerError::Conflict);
            }
            let present = state.containers.iter().any(|container| container.id == id);
            if !state.fail.contains("survive") {
                state.containers.retain(|container| container.id != id);
            }
            if present {
                Ok(())
            } else {
                Err(DockerError::NotFound)
            }
        })
    }

    fn list_networks<'a>(
        &'a self,
        _labels: Labels<'a>,
    ) -> BoxFuture<'a, Result<Vec<Network>, DockerError>> {
        Box::pin(async { Ok(Vec::new()) })
    }

    fn inspect_network<'a>(
        &'a self,
        _id: &'a str,
    ) -> BoxFuture<'a, Result<NetworkInspect, DockerError>> {
        Box::pin(async move {
            let state = self.reachable("network")?;
            let mut network = state.network.clone();
            let mut members: HashMap<String, EndpointResource> =
                HashMap::from([(EGRESS_ID.to_owned(), EndpointResource::default())]);
            for container in state
                .containers
                .iter()
                .filter(|container| container.running)
            {
                members.insert(container.id.clone(), EndpointResource::default());
            }
            let running = state.containers.iter().any(|container| container.running);
            for extra in state
                .extra_members
                .iter()
                .chain(state.strangers.iter().filter(|_| running))
            {
                members.insert(extra.clone(), EndpointResource::default());
            }
            network.containers = Some(members);
            Ok(network)
        })
    }

    fn create_network(
        &self,
        _request: NetworkCreateRequest,
    ) -> BoxFuture<'_, Result<String, DockerError>> {
        Box::pin(async { Err(DockerError::Unavailable) })
    }

    fn remove_network<'a>(&'a self, _id: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async { Err(DockerError::Unavailable) })
    }

    fn inspect_image<'a>(
        &'a self,
        reference: &'a str,
    ) -> BoxFuture<'a, Result<ImageInspect, DockerError>> {
        Box::pin(async move {
            let state = self.reachable("image")?;
            let id = state.images.get(reference).ok_or(DockerError::NotFound)?;
            Ok(ImageInspect {
                id: Some(id.clone()),
                config: Some(ImageConfig::default()),
                ..ImageInspect::default()
            })
        })
    }

    fn pull_image<'a>(&'a self, reference: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            if self.stalled("pull") {
                std::future::pending::<()>().await;
            }
            let mut state = self.reachable("pull")?;
            state.pulled.push(reference.to_owned());
            state
                .images
                .insert(reference.to_owned(), "sha256:published".to_owned());
            Ok(())
        })
    }
}

/// What one program received on stdin, once it ended.
type Received = Arc<Mutex<Vec<u8>>>;

/// What one scripted program answers: its stdout and its exit status.
pub(crate) type Answer = (Vec<u8>, i64);

#[derive(Debug, Default)]
pub(crate) struct ExecState {
    /// Scripted programs by their `browser-host` arguments; unscripted ones answer nothing with status 0.
    pub programs: HashMap<Vec<String>, Answer>,
    /// What each program received on stdin, by exec, once it ended.
    received: Vec<(Vec<String>, Received)>,
    /// Every exec: its id, arguments, and status.
    pub execs: Vec<(String, Vec<String>, Option<i64>)>,
    /// Failing operations: `exec`, `inspect`.
    pub fail: HashSet<&'static str>,
    /// Operations that never answer: `exec`, `inspect`.
    pub stall: HashSet<&'static str>,
    /// An exec that still runs, by its arguments.
    pub running: HashSet<Vec<String>>,
}

/// An attached exec's arguments and the far side of its stream.
pub(crate) type Attached = (Vec<String>, DuplexStream);

/// Exec into the Browser: scripted programs; `mcp` and `relay` hand their far side to the test.
#[derive(Debug)]
pub(crate) struct Execs {
    state: Mutex<ExecState>,
    attached: mpsc::UnboundedSender<Attached>,
}

impl Execs {
    pub(crate) fn new() -> (Self, mpsc::UnboundedReceiver<Attached>) {
        let (attached, receiver) = mpsc::unbounded_channel();
        (
            Self {
                state: Mutex::new(ExecState::default()),
                attached,
            },
            receiver,
        )
    }

    pub(crate) fn state(&self) -> MutexGuard<'_, ExecState> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Scripts `arguments` to answer `stdout` with `status`.
    pub(crate) fn script(&self, arguments: &[&str], stdout: &[u8], status: i64) {
        self.state().programs.insert(
            arguments
                .iter()
                .map(|argument| (*argument).to_owned())
                .collect(),
            (stdout.to_vec(), status),
        );
    }

    /// What the last `arguments` exec received on stdin, once it ended.
    pub(crate) fn input(&self, arguments: &[&str]) -> Vec<u8> {
        let state = self.state();
        state
            .received
            .iter()
            .rev()
            .find(|(known, _)| {
                known
                    .iter()
                    .map(String::as_str)
                    .eq(arguments.iter().copied())
            })
            .map(|(_, received)| {
                received
                    .lock()
                    .unwrap_or_else(PoisonError::into_inner)
                    .clone()
            })
            .unwrap_or_default()
    }
}

async fn play(mut far: DuplexStream, stdout: Vec<u8>, received: Arc<Mutex<Vec<u8>>>) {
    let mut input = Vec::new();
    let _ = far.read_to_end(&mut input).await;
    received
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .extend(input);
    if !stdout.is_empty() {
        let _ = far.write_all(&frame(1, &stdout)).await;
    }
}

impl ExecEngine for Execs {
    fn start_exec<'a>(
        &'a self,
        spec: &'a ExecSpec,
    ) -> BoxFuture<'a, Result<ExecSession, ExecError>> {
        Box::pin(async move {
            if self.state().stall.contains("exec") {
                std::future::pending::<()>().await;
            }
            if self.state().fail.contains("exec") {
                return Err(ExecError::Unavailable);
            }
            let arguments: Vec<String> = spec.argv[1..].to_vec();
            let (team, far) = duplex(4 * 1024 * 1024);
            let mut state = self.state();
            let id = format!("exec-{}", state.execs.len() + 1);
            let attached = arguments == ["mcp"] || arguments == ["relay"];
            let (stdout, status) = state.programs.get(&arguments).cloned().unwrap_or_default();
            state
                .execs
                .push((id.clone(), arguments.clone(), Some(status)));
            if attached {
                drop(state);
                let _ = self.attached.send((arguments, far));
            } else {
                let received = Arc::new(Mutex::new(Vec::new()));
                state.received.push((arguments, Arc::clone(&received)));
                drop(state);
                tokio::spawn(play(far, stdout, received));
            }
            Ok(ExecSession {
                id: ExecId(id),
                stream: Box::new(team),
            })
        })
    }

    fn inspect_exec<'a>(&'a self, id: &'a ExecId) -> BoxFuture<'a, Result<ExecExit, ExecError>> {
        Box::pin(async move {
            if self.state().stall.contains("inspect") {
                std::future::pending::<()>().await;
            }
            let state = self.state();
            if state.fail.contains("inspect") {
                return Err(ExecError::Unavailable);
            }
            let (_, arguments, status) = state
                .execs
                .iter()
                .find(|(known, _, _)| *known == id.0)
                .cloned()
                .ok_or(ExecError::NotFound)?;
            Ok(ExecExit {
                running: state.running.contains(&arguments),
                exit_code: status,
            })
        })
    }

    fn fail_stop<'a>(&'a self, _container: &'a str) -> BoxFuture<'a, Result<FailStop, ExecError>> {
        Box::pin(async { Ok(FailStop::Stopped) })
    }
}

/// A runtime over a fresh daemon and exec engine, with the attached `mcp` and `relay` far sides.
pub(crate) struct Fixture {
    pub(crate) daemon: Arc<Daemon>,
    pub(crate) execs: Arc<Execs>,
    pub(crate) attached: mpsc::UnboundedReceiver<Attached>,
}

impl Fixture {
    pub(crate) fn new() -> Self {
        let (execs, attached) = Execs::new();
        Self {
            daemon: Arc::new(Daemon::new()),
            execs: Arc::new(execs),
            attached,
        }
    }

    pub(crate) fn runtime(&self, image: Option<String>) -> BrowserRuntime {
        BrowserRuntime::new(
            Host {
                docker: self.daemon.clone(),
                exec: self.execs.clone(),
                entropy: Arc::new(OsEntropy),
            },
            SPACE,
            "0-3",
            image,
        )
    }
}

/// Answers one Cua Driver session on `far` as a Browser showing one page; with `replies`, it breaks after that many.
pub(crate) async fn driver(mut far: DuplexStream, mut replies: Option<usize>, windows: usize) {
    let mut pending = Vec::new();
    let mut buffer = [0_u8; 4096];
    loop {
        while let Some(end) = pending.iter().position(|byte| *byte == b'\n') {
            let line: Vec<u8> = pending.drain(..=end).collect();
            let request = strict_json::parse(&line).expect("json");
            let Some(reply) = reply(&request, windows) else {
                continue;
            };
            if replies == Some(0) {
                return;
            }
            replies = replies.map(|left| left - 1);
            let mut bytes = reply.into_bytes();
            bytes.push(b'\n');
            if far.write_all(&frame(1, &bytes)).await.is_err() {
                return;
            }
        }
        match far.read(&mut buffer).await {
            Ok(0) | Err(_) => return,
            Ok(read) => pending.extend_from_slice(&buffer[..read]),
        }
    }
}

pub(crate) fn field<'a>(value: &'a Value, key: &str) -> Option<&'a Value> {
    match value {
        Value::Object(members) => members
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value),
        _ => None,
    }
}

fn reply(request: &Value, windows: usize) -> Option<String> {
    let id = match field(request, "id")? {
        Value::Number(strict_json::Number::Int(id)) => *id,
        _ => return None,
    };
    let method = field(request, "method").and_then(|method| match method {
        Value::String(method) => Some(method.as_str()),
        _ => None,
    });
    let params = field(request, "params")?;
    let tool = field(params, "name").and_then(|name| match name {
        Value::String(name) => Some(name.as_str()),
        _ => None,
    });
    let snapshot =
        field(params, "arguments").and_then(|arguments| field(arguments, "snapshot_format"));
    let structured = match (method, tool) {
        (Some("initialize"), _) => {
            return Some(format!(r#"{{"jsonrpc":"2.0","id":{id},"result":{{}}}}"#));
        }
        (_, Some("list_windows")) => {
            let window = r#"{"pid":7,"window_id":9,"app_name":"Chromium"}"#;
            return Some(format!(
                r#"{{"jsonrpc":"2.0","id":{id},"result":{{"structuredContent":{{"windows":[{}]}},"content":[]}}}}"#,
                vec![window; windows].join(",")
            ));
        }
        (_, Some("browser_prepare")) => r#"{"status":"ok"}"#,
        (_, Some("get_browser_state")) if snapshot.is_some() => {
            r#"{"page":{"url":"https://example.com/","title":"Example"},"refs":[],"outline":"Example page"}"#
        }
        (_, Some("get_browser_state")) => {
            r#"{"binding_quality":"exact","target_id":"t1","tabs":[{"tab_id":"tab1","active":true}]}"#
        }
        _ => r#"{"effect":"ok"}"#,
    };
    Some(format!(
        r#"{{"jsonrpc":"2.0","id":{id},"result":{{"structuredContent":{structured},"content":[]}}}}"#
    ))
}
