//! Releasing every key and button a person held when their control of a Browser ends (ADR-0117, Sign-ins item 3).
//!
//! A person's control stream can end with a pointer button still pressed in the display: `KasmVNC` releases a closed
//! stream's keys itself, but keeps its buttons held. Before anything else acts on the Browser, Team opens its own
//! read-write stream through the relay, completes the RFB handshake, sends a pointer event with no button pressed, and
//! waits for one screen update, which proves the display handled it, then closes the stream and proves its relay exec
//! ended. It never presses anything, and it reads only what fits its bounds: the display is never trusted to size
//! Team's memory.

use std::time::Duration;

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use team_core::entropy::Entropy;
use tokio::time::Instant;

use super::channel::Channel;
use super::tunnel::{admitted, read_head, upgrade_request};
use super::view::Relays;

/// The whole release's deadline.
pub const RELEASE_SECONDS: Duration = Duration::from_secs(5);
/// The deadline to prove the release's own relay exec ended, once its stream closed.
pub const EXIT_SECONDS: Duration = Duration::from_secs(2);
/// The most lines the display's upgrade answer may have.
pub const MAX_HEADER_LINES: usize = 32;
/// The longest desktop name the display may announce.
pub const MAX_NAME_BYTES: usize = 4096;
// The largest WebSocket frame the display may send.
const MAX_FRAME_BYTES: u64 = 1 << 20;
// A full update request of one pixel: its answer comes only after the display handled everything sent before it.
const UPDATE_REQUEST: [u8; 10] = [3, 0, 0, 0, 0, 0, 0, 1, 0, 1];
// SetEncodings: one encoding, Raw.
const RAW_ENCODINGS: [u8; 8] = [2, 0, 0, 1, 0, 0, 0, 0];
// `KasmVNC`'s pointer event with no button pressed: type 5, a 16-bit button mask, the position, and two scroll deltas.
const POINTER: [u8; 11] = [5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];

/// The held input could not be proven released.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ReleaseError;

impl std::fmt::Display for ReleaseError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("the held input could not be proven released")
    }
}

impl std::error::Error for ReleaseError {}

/// One masked binary WebSocket frame, as a client sends it.
fn frame(payload: &[u8], mask: [u8; 4]) -> Vec<u8> {
    let length = payload.len();
    let mut out = vec![0x82];
    match u8::try_from(length) {
        Ok(short) if short < 126 => out.push(0x80 | short),
        _ => {
            out.push(0x80 | 0x7e);
            out.extend_from_slice(&u16::try_from(length).unwrap_or(u16::MAX).to_be_bytes());
        }
    }
    out.extend_from_slice(&mask);
    out.extend(
        payload
            .iter()
            .enumerate()
            .map(|(index, byte)| byte ^ mask[index % 4]),
    );
    out
}

/// The server side of one RFB stream, read through its WebSocket frames.
struct Rfb<'a> {
    display: &'a mut Channel,
    deadline: Instant,
    entropy: &'a dyn Entropy,
    buffer: Vec<u8>,
    raw: Vec<u8>,
}

impl Rfb<'_> {
    async fn wire(&mut self, size: usize) -> Result<Vec<u8>, ReleaseError> {
        while self.raw.len() < size {
            let byte = self
                .display
                .receive_exact(1, self.deadline)
                .await
                .map_err(|_| ReleaseError)?;
            self.raw.extend_from_slice(&byte);
        }
        Ok(self.raw.drain(..size).collect())
    }

    async fn read(&mut self, size: usize) -> Result<Vec<u8>, ReleaseError> {
        while self.buffer.len() < size {
            let head = self.wire(2).await?;
            let (first, second) = (head[0], head[1]);
            if (first & 0x0f != 0x0 && first & 0x0f != 0x2) || second & 0x80 != 0 {
                return Err(ReleaseError);
            }
            let mut length = u64::from(second & 0x7f);
            if length == 126 {
                let bytes = self.wire(2).await?;
                length = u64::from(u16::from_be_bytes([bytes[0], bytes[1]]));
            } else if length == 127 {
                let bytes = self.wire(8).await?;
                let mut array = [0_u8; 8];
                array.copy_from_slice(&bytes);
                length = u64::from_be_bytes(array);
            }
            if length > MAX_FRAME_BYTES {
                return Err(ReleaseError);
            }
            let payload = self
                .wire(usize::try_from(length).map_err(|_| ReleaseError)?)
                .await?;
            self.buffer.extend_from_slice(&payload);
        }
        Ok(self.buffer.drain(..size).collect())
    }

    async fn send(&mut self, payload: &[u8]) -> Result<(), ReleaseError> {
        let mut mask = [0_u8; 4];
        self.entropy.fill(&mut mask).map_err(|_| ReleaseError)?;
        self.display
            .send(&frame(payload, mask), self.deadline)
            .await
            .map_err(|_| ReleaseError)
    }
}

