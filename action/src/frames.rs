//! The Docker exec frame exchange of one Action process (`action/frames.py`, ADR-0093, ADR-0106).
//!
//! The invocation is written while output drains, within one deadline, so a workload that answers before it reads all
//! of a large invocation never stalls on a full buffer. Output arrives in Docker's multiplexed frames (`>BxxxL`: stream
//! 1 stdout or 2 stderr, then the payload); no frame larger than the response bound is ever buffered, and stdout plus
//! stderr together stay within it. Without a provider broker, stdin is one request and is half-closed once written.
//! With one, the request is the first stdin line and stdin stays open: each complete stdout `fetch` line is answered by
//! one reply line before the next is read, the first other line is the terminal frame, and anything after it is
//! refused.

use strict_json::Value;
use team_core::future::BoxFuture;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::time::Instant;

use team_core::exec::ExecStream;

const FRAME_HEADER_BYTES: usize = 8;
const CHUNK_BYTES: usize = 64 * 1024;

/// Why an exchange ended without a complete, well-formed output.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FrameError {
    /// The deadline passed.
    Timeout,
    /// The output broke the frame or line protocol (`frame-invalid`).
    Invalid,
    /// The stream failed, or a provider reply could not be written or produced (`transport-failed`).
    Transport,
}

/// Team's answer to an Action's provider calls on the exec channel.
pub trait CallBroker: Send {
    /// The encoded one-line reply to one `fetch` frame.
    ///
    /// # Errors
    /// [`FrameError::Transport`] when Team cannot answer at all (for example, its dispatch record could not be
    /// written); the attempt then ends uncertain, never silently.
    fn answer(
        &mut self,
        frame: Value,
        deadline: Instant,
    ) -> BoxFuture<'_, Result<Vec<u8>, FrameError>>;

    /// How many call frames the attempt sent.
    fn calls(&self) -> usize;

    /// Returns what the last reply held once it has been written or abandoned; idempotent.
    fn release(&mut self);
}

/// Parses Docker's multiplexed exec frames incrementally within one bound on the output held at once.
#[derive(Debug)]
pub struct FrameReader {
    maximum: usize,
    pending: Vec<u8>,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
    terminal: Option<usize>,
}

impl FrameReader {
    /// A reader holding at most `maximum` bytes of stdout and stderr together.
    #[must_use]
    pub fn new(maximum: usize) -> Self {
        Self {
            maximum,
            pending: Vec::new(),
            stdout: Vec::new(),
            stderr: Vec::new(),
            terminal: None,
        }
    }

    /// Takes `data` and every frame it completes.
    ///
    /// # Errors
    /// [`FrameError::Invalid`] for an unknown stream, a frame announcing more than the bound, or output beyond it.
    pub fn feed(&mut self, data: &[u8]) -> Result<(), FrameError> {
        self.pending.extend_from_slice(data);
        while self.pending.len() >= FRAME_HEADER_BYTES {
            let stream = self.pending[0];
            let length = u32::from_be_bytes([
                self.pending[4],
                self.pending[5],
                self.pending[6],
                self.pending[7],
            ]);
            let length = usize::try_from(length).map_err(|_| FrameError::Invalid)?;
            if !matches!(stream, 1 | 2) || length > self.maximum + 1 {
                return Err(FrameError::Invalid);
            }
            let end = FRAME_HEADER_BYTES + length;
            if self.pending.len() < end {
                return Ok(());
            }
            let target = if stream == 1 {
                &mut self.stdout
            } else {
                &mut self.stderr
            };
            target.extend_from_slice(&self.pending[FRAME_HEADER_BYTES..end]);
            self.pending.drain(..end);
            if self.stdout.len() + self.stderr.len() > self.maximum {
                return Err(FrameError::Invalid);
            }
        }
        Ok(())
    }

    /// The next complete provider-call line before the terminal line; output after the terminal is refused.
    ///
    /// # Errors
    /// [`FrameError::Invalid`] for a line that is not strict JSON or any output after the terminal line.
    pub fn next_call(&mut self) -> Result<Option<Value>, FrameError> {
        let mut frame = None;
        if self.terminal.is_none()
            && let Some(end) = self.stdout.iter().position(|byte| *byte == b'\n')
        {
            let line = strict_json::parse(&self.stdout[..end]).map_err(|_| FrameError::Invalid)?;
            if matches!(line, Value::Object(_))
                && line.get("type") == Some(&Value::String("fetch".to_owned()))
            {
                self.stdout.drain(..=end);
                frame = Some(line);
            } else {
                self.terminal = Some(end + 1);
            }
        }
        if self
            .terminal
            .is_some_and(|terminal| self.stdout.len() > terminal)
        {
            return Err(FrameError::Invalid);
        }
        Ok(frame)
    }

