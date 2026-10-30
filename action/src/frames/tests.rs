use std::time::Duration;

use strict_json::Value;
use team_core::future::BoxFuture;
use tokio::io::{AsyncReadExt, AsyncWriteExt, DuplexStream, duplex};
use tokio::time::Instant;

use super::{CallBroker, FrameError, FrameReader, exchange};

pub(crate) fn frame(stream: u8, payload: &[u8]) -> Vec<u8> {
    let mut framed = vec![stream, 0, 0, 0];
    framed.extend_from_slice(&u32::try_from(payload.len()).expect("small").to_be_bytes());
    framed.extend_from_slice(payload);
    framed
}

fn soon() -> Instant {
    Instant::now() + Duration::from_secs(5)
}

/// Writes `output` from the workload's end in `pieces`, then closes it.
fn replay(
    output: Vec<u8>,
    pieces: &[usize],
    buffer: usize,
) -> (DuplexStream, tokio::task::JoinHandle<()>) {
    let (ours, mut workload) = duplex(buffer);
    let pieces = pieces.to_vec();
    let task = tokio::spawn(async move {
        let mut offset = 0;
        for size in pieces {
            let _ = workload.write_all(&output[offset..offset + size]).await;
            offset += size;
            tokio::task::yield_now().await;
        }
        let _ = workload.write_all(&output[offset..]).await;
        let _ = workload.shutdown().await;
        let mut sink = Vec::new();
        let _ = workload.read_to_end(&mut sink).await;
    });
    (ours, task)
}

async fn read_all(
    output: Vec<u8>,
    pieces: &[usize],
    maximum: usize,
) -> Result<(Vec<u8>, Vec<u8>), FrameError> {
    let (ours, task) = replay(output, pieces, 64 * 1024);
    let result = exchange(Box::new(ours), b"", soon(), maximum, None).await;
    let _ = task.await;
    result
}

