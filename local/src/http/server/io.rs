//! A connection whose every write is bounded: a peer that stops reading fails the write after the idle bound instead
//! of holding an admission slot forever. Reads are bounded by the request's absolute deadlines instead, so a long
//! stream that reads nothing is never cut by an idle read.

use std::future::Future;
use std::io;
use std::pin::Pin;
use std::task::{Context, Poll};
use std::time::Duration;

use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::time::{Instant, Sleep};

/// `inner` with an idle bound on each pending write, flush, or shutdown.
#[derive(Debug)]
pub struct IdleWrite<T> {
    inner: T,
    idle: Duration,
    timer: Pin<Box<Sleep>>,
    armed: bool,
}

impl<T> IdleWrite<T> {
    /// Bounds each write of `inner` by `idle`.
    pub fn new(inner: T, idle: Duration) -> Self {
        Self {
            inner,
            idle,
            timer: Box::pin(tokio::time::sleep(idle)),
            armed: false,
        }
    }

    /// Settles one write step: a ready step disarms the timer, a pending one arms it once and fails once it fires.
    fn settle<R>(
        &mut self,
        cx: &mut Context<'_>,
        step: Poll<io::Result<R>>,
    ) -> Poll<io::Result<R>> {
        if step.is_ready() {
            self.armed = false;
            return step;
        }
        if !self.armed {
            self.timer.as_mut().reset(Instant::now() + self.idle);
            self.armed = true;
        }
        match self.timer.as_mut().poll(cx) {
            Poll::Ready(()) => Poll::Ready(Err(io::ErrorKind::TimedOut.into())),
            Poll::Pending => Poll::Pending,
        }
    }
}

impl<T: AsyncRead + Unpin> AsyncRead for IdleWrite<T> {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().inner).poll_read(cx, buf)
    }
}

impl<T: AsyncWrite + Unpin> AsyncWrite for IdleWrite<T> {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        let step = Pin::new(&mut this.inner).poll_write(cx, buf);
        this.settle(cx, step)
    }

    fn poll_write_vectored(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bufs: &[io::IoSlice<'_>],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        let step = Pin::new(&mut this.inner).poll_write_vectored(cx, bufs);
        this.settle(cx, step)
    }

    fn is_write_vectored(&self) -> bool {
        self.inner.is_write_vectored()
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        let step = Pin::new(&mut this.inner).poll_flush(cx);
        this.settle(cx, step)
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        let step = Pin::new(&mut this.inner).poll_shutdown(cx);
        this.settle(cx, step)
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    use super::IdleWrite;

    #[tokio::test(start_paused = true)]
    async fn a_write_the_peer_never_reads_fails_after_the_idle_bound() {
        let (near, mut far) = tokio::io::duplex(64);
        let mut bounded = IdleWrite::new(near, Duration::from_secs(10));
        bounded.write_all(&[1; 64]).await.expect("buffered");
        let started = tokio::time::Instant::now();
        let error = bounded.write_all(&[2; 64]).await.expect_err("stalled");
        assert_eq!(error.kind(), std::io::ErrorKind::TimedOut);
        assert_eq!(started.elapsed(), Duration::from_secs(10));
        // A peer that reads keeps every write within the bound however long the exchange lasts.
        let mut buffer = [0; 64];
        far.read_exact(&mut buffer).await.expect("read");
        bounded.write_all(&[3; 64]).await.expect("written");
        tokio::time::sleep(Duration::from_secs(30)).await;
        far.read_exact(&mut buffer).await.expect("read");
        bounded
            .write_all(&[4; 8])
            .await
            .expect("written after a long pause");
        bounded.flush().await.expect("flushed");
    }
}