    /// The bounded stdout and stderr at end of stream.
    ///
    /// # Errors
    /// [`FrameError::Invalid`] for a frame cut short.
    pub fn finish(self) -> Result<(Vec<u8>, Vec<u8>), FrameError> {
        if self.pending.is_empty() {
            Ok((self.stdout, self.stderr))
        } else {
            Err(FrameError::Invalid)
        }
    }
}

/// Writes `data` while draining output until end of stream, answering provider calls through `broker`; the bounded
/// stdout and stderr. A reply is answered only once the previous one has been written, so at most one is held.
///
/// # Errors
/// [`FrameError`] for a passed deadline, a protocol violation, or a failed stream.
pub async fn exchange(
    stream: Box<dyn ExecStream>,
    data: &[u8],
    deadline: Instant,
    maximum: usize,
    mut broker: Option<&mut dyn CallBroker>,
) -> Result<(Vec<u8>, Vec<u8>), FrameError> {
    let mut pending = data.to_vec();
    if broker.is_some() {
        pending.push(b'\n');
    }
    let reborrowed = broker
        .as_mut()
        .map(|broker| &mut **broker as &mut dyn CallBroker);
    let result = tokio::time::timeout_at(
        deadline,
        run(stream, pending, deadline, maximum, reborrowed),
    )
    .await;
    if let Some(broker) = broker {
        broker.release();
    }
    result.unwrap_or(Err(FrameError::Timeout))
}

async fn run<'b>(
    stream: Box<dyn ExecStream>,
    mut pending: Vec<u8>,
    deadline: Instant,
    maximum: usize,
    mut broker: Option<&'b mut (dyn CallBroker + 'b)>,
) -> Result<(Vec<u8>, Vec<u8>), FrameError> {
    let (mut reader, mut writer) = tokio::io::split(stream);
    let mut frames = FrameReader::new(maximum);
    let mut buffer = vec![0_u8; CHUNK_BYTES];
    let replies = broker.is_some();
    let mut closed = false;
    loop {
        // Like the Python reference, a pending write is tried before a read, so a reply the workload can no longer
        // receive ends the attempt even when its end of stream is already readable.
        tokio::select! {
            biased;
            written = writer.write(&pending[..pending.len().min(CHUNK_BYTES)]), if !pending.is_empty() => {
                settle(&written, &mut pending, replies)?;
                if pending.is_empty() && !replies && !closed {
                    closed = true;
                    let _ = writer.shutdown().await;
                }
            }
            read = reader.read(&mut buffer) => {
                let read = read.map_err(|_| FrameError::Transport)?;
                if read == 0 {
                    return frames.finish();
                }
                frames.feed(&buffer[..read])?;
            }
        }
        if let Some(broker) = broker.as_deref_mut()
            && pending.is_empty()
        {
            pending = answer(broker, &mut frames, deadline).await?;
        }
    }
}

/// Drops what one write sent. A reply that cannot be written ends the attempt; without calls the workload merely
/// stopped reading, and its output still decides the outcome.
fn settle(
    written: &std::io::Result<usize>,
    pending: &mut Vec<u8>,
    replies: bool,
) -> Result<(), FrameError> {
    match written {
        Ok(written) => {
            pending.drain(..*written);
        }
        Err(_) if replies => return Err(FrameError::Transport),
        Err(_) => pending.clear(),
    }
    Ok(())
}

/// Releases the last reply, then answers the next complete call line; the reply line to write, if any.
async fn answer(
    broker: &mut dyn CallBroker,
    frames: &mut FrameReader,
    deadline: Instant,
) -> Result<Vec<u8>, FrameError> {
    broker.release();
    let Some(frame) = frames.next_call()? else {
        return Ok(Vec::new());
    };
    let mut reply = broker.answer(frame, deadline).await?;
    reply.push(b'\n');
    Ok(reply)
}

#[cfg(test)]
mod tests;
