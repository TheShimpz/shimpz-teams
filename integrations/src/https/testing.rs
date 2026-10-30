//! A scripted CONNECT proxy with a TLS server behind it, for the broker and provider-call tests (feature `testing`).
//! A CA and a leaf for each test's destination are generated at test time, so no key is committed.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use rcgen::{BasicConstraints, CertificateParams, IsCa, Issuer, KeyPair};
use rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer};
use rustls::{RootCertStore, ServerConfig};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio_rustls::TlsAcceptor;

use super::Tunnel;

/// What the scripted proxy does with a CONNECT.
#[derive(Clone, Copy, Debug)]
pub enum Proxy {
    /// Establish the tunnel to the scripted server.
    Established,
    /// Answer these bytes and close.
    Reply(&'static [u8]),
    /// Read the CONNECT and never answer.
    Stall,
}

/// What the scripted server does with one request.
#[derive(Clone, Debug)]
pub enum Behaviour {
    /// Answer these bytes and close.
    Reply(Vec<u8>),
    /// Answer these bytes, then one more byte every 20 ms, forever.
    Trickle(Vec<u8>),
    /// Never answer.
    Stall,
    /// Answer these bytes after this delay.
    Late(Duration, Vec<u8>),
}

/// One request the server received: the CONNECT head, the request head, and the body.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Recorded {
    /// The CONNECT request head the proxy read.
    pub connect: String,
    /// The HTTP request head the server read.
    pub head: String,
    /// The request body.
    pub body: Vec<u8>,
}

type Script = Arc<dyn Fn(&Recorded) -> Behaviour + Send + Sync>;

/// The scripted proxy and server, a tunnel that reaches them, and what they received.
pub struct Fake {
    /// A tunnel through the scripted proxy that trusts the scripted server's CA.
    pub tunnel: Tunnel,
    /// Every request the server received, and every CONNECT head the proxy read.
    pub received: Arc<Mutex<Vec<Recorded>>>,
}

/// A JSON reply with this status line and body.
#[must_use]
pub fn reply(status: &str, content_type: &str, body: &str) -> Vec<u8> {
    format!(
        "HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
    .into_bytes()
}

fn certificates(names: &[&str]) -> (ServerConfig, RootCertStore) {
    let ca_key = KeyPair::generate().expect("a CA key");
    let mut ca_params = CertificateParams::new(Vec::<String>::new()).expect("CA parameters");
    ca_params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    let ca = ca_params.self_signed(&ca_key).expect("a CA");
    let leaf_key = KeyPair::generate().expect("a leaf key");
    let leaf = CertificateParams::new(
        names
            .iter()
            .map(|name| (*name).to_owned())
            .collect::<Vec<_>>(),
    )
    .expect("leaf parameters")
    .signed_by(&leaf_key, &Issuer::from_params(&ca_params, &ca_key))
    .expect("a leaf");
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let server = ServerConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .expect("protocol versions")
        .with_no_client_auth()
        .with_single_cert(
            vec![CertificateDer::from(leaf.der().to_vec())],
            PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(leaf_key.serialize_der())),
        )
        .expect("a server configuration");
    let mut roots = RootCertStore::empty();
    roots.add(ca.der().clone()).expect("the CA is a root");
    (server, roots)
}

/// A scripted proxy and a server presenting a certificate for `names`, answering every request with `script`.
///
/// # Panics
/// When the loopback listener cannot be bound.
pub async fn fake(
    proxy: Proxy,
    names: &[&str],
    script: impl Fn(&Recorded) -> Behaviour + Send + Sync + 'static,
) -> Fake {
    let (server, roots) = certificates(names);
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("a loopback listener");
    let port = listener.local_addr().expect("an address").port();
    let received = Arc::new(Mutex::new(Vec::new()));
    let script: Script = Arc::new(script);
    let acceptor = TlsAcceptor::from(Arc::new(server));
    let recorded = Arc::clone(&received);
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            tokio::spawn(serve(
                stream,
                proxy,
                acceptor.clone(),
                Arc::clone(&script),
                Arc::clone(&recorded),
            ));
        }
    });
    Fake {
        tunnel: Tunnel::new("127.0.0.1".to_owned(), port, Duration::from_secs(5), roots),
        received,
    }
}