/// Releases every modifier and pointer button in the display, as the read-write user.
///
/// # Errors
/// [`ReleaseError`] when the release, or the end of its own relay exec, was not proven.
pub async fn release(
    relays: &dyn Relays,
    authorization: &str,
    entropy: &dyn Entropy,
) -> Result<(), ReleaseError> {
    let (exec_id, mut display) = relays.open_relay().await.map_err(|_| ReleaseError)?;
    let deadline = Instant::now() + RELEASE_SECONDS;
    let released = speak(&mut display, authorization, deadline, entropy).await;
    display.close_input().await;
    drop(display);
    if !relays.exited(&exec_id, Instant::now() + EXIT_SECONDS).await {
        return Err(ReleaseError);
    }
    released
}

async fn speak(
    display: &mut Channel,
    authorization: &str,
    deadline: Instant,
    entropy: &dyn Entropy,
) -> Result<(), ReleaseError> {
    let mut key = [0_u8; 16];
    entropy.fill(&mut key).map_err(|_| ReleaseError)?;
    let key = STANDARD.encode(key);
    display
        .send(&upgrade_request(&key, Some(authorization)), deadline)
        .await
        .map_err(|_| ReleaseError)?;
    let head = read_head(display, deadline, Some(MAX_HEADER_LINES))
        .await
        .map_err(|_| ReleaseError)?;
    if !admitted(&head, &key) {
        return Err(ReleaseError);
    }
    let early = display.take(display.buffered());
    let mut rfb = Rfb {
        display,
        deadline,
        entropy,
        buffer: Vec::new(),
        raw: early,
    };
    handshake(&mut rfb).await?;
    rfb.send(&RAW_ENCODINGS).await?;
    rfb.send(&POINTER).await?;
    rfb.send(&UPDATE_REQUEST).await?;
    // Server messages before the update (a bell, a colour map, cut text) are not expected from this display.
    if rfb.read(1).await? != [0] {
        return Err(ReleaseError);
    }
    Ok(())
}

/// The RFB 3.8 handshake with no authentication and a shared session, up to the server's init and its bounded name.
async fn handshake(rfb: &mut Rfb<'_>) -> Result<(), ReleaseError> {
    admit(rfb.read(12).await?.starts_with(b"RFB 003."))?;
    rfb.send(b"RFB 003.008\n").await?;
    let count = rfb.read(1).await?[0];
    admit(rfb.read(usize::from(count)).await?.contains(&1))?;
    rfb.send(b"\x01").await?;
    admit(rfb.read(4).await? == [0, 0, 0, 0])?;
    server_init(rfb).await
}

/// The shared-session flag, then the server's init, whose desktop name is read only within its bound.
async fn server_init(rfb: &mut Rfb<'_>) -> Result<(), ReleaseError> {
    rfb.send(b"\x01").await?;
    rfb.read(20).await?;
    let name = rfb.read(4).await?;
    let name_length = u32::from_be_bytes([name[0], name[1], name[2], name[3]]);
    let name_length = usize::try_from(name_length).map_err(|_| ReleaseError)?;
    admit(name_length <= MAX_NAME_BYTES)?;
    rfb.read(name_length).await.map(drop)
}

fn admit(admitted: bool) -> Result<(), ReleaseError> {
    if admitted { Ok(()) } else { Err(ReleaseError) }
}

#[cfg(test)]
pub(crate) mod tests;
