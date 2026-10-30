//! One viewer's WebSocket carried from Admin to the Browser's display (ADR-0117, watching over `KasmVNC`).
//!
//! Team never reads the RFB stream it carries. It sends the display one canonical upgrade request bearing the viewer's
//! own key, admits only the display's exact 101 answer for that key before anything else passes, and then copies
//! bytes both ways: each direction holds at most one read of [`CHUNK`] bytes until it is written, a side that takes
//! nothing for [`WRITE_SECONDS`] ends the stream, and no stream outlives [`LIFE_SECONDS`]. Interrupting the tunnel, or
//! its view ending, ends the copy at once.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use team_core::stop::Stop;
use team_protocol::browser_view;
use tokio::io::{AsyncReadExt, AsyncWrite, AsyncWriteExt, split};
use tokio::time::{Instant, sleep_until, timeout};

use super::channel::{CHUNK, Channel};
use crate::http::service::Upgraded;

/// How long the display has to answer the upgrade.
pub const UPGRADE_SECONDS: Duration = Duration::from_secs(5);
/// The most the display's upgrade answer may take, its lines and their line ends together.
pub const MAX_UPGRADE_BYTES: usize = 4096;
/// How long a side may take nothing before the stream ends.
pub const WRITE_SECONDS: Duration = Duration::from_secs(10);
/// A stream outlives the longest Browser task, its start and its ingest included, so no task outlives its Watch stream
/// (ADR-0117, 2026-10-22 amendment §7); the view ends every stream when its task ends.
pub const LIFE_SECONDS: Duration = Duration::from_mins(35);
/// The display answers only an upgrade that names an origin; its own loopback origin is the one it is given.
pub const DISPLAY_ORIGIN: &str = "http://127.0.0.1:6901";

/// The display did not answer the upgrade exactly, or a side of the stream failed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TunnelError {
    /// The display's upgrade answer was missing, late, oversized, or not the exact 101.
    Refused,
    /// A side took nothing for [`WRITE_SECONDS`].
    Stalled,
    /// A side's connection failed.
    Failed,
}

/// The one upgrade request the display receives, whatever the viewer sent.
///
/// A controlled Browser's display authenticates every stream: Team adds the credential of the stream's role
/// (ADR-0117, Sign-ins amendment item 3), which never comes from the viewer.
#[must_use]
pub fn upgrade_request(key: &str, authorization: Option<&str>) -> Vec<u8> {
    let mut lines = vec![
        "GET /websockify HTTP/1.1".to_owned(),
        "Host: 127.0.0.1:6901".to_owned(),
        format!("Origin: {DISPLAY_ORIGIN}"),
        "Upgrade: websocket".to_owned(),
        "Connection: Upgrade".to_owned(),
        format!("Sec-WebSocket-Key: {key}"),
        format!("Sec-WebSocket-Version: {}", browser_view::WEBSOCKET_VERSION),
        format!("Sec-WebSocket-Protocol: {}", browser_view::SUBPROTOCOL),
    ];
    if let Some(authorization) = authorization {
        lines.push(format!("Authorization: {authorization}"));
    }
    format!("{}\r\n\r\n", lines.join("\r\n")).into_bytes()
}

/// Whether the display's answer is exactly the 101 for `key`: each header once, no extension, `binary` chosen.
#[must_use]
pub fn admitted(head: &[String], key: &str) -> bool {
    let Some(status) = head.first() else {
        return false;
    };
    if !status.starts_with("HTTP/1.1 101 ") {
        return false;
    }
    let mut headers: Vec<(String, String)> = Vec::new();
    for line in &head[1..] {
        let Some((name, value)) = line.split_once(':') else {
            return false;
        };
        let name = name.trim().to_ascii_lowercase();
        if name.is_empty() || headers.iter().any(|(known, _)| *known == name) {
            return false;
        }
        headers.push((name, value.trim().to_owned()));
    }
    let header = |name: &str| {
        headers
            .iter()
            .find(|(known, _)| known == name)
            .map(|(_, value)| value.as_str())
    };
    let upgrade_token = header("connection").is_some_and(|connection| {
        connection
            .split(',')
            .any(|token| token.trim().eq_ignore_ascii_case("upgrade"))
    });
    header("upgrade").is_some_and(|upgrade| upgrade.eq_ignore_ascii_case("websocket"))
        && upgrade_token
        && header("sec-websocket-accept") == Some(browser_view::accept(key).as_str())
        && header("sec-websocket-protocol") == Some(browser_view::SUBPROTOCOL)
        && header("sec-websocket-extensions").is_none()
}

/// The 101 Team gives the viewer once the display admitted the same key.
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

