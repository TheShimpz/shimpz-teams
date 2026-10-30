use std::sync::Arc;
use std::time::{Duration, UNIX_EPOCH};

use strict_json::Value;
use team_core::audit::AuditSink;
use team_core::future::BoxFuture;
use team_core::testing::{CountingEntropy, FakeClock, FakeExec, MemoryAudit};
use team_prepare::answer::Answer;
use team_prepare::fixtures::{self, Format};
use team_storage::files::FileStore;

use super::{Files, framed};
use crate::coordination::Coordination;
use crate::coordination::executions::ChatLimit;
use crate::http::server::RequestContext;
use crate::http::service::{Answer as Reply, FileUpload, Problem};
use crate::prepare::Rendition;
use crate::prepare::fakes::{Daemon, Worker, helpers, script};
use crate::prepare::service::LocalPreparer;
use crate::prepare::store::{Store, TeamPresence};
use crate::prepare::teardown::HelperTeardown;

const SUPERVISOR: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

struct Present;

impl TeamPresence for Present {
    fn require<'a>(&'a self, team_id: &'a str) -> BoxFuture<'a, Result<(), Problem>> {
        Box::pin(async move {
            if team_id == "team_1" {
                Ok(())
            } else {
                Err(Problem::new(404, "Team not found", "team-not-found"))
            }
        })
    }
}

struct Limit;

impl ChatLimit for Limit {
    fn current(&self) -> usize {
        1
    }

    fn save(&self, limit: u8) -> Result<u8, crate::coordination::executions::LimitUnavailable> {
        Ok(limit)
    }
}

struct Fixture {
    _directory: tempfile::TempDir,
    exec: Arc<FakeExec>,
    daemon: Arc<Daemon>,
    store: Store,
    audit: Arc<MemoryAudit>,
    files: Files,
}

/// A Supervisor request's context, or none at all.
fn context(principal: Option<&str>) -> Option<RequestContext> {
    principal.map(|principal| RequestContext {
        trace_id: "0".repeat(32),
        principal_id: Some(principal.to_owned()),
        principal_class: "human",
        credential_state: "valid",
        supervisor_key: None,
    })
}

impl Fixture {
    async fn new() -> Self {
        let directory = tempfile::tempdir().expect("a temporary directory");
        let store = Store(Arc::new(
            FileStore::new(
                directory.path().join("storage"),
                100 * 1024 * 1024,
                None,
                Arc::new(FakeClock::at(UNIX_EPOCH + Duration::from_secs(1_000_000))),
                Arc::new(CountingEntropy::default()),
            )
            .expect("a store"),
        ));
        let (daemon, exec) = (
            Daemon::running_controller().await,
            Arc::new(FakeExec::default()),
        );
        let preparer = Arc::new(LocalPreparer::new(helpers(&daemon, &exec), store.clone()));
        let audit = Arc::new(MemoryAudit::default());
        let files = Files::new(
            preparer,
            store.clone(),
            Arc::new(Present),
            Coordination::new(Arc::new(Limit)),
            Arc::clone(&audit) as Arc<dyn AuditSink>,
        );
        Self {
            _directory: directory,
            exec,
            daemon,
            store,
            audit,
            files,
        }
    }

    async fn download(&self, file: &str, who: Option<&str>) -> Result<Reply, Problem> {
        self.files.content("team_1", file, context(who)).await
    }

    async fn acknowledged(&self, file: &str, who: Option<&str>) -> Result<Reply, Problem> {
        self.files.acknowledge("team_1", file, context(who)).await
    }

    fn put(&self, name: &str, content: &[u8], web: bool) -> String {
        self.store
            .0
            .put("team_1", name, content, None, web)
            .expect("stored")
            .file
            .metadata
            .id
    }
}

fn json(answer: Result<Reply, Problem>) -> Value {
    match answer {
        Ok(Reply::Json { status: 200, body }) => body,
        other => panic!("not a JSON answer: {other:?}"),
    }
}

fn code(answer: Result<Reply, Problem>) -> (u16, Option<&'static str>) {
    match answer {
        Err(problem) => (problem.status, problem.code),
        Ok(other) => panic!("not refused: {other:?}"),
    }
}

