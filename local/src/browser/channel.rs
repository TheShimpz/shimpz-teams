//! The Browser's `docker exec` channels: the Cua Driver's MCP stdio, the display relay, and the bounded one-shot
//! `browser-host` programs (ADR-0117 §3.5).
//!
//! Docker multiplexes an exec's stdout and stderr in frames of an 8-byte `>BxxxL` header. Team parses the raw bytes
//! itself, payload by payload as they arrive, so no frame a Browser announces is ever buffered whole: stdout is kept
//! within its caller's bound, stderr is discarded up to [`MAX_STDERR`], and every read and write waits no longer than
//! its caller's absolute deadline.

use team_core::exec::ExecStream;
use tokio::io::{AsyncReadExt, AsyncWriteExt, ReadHalf, WriteHalf, split};
use tokio::time::{Instant, timeout_at};

/// The most one read takes from the exec stream.
pub const CHUNK: usize = 64 * 1024;
/// The most stderr a Browser program may write; it is discarded.
pub const MAX_STDERR: usize = 64 * 1024;
const HEADER: usize = 8;

/// A closed channel failure; it never carries the Browser's bytes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChannelError {
    /// The stream closed before what was asked arrived.
    Closed,
    /// A line was longer than its bound.
    Oversized,
    /// A frame named a stream other than stdout or stderr.
    Framing,
    /// More stderr than [`MAX_STDERR`] arrived.
    Stderr,
    /// More stdout arrived than the read asked for.
    Excess,
    /// The stream ended inside a frame.
    EndedInFrame,
    /// Stdout arrived after the program's last expected byte.
    Trailing,
    /// The deadline passed.
    Deadline,
    /// The stream failed.
    Failed,
}

impl std::fmt::Display for ChannelError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("the Browser channel failed")
    }
}

impl std::error::Error for ChannelError {}

/// The demultiplexing read side of one exec stream.
pub struct Demux {
    reader: ReadHalf<Box<dyn ExecStream>>,
    // The header bytes of the frame being read, until all eight arrived.
    header: Vec<u8>,
    // The current frame's stream and payload bytes still to come.
    stream: u8,
    remaining: usize,
    stdout: Vec<u8>,
    stderr: usize,
    closed: bool,
}

/// One exec's channel: the demultiplexed stdout to read and stdin to write.
pub struct Channel {
    demux: Demux,
    writer: WriteHalf<Box<dyn ExecStream>>,
}

impl std::fmt::Debug for Channel {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Channel")
    }
}

impl Channel {
    /// The channel over one attached exec stream.
    #[must_use]
    pub fn new(stream: Box<dyn ExecStream>) -> Self {
        let (reader, writer) = split(stream);
        Self {
            demux: Demux {
                reader,
                header: Vec::with_capacity(HEADER),
                stream: 0,
                remaining: 0,
                stdout: Vec::new(),
                stderr: 0,
                closed: false,
            },
            writer,
        }
    }

    /// The read side and the write side at once, for a caller that copies both ways.
    pub fn parts(&mut self) -> (&mut Demux, &mut WriteHalf<Box<dyn ExecStream>>) {
        (&mut self.demux, &mut self.writer)
    }

    /// Writes all of `data` before `deadline`.
    ///
    /// # Errors
    /// [`ChannelError`] when it was not all written in time.
    pub async fn send(&mut self, data: &[u8], deadline: Instant) -> Result<(), ChannelError> {
        if Instant::now() >= deadline {
            return Err(ChannelError::Deadline);
        }
        match timeout_at(deadline, async {
            self.writer.write_all(data).await?;
            self.writer.flush().await
        })
        .await
        {
            Ok(Ok(())) => Ok(()),
            Ok(Err(_)) => Err(ChannelError::Failed),
            Err(_) => Err(ChannelError::Deadline),
        }
    }

    /// Closes the program's stdin; the read side stays open. A failure is ignored: the program then sees its input
    /// end when the channel is dropped.
    pub async fn close_input(&mut self) {
        let _ = timeout_at(
            Instant::now() + std::time::Duration::from_secs(1),
            self.writer.shutdown(),
        )
        .await;
    }

    /// The next stdout line without its newline, of at most `limit` bytes.
    ///
    /// # Errors
    /// [`ChannelError`] when it did not arrive whole, in bounds, and in time.
    pub async fn receive_line(
        &mut self,
        deadline: Instant,
        limit: usize,
    ) -> Result<Vec<u8>, ChannelError> {
        self.demux.receive_line(deadline, limit).await
    }

    /// Exactly `size` more stdout bytes.
    ///
    /// # Errors
    /// [`ChannelError`] when they did not arrive in time.
    pub async fn receive_exact(
        &mut self,
        size: usize,
        deadline: Instant,
    ) -> Result<Vec<u8>, ChannelError> {
        self.demux.receive_exact(size, deadline).await
    }

    /// The next stdout bytes, at most `size` and at least one, or nothing once the stream ended.
    ///
    /// # Errors
    /// [`ChannelError`] when nothing arrived in time.
    pub async fn receive_chunk(
        &mut self,
        size: usize,
        deadline: Instant,
    ) -> Result<Vec<u8>, ChannelError> {
        self.demux.receive_chunk(size, deadline).await
    }

