//! A real HTTP peer for the Brain's closed thread-deletion contract: `POST /v1/threads/delete` with exactly
//! `{"thread_id": <string>}` answers `{"status":"deleted"}`, anything else 400. A Team teardown deletes the Brain
//! threads of its Team through it.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{IpAddr, SocketAddr, TcpListener, TcpStream};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use strict_json::Value;

/// The largest request body read.
const MAX_BODY_BYTES: usize = 64 * 1024;

/// The running fake Brain.
#[derive(Debug)]
pub struct Brain {
    address: SocketAddr,
    stopping: Arc<AtomicBool>,
    deletions: Arc<AtomicUsize>,
    thread: Option<JoinHandle<()>>,
}

impl Brain {
    /// Serves on `host`, at a port the system chooses.
    pub fn start(host: IpAddr) -> std::io::Result<Self> {
        let listener = TcpListener::bind((host, 0))?;
        let address = listener.local_addr()?;
        let stopping = Arc::new(AtomicBool::new(false));
        let deletions = Arc::new(AtomicUsize::new(0));
        let thread = {
            let stopping = Arc::clone(&stopping);
            let deletions = Arc::clone(&deletions);
            thread::spawn(move || {
                for stream in listener.incoming() {
                    if stopping.load(Ordering::SeqCst) {
                        break;
                    }
                    if let Ok(stream) = stream {
                        let _ = answer(stream, &deletions);
                    }
                }
            })
        };
        Ok(Self {
            address,
            stopping,
            deletions,
            thread: Some(thread),
        })
    }

    /// The URL the controller reaches it at.
    pub fn url(&self) -> String {
        format!("http://{}", self.address)
    }

    /// How many valid thread deletions it answered.
    pub fn deletions(&self) -> usize {
        self.deletions.load(Ordering::SeqCst)
    }

    /// Stops serving and waits for its thread.
    pub fn stop(&mut self) {
        self.stopping.store(true, Ordering::SeqCst);
        let _ = TcpStream::connect_timeout(&self.address, Duration::from_secs(1));
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

impl Drop for Brain {
    fn drop(&mut self) {
        self.stop();
    }
}

fn answer(stream: TcpStream, deletions: &AtomicUsize) -> std::io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(10)))?;
    let mut reader = BufReader::new(stream.try_clone()?);
    let mut request_line = String::new();
    reader.read_line(&mut request_line)?;
    let mut length = 0_usize;
    loop {
        let mut line = String::new();
        reader.read_line(&mut line)?;
        let line = line.trim_end();
        if line.is_empty() {
            break;
        }
        if let Some((name, value)) = line.split_once(':')
            && name.trim().eq_ignore_ascii_case("content-length")
        {
            length = value.trim().parse().unwrap_or(usize::MAX);
        }
    }
    let mut body = vec![0_u8; length.min(MAX_BODY_BYTES + 1)];
    reader.read_exact(&mut body)?;
    let valid = length <= MAX_BODY_BYTES
        && request_line.starts_with("POST /v1/threads/delete ")
        && deletion(&body);
    if valid {
        deletions.fetch_add(1, Ordering::SeqCst);
    }
    let (status, payload): (&str, &[u8]) = if valid {
        ("200 OK", br#"{"status": "deleted"}"#)
    } else {
        ("400 Bad Request", br#"{"error": "invalid request"}"#)
    };
    let mut stream = stream;
    write!(
        stream,
        "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        payload.len()
    )?;
    stream.write_all(payload)
}

fn deletion(body: &[u8]) -> bool {
    matches!(
        strict_json::parse(body),
        Ok(Value::Object(members))
            if members.len() == 1 && members[0].0 == "thread_id" && matches!(members[0].1, Value::String(_))
    )
}

#[cfg(test)]
mod tests {
    use std::io::{Read, Write};
    use std::net::{Ipv4Addr, TcpStream};

    use super::Brain;

    fn post(brain: &Brain, path: &str, body: &str) -> String {
        let address = brain.url().trim_start_matches("http://").to_owned();
        let mut stream = TcpStream::connect(address).expect("connected");
        write!(
            stream,
            "POST {path} HTTP/1.1\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        )
        .expect("sent");
        let mut answer = String::new();
        stream.read_to_string(&mut answer).expect("answered");
        answer
    }

    #[test]
    fn only_a_closed_thread_deletion_is_answered() {
        let mut brain = Brain::start(Ipv4Addr::LOCALHOST.into()).expect("serving");
        assert!(
            post(&brain, "/v1/threads/delete", r#"{"thread_id":"t"}"#)
                .starts_with("HTTP/1.1 200 OK")
        );
        assert!(
            post(&brain, "/v1/threads/delete", r#"{"thread_id":"t","x":1}"#)
                .starts_with("HTTP/1.1 400")
        );
        assert!(post(&brain, "/v1/turns", r#"{"thread_id":"t"}"#).starts_with("HTTP/1.1 400"));
        assert_eq!(brain.deletions(), 1);
        brain.stop();
    }
}
