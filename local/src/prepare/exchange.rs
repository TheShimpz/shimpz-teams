//! One file's exchange with a fresh worker process in the helper: the framed request on standard input, then exactly
//! one JSON answer on standard output, read through Docker's multiplexed frames within their bounds.
//!
//! Standard output may carry at most 1 MiB, and a frame announcing more is refused before its payload is read. Any
//! byte on standard error, any other stream, a malformed frame header, a non-zero exit, a process still running, or
//! the 10 s wall time (from the exec's start to its exit state) fails the file; the caller then replaces the helper.

use strict_json::Value;
use team_core::exec::{ExecEngine, ExecSpec, ExecStream};
use team_prepare::limits::{HELPER_WALL, MAX_HELPER_OUTPUT_BYTES};
use tokio::io::{AsyncReadExt, AsyncWriteExt, ReadHalf, split};

use super::helper::{PROGRAM, USER};

/// The exchange failed; the file is unreadable and its helper must not serve another file.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ExchangeFailed;

/// The worker's exec in `container`.
#[must_use]
pub fn spec(container: &str) -> ExecSpec {
    ExecSpec {
        container: container.to_owned(),
        argv: vec![PROGRAM.to_owned(), "prepare".to_owned()],
        user: USER.to_owned(),
        working_dir: Some("/".to_owned()),
        env: Vec::new(),
        stdin: true,
    }
}

/// Runs one worker process for `request` and returns its parsed answer.
///
/// # Errors
/// [`ExchangeFailed`] for every failure of the process, its frames, its exit, or the wall time.
pub async fn exchange(
    exec: &dyn ExecEngine,
    container: &str,
    request: &[u8],
) -> Result<Value, ExchangeFailed> {
    let spec = spec(container);
    let output = tokio::time::timeout(HELPER_WALL, async {
        let session = exec.start_exec(&spec).await.map_err(|_| ExchangeFailed)?;
        let (mut reader, mut writer) = split(session.stream);
        let send = async {
            writer
                .write_all(request)
                .await
                .map_err(|_| ExchangeFailed)?;
            writer.shutdown().await.map_err(|_| ExchangeFailed)
        };
        let ((), output) = tokio::try_join!(send, stdout(&mut reader))?;
        let exit = exec
            .inspect_exec(&session.id)
            .await
            .map_err(|_| ExchangeFailed)?;
        if exit.running || exit.exit_code != Some(0) {
            return Err(ExchangeFailed);
        }
        Ok(output)
    })
    .await
    .map_err(|_| ExchangeFailed)??;
    strict_json::parse(&output).map_err(|_| ExchangeFailed)
}

/// Reads frames until the stream ends, keeping standard output within its bound and refusing everything else.
async fn stdout(reader: &mut ReadHalf<Box<dyn ExecStream>>) -> Result<Vec<u8>, ExchangeFailed> {
    let mut output = Vec::new();
    loop {
        let mut header = [0_u8; 8];
        let first = reader
            .read(&mut header[..1])
            .await
            .map_err(|_| ExchangeFailed)?;
        if first == 0 {
            return Ok(output);
        }
        reader
            .read_exact(&mut header[1..])
            .await
            .map_err(|_| ExchangeFailed)?;
        let length = usize::try_from(u32::from_be_bytes([
            header[4], header[5], header[6], header[7],
        ]))
        .map_err(|_| ExchangeFailed)?;
        if header[0] != 1
            || header[1..4] != [0, 0, 0]
            || output.len() + length > MAX_HELPER_OUTPUT_BYTES
        {
            return Err(ExchangeFailed);
        }
        let start = output.len();
        output.resize(start + length, 0);
        reader
            .read_exact(&mut output[start..])
            .await
            .map_err(|_| ExchangeFailed)?;
    }
}

#[cfg(test)]
mod tests;