/// Reads the display's upgrade answer within its bounds: its lines without line ends, up to the empty one.
pub(crate) async fn read_head(
    display: &mut Channel,
    deadline: Instant,
    max_lines: Option<usize>,
) -> Result<Vec<String>, TunnelError> {
    let mut head = Vec::new();
    let mut size = 0;
    loop {
        let line = display
            .receive_line(deadline, MAX_UPGRADE_BYTES)
            .await
            .map_err(|_| TunnelError::Refused)?;
        size += line.len() + 1;
        if size > MAX_UPGRADE_BYTES {
            return Err(TunnelError::Refused);
        }
        // Latin-1: every byte is the character of its value.
        let mut text: String = line.iter().map(|byte| char::from(*byte)).collect();
        if text.ends_with('\r') {
            text.pop();
        }
        if text.is_empty() {
            return Ok(head);
        }
        head.push(text);
        if max_lines.is_some_and(|max| head.len() > max) {
            return Err(TunnelError::Refused);
        }
    }
}

/// The bytes carried each way so far.
#[derive(Debug, Default)]
pub struct Carried {
    /// Display bytes written to the viewer.
    pub to_viewer: AtomicU64,
    /// Viewer bytes written to the display.
    pub to_display: AtomicU64,
}

impl Carried {
    fn add(counter: &AtomicU64, bytes: usize) {
        counter.fetch_add(u64::try_from(bytes).unwrap_or(u64::MAX), Ordering::Relaxed);
    }
}

/// The bytes between one viewer's connection and one relay exec into the display.
pub struct Tunnel {
    display: Channel,
    interrupt: Stop,
    /// What was carried each way.
    pub carried: Carried,
}

impl std::fmt::Debug for Tunnel {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Tunnel")
    }
}

impl Tunnel {
    /// A tunnel over one relay channel; setting `interrupt` ends its copy at once.
    #[must_use]
    pub fn new(display: Channel, interrupt: Stop) -> Self {
        Self {
            display,
            interrupt,
            carried: Carried::default(),
        }
    }

    /// The relay channel, once the tunnel is done with it.
    #[must_use]
    pub fn into_display(self) -> Channel {
        self.display
    }

    /// Upgrades the display with the viewer's key; the 101 to send the viewer, and any display bytes already read.
    ///
    /// # Errors
    /// [`TunnelError::Refused`] when the display did not answer exactly, in bounds, and in time.
    pub async fn upgrade(
        &mut self,
        key: &str,
        authorization: Option<&str>,
    ) -> Result<(Vec<u8>, Vec<u8>), TunnelError> {
        let deadline = Instant::now() + UPGRADE_SECONDS;
        self.display
            .send(&upgrade_request(key, authorization), deadline)
            .await
            .map_err(|_| TunnelError::Refused)?;
        let head = read_head(&mut self.display, deadline, None).await?;
        if !admitted(&head, key) {
            return Err(TunnelError::Refused);
        }
        let early = self.display.take(self.display.buffered());
        Ok((answer(key), early))
    }

    /// Copies both ways until a side ends, a side stalls, the tunnel is interrupted or `ended` is set, or the stream's
    /// life runs out.
    ///
    /// # Errors
    /// [`TunnelError`] when a side stalled or failed; the stream then ends.
    pub async fn splice(
        &mut self,
        client: Box<dyn Upgraded>,
        ended: &Stop,
    ) -> Result<(), TunnelError> {
        let life = Instant::now() + LIFE_SECONDS;
        let (mut client_read, mut client_write) = split(client);
        let (demux, display_write) = self.display.parts();
        let carried = &self.carried;
        let to_viewer = async {
            loop {
                // Display bytes already read move first, so a quiet display never strands the end of an update.
                if demux.buffered() > 0 {
                    let data = demux.take(CHUNK);
                    write_within(&mut client_write, &data).await?;
                    Carried::add(&carried.to_viewer, data.len());
                    continue;
                }
                if !demux.pump(CHUNK).await.map_err(|_| TunnelError::Failed)? {
                    return Ok(());
                }
            }
        };
        let to_display = async {
            let mut buffer = vec![0_u8; CHUNK];
            loop {
                let read = match client_read.read(&mut buffer).await {
                    Ok(0) | Err(_) => return Ok(()),
                    Ok(read) => read,
                };
                write_within(display_write, &buffer[..read]).await?;
                Carried::add(&carried.to_display, read);
            }
        };
        tokio::select! {
            outcome = to_viewer => outcome,
            outcome = to_display => outcome,
            () = self.interrupt.stopped() => Ok(()),
            () = ended.stopped() => Ok(()),
            () = sleep_until(life) => Ok(()),
        }
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

/// Reads one viewer side that is never written to (a test's).
#[cfg(test)]
pub(crate) async fn drain<R: tokio::io::AsyncRead + Unpin>(reader: &mut R, size: usize) -> Vec<u8> {
    let mut received = Vec::new();
    let mut buffer = vec![0_u8; size];
    while received.len() < size {
        match reader.read(&mut buffer[..size - received.len()]).await {
            Ok(0) | Err(_) => break,
            Ok(read) => received.extend_from_slice(&buffer[..read]),
        }
    }
    received
}

#[cfg(test)]
mod tests;
