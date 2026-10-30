//! One bounded Action RPC to an Assistant workload over Docker exec (`action/execution.py`, `action/dispatch.py`,
//! `local/assistant/rpc.py`; ADR-0092, ADR-0093).
//!
//! The process is `/usr/local/bin/shimpz-action <action>` as `10001:10001` in `/tmp`. One absolute 8-second deadline
//! covers setup, exchange, and exit inspection. Every Docker call is admitted to one Team-wide pool of eight slots
//! within that deadline; a wait for a slot observes the turn's Stop, and a dispatch Team refuses before the workload
//! process starts is reported as never dispatched, so the journal returns that attempt to `prepared`. Any failure after
//! the process may have started fail-stops the workload; one that cannot be proven stopped is blocked.

use assistant_protocol::input_file::{
    MAX_FILE_INVOCATION_BYTES, MAX_INVOCATION_BYTES, delivers_content,
};
use strict_json::Value;
use team_core::exec::{ExecEngine, ExecSpec, FailStop};
use team_core::stop::Stop;
use team_protocol::python::json::{self, Options};
use tokio::sync::{Semaphore, SemaphorePermit};
use tokio::time::Instant;

use crate::frames::{self, CallBroker, FrameError};
use crate::journal::valid_operation_id;

/// The Action entrypoint every Assistant image ships.
pub const ACTION_COMMAND: &str = "/usr/local/bin/shimpz-action";
/// The workload user.
pub const ASSISTANT_RPC_USER: &str = "10001:10001";
/// The workload directory.
pub const ASSISTANT_WORKDIR: &str = "/tmp";
/// The one absolute deadline of an ordinary RPC.
pub const RPC_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(8);
/// The largest stdout plus stderr.
pub const MAX_RPC_RESPONSE_BYTES: usize = 512 * 1024;
/// The largest ordinary invocation.
pub const MAX_RPC_REQUEST_BYTES: usize = MAX_INVOCATION_BYTES;
/// Docker calls of Action RPCs in flight at once, Team-wide.
pub const MAX_DOCKER_CALLS: usize = 8;
/// Sizes an invocation before the journal mints the real id, which always has this exact length.
pub const OPERATION_ID_PLACEHOLDER: &str = "00000000-0000-4000-8000-000000000000";
const INVOCATION_JSON: Options = Options {
    ensure_ascii: true,
    sort_keys: false,
    compact: true,
};

/// An invocation that cannot be sent: an invalid operation id or one beyond its bound.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct InvocationTooLarge;

/// One Spec v1 invocation of one logical operation: `{"input","stored_inputs","files","operation_id"[,"responses"]}`
/// in that order, ASCII, compact. `stored_inputs` names the held Stored Inputs, never their values (ADR-0106); only an
/// invocation carrying delivered file content admits the larger bound.
///
/// # Errors
/// [`InvocationTooLarge`] for an invalid operation id or an invocation beyond its bound.
pub fn encode_invocation(
    input: &Value,
    stored_inputs: &[String],
    files: &Value,
    operation_id: &str,
    responses: &[Value],
) -> Result<Vec<u8>, InvocationTooLarge> {
    if operation_id != OPERATION_ID_PLACEHOLDER && !valid_operation_id(operation_id) {
        return Err(InvocationTooLarge);
    }
    let mut names = stored_inputs.to_vec();
    names.sort();
    let mut invocation = vec![
        ("input".to_owned(), input.clone()),
        (
            "stored_inputs".to_owned(),
            Value::Array(names.into_iter().map(Value::String).collect()),
        ),
        ("files".to_owned(), files.clone()),
        (
            "operation_id".to_owned(),
            Value::String(operation_id.to_owned()),
        ),
    ];
    if !responses.is_empty() {
        invocation.push(("responses".to_owned(), Value::Array(responses.to_vec())));
    }
    let invocation = Value::Object(invocation);
    let encoded = json::dumps(&invocation, INVOCATION_JSON).into_bytes();
    let bound = if delivers_content(&invocation) {
        MAX_FILE_INVOCATION_BYTES
    } else {
        MAX_RPC_REQUEST_BYTES
    };
    if encoded.len() > bound {
        return Err(InvocationTooLarge);
    }
    Ok(encoded)
}

/// The Team-wide pool of Docker call slots.
#[derive(Debug)]
pub struct DockerSlots(Semaphore);

impl Default for DockerSlots {
    fn default() -> Self {
        Self(Semaphore::new(MAX_DOCKER_CALLS))
    }
}

impl DockerSlots {
    /// A pool of `slots` (tests).
    #[must_use]
    pub fn with(slots: usize) -> Self {
        Self(Semaphore::new(slots))
    }

