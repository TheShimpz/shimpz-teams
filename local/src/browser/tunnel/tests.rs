use std::time::Duration;

use team_core::stop::Stop;
use team_protocol::browser_view;
use tokio::io::{AsyncReadExt, AsyncWriteExt, DuplexStream, duplex};

use super::{Tunnel, TunnelError, admitted, answer, drain, upgrade_request};
use crate::browser::channel::Channel;
use crate::browser::channel::tests::frame;

const KEY: &str = "AAAAAAAAAAAAAAAAAAAAAA==";

fn admitted_answer(key: &str, extra: Option<&str>) -> String {
    let mut lines = vec![
        "HTTP/1.1 101 Switching Protocols".to_owned(),
        "Upgrade: websocket".to_owned(),
        "Connection: Upgrade".to_owned(),
        format!("Sec-WebSocket-Accept: {}", browser_view::accept(key)),
        "Sec-WebSocket-Protocol: binary".to_owned(),
    ];
    if let Some(extra) = extra {
        lines.push(extra.to_owned());
    }
    format!("{}\r\n\r\n", lines.join("\r\n"))
}

fn head(answer: &str) -> Vec<String> {
    let lines: Vec<String> = answer.split("\r\n").map(str::to_owned).collect();
    lines[..lines.len() - 2].to_vec()
}

fn display() -> (Channel, DuplexStream) {
    let (team, display) = duplex(1024 * 1024);
    (Channel::new(Box::new(team)), display)
}

async fn read_request(display: &mut DuplexStream) -> Vec<u8> {
    let mut data = Vec::new();
    let mut buffer = [0_u8; 4096];
    while !data.windows(4).any(|window| window == b"\r\n\r\n") {
        let read = display.read(&mut buffer).await.expect("read");
        assert!(read > 0);
        data.extend_from_slice(&buffer[..read]);
    }
    data
}

#[tokio::test]
async fn the_display_receives_one_canonical_request_and_the_viewer_its_exact_101() {
    let (channel, mut far) = display();
    let mut tunnel = Tunnel::new(channel, Stop::new());
    let display_side = tokio::spawn(async move {
        let request = read_request(&mut far).await;
        let mut reply = admitted_answer(KEY, None).into_bytes();
        reply.extend_from_slice(b"RFB 003.008\n");
        far.write_all(&frame(1, &reply)).await.expect("answered");
        (request, far)
    });
    let first = tunnel.upgrade(KEY, None).await;
    let (request, _far) = display_side.await.expect("joined");
    assert_eq!(request, upgrade_request(KEY, None));
    assert_eq!(first, Ok((answer(KEY), b"RFB 003.008\n".to_vec())));
    let text = String::from_utf8(upgrade_request(KEY, Some("Basic x"))).expect("ascii");
    assert!(text.contains("Origin: http://127.0.0.1:6901\r\n"));
    assert!(text.ends_with("Authorization: Basic x\r\n\r\n"));
}

#[test]
fn anything_but_the_exact_101_for_the_key_is_refused() {
    let base = admitted_answer(KEY, None);
    let refused = [
        base.replace("101 Switching", "200 OK"),
        admitted_answer("BBBBBBBBBBBBBBBBBBBBBA==", None),
        base.replace("binary", "base64"),
        admitted_answer(KEY, Some("Sec-WebSocket-Extensions: permessage-deflate")),
        admitted_answer(KEY, Some("Upgrade: websocket")),
        admitted_answer(KEY, Some("no colon here")),
        base.replace("Upgrade: websocket", "Upgrade: h2c"),
        base.replace("Connection: Upgrade", "Connection: keep-alive"),
    ];
    for answer in refused {
        assert!(!admitted(&head(&answer), KEY), "{answer}");
    }
    assert!(!admitted(&[], KEY));
    assert!(admitted(&head(&base), KEY));
}

#[tokio::test(start_paused = true)]
async fn an_upgrade_the_display_never_answers_overflows_or_refuses_fails() {
    let (channel, _silent) = display();
    assert_eq!(
        Tunnel::new(channel, Stop::new()).upgrade(KEY, None).await,
        Err(TunnelError::Refused)
    );
    let oversized = format!(
        "HTTP/1.1 101 Switching Protocols\r\nX-Filler: {}\r\n\r\n",
        "a".repeat(5000)
    );
    let filler = (0..200)
        .map(|index| format!("X-Filler-{index:03}: aaaaaaaaaa\r\n"))
        .collect::<Vec<_>>()
        .concat();
    let many = format!("HTTP/1.1 101 Switching Protocols\r\n{filler}\r\n");
    let not_found = admitted_answer(KEY, None).replace("101 Switching", "404 Not");
    for reply in [oversized, many, not_found] {
        let (channel, mut far) = display();
        far.write_all(&frame(1, reply.as_bytes()))
            .await
            .expect("answered");
        assert_eq!(
            Tunnel::new(channel, Stop::new()).upgrade(KEY, None).await,
            Err(TunnelError::Refused)
        );
    }
}

