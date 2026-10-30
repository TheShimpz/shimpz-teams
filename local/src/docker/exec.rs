//! Workload exec over the daemon: bollard creates and inspects each exec, and Team attaches to it with its own
//! upgrade of `POST /exec/{id}/start` on a dedicated socket connection, so the stream it hands back carries Docker's
//! raw multiplexed frames unparsed and unbuffered (bollard's demultiplexer would hold a whole announced frame, up to
//! 4 GiB, before any bounded parser saw it). Shutting the stream's write half shuts the socket's write half, which
//! Docker delivers to the process as end of stdin.
//!
//! Fail-stop stops a workload container with a 3-second grace, kills it if it may still run, and only then proves it
//! no longer runs; anything less is [`FailStop::Blocked`].

use std::path::Path;

use bollard::models::ExecConfig;
use bollard::query_parameters::{
    InspectContainerOptions, KillContainerOptions, StopContainerOptions,
};
use http_body_util::Full;
use hyper::header::{CONNECTION, CONTENT_TYPE, HOST, UPGRADE};
use hyper::{Request, StatusCode};
use hyper_util::rt::TokioIo;
use team_core::exec::{
    ExecEngine, ExecError, ExecExit, ExecId, ExecSession, ExecSpec, ExecStream, FailStop,
};
use team_core::future::BoxFuture;
use tokio::net::UnixStream;

use super::DockerError;
use super::daemon::{Daemon, closed};

/// The grace a fail-stop gives the workload before it is killed.
pub const STOP_GRACE_SECONDS: i32 = 3;

/// The exec start body: attached, never a TTY (a TTY would merge stdout and stderr and drop the frame headers).
const START_BODY: &[u8] = br#"{"Detach":false,"Tty":false}"#;

/// Whether `id` is one full Docker object id (64 lowercase hex digits): nothing else reaches a request path.
fn docker_id(id: &str) -> bool {
    id.len() == 64
        && id
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

fn exec_error(error: DockerError) -> ExecError {
    match error {
        DockerError::NotFound => ExecError::NotFound,
        DockerError::Conflict | DockerError::Unavailable => ExecError::Unavailable,
    }
}

/// Upgrades `POST /exec/{exec_id}/start` on a new connection to `socket` and returns the raw attached stream.
///
/// # Errors
/// [`ExecError::NotFound`] when Docker does not know the exec; [`ExecError::Unavailable`] for any other refusal or
/// transport failure.
pub async fn attach(socket: &Path, exec_id: &str) -> Result<Box<dyn ExecStream>, ExecError> {
    if !docker_id(exec_id) {
        return Err(ExecError::NotFound);
    }
    let stream = UnixStream::connect(socket)
        .await
        .map_err(|_| ExecError::Unavailable)?;
    let (mut sender, connection) = hyper::client::conn::http1::handshake(TokioIo::new(stream))
        .await
        .map_err(|_| ExecError::Unavailable)?;
    // The connection task ends once the upgrade hands its socket over (or the daemon closes it).
    let driver = tokio::spawn(connection.with_upgrades());
    let request = Request::post(format!("/exec/{exec_id}/start"))
        .header(HOST, "docker")
        .header(CONTENT_TYPE, "application/json")
        .header(CONNECTION, "Upgrade")
        .header(UPGRADE, "tcp")
        .body(Full::new(START_BODY))
        .map_err(|_| ExecError::Unavailable)?;
    let response = sender
        .send_request(request)
        .await
        .map_err(|_| ExecError::Unavailable)?;
    match response.status() {
        StatusCode::SWITCHING_PROTOCOLS => {}
        StatusCode::NOT_FOUND => {
            driver.abort();
            return Err(ExecError::NotFound);
        }
        _ => {
            driver.abort();
            return Err(ExecError::Unavailable);
        }
    }
    let upgraded = hyper::upgrade::on(response)
        .await
        .map_err(|_| ExecError::Unavailable)?;
    Ok(Box::new(TokioIo::new(upgraded)))
}

/// The container operations a fail-stop needs.
pub trait ContainerStop: Send + Sync {
    /// Stops `container` within `grace_seconds`; an already stopped container is a success.
    ///
    /// # Errors
    /// [`DockerError`] when it could not be stopped.
    fn stop<'a>(
        &'a self,
        container: &'a str,
        grace_seconds: i32,
    ) -> BoxFuture<'a, Result<(), DockerError>>;

    /// Kills `container` with `SIGKILL`.
    ///
    /// # Errors
    /// [`DockerError`] when it could not be killed.
    fn kill<'a>(&'a self, container: &'a str) -> BoxFuture<'a, Result<(), DockerError>>;

    /// Whether `container` runs, from its inspection (`State.Running`); `None` when the state is not reported.
    ///
    /// # Errors
    /// [`DockerError`] when it cannot be inspected.
    fn running<'a>(
        &'a self,
        container: &'a str,
    ) -> BoxFuture<'a, Result<Option<bool>, DockerError>>;
}

