//! The workload exec seam: how Team runs one process inside a container it owns and controls that container.
//!
//! The `local` crate implements it over the Docker socket with its own upgrade of `POST /exec/{id}/start`, so the
//! stream carries Docker's raw multiplexed frames (an 8-byte `>BxxxL` header, stream 1 stdout or 2 stderr, then the
//! payload) unparsed: each consumer (the Action RPC, the Browser channel, the preparation helper) applies its own
//! bounded frame parser, and no frame an untrusted workload announces is buffered before that parser bounds it.
//! Shutting down the stream's write half closes the process's stdin.

use tokio::io::{AsyncRead, AsyncWrite};

use crate::future::BoxFuture;

/// One process to run in a container Team owns.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ExecSpec {
    /// The container's full id.
    pub container: String,
    /// The program and its arguments; never a shell.
    pub argv: Vec<String>,
    /// The numeric `uid[:gid]` it runs as.
    pub user: String,
    /// Its working directory, when not the image's.
    pub working_dir: Option<String>,
    /// Its exact environment, `NAME=value` each; nothing is inherited from Team.
    pub env: Vec<String>,
    /// Whether stdin is attached.
    pub stdin: bool,
}

/// A started exec's Docker id.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ExecId(pub String);

/// The attached stream of one exec: raw multiplexed output to read, stdin to write and half-close.
pub trait ExecStream: AsyncRead + AsyncWrite + Send + Unpin {}

impl<T: AsyncRead + AsyncWrite + Send + Unpin> ExecStream for T {}

/// One started exec.
pub struct ExecSession {
    /// Its id, for [`ExecEngine::inspect_exec`].
    pub id: ExecId,
    /// Its attached stream.
    pub stream: Box<dyn ExecStream>,
}

impl std::fmt::Debug for ExecSession {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ExecSession")
            .field("id", &self.id)
            .finish_non_exhaustive()
    }
}

/// An exec's state after its stream ended.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ExecExit {
    /// Whether the process still runs.
    pub running: bool,
    /// Its exit code once it ended.
    pub exit_code: Option<i64>,
}

/// The outcome of fail-stopping a workload container: stop with a 3-second grace, then kill, then prove it no longer
/// runs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FailStop {
    /// The container is proven not running.
    Stopped,
    /// It could not be proven stopped; the caller marks the workload blocked and runs nothing more in it.
    Blocked,
}

/// A closed exec failure; it never carries the daemon's text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExecError {
    /// The container or exec does not exist.
    NotFound,
    /// The daemon could not be reached or refused the operation.
    Unavailable,
}

impl std::fmt::Display for ExecError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::NotFound => "the workload does not exist",
            Self::Unavailable => "Docker is unavailable",
        })
    }
}

impl std::error::Error for ExecError {}

/// Runs processes in owned workload containers and fail-stops those containers.
pub trait ExecEngine: Send + Sync {
    /// Creates and starts `spec`, returning its attached stream.
    ///
    /// # Errors
    /// [`ExecError`] when the exec could not be created or attached.
    fn start_exec<'a>(
        &'a self,
        spec: &'a ExecSpec,
    ) -> BoxFuture<'a, Result<ExecSession, ExecError>>;

    /// The exec's state, read after its stream ended.
    ///
    /// # Errors
    /// [`ExecError`] when it cannot be read.
    fn inspect_exec<'a>(&'a self, id: &'a ExecId) -> BoxFuture<'a, Result<ExecExit, ExecError>>;

    /// Stops `container` (3-second grace), kills it if it still runs, and proves it no longer runs.
    ///
    /// # Errors
    /// [`ExecError`] when the daemon cannot be reached at all.
    fn fail_stop<'a>(&'a self, container: &'a str) -> BoxFuture<'a, Result<FailStop, ExecError>>;
}