#[tokio::test]
async fn a_listing_and_an_upload_answer_under_their_team_and_absent_teams_have_none() {
    let fixture = Fixture::new().await;
    let upload = FileUpload {
        filename: "notes.txt".to_owned(),
        media_type: "text/plain".to_owned(),
        content: b"hello".to_vec(),
    };
    let stored = json(
        fixture
            .files
            .upload("team_1", &crate::http::service::Body::File(upload))
            .await,
    );
    assert_eq!(
        stored.get("team_id"),
        Some(&Value::String("team_1".to_owned()))
    );
    let file = stored.get("file").expect("the stored file");
    assert_eq!(
        file.get("name"),
        Some(&Value::String("notes.txt".to_owned()))
    );
    let listing = json(fixture.files.list("team_1").await);
    let Some(Value::Array(listed)) = listing.get("files") else {
        panic!("a listing")
    };
    assert_eq!(listed.len(), 1);
    assert!(listed[0].get("content").is_none());
    assert_eq!(
        code(fixture.files.list("team_2").await),
        (404, Some("team-not-found"))
    );
    assert_eq!(
        code(
            fixture
                .files
                .upload("team_1", &crate::http::service::Body::None)
                .await
        ),
        (422, Some("invalid-file"))
    );
}

#[tokio::test]
async fn a_listed_image_previews_through_the_helper_and_a_page_names_its_count() {
    let fixture = Fixture::new().await;
    let photo = fixture.put(
        "photo.png",
        &fixtures::image(Format::Png, 64, 32, true),
        false,
    );
    script(&fixture.exec, Worker::Real);
    let Ok(Reply::Binary {
        media_type,
        body,
        headers,
    }) = fixture.files.preview("team_1", &photo, None).await
    else {
        panic!("an image rendition");
    };
    assert_eq!(media_type, "image/png");
    assert!(body.starts_with(b"\x89PNG\r\n\x1a\n"));
    assert_eq!(headers.len(), 0);
    assert_eq!(fixture.daemon.helpers().await.len(), 0);

    let pdf = fixture.put("a.pdf", &fixtures::text_pdf(&["one", "two"]), false);
    let page = team_prepare::image::prepare(&fixtures::image(Format::Png, 8, 8, false))
        .expect("a derivative");
    script(
        &fixture.exec,
        Worker::Fixed(Answer::Page(page, 2).to_value()),
    );
    let Ok(Reply::Binary { headers, .. }) = fixture.files.preview("team_1", &pdf, Some("2")).await
    else {
        panic!("a page rendition");
    };
    assert_eq!(headers, [("X-Page-Count", "2".to_owned())]);
    for page in ["0", "51", "01", "x"] {
        assert_eq!(
            code(fixture.files.preview("team_1", &pdf, Some(page)).await),
            (404, Some("file-not-found"))
        );
    }
}

#[tokio::test]
async fn missing_unsupported_and_ended_previews_answer_their_outcome() {
    let fixture = Fixture::new().await;
    let notes = fixture.put("notes.txt", b"plain text", false);
    assert_eq!(
        code(fixture.files.preview("team_1", &notes, None).await),
        (422, Some("file-preview-unsupported"))
    );
    assert_eq!(
        code(fixture.files.preview("team_1", &"f".repeat(32), None).await),
        (404, Some("file-not-found"))
    );
    assert_eq!(
        code(fixture.files.preview("team_2", &notes, None).await),
        (404, Some("team-not-found"))
    );
    let photo = fixture.put(
        "photo.png",
        &fixtures::image(Format::Png, 8, 8, false),
        false,
    );
    let teardown = HelperTeardown {
        docker: Arc::clone(&fixture.daemon) as Arc<dyn crate::docker::DockerEngine>,
        space_id: "space".to_owned(),
    };
    let closed = fixture
        .files
        .coordination
        .previews
        .close(Some("team_1"), &teardown, Duration::from_secs(1))
        .await
        .expect("closed");
    assert_eq!(
        code(fixture.files.preview("team_1", &photo, None).await),
        (404, Some("file-not-found"))
    );
    assert_eq!(
        code(fixture.files.content("team_1", &photo, context(None)).await),
        (404, Some("file-not-found"))
    );
    drop(closed);
    assert_eq!(fixture.exec.started().len(), 0);
}

#[tokio::test]
async fn a_file_collected_while_it_was_prepared_is_not_answered() {
    let fixture = Fixture::new().await;
    let photo = fixture.put(
        "photo.png",
        &fixtures::image(Format::Png, 8, 8, false),
        false,
    );
    let store = Arc::clone(&fixture.store.0);
    let worker = script(&fixture.exec, Worker::Real);
    let collector = tokio::spawn(async move {
        let _ = worker.await;
        store.destroy("team_1").expect("collected");
    });
    let answered = fixture.files.preview("team_1", &photo, None);
    let (answered, ()) = tokio::join!(answered, async { collector.await.expect("ran") });
    assert_eq!(code(answered), (404, Some("file-not-found")));
}

