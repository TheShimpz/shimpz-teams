use std::sync::Arc;
use std::time::{Duration, UNIX_EPOCH};

use strict_json::Value;
use team_core::exec::ExecExit;
use team_core::stop::Stop;
use team_core::testing::{CountingEntropy, FakeClock, FakeExec, ScriptedExec};
use team_prepare::answer::{Answer, Derivative, MediaType, Reason, opaque};
use team_prepare::fixtures::{self, Format};
use team_prepare::message::LimitError;
use team_storage::files::{FileMetadata, FileStore};
use tokio::io::{AsyncReadExt, duplex};

use super::{LocalPreparer, RenditionError, SegmentError};
use crate::coordination::previews::PreviewBook;
use crate::prepare::fakes::{Daemon, Worker, helpers, script};
use crate::prepare::store::Store;
use crate::prepare::{PrepareError, Preparer};

struct Fixture {
    _directory: tempfile::TempDir,
    daemon: Arc<Daemon>,
    exec: Arc<FakeExec>,
    store: Store,
    preparer: LocalPreparer,
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
        let preparer = LocalPreparer::new(helpers(&daemon, &exec), store.clone());
        Self {
            _directory: directory,
            daemon,
            exec,
            store,
            preparer,
        }
    }

    fn put(&self, name: &str, content: &[u8]) -> FileMetadata {
        self.store
            .0
            .put("team_1", name, content, None, false)
            .expect("stored")
            .file
            .metadata
    }

    async fn prepare(&self, selected: &[FileMetadata]) -> Result<Vec<Value>, SegmentError> {
        let prepared = self
            .preparer
            .attachments("team_1", selected, &Stop::new())
            .await?;
        Ok(prepared
            .into_iter()
            .map(|attachment| attachment.content)
            .collect())
    }
}

fn kind(content: &Value) -> Option<&str> {
    match content.get("type") {
        Some(Value::String(kind)) => Some(kind),
        _ => None,
    }
}

#[tokio::test]
async fn text_is_read_in_the_controller_without_a_helper() {
    let fixture = Fixture::new().await;
    let notes = fixture.put("notes.md", "# Ação\nItem".as_bytes());
    let prepared = fixture
        .preparer
        .attachments("team_1", &[notes], &Stop::new())
        .await
        .expect("prepared");
    assert_eq!(prepared[0].media_type, "text/markdown");
    assert_eq!(
        prepared[0].content,
        team_prepare::content::text(Some("# Ação\nItem"), false)
    );
    assert_eq!(fixture.exec.started(), [] as [team_core::exec::ExecSpec; 0]);
    assert_eq!(fixture.daemon.events(), [] as [std::string::String; 0]);
}

#[tokio::test]
async fn one_helper_serves_every_image_and_pdf_of_a_segment_and_is_gone_after() {
    let fixture = Fixture::new().await;
    let selected = [
        fixture.put("a.png", &fixtures::image(Format::Png, 40, 20, false)),
        fixture.put("b.pdf", &fixtures::text_pdf(&["Report body"])),
        fixture.put("c.zip", b"PK\x03\x04binary"),
    ];
    script(&fixture.exec, Worker::Real);
    script(
        &fixture.exec,
        Worker::Fixed(Answer::Text("Report body".to_owned()).to_value()),
    );
    let prepared = fixture
        .preparer
        .attachments("team_1", &selected, &Stop::new())
        .await
        .expect("prepared");
    assert_eq!(kind(&prepared[0].content), Some("image"));
    assert!(prepared[0].content.get("sha256").is_some());
    assert_eq!(
        prepared[1].content,
        team_prepare::content::text(Some("Report body"), true)
    );
    assert_eq!(
        (prepared[2].media_type.as_str(), &prepared[2].content),
        ("application/octet-stream", &opaque(Reason::Unsupported))
    );
    assert_eq!(fixture.exec.started().len(), 2);
    assert_eq!(
        fixture
            .daemon
            .events()
            .iter()
            .filter(|event| event.starts_with("create"))
            .count(),
        1
    );
    assert_eq!(
        fixture.daemon.helpers().await,
        [] as [std::string::String; 0]
    );
}

#[tokio::test]
async fn untrusted_helper_answers_are_revalidated() {
    let fixture = Fixture::new().await;
    let png = fixture.put("a.png", &fixtures::image(Format::Png, 8, 8, false));
    let derivative = Derivative {
        media_type: MediaType::Png,
        width: 4000,
        height: 8,
        bytes: fixtures::image(Format::Png, 8, 8, false),
    };
    script(
        &fixture.exec,
        Worker::Fixed(Answer::Image(derivative).to_value()),
    );
    assert_eq!(
        fixture.prepare(&[png]).await,
        Ok(vec![opaque(Reason::Unreadable)])
    );
}

