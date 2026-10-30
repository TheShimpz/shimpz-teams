//! The shipping cosign as a direct child process (ADR-0029 "isolated Cosign child executions", ADR-0123 R7).
//!
//! Each verification writes the one bundle Team read by content address into a fresh private directory and runs
//! `cosign verify-blob-attestation` on exactly those bytes, with a cleared environment (its own `HOME`, the private
//! `TUF_ROOT` cache, and the release proxy as every proxy), no stdin, and its stdout and stderr drained together within
//! a bound. A child that overruns its time is sent `SIGTERM`, then `SIGKILL` after a grace period; a child that
//! overruns its output is killed at once; a cancelled verification kills it on drop. Every child is reaped.

use std::path::{Path, PathBuf};
use std::process::{ExitStatus, Stdio};
use std::time::Duration;

use rustix::process::{Pid, Signal};
use tokio::io::{AsyncRead, AsyncReadExt};
use tokio::process::{Child, Command};

/// The signer every Assistant artifact must carry.
pub const SIGNER_IDENTITY: &str = "https://github.com/TheShimpz/shimpz-developers/.github/workflows/build-assistant.yml@refs/heads/main";
/// The OIDC issuer of that signer.
pub const OIDC_ISSUER: &str = "https://token.actions.githubusercontent.com";
/// Where the Team image installs cosign.
pub const COSIGN: &str = "/usr/local/bin/cosign";
/// How long one verification may run before it is terminated.
pub const TIMEOUT: Duration = Duration::from_secs(90);
/// How long a terminated child has before it is killed.
pub const KILL_GRACE: Duration = Duration::from_secs(5);
/// The most stdout and stderr bytes one verification may write together.
pub const MAX_OUTPUT_BYTES: usize = 2 * 1024 * 1024;
const BUNDLE_FILE: &str = "bundle.sigstore.json";

/// Why cosign did not prove a bundle.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CosignError {
    /// It ran and refused the bundle, or wrote more than its output bound.
    Refused,
    /// It could not run, or did not finish in time.
    Unavailable,
}

/// Who must have signed an artifact: the certificate identity and its OIDC issuer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Signer {
    /// The certificate identity.
    pub identity: String,
    /// The OIDC issuer.
    pub issuer: String,
}

impl Default for Signer {
    /// The Developers publication workflow on GitHub Actions.
    fn default() -> Self {
        Self {
            identity: SIGNER_IDENTITY.to_owned(),
            issuer: OIDC_ISSUER.to_owned(),
        }
    }
}

/// One configured cosign.
#[derive(Clone, Debug)]
pub struct Cosign {
    program: PathBuf,
    signer: Signer,
    trust_root: PathBuf,
    proxy_url: String,
    timeout: Duration,
    kill_grace: Duration,
    max_output: usize,
}

impl Cosign {
    /// The shipping cosign with its production bounds, caching Sigstore's trust root under `trust_root`.
    #[must_use]
    pub fn new(trust_root: &Path, proxy_url: &str) -> Self {
        Self::with_bounds(
            Path::new(COSIGN),
            trust_root,
            proxy_url,
            TIMEOUT,
            KILL_GRACE,
            MAX_OUTPUT_BYTES,
        )
    }

    /// Another program and other bounds, for the tests of the child-process contract.
    #[must_use]
    pub fn with_bounds(
        program: &Path,
        trust_root: &Path,
        proxy_url: &str,
        timeout: Duration,
        kill_grace: Duration,
        max_output: usize,
    ) -> Self {
        Self {
            program: program.to_owned(),
            signer: Signer::default(),
            trust_root: trust_root.to_owned(),
            proxy_url: proxy_url.to_owned(),
            timeout,
            kill_grace,
            max_output,
        }
    }

    /// The same cosign proving another signer (the live lane's own Sigstore bundles); production keeps the default.
    #[must_use]
    pub fn with_signer(mut self, signer: Signer) -> Self {
        self.signer = signer;
        self
    }

    /// The signer it proves.
    #[must_use]
    pub const fn signer(&self) -> &Signer {
        &self.signer
    }

    /// The exact arguments of one attestation verification, before `--bundle <path>`.
    #[must_use]
    pub fn arguments(&self, oci_digest: &str, predicate_type: &str) -> Vec<String> {
        [
            "verify-blob-attestation",
            "--certificate-identity",
            &self.signer.identity,
            "--certificate-oidc-issuer",
            &self.signer.issuer,
            "--type",
            predicate_type,
            "--digest",
            oci_digest.trim_start_matches("sha256:"),
            "--digestAlg",
            "sha256",
        ]
        .map(str::to_owned)
        .to_vec()
    }

