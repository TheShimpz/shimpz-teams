use std::sync::Mutex;

use team_core::entropy::OsEntropy;
use team_core::future::BoxFuture;
use tokio::io::{AsyncReadExt, AsyncWriteExt, DuplexStream, duplex};
use tokio::time::Instant;

use super::{MAX_HEADER_LINES, MAX_NAME_BYTES, ReleaseError, release};
use crate::browser::channel::Channel;
use crate::browser::channel::tests::frame;
use crate::browser::view::{RelayUnavailable, Relays};
use team_protocol::browser_view;

const AUTHORIZATION: &str = "Basic Y29udHJvbGxlcjp0ZXN0";

fn server_frame(payload: &[u8]) -> Vec<u8> {
    let mut bytes = vec![0x82, u8::try_from(payload.len()).expect("short")];
    bytes.extend_from_slice(payload);
    bytes
}

/// Unmasks every complete client frame in `data`.
fn client_frames(data: &[u8]) -> Vec<Vec<u8>> {
    let mut frames = Vec::new();
    let mut offset = 0;
    while offset + 2 <= data.len() {
        let mut length = usize::from(data[offset + 1] & 0x7f);
        let mut start = offset + 2;
        if length == 126 {
            length = usize::from(u16::from_be_bytes([data[start], data[start + 1]]));
            start += 2;
        }
        if start + 4 + length > data.len() {
            break;
        }
        let mask = &data[start..start + 4];
        frames.push(
            data[start + 4..start + 4 + length]
                .iter()
                .enumerate()
                .map(|(index, byte)| byte ^ mask[index % 4])
                .collect(),
        );
        offset = start + 4 + length;
    }
    frames
}

/// How the scripted display answers.
#[derive(Clone)]
struct Script {
    upgrade: bool,
    update: bool,
    version: Vec<u8>,
    security: Vec<u8>,
    result: Vec<u8>,
    first: Vec<u8>,
    raw: Vec<u8>,
    extra_headers: usize,
    name_length: u32,
}

impl Default for Script {
    fn default() -> Self {
        Self {
            upgrade: true,
            update: true,
            version: b"RFB 003.008\n".to_vec(),
            security: vec![1, 1],
            result: vec![0, 0, 0, 0],
            first: vec![0, 0, 0, 0],
            raw: Vec::new(),
            extra_headers: 0,
            name_length: 4,
        }
    }
}

/// The display's 101 for `key` and its RFB greeting up to the server init, as `script` says.
fn greeting(script: &Script, key: &str) -> Vec<u8> {
    let mut answer = format!(
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\
         Sec-WebSocket-Accept: {}\r\nSec-WebSocket-Protocol: binary\r\n",
        browser_view::accept(key)
    );
    answer.push_str(&"X-Extra: 1\r\n".repeat(script.extra_headers));
    answer.push_str("\r\n");
    let mut init = vec![5, 0, 3, 32];
    init.extend_from_slice(&[0; 16]);
    init.extend_from_slice(&script.name_length.to_be_bytes());
    init.extend_from_slice(b"test");
    let mut out = answer.into_bytes();
    out.extend_from_slice(&script.raw);
    for part in [&script.version, &script.security, &script.result, &init] {
        out.extend_from_slice(&server_frame(part));
    }
    out
}

async fn request(far: &mut DuplexStream) -> Option<String> {
    let mut request = Vec::new();
    let mut buffer = [0_u8; 4096];
    while !request.windows(4).any(|window| window == b"\r\n\r\n") {
        let read = far.read(&mut buffer).await.ok().filter(|read| *read > 0)?;
        request.extend_from_slice(&buffer[..read]);
    }
    String::from_utf8(request).ok()
}

async fn serve(mut far: DuplexStream, script: Script) -> (String, Vec<Vec<u8>>) {
    let mut buffer = [0_u8; 4096];
    let Some(request) = request(&mut far).await else {
        return (String::new(), Vec::new());
    };
    if !script.upgrade {
        let _ = far
            .write_all(&frame(1, b"HTTP/1.1 401 Forbidden\r\n\r\n"))
            .await;
        return (request, Vec::new());
    }
    let key = request
        .lines()
        .find_map(|line| line.strip_prefix("Sec-WebSocket-Key: "))
        .expect("a key")
        .trim()
        .to_owned();
    if far
        .write_all(&frame(1, &greeting(&script, &key)))
        .await
        .is_err()
    {
        return (request, Vec::new());
    }
    let mut received = Vec::new();
    while client_frames(&received).len() < 6 {
        match far.read(&mut buffer).await {
            Ok(0) | Err(_) => return (request, client_frames(&received)),
            Ok(read) => received.extend_from_slice(&buffer[..read]),
        }
    }
    if script.update {
        let _ = far.write_all(&frame(1, &server_frame(&script.first))).await;
        // Hold the stream open until Team closes it.
        let _ = far.read(&mut buffer).await;
    }
    (request, client_frames(&received))
}

