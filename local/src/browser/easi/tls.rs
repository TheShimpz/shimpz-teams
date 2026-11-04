//! The connection to easi.run's admin: TLS to the exact certificate its container holds (ADR-0126).
//!
//! The admin generates its own certificate for its own names, so a name proves nothing here. The Docker daemon, which
//! Team already trusts with every container, says which certificate the admin holds; only that one is admitted, and the
//! handshake's signature must still prove its key. A certificate that stops matching is read again once per dial, as
//! the admin makes a new one when its names change.

use std::io::Read as _;
use std::pin::Pin;
use std::sync::{Arc, Mutex, PoisonError};

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::{CryptoProvider, ring, verify_tls12_signature, verify_tls13_signature};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{ClientConfig, DigitallySignedStruct, Error, SignatureScheme};
use team_core::future::BoxFuture;
use tokio::net::TcpStream;
use tokio_rustls::TlsConnector;

use super::{ADMIN_CONTAINER, Connection, Dial, EasiError, HOST_NAME, PORT};
use crate::docker::{DockerEngine, DockerError};

const CERTIFICATE_PATH: &str = "/var/lib/admin/tls-cert.pem";
/// The most a certificate's tar answer may hold.
const MAX_CERTIFICATE_BYTES: usize = 64 * 1024;
const TAR_BLOCK: usize = 512;

/// Admits exactly one certificate.
#[derive(Debug)]
struct Pinned {
    certificate: Vec<u8>,
    provider: Arc<CryptoProvider>,
}

impl ServerCertVerifier for Pinned {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, Error> {
        if end_entity.as_ref() == self.certificate.as_slice() {
            Ok(ServerCertVerified::assertion())
        } else {
            Err(Error::General("not the admin's certificate".to_owned()))
        }
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        certificate: &CertificateDer<'_>,
        signature: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, Error> {
        verify_tls12_signature(
            message,
            certificate,
            signature,
            &self.provider.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        certificate: &CertificateDer<'_>,
        signature: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, Error> {
        verify_tls13_signature(
            message,
            certificate,
            signature,
            &self.provider.signature_verification_algorithms,
        )
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.provider
            .signature_verification_algorithms
            .supported_schemes()
    }
}

/// Dials the admin on the internal Browser network, admitting the certificate its container holds.
pub struct PinnedDial {
    docker: Arc<dyn DockerEngine>,
    container: String,
    address: (String, u16),
    certificate: Mutex<Option<Vec<u8>>>,
}

impl PinnedDial {
    /// A dial reading the certificate through `docker`.
    #[must_use]
    pub fn new(docker: Arc<dyn DockerEngine>) -> Self {
        Self::at(docker, ADMIN_CONTAINER, (HOST_NAME, PORT))
    }

    /// A dial of the admin at `address`, whose certificate its `container` holds.
    #[must_use]
    pub fn at(docker: Arc<dyn DockerEngine>, container: &str, address: (&str, u16)) -> Self {
        Self {
            docker,
            container: container.to_owned(),
            address: (address.0.to_owned(), address.1),
            certificate: Mutex::new(None),
        }
    }

    fn held(&self) -> Option<Vec<u8>> {
        self.certificate
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    fn hold(&self, certificate: Option<Vec<u8>>) {
        *self
            .certificate
            .lock()
            .unwrap_or_else(PoisonError::into_inner) = certificate;
    }

    async fn read(&self) -> Result<Vec<u8>, EasiError> {
        let archive = self
            .docker
            .get_archive(&self.container, CERTIFICATE_PATH, MAX_CERTIFICATE_BYTES)
            .await
            .map_err(|error| match error {
                DockerError::NotFound => EasiError::Absent,
                _ => EasiError::Lost,
            })?;
        der(&archive.tar).ok_or(EasiError::Invalid)
    }

    async fn over(&self, certificate: Vec<u8>) -> Result<Pin<Box<dyn Connection>>, std::io::Error> {
        let provider = Arc::new(ring::default_provider());
        let config = ClientConfig::builder_with_provider(Arc::clone(&provider))
            .with_safe_default_protocol_versions()
            .map_err(std::io::Error::other)?
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(Pinned {
                certificate,
                provider,
            }))
            .with_no_client_auth();
        let stream = TcpStream::connect((self.address.0.as_str(), self.address.1)).await?;
        stream.set_nodelay(true)?;
        // The name is only the handshake's: the certificate is what is checked.
        let name = ServerName::try_from("localhost").map_err(std::io::Error::other)?;
        let tls = TlsConnector::from(Arc::new(config))
            .connect(name, stream)
            .await?;
        Ok(Box::pin(tls))
    }
}

impl Dial for PinnedDial {
    fn dial(&self) -> BoxFuture<'_, Result<Pin<Box<dyn Connection>>, EasiError>> {
        Box::pin(async move {
            if let Some(certificate) = self.held()
                && let Ok(connection) = self.over(certificate).await
            {
                return Ok(connection);
            }
            let certificate = self.read().await?;
            self.hold(Some(certificate.clone()));
            self.over(certificate).await.map_err(|_| EasiError::Lost)
        })
    }
}

/// The DER of the one PEM certificate in a one-file tar answer.
fn der(tar: &[u8]) -> Option<Vec<u8>> {
    let header = tar.get(..TAR_BLOCK)?;
    let size = std::str::from_utf8(header.get(124..135)?).ok()?;
    let size = usize::from_str_radix(size.trim_matches(['\0', ' ']), 8).ok()?;
    let mut pem = String::new();
    tar.get(TAR_BLOCK..TAR_BLOCK.checked_add(size)?)?
        .take(u64::try_from(size).ok()?)
        .read_to_string(&mut pem)
        .ok()?;
    let body = pem
        .split_once("-----BEGIN CERTIFICATE-----")?
        .1
        .split_once("-----END CERTIFICATE-----")?
        .0;
    let joined: String = body.split_whitespace().collect();
    STANDARD.decode(joined).ok()
}

#[cfg(test)]
mod tests {
    use super::der;

    fn tar(content: &[u8]) -> Vec<u8> {
        let mut block = vec![0_u8; 512];
        block[124..135].copy_from_slice(format!("{:011o}", content.len()).as_bytes());
        block.extend_from_slice(content);
        block.resize(512 + content.len().next_multiple_of(512), 0);
        block
    }

    #[test]
    fn the_certificate_is_read_from_the_archive() {
        let pem = b"-----BEGIN CERTIFICATE-----\nAQID\nBAU=\n-----END CERTIFICATE-----\n";
        assert_eq!(
            der(&tar(pem)),
            None::<Vec<u8>>.or(Some(vec![1, 2, 3, 4, 5]))
        );
    }

    #[test]
    fn anything_else_is_refused() {
        assert_eq!(der(&tar(b"no certificate here")), None);
        assert_eq!(der(b"short"), None);
    }
}
