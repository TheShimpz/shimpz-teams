//! Reads through a CONNECT proxy and TLS: the exact unauthenticated tunnel request, the proxy's exact acceptance, the
//! verified destination, and the body bound.

use std::sync::Arc;
use std::time::Duration;

use rustls::RootCertStore;
use rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer};
use team_install::release::{ReadError, ReleaseProxy, Transport};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::oneshot;

const HOST: &str = "developers.test";

async fn head(stream: &mut (impl AsyncReadExt + Unpin)) -> String {
    let mut head = Vec::new();
    let mut byte = [0_u8; 1];
    while !head.ends_with(b"\r\n\r\n") {
        if stream.read(&mut byte).await.expect("read") == 0 {
            break;
        }
        head.push(byte[0]);
    }
    String::from_utf8(head).expect("ASCII head")
}

/// A one-connection proxy and TLS origin for `HOST`; it reports the CONNECT head and the request head.
async fn origin(
    reply: &'static [u8],
    body: Vec<u8>,
) -> (u16, RootCertStore, oneshot::Receiver<(String, String)>) {
    let certified = rcgen::generate_simple_self_signed(vec![HOST.to_owned()]).expect("certificate");
    let certificate = CertificateDer::from(certified.cert.der().to_vec());
    let key = PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(
        certified.signing_key.serialize_der(),
    ));
    let mut roots = RootCertStore::empty();
    roots.add(certificate.clone()).expect("root");
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let config = rustls::ServerConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .expect("versions")
        .with_no_client_auth()
        .with_single_cert(vec![certificate], key)
        .expect("server");
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let port = listener.local_addr().expect("address").port();
    let (report, received) = oneshot::channel();
    tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.expect("accept");
        let connect = head(&mut stream).await;
        stream.write_all(reply).await.expect("reply");
        let Ok(mut tls) = tokio_rustls::TlsAcceptor::from(Arc::new(config))
            .accept(stream)
            .await
        else {
            let _ = report.send((connect, String::new()));
            return;
        };
        let request = head(&mut tls).await;
        let response = format!(
            "HTTP/1.1 200 OK\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
            body.len()
        );
        tls.write_all(response.as_bytes()).await.expect("response");
        tls.write_all(&body).await.expect("body");
        tls.shutdown().await.ok();
        let _ = report.send((connect, request));
    });
    (port, roots, received)
}

#[tokio::test]
async fn a_read_tunnels_without_credentials_and_verifies_its_destination() {
    let (port, roots, received) = origin(
        b"HTTP/1.1 200 Connection established\r\n\r\n",
        b"{}".to_vec(),
    )
    .await;
    let proxy = ReleaseProxy::new("127.0.0.1", port, Duration::from_secs(5), roots);
    let response = proxy
        .get(
            HOST,
            "/api/v1/x",
            &[("accept", "application/json".to_owned())],
            16,
        )
        .await
        .expect("read");
    assert_eq!(
        (response.status, response.body.as_slice()),
        (200, &b"{}"[..])
    );
    let (connect, request) = received.await.expect("report");
    assert_eq!(
        connect,
        format!("CONNECT {HOST}:443 HTTP/1.1\r\nHost: {HOST}:443\r\n\r\n")
    );
    assert!(request.starts_with("GET /api/v1/x HTTP/1.1\r\n"));
    assert!(
        request
            .to_ascii_lowercase()
            .contains("accept: application/json")
            && !request.to_ascii_lowercase().contains("authorization")
    );
}

#[tokio::test]
async fn a_body_past_its_bound_is_refused() {
    let (port, roots, _received) = origin(b"HTTP/1.1 200 OK\r\n\r\n", vec![b'x'; 64]).await;
    let proxy = ReleaseProxy::new("127.0.0.1", port, Duration::from_secs(5), roots);
    assert_eq!(
        proxy.get(HOST, "/", &[], 63).await,
        Err(ReadError::TooLarge)
    );
}

#[tokio::test]
async fn a_refused_or_garbled_tunnel_or_an_untrusted_origin_is_unavailable() {
    for reply in [
        &b"HTTP/1.1 403 Forbidden\r\n\r\n"[..],
        b"HTTP/1.1 200 OK\r\n\r\nextra",
        b"HTTP/1.1 2000 OK\r\n\r\n",
    ] {
        let (port, roots, _received) = origin(reply, Vec::new()).await;
        let proxy = ReleaseProxy::new("127.0.0.1", port, Duration::from_secs(5), roots);
        assert_eq!(
            proxy.get(HOST, "/", &[], 16).await,
            Err(ReadError::Unavailable)
        );
    }
    let (port, _roots, _received) = origin(b"HTTP/1.1 200 OK\r\n\r\n", Vec::new()).await;
    let proxy = ReleaseProxy::new(
        "127.0.0.1",
        port,
        Duration::from_secs(5),
        RootCertStore::empty(),
    );
    assert_eq!(
        proxy.get(HOST, "/", &[], 16).await,
        Err(ReadError::Unavailable)
    );
}