#[tokio::test]
async fn split_stdout_and_stderr_frames_are_read_exactly() {
    let payload = [
        frame(1, br#"{"ok":"#),
        frame(2, b"warning"),
        frame(1, b"true}"),
    ]
    .concat();
    for pieces in [&[1, 2, 5, 3, 7][..], &[4, 1, 6, 2]] {
        let read = read_all(payload.clone(), pieces, 512 * 1024).await;
        assert_eq!(read, Ok((br#"{"ok":true}"#.to_vec(), b"warning".to_vec())));
    }
}

#[tokio::test]
async fn exec_frame_over_budget_refused_without_buffering() {
    let mut oversized = vec![1, 0, 0, 0];
    oversized.extend_from_slice(&(512 * 1024 + 2_u32).to_be_bytes());
    for malformed in [
        b"\x01\x00\x00".to_vec(),
        frame(1, b"payload")[..13].to_vec(),
        oversized,
        b"garbage!".to_vec(),
    ] {
        assert_eq!(
            read_all(malformed, &[], 512 * 1024).await,
            Err(FrameError::Invalid)
        );
    }
    let cumulative = [frame(1, b"ab"), frame(2, b"cd")].concat();
    assert_eq!(read_all(cumulative, &[], 3).await, Err(FrameError::Invalid));
    // A frame announcing far more than the bound is refused from its header alone.
    let mut reader = FrameReader::new(16);
    assert_eq!(
        reader.feed(&[1, 0, 0, 0, 0xff, 0xff, 0xff, 0xff]),
        Err(FrameError::Invalid)
    );
}

#[tokio::test]
async fn a_clean_eof_is_the_only_empty_success_and_the_deadline_bounds_the_wait() {
    assert_eq!(
        read_all(Vec::new(), &[], 1024).await,
        Ok((Vec::new(), Vec::new()))
    );
    let (ours, _workload) = duplex(1024);
    let expired = exchange(Box::new(ours), b"", Instant::now(), 3, None).await;
    assert_eq!(expired, Err(FrameError::Timeout));
}

#[tokio::test]
async fn a_workload_that_answers_before_reading_its_input_never_stalls() {
    let (ours, mut workload) = duplex(4096);
    let answer = vec![b'y'; 300_000];
    let expected = answer.clone();
    let task = tokio::spawn(async move {
        workload
            .write_all(&frame(1, &answer))
            .await
            .expect("written");
        let mut received = Vec::new();
        workload.read_to_end(&mut received).await.expect("read");
        workload.shutdown().await.expect("closed");
        received.len()
    });
    let read = exchange(
        Box::new(ours),
        &vec![b'x'; 1_000_000],
        soon(),
        512 * 1024,
        None,
    )
    .await;
    assert_eq!(read, Ok((expected, Vec::new())));
    assert_eq!(task.await.expect("joined"), 1_000_000);
}

#[tokio::test]
async fn exec_half_close_delivers_eof() {
    let (ours, mut workload) = duplex(1024);
    let task = tokio::spawn(async move {
        let mut request = Vec::new();
        workload
            .read_to_end(&mut request)
            .await
            .expect("stdin reaches EOF");
        workload
            .write_all(&frame(1, &request))
            .await
            .expect("written");
        workload.shutdown().await.expect("closed");
    });
    let read = exchange(Box::new(ours), b"request", soon(), 1024, None).await;
    assert_eq!(read, Ok((b"request".to_vec(), Vec::new())));
    task.await.expect("joined");
}

#[tokio::test]
async fn a_workload_that_exits_before_reading_all_input_ends_the_exchange() {
    let (ours, mut workload) = duplex(1024);
    workload
        .write_all(&frame(1, br#"{"ok":true}"#))
        .await
        .expect("written");
    drop(workload);
    let read = exchange(Box::new(ours), &vec![b'x'; 1_000_000], soon(), 1024, None).await;
    assert_eq!(read, Ok((br#"{"ok":true}"#.to_vec(), Vec::new())));
}

/// Refuses every call, recording each frame and how often a held reply was released.
#[derive(Default)]
pub(crate) struct Refusing {
    pub frames: Vec<Value>,
    pub held: bool,
    pub released: usize,
    pub delay: Duration,
    pub fail: bool,
}

impl CallBroker for Refusing {
    fn answer(
        &mut self,
        frame: Value,
        _deadline: Instant,
    ) -> BoxFuture<'_, Result<Vec<u8>, FrameError>> {
        Box::pin(async move {
            tokio::time::sleep(self.delay).await;
            if self.fail {
                return Err(FrameError::Transport);
            }
            self.frames.push(frame);
            self.held = true;
            Ok(br#"{"error":"refused"}"#.to_vec())
        })
    }

    fn calls(&self) -> usize {
        self.frames.len()
    }

    fn release(&mut self) {
        if self.held {
            self.held = false;
            self.released += 1;
        }
    }
}

const CALL: &[u8] =
    br#"{"type":"fetch","method":"GET","url":"https://api.example.com/","headers":[]}"#;
const TERMINAL: &[u8] = b"{\"type\":\"result\",\"result\":{}}\n";

async fn lines(stream: &mut DuplexStream, count: usize) -> Vec<Vec<u8>> {
    let mut buffer = Vec::new();
    let mut chunk = [0_u8; 4096];
    while buffer.split(|byte| *byte == b'\n').count() <= count {
        let read = stream.read(&mut chunk).await.expect("read");
        if read == 0 {
            break;
        }
        buffer.extend_from_slice(&chunk[..read]);
    }
    buffer
        .split(|byte| *byte == b'\n')
        .take(count)
        .map(<[u8]>::to_vec)
        .collect()
}

#[tokio::test]
async fn each_call_is_answered_in_order_and_the_terminal_line_ends_the_exchange() {
    let (ours, mut workload) = duplex(64 * 1024);
    let task = tokio::spawn(async move {
        let mut seen = lines(&mut workload, 1).await;
        for _ in 0..2 {
            workload
                .write_all(&frame(1, &[CALL, b"\n"].concat()))
                .await
                .expect("written");
            seen.extend(lines(&mut workload, 1).await);
        }
        workload
            .write_all(&frame(1, TERMINAL))
            .await
            .expect("written");
        workload.shutdown().await.expect("closed");
        seen
    });
    let mut broker = Refusing::default();
    let read = exchange(
        Box::new(ours),
        br#"{"input":{}}"#,
        soon(),
        4096,
        Some(&mut broker),
    )
    .await;
    assert_eq!(read, Ok((TERMINAL.to_vec(), Vec::new())));
    let seen = task.await.expect("joined");
    assert_eq!(
        seen,
        [
            br#"{"input":{}}"#.to_vec(),
            br#"{"error":"refused"}"#.to_vec(),
            br#"{"error":"refused"}"#.to_vec()
        ]
    );
    assert_eq!((broker.calls(), broker.held), (2, false));
    assert_eq!(
        broker.frames[0].get("url"),
        Some(&Value::String("https://api.example.com/".to_owned()))
    );
}

#[tokio::test]
async fn pipelined_calls_are_answered_one_at_a_time() {
    let (ours, mut workload) = duplex(64 * 1024);
    let task = tokio::spawn(async move {
        let mut seen = lines(&mut workload, 1).await;
        workload
            .write_all(&frame(1, &[CALL, b"\n"].concat().repeat(3)))
            .await
            .expect("written");
        seen.extend(lines(&mut workload, 3).await);
        workload
            .write_all(&frame(1, TERMINAL))
            .await
            .expect("written");
        workload.shutdown().await.expect("closed");
        seen
    });
    let mut broker = Refusing::default();
    let read = exchange(
        Box::new(ours),
        br#"{"input":{}}"#,
        soon(),
        4096,
        Some(&mut broker),
    )
    .await;
    assert_eq!(read.map(|(stdout, _)| stdout), Ok(TERMINAL.to_vec()));
    assert_eq!(
        (broker.calls(), broker.released, broker.held),
        (3, 3, false)
    );
    assert_eq!(
        task.await.expect("joined")[1..],
        [
            br#"{"error":"refused"}"#.to_vec(),
            br#"{"error":"refused"}"#.to_vec(),
            br#"{"error":"refused"}"#.to_vec()
        ]
    );
}

#[tokio::test]
async fn a_reply_that_cannot_be_written_ends_the_attempt_before_any_later_call() {
    let (ours, mut workload) = duplex(64 * 1024);
    let task = tokio::spawn(async move {
        lines(&mut workload, 1).await;
        let output = [CALL, b"\n", CALL, b"\n", TERMINAL].concat();
        workload
            .write_all(&frame(1, &output))
            .await
            .expect("written");
        drop(workload);
    });
    let mut broker = Refusing {
        delay: Duration::from_millis(200),
        ..Refusing::default()
    };
    let read = exchange(
        Box::new(ours),
        br#"{"input":{}}"#,
        soon(),
        4096,
        Some(&mut broker),
    )
    .await;
    task.await.expect("joined");
    assert_eq!(read, Err(FrameError::Transport));
    assert_eq!((broker.calls(), broker.held), (1, false));
}

#[tokio::test]
async fn exec_trailing_output_after_terminal_refused() {
    for output in [
        [TERMINAL, b"{\"type\":\"fetch\"}\n"].concat(),
        b"not json\n".to_vec(),
    ] {
        let (ours, task) = replay(frame(1, &output), &[], 64 * 1024);
        let mut broker = Refusing::default();
        let read = exchange(
            Box::new(ours),
            br#"{"input":{}}"#,
            soon(),
            4096,
            Some(&mut broker),
        )
        .await;
        let _ = task.await;
        assert_eq!(read, Err(FrameError::Invalid));
    }
}

#[tokio::test]
async fn a_broker_that_cannot_answer_ends_the_attempt_as_a_transport_fault() {
    let (ours, mut workload) = duplex(64 * 1024);
    let task = tokio::spawn(async move {
        lines(&mut workload, 1).await;
        workload
            .write_all(&frame(1, b"{\"type\":\"fetch\"}\n"))
            .await
            .expect("written");
        workload
    });
    let mut broker = Refusing {
        fail: true,
        ..Refusing::default()
    };
    let read = exchange(
        Box::new(ours),
        br#"{"input":{}}"#,
        soon(),
        4096,
        Some(&mut broker),
    )
    .await;
    assert_eq!(read, Err(FrameError::Transport));
    drop(task.await);
}
