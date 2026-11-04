//! The bytes between one viewer's WebSocket and its stream on easi.run.
//!
//! Team never reads what it carries. Each direction holds at most one read of [`CHUNK`] bytes until it is written, a side
//! that takes nothing for [`WRITE_SECONDS`] ends the stream, and no stream outlives [`LIFE_SECONDS`]. Interrupting the
//! tunnel, or its view ending, ends the copy at once.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use team_core::stop::Stop;
use team_protocol::browser_view;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, split};
use tokio::time::{Instant, sleep_until, timeout};

/// How long easi.run has to answer the upgrade.
pub const UPGRADE_SECONDS: Duration = Duration::from_secs(5);
/// How long a side may take nothing before the stream ends.
pub const WRITE_SECONDS: Duration = Duration::from_secs(10);
/// A stream outlives the longest Browser task, its start and its ingest included, so no task outlives its Watch stream;
/// the view ends every stream when its task ends.
pub const LIFE_SECONDS: Duration = Duration::from_mins(35);
/// The most one read takes from a side.
pub const CHUNK: usize = 64 * 1024;

/// A side of the stream stalled or failed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TunnelError {
    /// A side took nothing for [`WRITE_SECONDS`].
    Stalled,
    /// A side's connection failed.
    Failed,
}

/// The 101 Team gives the viewer once easi.run admitted the same key.
#[must_use]
pub fn answer(key: &str) -> Vec<u8> {
    let lines = [
        "HTTP/1.1 101 Switching Protocols".to_owned(),
        "Upgrade: websocket".to_owned(),
        "Connection: Upgrade".to_owned(),
        format!("Sec-WebSocket-Accept: {}", browser_view::accept(key)),
        format!("Sec-WebSocket-Protocol: {}", browser_view::SUBPROTOCOL),
    ];
    format!("{}\r\n\r\n", lines.join("\r\n")).into_bytes()
}

/// The bytes carried each way so far.
#[derive(Debug, Default)]
pub struct Carried {
    /// easi.run's bytes written to the viewer.
    pub to_viewer: AtomicU64,
    /// The viewer's bytes written to easi.run.
    pub to_easi: AtomicU64,
}

impl Carried {
    fn add(counter: &AtomicU64, bytes: usize) {
        counter.fetch_add(u64::try_from(bytes).unwrap_or(u64::MAX), Ordering::Relaxed);
    }

    /// The audit line of a stream that carried this.
    #[must_use]
    pub fn ended(&self) -> String {
        format!(
            "ended:to_viewer={}:to_easi={}",
            self.to_viewer.load(Ordering::Relaxed),
            self.to_easi.load(Ordering::Relaxed)
        )
    }
}

/// Copies both ways until a side ends, a side stalls, the stream is interrupted or `ended` is set, or its life runs out.
///
/// # Errors
/// [`TunnelError`] when a side stalled or failed; the stream then ends.
pub async fn splice<E, C>(
    easi: E,
    client: &mut C,
    interrupt: &Stop,
    ended: &Stop,
    carried: &Carried,
) -> Result<(), TunnelError>
where
    E: AsyncRead + AsyncWrite + Unpin,
    C: AsyncRead + AsyncWrite + Unpin + ?Sized,
{
    let life = Instant::now() + LIFE_SECONDS;
    let (mut easi_read, mut easi_write) = split(easi);
    let (mut client_read, mut client_write) = split(client);
    let to_viewer = async {
        let mut buffer = vec![0_u8; CHUNK];
        loop {
            let read = match easi_read.read(&mut buffer).await {
                Ok(0) | Err(_) => return Ok(()),
                Ok(read) => read,
            };
            write_within(&mut client_write, &buffer[..read]).await?;
            Carried::add(&carried.to_viewer, read);
        }
    };
    let to_easi = async {
        let mut buffer = vec![0_u8; CHUNK];
        loop {
            let read = match client_read.read(&mut buffer).await {
                Ok(0) | Err(_) => return Ok(()),
                Ok(read) => read,
            };
            write_within(&mut easi_write, &buffer[..read]).await?;
            Carried::add(&carried.to_easi, read);
        }
    };
    tokio::select! {
        outcome = to_viewer => outcome,
        outcome = to_easi => outcome,
        () = interrupt.stopped() => Ok(()),
        () = ended.stopped() => Ok(()),
        () = sleep_until(life) => Ok(()),
    }
}

/// Writes all of `data`, each write within [`WRITE_SECONDS`]: a side that takes nothing that long has stalled.
async fn write_within<W: AsyncWrite + Unpin>(sink: &mut W, data: &[u8]) -> Result<(), TunnelError> {
    let mut offset = 0;
    while offset < data.len() {
        match timeout(WRITE_SECONDS, sink.write(&data[offset..])).await {
            Err(_) => return Err(TunnelError::Stalled),
            Ok(Err(_) | Ok(0)) => return Err(TunnelError::Failed),
            Ok(Ok(written)) => offset += written,
        }
    }
    match timeout(WRITE_SECONDS, sink.flush()).await {
        Err(_) => Err(TunnelError::Stalled),
        Ok(Err(_)) => Err(TunnelError::Failed),
        Ok(Ok(())) => Ok(()),
    }
}
