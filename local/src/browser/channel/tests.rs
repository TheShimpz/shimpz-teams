use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt, DuplexStream, duplex};
use tokio::time::Instant;

use super::{CHUNK, Channel, ChannelError, MAX_STDERR};

pub(crate) fn frame(stream: u8, payload: &[u8]) -> Vec<u8> {
    let mut bytes = vec![stream, 0, 0, 0];
    bytes.extend_from_slice(
        &u32::try_from(payload.len())
            .expect("a test frame fits")
            .to_be_bytes(),
    );
    bytes.extend_from_slice(payload);
    bytes
}

fn pair() -> (Channel, DuplexStream) {
    let (team, docker) = duplex(4 * 1024 * 1024);
    (Channel::new(Box::new(team)), docker)
}

fn soon() -> Instant {
    Instant::now() + Duration::from_secs(10)
}

async fn fed(chunks: &[&[u8]]) -> Channel {
    let (channel, mut docker) = pair();
    for chunk in chunks {
        docker.write_all(chunk).await.expect("fed");
    }
    docker.shutdown().await.expect("closed");
    drop(docker);
    channel
}

#[tokio::test]
async fn lines_and_exact_bytes_come_from_stdout_frames_only() {
    let mut channel = fed(&[
        &frame(2, b"a warning\n"),
        &frame(1, b"{\"id\":1}\n{\"id\""),
        &frame(1, b":2}\n12\nabc"),
        &frame(1, b"defghijkl"),
    ])
    .await;
    assert_eq!(
        channel.receive_line(soon(), 64).await,
        Ok(b"{\"id\":1}".to_vec())
    );
    assert_eq!(
        channel.receive_line(soon(), 64).await,
        Ok(b"{\"id\":2}".to_vec())
    );
    assert_eq!(channel.receive_line(soon(), 64).await, Ok(b"12".to_vec()));
    assert_eq!(
        channel.receive_exact(12, soon()).await,
        Ok(b"abcdefghijkl".to_vec())
    );
    assert_eq!(channel.receive_end(soon()).await, Ok(()));
}

#[tokio::test]
async fn a_frame_split_across_reads_is_reassembled() {
    let (mut channel, mut docker) = pair();
    let whole = frame(1, b"split line\n");
    docker.write_all(&whole[..3]).await.expect("fed");
    let writer = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(20)).await;
        docker.write_all(&whole[3..]).await.expect("fed");
        docker
    });
    assert_eq!(
        channel.receive_line(soon(), 64).await,
        Ok(b"split line".to_vec())
    );
    drop(writer.await);
}

#[tokio::test]
async fn input_is_written_whole_and_can_be_closed() {
    let (team, mut docker) = duplex(64 * 1024);
    let mut channel = Channel::new(Box::new(team));
    let reader = tokio::spawn(async move {
        let mut received = Vec::new();
        docker.read_to_end(&mut received).await.expect("read");
        received
    });
    channel
        .send(&vec![b'x'; 300_000], soon())
        .await
        .expect("written");
    channel.close_input().await;
    channel.close_input().await;
    assert_eq!(reader.await.expect("joined").len(), 300_000);
}

#[tokio::test(start_paused = true)]
async fn a_full_input_buffer_or_a_passed_deadline_outlives_no_deadline() {
    let (team, _docker) = duplex(1024);
    let mut channel = Channel::new(Box::new(team));
    let deadline = Instant::now() + Duration::from_secs(5);
    assert_eq!(
        channel.send(&vec![b'x'; 10_000], deadline).await,
        Err(ChannelError::Deadline)
    );
    assert_eq!(
        channel.send(b"x", Instant::now()).await,
        Err(ChannelError::Deadline)
    );
}

#[tokio::test]
async fn bounds_framing_and_endings_are_enforced() {
    let stderr = vec![b'e'; MAX_STDERR + 1];
    let long = vec![b'x'; 100];
    let mut long_line = long.clone();
    long_line.push(b'\n');
    let cut = frame(1, b"cut");
    let cases: Vec<(Vec<u8>, &str, ChannelError)> = vec![
        (frame(1, &long), "line", ChannelError::Oversized),
        (frame(1, &long_line), "line", ChannelError::Oversized),
        (frame(3, b"x"), "line", ChannelError::Framing),
        (frame(0, b"x"), "line", ChannelError::Framing),
        (frame(2, &stderr), "line", ChannelError::Stderr),
        (frame(1, b"partial"), "line", ChannelError::Closed),
        (frame(1, b"short"), "exact", ChannelError::Closed),
        (frame(1, b"tail"), "end", ChannelError::Trailing),
        (
            cut[..cut.len() - 1].to_vec(),
            "end",
            ChannelError::EndedInFrame,
        ),
        (
            frame(1, b"x")[..5].to_vec(),
            "end",
            ChannelError::EndedInFrame,
        ),
    ];
    for (bytes, read, expected) in cases {
        let mut channel = fed(&[&bytes]).await;
        let outcome = match read {
            "line" => channel.receive_line(soon(), 50).await.map(|_| ()),
            "exact" => channel.receive_exact(10, soon()).await.map(|_| ()),
            _ => channel.receive_end(soon()).await,
        };
        assert_eq!(outcome, Err(expected), "{read}");
    }
}

#[test]
fn stdout_past_its_bound_is_refused_as_it_arrives_never_buffered_whole() {
    let (team, _docker) = duplex(16);
    let mut channel = Channel::new(Box::new(team));
    // A frame announcing 4 GiB: its header is admitted, and its payload is counted as it arrives.
    let mut announced = vec![1, 0, 0, 0];
    announced.extend_from_slice(&u32::MAX.to_be_bytes());
    assert_eq!(channel.demux.absorb(&announced, 10), Ok(()));
    assert_eq!(channel.demux.absorb(&vec![b'x'; CHUNK], 10), Ok(()));
    assert_eq!(
        channel.demux.absorb(&[b'x'; 11], 10),
        Err(ChannelError::Excess)
    );
}

#[tokio::test(start_paused = true)]
async fn a_silent_stream_fails_at_its_deadline() {
    let (mut channel, _docker) = pair();
    assert_eq!(
        channel.receive_line(Instant::now(), 10).await,
        Err(ChannelError::Deadline)
    );
    let deadline = Instant::now() + Duration::from_millis(10);
    assert_eq!(
        channel.receive_line(deadline, 10).await,
        Err(ChannelError::Deadline)
    );
}

#[tokio::test]
async fn a_chunk_read_returns_what_arrived_then_nothing_at_the_end() {
    let mut channel = fed(&[&frame(1, b"abcdef")]).await;
    assert_eq!(channel.receive_chunk(4, soon()).await, Ok(b"abcd".to_vec()));
    assert_eq!(channel.receive_chunk(4, soon()).await, Ok(b"ef".to_vec()));
    assert_eq!(channel.receive_chunk(4, soon()).await, Ok(Vec::new()));
}
