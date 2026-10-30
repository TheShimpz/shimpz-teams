//! The cosign child-process contract with scripted stand-ins for cosign: exact arguments, a cleared environment,
//! the exit verdict, the output bound, and kill-and-reap on timeout, on overflow, and on cancellation.

use std::os::unix::fs::PermissionsExt as _;
use std::path::{Path, PathBuf};
use std::time::Duration;

use team_install::cosign::{Cosign, CosignError, OIDC_ISSUER, SIGNER_IDENTITY};

/// One stand-in at a time: a script written while another test forks can be held open by that child and then refuse
/// to execute (`ETXTBSY`).
static SERIAL: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

const OCI: &str = "sha256:1111111111111111111111111111111111111111111111111111111111111111";
const PREDICATE: &str = "https://slsa.dev/provenance/v1";

/// A stand-in for cosign: `body` runs under `/bin/sh` with no `PATH`, so it calls only builtins and absolute paths.
fn script(directory: &Path, body: &str) -> PathBuf {
    let path = directory.join("cosign");
    std::fs::write(
        &path,
        format!("#!/bin/sh\necho $$ > {}/pid\n{body}\n", directory.display()),
    )
    .expect("script");
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).expect("mode");
    path
}

fn cosign(program: &Path, trust_root: &Path, timeout: Duration, max_output: usize) -> Cosign {
    Cosign::with_bounds(
        program,
        trust_root,
        "http://shimpz-assistant-release:8888",
        timeout,
        Duration::from_millis(300),
        max_output,
    )
}