#[tokio::test]
async fn an_original_is_released_whole_with_its_digest_after_its_audit() {
    let fixture = Fixture::new().await;
    let id = fixture.put("report.bin", b"\x00original bytes", false);
    let Ok(Reply::Binary {
        media_type,
        body,
        headers,
    }) = fixture
        .files
        .content("team_1", &id, context(Some(SUPERVISOR)))
        .await
    else {
        panic!("the original");
    };
    assert_eq!(
        (media_type, body.as_slice()),
        ("application/octet-stream", &b"\x00original bytes"[..])
    );
    assert_eq!(headers[0].0, "Repr-Digest");
    assert!(headers[0].1.starts_with("sha-256=:") && headers[0].1.ends_with(':'));
    let events = fixture.audit.events();
    let detail = events[0]
        .fields()
        .iter()
        .find(|(name, _)| name == "detail")
        .map(|(_, value)| value.clone());
    assert_eq!(detail, Some(Value::String(format!("file={id} bytes=15"))));

    fixture.audit.fail(true);
    assert_eq!(
        code(
            fixture
                .files
                .content("team_1", &id, context(Some(SUPERVISOR)))
                .await
        ),
        (500, Some("internal-error"))
    );
}

#[tokio::test]
async fn at_most_two_downloads_run_at_once() {
    let fixture = Fixture::new().await;
    let id = fixture.put("report.bin", b"bytes", false);
    let (_first, _second) = (
        fixture.files.downloads.try_acquire().expect("free"),
        fixture.files.downloads.try_acquire().expect("free"),
    );
    let refused = fixture
        .files
        .content("team_1", &id, context(Some(SUPERVISOR)))
        .await;
    let Err(problem) = refused else {
        panic!("busy")
    };
    assert_eq!(
        (problem.status, problem.code, problem.retry_after_ms),
        (503, Some("file-download-busy"), Some(250))
    );
}

#[tokio::test]
async fn a_web_original_leaves_only_for_the_supervisor_who_acknowledged_it() {
    let fixture = Fixture::new().await;
    let web = fixture.put("download.pdf", b"%PDF-1.7 from the web", true);
    let required = (403, Some("file-acknowledgement-required"));
    assert_eq!(
        code(fixture.download(&web, Some(SUPERVISOR)).await),
        required
    );
    assert_eq!(code(fixture.acknowledged(&web, None).await), required);
    // No acknowledgement exists without its audit record.
    fixture.audit.fail(true);
    let unrecorded = fixture.acknowledged(&web, Some(SUPERVISOR)).await;
    assert_eq!(code(unrecorded), (500, Some("internal-error")));
    assert_eq!(
        code(fixture.download(&web, Some(SUPERVISOR)).await),
        required
    );
    fixture.audit.fail(false);
    let acknowledged = json(fixture.acknowledged(&web, Some(SUPERVISOR)).await);
    assert_eq!(acknowledged.get("acknowledged"), Some(&Value::Bool(true)));
    assert!(fixture.download(&web, Some(SUPERVISOR)).await.is_ok());
    // Another Supervisor, or nobody, cannot use it.
    let other = "b".repeat(32);
    assert_eq!(code(fixture.download(&web, Some(&other)).await), required);
    assert_eq!(code(fixture.download(&web, None).await), required);
}

#[tokio::test]
async fn only_a_listed_file_from_the_web_can_be_acknowledged() {
    let fixture = Fixture::new().await;
    let uploaded = fixture.put("upload.txt", b"mine", false);
    assert_eq!(
        code(
            fixture
                .files
                .acknowledge("team_1", &uploaded, context(Some(SUPERVISOR)))
                .await
        ),
        (409, Some("file-not-from-web"))
    );
    assert_eq!(
        code(
            fixture
                .files
                .acknowledge("team_1", &"f".repeat(32), context(Some(SUPERVISOR)))
                .await
        ),
        (404, Some("file-not-found"))
    );
}

#[test]
fn a_rendition_outside_its_framing_is_refused() {
    let png = fixtures::image(Format::Png, 8, 8, false);
    for (media_type, bytes, pages) in [
        ("image/jpeg", png.clone(), None),
        ("image/png", vec![0; 8], None),
        ("image/png", png.clone(), Some(51)),
        ("image/gif", png.clone(), None),
        ("image/png", vec![0x89; 512 * 1024 + 1], None),
    ] {
        let paged = pages.is_some();
        let rendition = Rendition {
            media_type,
            bytes,
            pages,
        };
        assert!(framed(rendition, paged).is_err());
    }
    let rendition = Rendition {
        media_type: "image/png",
        bytes: png,
        pages: Some(3),
    };
    assert!(framed(rendition, true).is_ok());
}