#[tokio::test]
async fn bytes_cross_both_ways_until_a_side_ends() {
    let (channel, mut far) = display();
    let (team_viewer, mut viewer) = duplex(1024 * 1024);
    let mut tunnel = Tunnel::new(channel, Stop::new());
    let ended = Stop::new();
    let peer = tokio::spawn(async move {
        viewer.write_all(b"to the display").await.expect("sent");
        assert_eq!(drain(&mut far, 14).await, b"to the display");
        far.write_all(&frame(1, b"to the viewer"))
            .await
            .expect("sent");
        far.write_all(&frame(2, b"noise on stderr"))
            .await
            .expect("sent");
        assert_eq!(drain(&mut viewer, 13).await, b"to the viewer");
        viewer.shutdown().await.expect("closed");
        (viewer, far)
    });
    let outcome = tunnel.splice(Box::new(team_viewer), &ended).await;
    drop(peer.await);
    assert_eq!(outcome, Ok(()));
    assert_eq!(tunnel.carried.to_viewer.into_inner(), 13);
    assert_eq!(tunnel.carried.to_display.into_inner(), 14);
}

#[tokio::test]
async fn the_display_ending_ends_the_stream() {
    let (channel, far) = display();
    let (team_viewer, _viewer) = duplex(1024);
    drop(far);
    let mut tunnel = Tunnel::new(channel, Stop::new());
    assert_eq!(
        tunnel.splice(Box::new(team_viewer), &Stop::new()).await,
        Ok(())
    );
}

#[tokio::test(start_paused = true)]
async fn an_interrupt_the_view_ending_or_the_stream_s_life_stops_it_at_once() {
    for which in ["interrupt", "ended", "life"] {
        let (channel, _far) = display();
        let (team_viewer, _viewer) = duplex(1024);
        let interrupt = Stop::new();
        let ended = Stop::new();
        let mut tunnel = Tunnel::new(channel, interrupt.clone());
        match which {
            "interrupt" => interrupt.stop(),
            "ended" => ended.stop(),
            _ => {}
        }
        let started = tokio::time::Instant::now();
        assert_eq!(tunnel.splice(Box::new(team_viewer), &ended).await, Ok(()));
        let took = started.elapsed();
        if which == "life" {
            assert_eq!(took, super::LIFE_SECONDS);
        } else {
            assert!(took < Duration::from_secs(1));
        }
    }
}

#[tokio::test(start_paused = true)]
async fn a_viewer_that_stops_reading_ends_the_stream_and_buffers_stay_bounded() {
    let (channel, mut far) = display();
    let (team_viewer, _viewer) = duplex(4096);
    let mut tunnel = Tunnel::new(channel, Stop::new());
    let feeder = tokio::spawn(async move {
        for _ in 0..64 {
            if far.write_all(&frame(1, &vec![b'x'; 60_000])).await.is_err() {
                break;
            }
        }
        far
    });
    assert_eq!(
        tunnel.splice(Box::new(team_viewer), &Stop::new()).await,
        Err(TunnelError::Stalled)
    );
    // Only what the viewer's buffer took was carried; Team held at most one read beside it.
    assert!(tunnel.carried.to_viewer.into_inner() <= 4096);
    feeder.abort();
}

#[tokio::test]
async fn a_viewer_connection_that_fails_ends_the_stream() {
    let (channel, mut far) = display();
    let (team_viewer, viewer) = duplex(1024);
    drop(viewer);
    far.write_all(&frame(1, b"to the viewer"))
        .await
        .expect("sent");
    let mut tunnel = Tunnel::new(channel, Stop::new());
    let outcome = tunnel.splice(Box::new(team_viewer), &Stop::new()).await;
    // The viewer's closed side either ends the copy from it or fails the copy to it.
    assert!(matches!(outcome, Ok(()) | Err(TunnelError::Failed)));
}