#[tokio::test]
async fn per_message_ceilings_refuse_the_whole_message() {
    let fixture = Fixture::new().await;
    let images: Vec<_> = (0..5_u8)
        .map(|index| {
            fixture.put(
                &format!("{index}.png"),
                &fixtures::image(Format::Png, 8 + u32::from(index), 8, false),
            )
        })
        .collect();
    for _ in 0..5 {
        script(&fixture.exec, Worker::Real);
    }
    assert_eq!(
        fixture.prepare(&images).await,
        Err(SegmentError::Limit(LimitError::TooManyImages))
    );
    assert_eq!(
        fixture.daemon.helpers().await,
        [] as [std::string::String; 0]
    );
    let texts: Vec<_> = (0..9)
        .map(|index| fixture.put(&format!("{index}.txt"), format!("t{index}").as_bytes()))
        .collect();
    assert_eq!(
        fixture.prepare(&texts).await,
        Err(SegmentError::Limit(LimitError::TooMany))
    );
}

#[tokio::test]
async fn an_original_beyond_every_ceiling_is_never_read_and_changed_bytes_are_refused() {
    let fixture = Fixture::new().await;
    // Never stored: reading it would fail, so only an unread original can be prepared.
    let huge = FileMetadata {
        id: "d".repeat(32),
        name: "video.mp4".to_owned(),
        media_type: "video/mp4".to_owned(),
        size: 9 * 1024 * 1024,
        sha256: "0".repeat(64),
    };
    assert_eq!(
        fixture.prepare(&[huge]).await,
        Ok(vec![opaque(Reason::TooLarge)])
    );
    let mut changed = fixture.put("a.txt", b"original");
    changed.sha256 = "0".repeat(64);
    assert_eq!(
        fixture.prepare(&[changed]).await,
        Err(SegmentError::Changed)
    );
}

#[tokio::test]
async fn a_stopped_turn_reads_nothing_and_a_stop_during_preparation_removes_the_helper() {
    let fixture = Fixture::new().await;
    let pdf = fixture.put("a.pdf", &fixtures::text_pdf(&["x"]));
    let stop = Stop::new();
    stop.stop();
    assert_eq!(
        fixture
            .preparer
            .attachments("team_1", std::slice::from_ref(&pdf), &stop)
            .await
            .map(|_| ()),
        Err(SegmentError::Stopped)
    );
    assert_eq!(fixture.daemon.events(), [] as [std::string::String; 0]);

    let (team_end, mut workload_end) = duplex(64 * 1024);
    fixture.exec.push(ScriptedExec {
        stream: team_end,
        exit: ExecExit {
            running: false,
            exit_code: Some(0),
        },
    });
    let stop = Stop::new();
    let stopper = {
        let stop = stop.clone();
        tokio::spawn(async move {
            // The worker reads its request and never answers; the turn is stopped meanwhile.
            let mut request = Vec::new();
            let _ = workload_end.read_to_end(&mut request).await;
            stop.stop();
            tokio::time::sleep(Duration::from_secs(3600)).await;
            drop(workload_end);
        })
    };
    let prepared = fixture.preparer.attachments("team_1", &[pdf], &stop).await;
    assert_eq!(prepared.map(|_| ()), Err(SegmentError::Stopped));
    assert_eq!(
        fixture.daemon.helpers().await,
        [] as [std::string::String; 0]
    );
    stopper.abort();
}

#[tokio::test]
async fn the_preparer_seam_answers_each_file_s_wire_attachment() {
    let fixture = Fixture::new().await;
    let notes = fixture.put("notes.txt", b"hello");
    let prepared = fixture
        .preparer
        .prepare_segment("team_1", std::slice::from_ref(&notes.id), &Stop::new())
        .await
        .expect("prepared");
    assert_eq!(prepared[0].file_id, notes.id);
    assert_eq!(
        prepared[0].derivative.get("sha256"),
        Some(&Value::String(notes.sha256.clone()))
    );
    assert_eq!(
        prepared[0].derivative.get("media_type"),
        Some(&Value::String("text/plain".to_owned()))
    );
    let missing = ["f".repeat(32)];
    assert_eq!(
        fixture
            .preparer
            .prepare_segment("team_1", &missing, &Stop::new())
            .await
            .map(|_| ()),
        Err(PrepareError::Unreadable)
    );
}