struct OneRelay {
    channel: Mutex<Option<Channel>>,
    proven: bool,
    exits: Mutex<Vec<String>>,
}

impl Relays for OneRelay {
    fn open_relay(&self) -> BoxFuture<'_, Result<(String, Channel), RelayUnavailable>> {
        Box::pin(async move {
            let channel = self.channel.lock().expect("usable").take();
            channel
                .map(|channel| ("exec-1".to_owned(), channel))
                .ok_or(RelayUnavailable)
        })
    }

    fn exited<'a>(&'a self, exec_id: &'a str, _deadline: Instant) -> BoxFuture<'a, bool> {
        Box::pin(async move {
            self.exits.lock().expect("usable").push(exec_id.to_owned());
            self.proven
        })
    }
}

async fn run(
    script: Script,
    proven: bool,
) -> (Result<(), ReleaseError>, String, Vec<Vec<u8>>, Vec<String>) {
    let (team, far) = duplex(1024 * 1024);
    let relays = OneRelay {
        channel: Mutex::new(Some(Channel::new(Box::new(team)))),
        proven,
        exits: Mutex::new(Vec::new()),
    };
    let display = tokio::spawn(serve(far, script));
    let outcome = release(&relays, AUTHORIZATION, &OsEntropy).await;
    let (request, frames) = display.await.expect("joined");
    let exits = relays.exits.lock().expect("usable").clone();
    (outcome, request, frames, exits)
}

#[tokio::test]
async fn team_releases_every_button_as_the_writer_and_presses_nothing() {
    let (outcome, request, frames, exits) = run(Script::default(), true).await;
    assert_eq!(outcome, Ok(()));
    assert!(request.contains(&format!("Authorization: {AUTHORIZATION}\r\n")));
    assert_eq!(frames.len(), 6);
    assert_eq!(frames[0], b"RFB 003.008\n");
    assert_eq!(frames[1], [1]);
    assert_eq!(frames[2], [1]);
    assert_eq!(frames[3][0], 2);
    assert_eq!(frames[4], [5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
    assert_eq!(frames[5][0], 3);
    assert!(!frames.iter().any(|frame| frame.first() == Some(&4)));
    assert_eq!(exits, ["exec-1"]);
}

#[tokio::test]
async fn a_release_whose_relay_is_not_proven_ended_is_no_release() {
    let (outcome, ..) = run(Script::default(), false).await;
    assert_eq!(outcome, Err(ReleaseError));
}

#[tokio::test(start_paused = true)]
async fn a_refused_stream_or_a_display_that_never_answers_is_no_release() {
    for script in [
        Script {
            upgrade: false,
            ..Script::default()
        },
        Script {
            update: false,
            ..Script::default()
        },
    ] {
        let (outcome, ..) = run(script, true).await;
        assert_eq!(outcome, Err(ReleaseError));
    }
}

/// Displays that each break `KasmVNC`'s handshake in one place.
fn foreign() -> Vec<Script> {
    let mut huge = vec![0x82, 127];
    huge.extend_from_slice(&(1_u64 << 21).to_be_bytes());
    let base = Script::default();
    vec![
        Script {
            version: b"HTTP/1.1 200\n".to_vec(),
            ..base.clone()
        },
        Script {
            security: vec![1, 2],
            ..base.clone()
        },
        Script {
            result: vec![0, 0, 0, 1],
            ..base.clone()
        },
        Script {
            first: vec![2, 0, 0, 0],
            ..base.clone()
        },
        Script {
            raw: vec![0x81, 1, b'x'],
            ..base.clone()
        },
        Script {
            raw: huge,
            ..base.clone()
        },
        Script {
            extra_headers: MAX_HEADER_LINES,
            ..base.clone()
        },
        Script {
            name_length: u32::try_from(MAX_NAME_BYTES + 1).expect("small"),
            ..base.clone()
        },
    ]
}

#[tokio::test(start_paused = true)]
async fn a_display_that_speaks_other_than_kasm_does_is_no_release() {
    for script in foreign() {
        let (outcome, ..) = run(script, true).await;
        assert_eq!(outcome, Err(ReleaseError));
    }
    let base = Script::default();
    // A 16-bit length is read as one: two bytes the handshake did not ask for are kept until it does.
    let mut long = vec![0x82, 126];
    long.extend_from_slice(&2_u16.to_be_bytes());
    long.extend_from_slice(&[0, 0]);
    let (outcome, ..) = run(
        Script {
            raw: long,
            version: b"FB 003.008\n".to_vec(),
            ..base
        },
        true,
    )
    .await;
    assert_eq!(outcome, Err(ReleaseError));
}
