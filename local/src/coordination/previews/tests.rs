use std::sync::Mutex;
use std::time::Duration;

use team_core::future::BoxFuture;

use super::{
    HelperRemover, PREVIEW_DRAIN, PreviewBook, PreviewDrainError, PreviewEnded, RemoveFailed,
};

#[derive(Default)]
struct Remover {
    removed: Mutex<Vec<String>>,
    failing: bool,
}

impl HelperRemover for Remover {
    fn remove<'a>(&'a self, container: &'a str) -> BoxFuture<'a, Result<(), RemoveFailed>> {
        Box::pin(async move {
            if self.failing {
                return Err(RemoveFailed);
            }
            self.removed
                .lock()
                .expect("usable")
                .push(container.to_owned());
            Ok(())
        })
    }
}

#[tokio::test(start_paused = true)]
async fn closing_a_team_ends_its_previews_removes_their_helpers_and_waits_for_them() {
    let book = PreviewBook::new();
    let remover = Remover::default();
    let alpha = book.open("alpha").expect("open");
    alpha.started("helper-1").expect("running");
    let beta = book.open("beta").expect("open");
    let leaving = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_secs(1)).await;
        assert_eq!(alpha.check(), Err(PreviewEnded));
        drop(alpha);
    });
    let closed = book
        .close(Some("alpha"), &remover, PREVIEW_DRAIN)
        .await
        .expect("drained");
    leaving.await.expect("left");
    assert_eq!(*remover.removed.lock().expect("usable"), ["helper-1"]);
    assert_eq!(book.open("alpha").err(), Some(PreviewEnded));
    assert!(beta.check().is_ok(), "another Team's preview runs on");
    drop(closed);
    assert!(book.open("alpha").is_ok());
}

#[tokio::test(start_paused = true)]
async fn a_helper_started_after_the_end_is_refused() {
    let book = PreviewBook::new();
    let preview = book.open("alpha").expect("open");
    let remover = Remover::default();
    let close = book.close(None, &remover, Duration::from_millis(10));
    assert_eq!(close.await.err(), Some(PreviewDrainError::Timeout));
    assert_eq!(preview.started("late"), Err(PreviewEnded));
    assert!(book.open("beta").is_ok(), "a failed close reopens");
}

#[tokio::test(start_paused = true)]
async fn a_helper_that_cannot_be_removed_fails_the_close() {
    let book = PreviewBook::new();
    let preview = book.open("alpha").expect("open");
    preview.started("helper-1").expect("running");
    let remover = Remover {
        failing: true,
        ..Remover::default()
    };
    assert_eq!(
        book.close(None, &remover, PREVIEW_DRAIN).await.err(),
        Some(PreviewDrainError::HelperNotRemoved)
    );
    preview.stopped("helper-1");
}