    /// One slot, admitted only while it frees before `deadline` and `stop` is not set; a Stop that wins the race
    /// returns the slot before anything runs.
    async fn admit(&self, deadline: Instant, stop: Option<&Stop>) -> Option<SemaphorePermit<'_>> {
        if stop.is_some_and(Stop::is_stopped) {
            return None;
        }
        let stopped = async {
            match stop {
                Some(stop) => stop.stopped().await,
                None => std::future::pending().await,
            }
        };
        let permit = tokio::select! {
            permit = self.0.acquire() => permit.ok()?,
            () = stopped => return None,
            () = tokio::time::sleep_until(deadline) => return None,
        };
        if stop.is_some_and(Stop::is_stopped) || Instant::now() >= deadline {
            return None;
        }
        Some(permit)
    }
}

/// How an RPC failed, as the controller projects it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RpcFailure {
    /// The deadline passed.
    Timeout,
    /// The exit status could not be read.
    Ambiguous,
    /// The terminal frame is not a Spec v1 result.
    InvalidResult,
    /// The exchange or the process failed.
    Failed,
}

impl RpcFailure {
    /// The shared HTTP status.
    #[must_use]
    pub fn status(self) -> u16 {
        if self == Self::Timeout { 504 } else { 502 }
    }

    /// The fixed public message and code.
    #[must_use]
    pub fn message(self) -> (&'static str, &'static str) {
        match self {
            Self::Timeout => ("Assistant Action timed out", "assistant-timeout"),
            Self::Ambiguous => (
                "Assistant Action status is ambiguous",
                "assistant-rpc-failed",
            ),
            Self::InvalidResult => (
                "Assistant Action returned an invalid result",
                "assistant-rpc-failed",
            ),
            Self::Failed => ("Assistant Action failed", "assistant-rpc-failed"),
        }
    }
}

/// An RPC that did not return a terminal frame.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RpcError {
    /// Team refused before the workload process started; nothing ran.
    NotDispatched,
    /// The workload could not be proven stopped after a failure; nothing more may run in it.
    Blocked,
    /// It failed after it may have run; `condition` is the safe transport condition, never child output.
    Failed {
        /// The failure kind.
        kind: RpcFailure,
        /// `timeout`, `transport-failed`, `frame-invalid`, `exit-unavailable`, `exit-status:<n>`, `stderr-output`,
        /// `stopped`, or `request-after-provider-call`.
        condition: String,
    },
}

/// One RPC's inputs.
pub struct RpcCall<'a> {
    /// The workload container's full id.
    pub container: &'a str,
    /// The Action id.
    pub action: &'a str,
    /// The encoded invocation.
    pub encoded: &'a [u8],
    /// The absolute deadline.
    pub deadline: Instant,
    /// The turn's Stop, observed while waiting for a slot and throughout the exec's startup and exchange.
    pub stop: &'a Stop,
    /// Team's answer to the Action's provider calls, when it declares any.
    pub broker: Option<&'a mut dyn CallBroker>,
}

/// The exec that runs `action` in `container` as the Assistant's RPC user.
fn action_exec(container: &str, action: &str) -> ExecSpec {
    ExecSpec {
        container: container.to_owned(),
        argv: vec![ACTION_COMMAND.to_owned(), action.to_owned()],
        user: ASSISTANT_RPC_USER.to_owned(),
        working_dir: Some(ASSISTANT_WORKDIR.to_owned()),
        env: Vec::new(),
        stdin: true,
    }
}

/// Runs one RPC and returns its terminal frame, a strict JSON object.
///
/// # Errors
/// [`RpcError`] as the module describes.
pub async fn exchange(
    engine: &dyn ExecEngine,
    slots: &DockerSlots,
    call: RpcCall<'_>,
) -> Result<Value, RpcError> {
    let RpcCall {
        container,
        action,
        encoded,
        deadline,
        stop,
        mut broker,
    } = call;
    let spec = action_exec(container, action);
    let failed = |kind, condition: &str| (kind, condition.to_owned());
    let Some(permit) = slots.admit(deadline, Some(stop)).await else {
        return Err(RpcError::NotDispatched);
    };
    if stop.is_stopped() {
        return Err(RpcError::NotDispatched);
    }
    let attempt = async {
        let session = match tokio::time::timeout_at(deadline, engine.start_exec(&spec)).await {
            Err(_) => return Err(failed(RpcFailure::Timeout, "timeout")),
            Ok(Err(_)) => return Err(failed(RpcFailure::Failed, "transport-failed")),
            Ok(Ok(session)) => session,
        };
        drop(permit);
        run(
            engine,
            slots,
            session,
            encoded,
            deadline,
            broker
                .as_mut()
                .map(|broker| &mut **broker as &mut dyn CallBroker),
        )
        .await
    };
    // From the moment the exec may start, Stop interrupts it: the workload may already be running, so it is
    // fail-stopped and its outcome stays uncertain, exactly as for any other fault after a possible dispatch.
    let outcome = tokio::select! {
        biased;
        () = stop.stopped() => Err(failed(RpcFailure::Ambiguous, "stopped")),
        answer = attempt => answer,
    };
    let (stdout, calls) = match outcome {
        Ok(answer) => answer,
        Err((kind, condition)) => return Err(fail_stop(engine, container, kind, condition).await),
    };
    terminal(&stdout, calls)
}