    /// Proves one local bundle's signer, issuer, transparency evidence, predicate type, and subject digest.
    ///
    /// # Errors
    /// [`CosignError::Refused`] for a non-zero exit or output past the bound; [`CosignError::Unavailable`] when it
    /// cannot run or overruns its time.
    pub async fn verify(
        &self,
        bundle: &[u8],
        oci_digest: &str,
        predicate_type: &str,
    ) -> Result<(), CosignError> {
        let directory = tempfile::Builder::new()
            .prefix("shimpz-cosign-")
            .tempdir()
            .map_err(|_| CosignError::Unavailable)?;
        let bundle_path = directory.path().join(BUNDLE_FILE);
        write_private(&bundle_path, bundle).map_err(|_| CosignError::Unavailable)?;
        let mut command = Command::new(&self.program);
        command
            .args(self.arguments(oci_digest, predicate_type))
            .arg("--bundle")
            .arg(&bundle_path)
            .env_clear()
            .env("HOME", directory.path())
            .env("TUF_ROOT", &self.trust_root)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        for name in ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"] {
            command.env(name, &self.proxy_url);
        }
        command.env("NO_PROXY", "").env("no_proxy", "");
        let child = command.spawn().map_err(|_| CosignError::Unavailable)?;
        let status = Box::pin(self.supervise(child)).await?;
        if status.success() {
            Ok(())
        } else {
            Err(CosignError::Refused)
        }
    }

    /// Drains the child's output within the bound and waits for it within the deadline, killing and reaping it
    /// otherwise.
    async fn supervise(&self, mut child: Child) -> Result<ExitStatus, CosignError> {
        let (Some(stdout), Some(stderr)) = (child.stdout.take(), child.stderr.take()) else {
            terminate(&mut child, Duration::ZERO).await;
            return Err(CosignError::Unavailable);
        };
        let max_output = self.max_output;
        let outcome = tokio::time::timeout(self.timeout, async {
            if !drained_within(stdout, stderr, max_output).await {
                return Err(CosignError::Refused);
            }
            child.wait().await.map_err(|_| CosignError::Unavailable)
        })
        .await;
        match outcome {
            Ok(Ok(status)) => Ok(status),
            Ok(Err(error)) => {
                terminate(&mut child, Duration::ZERO).await;
                Err(error)
            }
            Err(_elapsed) => {
                terminate(&mut child, self.kill_grace).await;
                Err(CosignError::Unavailable)
            }
        }
    }
}

/// Writes `bytes` to a new owner-only file.
fn write_private(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::io::Write as _;
    use std::os::unix::fs::OpenOptionsExt as _;
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    file.write_all(bytes)?;
    file.sync_all()
}

/// Reads both streams to their end together; `false` once more than `maximum` bytes arrived.
async fn drained_within(
    mut stdout: impl AsyncRead + Unpin,
    mut stderr: impl AsyncRead + Unpin,
    maximum: usize,
) -> bool {
    let (mut total, mut open) = (0_usize, (true, true));
    let (mut left, mut right) = (vec![0_u8; 8192], vec![0_u8; 8192]);
    while open.0 || open.1 {
        let read = tokio::select! {
            read = stdout.read(&mut left), if open.0 => (0, read),
            read = stderr.read(&mut right), if open.1 => (1, read),
        };
        match read {
            (stream, Ok(0) | Err(_)) => {
                if stream == 0 {
                    open.0 = false;
                } else {
                    open.1 = false;
                }
            }
            (_, Ok(count)) => {
                total += count;
                if total > maximum {
                    return false;
                }
            }
        }
    }
    true
}

/// Sends `SIGTERM`, gives the child `grace` to exit, then kills it, and always reaps it.
async fn terminate(child: &mut Child, grace: Duration) {
    if !grace.is_zero() {
        if let Some(pid) = child
            .id()
            .and_then(|id| i32::try_from(id).ok())
            .and_then(Pid::from_raw)
        {
            let _ = rustix::process::kill_process(pid, Signal::TERM);
        }
        if tokio::time::timeout(grace, child.wait()).await.is_ok() {
            return;
        }
    }
    let _ = child.start_kill();
    let _ = child.wait().await;
}
