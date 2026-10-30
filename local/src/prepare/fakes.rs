//! Test doubles for the preparation modules: a daemon with injectable faults over the in-memory one, and helper
//! workloads that answer like the real worker.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, PoisonError};

use bollard::models::{
    ContainerCreateBody, ContainerInspectResponse, ContainerSummary, ImageInspect, Network,
    NetworkCreateRequest, NetworkInspect, SystemInfo,
};
use strict_json::Value;
use team_core::exec::ExecExit;
use team_core::future::BoxFuture;
use team_core::testing::{CountingEntropy, FakeExec, ScriptedExec};
use team_prepare::answer::Answer;
use team_prepare::request;
use tokio::io::{AsyncReadExt, AsyncWriteExt, duplex};

use super::session::{Ended, Helpers, Hooks};
use crate::docker::{DockerEngine, DockerError, Labels};
use crate::testing::FakeDocker;

/// The controller's own image.
pub const IMAGE: &str = "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";

/// The in-memory daemon with faults a test switches on, recording every helper lifecycle call.
#[derive(Debug, Default)]
pub struct Daemon {
    pub inner: FakeDocker,
    pub fail_create: AtomicBool,
    pub fail_start: AtomicBool,
    pub fail_remove: AtomicBool,
    pub fail_list: AtomicBool,
    pub drift: AtomicBool,
    pub events: Mutex<Vec<String>>,
}

impl Daemon {
    /// A daemon running the controller as `controller` from [`IMAGE`].
    pub async fn running_controller() -> Arc<Self> {
        let daemon = Arc::new(Self::default());
        let body = ContainerCreateBody {
            image: Some(IMAGE.to_owned()),
            ..ContainerCreateBody::default()
        };
        daemon
            .inner
            .create_container("controller", body)
            .await
            .expect("the controller exists");
        daemon
    }

    pub fn events(&self) -> Vec<String> {
        self.events
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    fn note(&self, event: String) {
        self.events
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(event);
    }

    fn fails(flag: &AtomicBool) -> Result<(), DockerError> {
        if flag.load(Ordering::SeqCst) {
            Err(DockerError::Unavailable)
        } else {
            Ok(())
        }
    }

    /// The helpers that exist now, by name.
    pub async fn helpers(&self) -> Vec<String> {
        self.inner
            .list_containers(&[("com.shimpz.local.kind", "prepare")])
            .await
            .expect("listed")
            .into_iter()
            .filter_map(|summary| summary.names?.first().cloned())
            .collect()
    }
}

impl DockerEngine for Daemon {
    fn info(&self) -> BoxFuture<'_, Result<SystemInfo, DockerError>> {
        self.inner.info()
    }

    fn list_containers<'a>(
        &'a self,
        labels: Labels<'a>,
    ) -> BoxFuture<'a, Result<Vec<ContainerSummary>, DockerError>> {
        Box::pin(async move {
            Self::fails(&self.fail_list)?;
            self.inner.list_containers(labels).await
        })
    }

    fn inspect_container<'a>(
        &'a self,
        id: &'a str,
    ) -> BoxFuture<'a, Result<ContainerInspectResponse, DockerError>> {
        Box::pin(async move {
            let mut found = self.inner.inspect_container(id).await?;
            if let Some(config) = found.config.as_mut() {
                let created = self.inner.container(
                    found
                        .name
                        .as_deref()
                        .unwrap_or_default()
                        .trim_start_matches('/'),
                );
                config.entrypoint = created.and_then(|(body, _)| body.entrypoint);
            }
            if self.drift.load(Ordering::SeqCst)
                && id != "controller"
                && let Some(host) = found.host_config.as_mut()
            {
                host.network_mode = Some("bridge".to_owned());
            }
            Ok(found)
        })
    }

    fn create_container<'a>(
        &'a self,
        name: &'a str,
        body: ContainerCreateBody,
    ) -> BoxFuture<'a, Result<String, DockerError>> {
        Box::pin(async move {
            Self::fails(&self.fail_create)?;
            self.note(format!("create {name}"));
            self.inner.create_container(name, body).await
        })
    }

    fn start_container<'a>(&'a self, id: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            Self::fails(&self.fail_start)?;
            self.note(format!("start {id}"));
            self.inner.start_container(id).await
        })
    }

    fn restart_container<'a>(
        &'a self,
        id: &'a str,
        grace: i32,
    ) -> BoxFuture<'a, Result<(), DockerError>> {
        self.inner.restart_container(id, grace)
    }

    fn remove_container<'a>(&'a self, id: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            Self::fails(&self.fail_remove)?;
            self.note(format!("remove {id}"));
            self.inner.remove_container(id).await
        })
    }

    fn list_networks<'a>(
        &'a self,
        labels: Labels<'a>,
    ) -> BoxFuture<'a, Result<Vec<Network>, DockerError>> {
        self.inner.list_networks(labels)
    }

    fn inspect_network<'a>(
        &'a self,
        id: &'a str,
    ) -> BoxFuture<'a, Result<NetworkInspect, DockerError>> {
        self.inner.inspect_network(id)
    }

    fn create_network(
        &self,
        request: NetworkCreateRequest,
    ) -> BoxFuture<'_, Result<String, DockerError>> {
        self.inner.create_network(request)
    }

    fn remove_network<'a>(&'a self, id: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        self.inner.remove_network(id)
    }

    fn inspect_image<'a>(
        &'a self,
        reference: &'a str,
    ) -> BoxFuture<'a, Result<ImageInspect, DockerError>> {
        self.inner.inspect_image(reference)
    }

    fn pull_image<'a>(&'a self, reference: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        self.inner.pull_image(reference)
    }
}