type Attempted = Result<(Vec<u8>, usize), (RpcFailure, String)>;

/// The started process's exchange and exit inspection: its stdout and how many provider calls it made.
async fn run<'b>(
    engine: &dyn ExecEngine,
    slots: &DockerSlots,
    session: team_core::exec::ExecSession,
    encoded: &[u8],
    deadline: Instant,
    mut broker: Option<&'b mut (dyn CallBroker + 'b)>,
) -> Attempted {
    let failed = |kind, condition: &str| (kind, condition.to_owned());
    let reborrowed = broker
        .as_mut()
        .map(|broker| &mut **broker as &mut dyn CallBroker);
    let output = frames::exchange(
        session.stream,
        encoded,
        deadline,
        MAX_RPC_RESPONSE_BYTES,
        reborrowed,
    )
    .await;
    let calls = broker.as_ref().map_or(0, |broker| broker.calls());
    let (stdout, stderr) = output.map_err(|error| match error {
        FrameError::Timeout => failed(RpcFailure::Timeout, "timeout"),
        FrameError::Invalid => failed(RpcFailure::Failed, "frame-invalid"),
        FrameError::Transport => failed(RpcFailure::Failed, "transport-failed"),
    })?;
    let exit = inspect(engine, slots, &session.id, deadline).await?;
    if exit != 0 {
        return Err(failed(RpcFailure::Failed, &format!("exit-status:{exit}")));
    }
    if !stderr.is_empty() {
        return Err(failed(RpcFailure::Failed, "stderr-output"));
    }
    Ok((stdout, calls))
}

/// The exit code, read within the same deadline; an answer that comes too late or not at all is ambiguous.
async fn inspect(
    engine: &dyn ExecEngine,
    slots: &DockerSlots,
    id: &team_core::exec::ExecId,
    deadline: Instant,
) -> Result<i64, (RpcFailure, String)> {
    let unavailable = |kind| (kind, "exit-unavailable".to_owned());
    // The workload already ran, so a refused slot here is never a dispatch refusal.
    let _permit = slots
        .admit(deadline, None)
        .await
        .ok_or_else(|| unavailable(RpcFailure::Timeout))?;
    match tokio::time::timeout_at(deadline, engine.inspect_exec(id)).await {
        Err(_) => Err(unavailable(RpcFailure::Timeout)),
        Ok(Err(_)) => Err(unavailable(RpcFailure::Ambiguous)),
        Ok(Ok(exit)) if exit.running => Err(unavailable(RpcFailure::Ambiguous)),
        Ok(Ok(exit)) => exit
            .exit_code
            .ok_or_else(|| unavailable(RpcFailure::Ambiguous)),
    }
}

/// Fail-stops the workload of an outcome that may have run; one not proven stopped is blocked.
async fn fail_stop(
    engine: &dyn ExecEngine,
    container: &str,
    kind: RpcFailure,
    condition: String,
) -> RpcError {
    match engine.fail_stop(container).await {
        Ok(FailStop::Stopped) => RpcError::Failed { kind, condition },
        Ok(FailStop::Blocked) | Err(_) => RpcError::Blocked,
    }
}

/// Decodes the terminal frame; a replay would repeat every provider call before a request, so none may follow one.
///
/// # Errors
/// [`RpcError::Failed`] with [`RpcFailure::InvalidResult`].
pub fn terminal(stdout: &[u8], calls: usize) -> Result<Value, RpcError> {
    let invalid = |condition: &str| RpcError::Failed {
        kind: RpcFailure::InvalidResult,
        condition: condition.to_owned(),
    };
    let Ok(response @ Value::Object(_)) = strict_json::parse(stdout) else {
        return Err(invalid("frame-invalid"));
    };
    if calls > 0 && response.get("type") == Some(&Value::String("request".to_owned())) {
        return Err(invalid("request-after-provider-call"));
    }
    Ok(response)
}

#[cfg(test)]
mod tests;