    /// Waits for the end of the stream; any further stdout is refused.
    ///
    /// # Errors
    /// [`ChannelError`] when it did not end, ended inside a frame, or carried more stdout.
    pub async fn receive_end(&mut self, deadline: Instant) -> Result<(), ChannelError> {
        self.demux.receive_end(deadline).await
    }

    /// Stdout bytes already read.
    #[must_use]
    pub fn buffered(&self) -> usize {
        self.demux.stdout.len()
    }

    /// Up to `size` buffered stdout bytes.
    pub fn take(&mut self, size: usize) -> Vec<u8> {
        self.demux.take(size)
    }
}

impl Demux {
    /// Up to `size` buffered stdout bytes.
    pub fn take(&mut self, size: usize) -> Vec<u8> {
        let size = size.min(self.stdout.len());
        self.stdout.drain(..size).collect()
    }

    /// Stdout bytes already read.
    #[must_use]
    pub fn buffered(&self) -> usize {
        self.stdout.len()
    }

    async fn receive_line(
        &mut self,
        deadline: Instant,
        limit: usize,
    ) -> Result<Vec<u8>, ChannelError> {
        loop {
            if let Some(end) = self.stdout.iter().position(|byte| *byte == b'\n') {
                if end > limit {
                    return Err(ChannelError::Oversized);
                }
                let line = self.stdout.drain(..=end).take(end).collect();
                return Ok(line);
            }
            if self.stdout.len() > limit {
                return Err(ChannelError::Oversized);
            }
            if self.closed {
                return Err(ChannelError::Closed);
            }
            self.fill(deadline, limit).await?;
        }
    }

    async fn receive_exact(
        &mut self,
        size: usize,
        deadline: Instant,
    ) -> Result<Vec<u8>, ChannelError> {
        while self.stdout.len() < size {
            if self.closed {
                return Err(ChannelError::Closed);
            }
            self.fill(deadline, size).await?;
        }
        Ok(self.stdout.drain(..size).collect())
    }

    async fn receive_chunk(
        &mut self,
        size: usize,
        deadline: Instant,
    ) -> Result<Vec<u8>, ChannelError> {
        while self.stdout.len() < size && !self.closed {
            self.fill(deadline, size).await?;
        }
        Ok(self.take(size))
    }

    async fn receive_end(&mut self, deadline: Instant) -> Result<(), ChannelError> {
        while !self.closed {
            self.fill(deadline, 0).await?;
        }
        if self.stdout.is_empty() {
            Ok(())
        } else {
            Err(ChannelError::Trailing)
        }
    }

    /// Reads once what the stream has, at most `limit` bytes; whether the stream is still open.
    ///
    /// # Errors
    /// [`ChannelError`] when the stream failed or broke its framing.
    pub async fn pump(&mut self, limit: usize) -> Result<bool, ChannelError> {
        if self.closed {
            return Ok(false);
        }
        let mut buffer = vec![0_u8; CHUNK.min(limit.max(1))];
        let read = self
            .reader
            .read(&mut buffer)
            .await
            .map_err(|_| ChannelError::Failed)?;
        self.absorb(&buffer[..read], limit)?;
        Ok(!self.closed)
    }

    async fn fill(&mut self, deadline: Instant, limit: usize) -> Result<(), ChannelError> {
        if Instant::now() >= deadline {
            return Err(ChannelError::Deadline);
        }
        let mut buffer = vec![0_u8; CHUNK];
        let read = match timeout_at(deadline, self.reader.read(&mut buffer)).await {
            Ok(Ok(read)) => read,
            Ok(Err(_)) => return Err(ChannelError::Failed),
            Err(_) => return Err(ChannelError::Deadline),
        };
        self.absorb(&buffer[..read], limit)
    }

    /// Parses `bytes` as they arrive: headers, then each payload byte straight to stdout or the stderr count.
    fn absorb(&mut self, mut bytes: &[u8], limit: usize) -> Result<(), ChannelError> {
        if bytes.is_empty() {
            if !self.header.is_empty() || self.remaining > 0 {
                return Err(ChannelError::EndedInFrame);
            }
            self.closed = true;
            return Ok(());
        }
        while !bytes.is_empty() {
            if self.remaining == 0 {
                let wanted = HEADER - self.header.len();
                let taken = wanted.min(bytes.len());
                self.header.extend_from_slice(&bytes[..taken]);
                bytes = &bytes[taken..];
                if self.header.len() < HEADER {
                    break;
                }
                let stream = self.header[0];
                if stream != 1 && stream != 2 {
                    return Err(ChannelError::Framing);
                }
                let length = u32::from_be_bytes([
                    self.header[4],
                    self.header[5],
                    self.header[6],
                    self.header[7],
                ]);
                self.header.clear();
                self.stream = stream;
                self.remaining = usize::try_from(length).map_err(|_| ChannelError::Framing)?;
                continue;
            }
            let taken = self.remaining.min(bytes.len());
            let payload = &bytes[..taken];
            bytes = &bytes[taken..];
            self.remaining -= taken;
            if self.stream == 2 {
                self.stderr += payload.len();
                if self.stderr > MAX_STDERR {
                    return Err(ChannelError::Stderr);
                }
            } else {
                self.stdout.extend_from_slice(payload);
                if self.stdout.len() > limit + CHUNK {
                    return Err(ChannelError::Excess);
                }
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests;