/// Whether the stand-in's process is gone, reaped, within two seconds.
async fn reaped(directory: &Path) -> bool {
    let pid = std::fs::read_to_string(directory.join("pid"))
        .expect("pid")
        .trim()
        .to_owned();
    for _ in 0..200 {
        if !Path::new(&format!("/proc/{pid}")).exists() {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    false
}

/// Runs a stand-in that records its arguments, environment, and the bundle it was given.
async fn recorded_run() -> (tempfile::TempDir, PathBuf) {
    let _serial = SERIAL.lock().await;
    let directory = tempfile::tempdir().expect("directory");
    let out = directory.path().display().to_string();
    let body = format!(
        "for argument in \"$@\"; do printf '%s\\n' \"$argument\"; done > {out}/arguments\nexport -p > {out}/environment\nread -r line < \"$HOME/bundle.sigstore.json\"; printf '%s' \"$line\" > {out}/bundle"
    );
    let program = script(directory.path(), &body);
    let trust_root = directory.path().join("tuf");
    let verified = cosign(&program, &trust_root, Duration::from_secs(10), 1024)
        .verify(b"{\"bundle\": 1}", OCI, PREDICATE)
        .await;
    assert_eq!(verified, Ok(()));
    (directory, trust_root)
}

fn recorded(directory: &tempfile::TempDir, name: &str) -> String {
    std::fs::read_to_string(directory.path().join(name)).expect("recorded")
}

#[tokio::test]
async fn cosign_arguments_exact() {
    let (directory, _) = recorded_run().await;
    let arguments = recorded(&directory, "arguments");
    let lines: Vec<&str> = arguments.lines().collect();
    let expected = [
        "verify-blob-attestation",
        "--certificate-identity",
        SIGNER_IDENTITY,
        "--certificate-oidc-issuer",
        OIDC_ISSUER,
        "--type",
        PREDICATE,
        "--digest",
        &OCI[7..],
        "--digestAlg",
        "sha256",
        "--bundle",
    ];
    assert_eq!(lines[..12], expected);
    assert!(lines[12].ends_with("/bundle.sigstore.json") && lines.len() == 13);
    assert_eq!(recorded(&directory, "bundle"), "{\"bundle\": 1}");
}

#[tokio::test]
async fn cosign_env_cleared_except_home_tuf_proxy() {
    let (directory, trust_root) = recorded_run().await;
    let environment = recorded(&directory, "environment");
    let mut names: Vec<&str> = environment
        .lines()
        .filter_map(|line| {
            line.strip_prefix("export ")
                .and_then(|rest| rest.split('=').next())
        })
        .filter(|name| !["PWD", "OLDPWD", "SHLVL", "_"].contains(name))
        .collect();
    names.sort_unstable();
    let expected = [
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "TUF_ROOT",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    ];
    assert_eq!(names, expected);
    assert!(environment.contains(&trust_root.display().to_string()));
    assert!(environment.contains("http://shimpz-assistant-release:8888"));
}

#[tokio::test]
async fn cosign_nonzero_refuses() {
    let _serial = SERIAL.lock().await;
    let directory = tempfile::tempdir().expect("directory");
    let program = script(directory.path(), "echo refused >&2\nexit 1");
    let verified = cosign(&program, directory.path(), Duration::from_secs(10), 1024)
        .verify(b"{}", OCI, PREDICATE)
        .await;
    assert_eq!(verified, Err(CosignError::Refused));
    assert!(reaped(directory.path()).await);
}

#[tokio::test]
async fn cosign_killed_and_reaped_on_timeout() {
    let _serial = SERIAL.lock().await;
    let directory = tempfile::tempdir().expect("directory");
    let program = script(directory.path(), "trap '' TERM\nwhile :; do :; done");
    let verified = cosign(&program, directory.path(), Duration::from_millis(300), 1024)
        .verify(b"{}", OCI, PREDICATE)
        .await;
    assert_eq!(verified, Err(CosignError::Unavailable));
    assert!(reaped(directory.path()).await);
}

#[tokio::test]
async fn cosign_killed_and_reaped_on_overflow() {
    let _serial = SERIAL.lock().await;
    let directory = tempfile::tempdir().expect("directory");
    let program = script(
        directory.path(),
        "while :; do echo 0123456789012345678901234567890123456789; echo 0123456789 >&2; done",
    );
    let verified = cosign(&program, directory.path(), Duration::from_secs(10), 4096)
        .verify(b"{}", OCI, PREDICATE)
        .await;
    assert_eq!(verified, Err(CosignError::Refused));
    assert!(reaped(directory.path()).await);
}

#[tokio::test]
async fn cosign_killed_and_reaped_on_cancel() {
    let _serial = SERIAL.lock().await;
    let directory = tempfile::tempdir().expect("directory");
    let program = script(directory.path(), "while :; do :; done");
    let runner = cosign(&program, directory.path(), Duration::from_secs(60), 1024);
    let verification = runner.verify(b"{}", OCI, PREDICATE);
    assert!(
        tokio::time::timeout(Duration::from_millis(300), verification)
            .await
            .is_err()
    );
    assert!(reaped(directory.path()).await);
}

#[tokio::test]
async fn cosign_that_cannot_run_is_unavailable() {
    let _serial = SERIAL.lock().await;
    let directory = tempfile::tempdir().expect("directory");
    let missing = directory.path().join("absent");
    let verified = cosign(&missing, directory.path(), Duration::from_secs(1), 1024)
        .verify(b"{}", OCI, PREDICATE)
        .await;
    assert_eq!(verified, Err(CosignError::Unavailable));
}

#[test]
fn cosign_proves_the_configured_signer() {
    let signer = team_install::cosign::Signer {
        identity: "https://example.test/workflow".to_owned(),
        issuer: "https://issuer.test".to_owned(),
    };
    let runner = Cosign::new(Path::new("/tmp/tuf"), "http://proxy:8888").with_signer(signer);
    let arguments = runner.arguments(OCI, PREDICATE);
    assert_eq!(
        arguments[1..5],
        [
            "--certificate-identity",
            "https://example.test/workflow",
            "--certificate-oidc-issuer",
            "https://issuer.test"
        ]
    );
    assert_eq!(
        Cosign::new(Path::new("/tmp/tuf"), "http://proxy:8888").arguments(OCI, PREDICATE)[2],
        SIGNER_IDENTITY
    );
}