#[tokio::test]
async fn a_stored_image_or_pdf_page_becomes_a_validated_rendition() {
    let fixture = Fixture::new().await;
    let book = PreviewBook::new();
    let photo = fixture.put("photo.jpg", &fixtures::oriented_jpeg(300, 100, 6));
    script(&fixture.exec, Worker::Real);
    let preview = book.open("team_1").expect("open");
    let rendition = fixture
        .preparer
        .render("team_1", &photo, None, &preview)
        .await
        .expect("rendered");
    let rendition = rendition.expect("an image has a rendition");
    assert_eq!(
        (rendition.media_type, rendition.pages),
        ("image/jpeg", None)
    );
    assert!(rendition.bytes.starts_with(b"\xff\xd8\xff"));
    assert_eq!(
        fixture.daemon.helpers().await,
        [] as [std::string::String; 0]
    );

    let pdf = fixture.put("a.pdf", &fixtures::text_pdf(&["one", "two"]));
    let page = team_prepare::image::prepare(&fixtures::image(Format::Png, 8, 8, false))
        .expect("a derivative");
    script(
        &fixture.exec,
        Worker::Fixed(Answer::Page(page.clone(), 2).to_value()),
    );
    let rendition = fixture
        .preparer
        .render("team_1", &pdf, Some(2), &preview)
        .await
        .expect("rendered");
    assert_eq!(rendition.map(|rendition| rendition.pages), Some(Some(2)));
    // A page count below the page asked for is refused.
    script(
        &fixture.exec,
        Worker::Fixed(Answer::Page(page, 1).to_value()),
    );
    assert_eq!(
        fixture
            .preparer
            .render("team_1", &pdf, Some(2), &preview)
            .await,
        Ok(None)
    );
}

#[tokio::test]
async fn the_kind_comes_from_the_bytes_and_an_ended_preview_reads_nothing() {
    let fixture = Fixture::new().await;
    let book = PreviewBook::new();
    let preview = book.open("team_1").expect("open");
    let disguised = fixture.put("photo.png", &fixtures::text_pdf(&["not an image"]));
    assert_eq!(
        fixture
            .preparer
            .render("team_1", &disguised, None, &preview)
            .await,
        Ok(None)
    );
    let image = fixture.put("photo2.png", &fixtures::image(Format::Png, 8, 8, false));
    assert_eq!(
        fixture
            .preparer
            .render("team_1", &image, Some(1), &preview)
            .await,
        Ok(None)
    );
    let mut oversized = image.clone();
    oversized.size = 9 * 1024 * 1024;
    assert_eq!(
        fixture
            .preparer
            .render("team_1", &oversized, None, &preview)
            .await,
        Ok(None)
    );
    assert_eq!(fixture.exec.started(), [] as [team_core::exec::ExecSpec; 0]);

    let ending = book.close(Some("team_1"), &Removes, Duration::from_millis(1));
    let (_closed, rendered) = tokio::join!(
        ending,
        fixture.preparer.render("team_1", &image, None, &preview)
    );
    assert_eq!(rendered, Err(RenditionError::Ended));
    assert_eq!(fixture.daemon.events(), [] as [std::string::String; 0]);
}

struct Removes;

impl crate::coordination::previews::HelperRemover for Removes {
    fn remove<'a>(
        &'a self,
        _container: &'a str,
    ) -> team_core::future::BoxFuture<'a, Result<(), crate::coordination::previews::RemoveFailed>>
    {
        Box::pin(async { Ok(()) })
    }
}

#[tokio::test]
async fn the_slot_is_held_before_any_original_is_read_even_for_text() {
    let fixture = Fixture::new().await;
    let mut notes = fixture.put("notes.txt", b"plain");
    let held = fixture
        .preparer
        .admission
        .turn(&Stop::new())
        .await
        .expect("free");
    // While another segment holds the slot, this one reads nothing: a changed file it would refuse stays unread.
    notes.sha256 = "0".repeat(64);
    let stop = Stop::new();
    let waiting = fixture
        .preparer
        .attachments("team_1", std::slice::from_ref(&notes), &stop);
    let waited = tokio::time::timeout(Duration::from_millis(50), waiting).await;
    assert!(waited.is_err());
    drop(held);
    let read = fixture
        .preparer
        .attachments("team_1", &[notes], &Stop::new())
        .await;
    assert_eq!(read.map(|_| ()), Err(SegmentError::Changed));
}