/// Whether `container` is proven not to run: absent, or inspected with `State.Running` false.
async fn proven_stopped(control: &dyn ContainerStop, container: &str) -> Result<bool, ExecError> {
    match control.running(container).await {
        Err(DockerError::NotFound) | Ok(Some(false)) => Ok(true),
        Ok(_) | Err(DockerError::Conflict) => Ok(false),
        Err(DockerError::Unavailable) => Err(ExecError::Unavailable),
    }
}

/// Stops `container` (3-second grace), kills it if it may still run, and proves it no longer runs.
///
/// # Errors
/// [`ExecError::Unavailable`] when the daemon could not be reached for the last proof; the workload is then as
/// unproven as [`FailStop::Blocked`].
pub async fn fail_stop(
    control: &dyn ContainerStop,
    container: &str,
) -> Result<FailStop, ExecError> {
    // A refused or failed stop proves nothing; only the inspection after it does.
    if control.stop(container, STOP_GRACE_SECONDS).await == Err(DockerError::NotFound) {
        return Ok(FailStop::Stopped);
    }
    if proven_stopped(control, container).await.unwrap_or(false) {
        return Ok(FailStop::Stopped);
    }
    if control.kill(container).await == Err(DockerError::NotFound) {
        return Ok(FailStop::Stopped);
    }
    if proven_stopped(control, container).await? {
        Ok(FailStop::Stopped)
    } else {
        Ok(FailStop::Blocked)
    }
}

impl ContainerStop for Daemon {
    fn stop<'a>(
        &'a self,
        container: &'a str,
        grace_seconds: i32,
    ) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            let options = StopContainerOptions {
                t: Some(grace_seconds),
                ..StopContainerOptions::default()
            };
            self.api
                .stop_container(container, Some(options))
                .await
                .map_err(|error| closed(&error))
        })
    }

    fn kill<'a>(&'a self, container: &'a str) -> BoxFuture<'a, Result<(), DockerError>> {
        Box::pin(async move {
            let options = KillContainerOptions {
                signal: "SIGKILL".to_owned(),
            };
            self.api
                .kill_container(container, Some(options))
                .await
                .map_err(|error| closed(&error))
        })
    }

    fn running<'a>(
        &'a self,
        container: &'a str,
    ) -> BoxFuture<'a, Result<Option<bool>, DockerError>> {
        Box::pin(async move {
            let inspection = self
                .api
                .inspect_container(container, None::<InspectContainerOptions>)
                .await
                .map_err(|error| closed(&error))?;
            Ok(inspection.state.and_then(|state| state.running))
        })
    }
}

impl ExecEngine for Daemon {
    fn start_exec<'a>(
        &'a self,
        spec: &'a ExecSpec,
    ) -> BoxFuture<'a, Result<ExecSession, ExecError>> {
        Box::pin(async move {
            if !docker_id(&spec.container) || spec.argv.is_empty() {
                return Err(ExecError::NotFound);
            }
            let config = ExecConfig {
                attach_stdin: Some(spec.stdin),
                attach_stdout: Some(true),
                attach_stderr: Some(true),
                tty: Some(false),
                env: (!spec.env.is_empty()).then(|| spec.env.clone()),
                cmd: Some(spec.argv.clone()),
                privileged: Some(false),
                user: Some(spec.user.clone()),
                working_dir: spec.working_dir.clone(),
                ..ExecConfig::default()
            };
            let created = self
                .api
                .create_exec(&spec.container, config)
                .await
                .map_err(|error| exec_error(closed(&error)))?;
            let stream = attach(&self.socket, &created.id).await?;
            Ok(ExecSession {
                id: ExecId(created.id),
                stream,
            })
        })
    }

    fn inspect_exec<'a>(&'a self, id: &'a ExecId) -> BoxFuture<'a, Result<ExecExit, ExecError>> {
        Box::pin(async move {
            if !docker_id(&id.0) {
                return Err(ExecError::NotFound);
            }
            let inspection = self
                .api
                .inspect_exec(&id.0)
                .await
                .map_err(|error| exec_error(closed(&error)))?;
            Ok(ExecExit {
                running: inspection.running.ok_or(ExecError::Unavailable)?,
                exit_code: inspection.exit_code,
            })
        })
    }

    fn fail_stop<'a>(&'a self, container: &'a str) -> BoxFuture<'a, Result<FailStop, ExecError>> {
        Box::pin(fail_stop(self, container))
    }
}

#[cfg(test)]
mod tests;
