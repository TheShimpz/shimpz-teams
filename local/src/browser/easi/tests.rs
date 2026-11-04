use std::os::unix::fs::PermissionsExt as _;
use std::sync::Arc;
use std::time::Duration;

use hyper::Method;
use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _};
use tokio::time::Instant;

use super::{EasiError, PASSWORD_FILE};
use crate::browser::fake::Fake;

fn soon() -> Instant {
    Instant::now() + Duration::from_secs(10)
}

#[tokio::test]
async fn team_enrolls_once_keeps_its_password_private_and_logs_in_afterwards() {
    let scratch = tempfile::tempdir().expect("a directory");
    let fake = Fake::default();
    let answer = fake
        .client(scratch.path())
        .call(Method::GET, "/api/sessions", None, soon())
        .await
        .expect("an answer");
    assert_eq!(answer.status, 200);
    let path = scratch.path().join(PASSWORD_FILE);
    let held = std::fs::read_to_string(&path).expect("kept");
    assert_eq!(fake.state().password.as_deref(), Some(held.as_str()));
    assert_eq!(
        std::fs::metadata(&path)
            .expect("metadata")
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    // A restarted Team logs in with what it kept.
    fake.client(scratch.path())
        .call(Method::GET, "/api/sessions", None, soon())
        .await
        .expect("an answer");
    assert_eq!(fake.state().logins, 2);
}

#[tokio::test]
async fn an_installation_enrolled_by_someone_else_is_refused_and_never_taken_over() {
    let scratch = tempfile::tempdir().expect("a directory");
    let fake = Fake::default();
    fake.state().password = Some("someone-elses".to_owned());
    let client = fake.client(scratch.path());
    assert_eq!(
        client
            .call(Method::GET, "/api/sessions", None, soon())
            .await
            .map(|_| ()),
        Err(EasiError::Foreign)
    );
    std::fs::write(scratch.path().join(PASSWORD_FILE), "not-it").expect("written");
    assert_eq!(
        client
            .call(Method::GET, "/api/sessions", None, soon())
            .await
            .map(|_| ()),
        Err(EasiError::Foreign)
    );
    assert_eq!(fake.state().password.as_deref(), Some("someone-elses"));
}

#[tokio::test]
async fn a_lapsed_login_is_replaced_and_the_call_sent_again() {
    let scratch = tempfile::tempdir().expect("a directory");
    let fake = Fake::default();
    let client = fake.client(scratch.path());
    client
        .call(Method::GET, "/api/sessions", None, soon())
        .await
        .expect("an answer");
    fake.state().faults.insert("lapse-login");
    let answer = client
        .call(Method::GET, "/api/sessions", None, soon())
        .await
        .expect("an answer");
    assert_eq!(answer.status, 200);
    assert_eq!(fake.state().logins, 2);
}

#[tokio::test]
async fn a_write_carries_the_origin_and_csrf_the_admin_requires() {
    let scratch = tempfile::tempdir().expect("a directory");
    let fake = Fake::default();
    let answer = fake
        .client(scratch.path())
        .call(
            Method::PUT,
            "/api/agent-files",
            Some(&strict_json::Value::Object(Vec::new())),
            soon(),
        )
        .await
        .expect("an answer");
    assert_eq!(answer.status, 200);
}

#[tokio::test]
async fn connections_are_kept_open_between_calls() {
    let scratch = tempfile::tempdir().expect("a directory");
    let fake = Fake::default();
    let client = Arc::new(fake.client(scratch.path()));
    for _ in 0..5 {
        client
            .call(Method::GET, "/api/sessions", None, soon())
            .await
            .expect("an answer");
    }
    assert_eq!(client.idle.lock().await.len(), 1);
}

#[tokio::test]
async fn an_unreachable_admin_or_a_passed_deadline_answers_nothing() {
    let scratch = tempfile::tempdir().expect("a directory");
    let fake = Fake::default();
    fake.state().faults.insert("unreachable");
    assert_eq!(
        fake.client(scratch.path())
            .call(Method::GET, "/api/sessions", None, soon())
            .await
            .map(|_| ()),
        Err(EasiError::Lost)
    );
}

#[tokio::test]
async fn a_viewer_s_stream_is_upgraded_on_team_s_login() {
    let scratch = tempfile::tempdir().expect("a directory");
    let fake = Fake::default();
    fake.leave_session("abc");
    let client = fake.client(scratch.path());
    let mut stream = client
        .upgrade(
            "/ws/sessions/abc?viewer=00",
            "AAAAAAAAAAAAAAAAAAAAAA==",
            soon(),
        )
        .await
        .expect("upgraded");
    stream.write_all(b"frame").await.expect("written");
    let mut read = [0_u8; 5];
    stream.read_exact(&mut read).await.expect("read");
    assert_eq!(&read, b"frame");
    assert_eq!(
        client
            .upgrade(
                "/ws/sessions/gone?viewer=00",
                "AAAAAAAAAAAAAAAAAAAAAA==",
                soon()
            )
            .await
            .map(|_| ())
            .err(),
        Some(EasiError::Invalid)
    );
}