/// The helpers of Space `space` over `daemon` and `exec`.
pub fn helpers(daemon: &Arc<Daemon>, exec: &Arc<FakeExec>) -> Helpers {
    Helpers {
        docker: Arc::clone(daemon) as Arc<dyn DockerEngine>,
        exec: Arc::clone(exec) as Arc<dyn team_core::exec::ExecEngine>,
        entropy: Arc::new(CountingEntropy::default()),
        space_id: "space".to_owned(),
        controller: "controller".to_owned(),
        cpuset: Some("0-1".to_owned()),
    }
}

/// How a scripted worker answers one request.
#[derive(Clone, Debug)]
pub enum Worker {
    /// As the real worker does, without `PDFium` (images only; a PDF answers its text as `PDF`).
    Real,
    /// With this JSON value.
    Fixed(Value),
    /// With this standard error output and a failed exit.
    Failing,
}

/// Scripts the next exec as `worker`, returning the request it received.
pub fn script(exec: &FakeExec, worker: Worker) -> tokio::task::JoinHandle<Vec<u8>> {
    let (team_end, mut workload_end) = duplex(1024 * 1024);
    let failing = matches!(worker, Worker::Failing);
    exec.push(ScriptedExec {
        stream: team_end,
        exit: ExecExit {
            running: false,
            exit_code: Some(i64::from(failing)),
        },
    });
    tokio::spawn(async move {
        let mut raw = Vec::new();
        let _ = workload_end.read_to_end(&mut raw).await;
        let (stream, payload) = match worker {
            Worker::Real => (1, answered(&raw)),
            Worker::Fixed(value) => (1, team_protocol::python::json::canonical(&value)),
            Worker::Failing => (2, b"Traceback".to_vec()),
        };
        let mut framed = vec![stream, 0, 0, 0];
        framed.extend_from_slice(
            &u32::try_from(payload.len())
                .unwrap_or(u32::MAX)
                .to_be_bytes(),
        );
        framed.extend_from_slice(&payload);
        let _ = workload_end.write_all(&framed).await;
        raw
    })
}

fn answered(raw: &[u8]) -> Vec<u8> {
    let answer = match request::decode(raw) {
        Some(request) if request.kind == request::Kind::Image => {
            team_prepare::image::prepare(request.data).map_or_else(Answer::Opaque, Answer::Image)
        }
        Some(_) => Answer::Text("PDF".to_owned()),
        None => Answer::Opaque(team_prepare::answer::Reason::Unreadable),
    };
    answer.to_json().into_bytes()
}

/// Hooks that record the helper's start and stop and refuse a start once `ended`.
#[derive(Debug, Default)]
pub struct Recorded {
    pub ended: AtomicBool,
    pub seen: Mutex<Vec<String>>,
}

impl Recorded {
    pub fn seen(&self) -> Vec<String> {
        self.seen
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }
}

impl Hooks for Recorded {
    fn started(&self, id: &str) -> Result<(), Ended> {
        if self.ended.load(Ordering::SeqCst) {
            return Err(Ended);
        }
        self.seen
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(format!("started {id}"));
        Ok(())
    }

    fn stopped(&self, id: &str) {
        self.seen
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(format!("stopped {id}"));
    }
}