async fn head(stream: &mut (impl AsyncRead + Unpin)) -> Option<(String, Vec<u8>)> {
    let mut buffer = Vec::new();
    let mut chunk = [0_u8; 1024];
    loop {
        if let Some(end) = buffer.windows(4).position(|window| window == b"\r\n\r\n") {
            let rest = buffer.split_off(end + 4);
            return Some((String::from_utf8_lossy(&buffer).into_owned(), rest));
        }
        let read = stream.read(&mut chunk).await.ok()?;
        if read == 0 {
            return None;
        }
        buffer.extend_from_slice(&chunk[..read]);
    }
}

async fn serve(
    mut stream: TcpStream,
    proxy: Proxy,
    acceptor: TlsAcceptor,
    script: Script,
    received: Arc<Mutex<Vec<Recorded>>>,
) {
    let Some((connect, _)) = head(&mut stream).await else {
        return;
    };
    if !tunnel(&mut stream, proxy).await {
        return;
    }
    let Ok(mut tls) = acceptor.accept(stream).await else {
        return;
    };
    let Some((head, body)) = request(&mut tls).await else {
        return;
    };
    let recorded = Recorded {
        connect,
        head,
        body,
    };
    let behaviour = script(&recorded);
    received.lock().expect("usable").push(recorded);
    respond(&mut tls, behaviour).await;
}

/// Answers the CONNECT as `proxy` says; whether the tunnel is established.
async fn tunnel(stream: &mut TcpStream, proxy: Proxy) -> bool {
    match proxy {
        Proxy::Stall => {
            let _ = stream.read(&mut [0_u8; 1]).await;
            false
        }
        Proxy::Reply(bytes) => {
            let _ = stream.write_all(bytes).await;
            false
        }
        Proxy::Established => stream
            .write_all(b"HTTP/1.1 200 Connection established\r\n\r\n")
            .await
            .is_ok(),
    }
}

/// One request's head and its `Content-Length` body.
async fn request(stream: &mut (impl AsyncRead + Unpin)) -> Option<(String, Vec<u8>)> {
    let (head, mut body) = head(stream).await?;
    let length = head
        .lines()
        .find_map(|line| {
            line.to_ascii_lowercase()
                .strip_prefix("content-length:")
                .and_then(|value| value.trim().parse::<usize>().ok())
        })
        .unwrap_or(0);
    while body.len() < length {
        let mut chunk = vec![0_u8; length - body.len()];
        match stream.read(&mut chunk).await {
            Ok(0) | Err(_) => return None,
            Ok(read) => body.extend_from_slice(&chunk[..read]),
        }
    }
    Some((head, body))
}

async fn respond(stream: &mut (impl AsyncWrite + Unpin), behaviour: Behaviour) {
    match behaviour {
        Behaviour::Reply(bytes) => finish(stream, &bytes).await,
        Behaviour::Trickle(prefix) => trickle(stream, &prefix).await,
        Behaviour::Stall => std::future::pending::<()>().await,
        Behaviour::Late(delay, bytes) => {
            tokio::time::sleep(delay).await;
            finish(stream, &bytes).await;
        }
    }
}

async fn finish(stream: &mut (impl AsyncWrite + Unpin), bytes: &[u8]) {
    let _ = stream.write_all(bytes).await;
    let _ = stream.shutdown().await;
}

async fn trickle(stream: &mut (impl AsyncWrite + Unpin), prefix: &[u8]) {
    if stream.write_all(prefix).await.is_err() {
        return;
    }
    loop {
        tokio::time::sleep(Duration::from_millis(20)).await;
        if stream.write_all(b"a").await.is_err() || stream.flush().await.is_err() {
            return;
        }
    }
}
