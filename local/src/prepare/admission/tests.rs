use std::sync::Arc;
use std::time::Duration;

use team_core::stop::Stop;

use super::{Admission, PREVIEW_DEADLINE, PREVIEW_QUEUE, PreviewWait, Stopped};
use crate::coordination::previews::{HelperRemover, PreviewBook, RemoveFailed};

struct Removes;

impl HelperRemover for Removes {
    fn remove<'a>(
        &'a self,
        _container: &'a str,
    ) -> team_core::future::BoxFuture<'a, Result<(), RemoveFailed>> {
        Box::pin(async { Ok(()) })
    }
}

#[tokio::test(start_paused = true)]
async fn a_turn_waits_for_the_slot_and_a_stopped_one_leaves_at_once() {
    let admission = Arc::new(Admission::new());
    let held = admission.turn(&Stop::new()).await.expect("free");
    let stop = Stop::new();
    let waiting = {
        let (admission, stop) = (Arc::clone(&admission), stop.clone());
        tokio::spawn(async move { admission.turn(&stop).await.map(drop) })
    };
    tokio::time::sleep(Duration::from_secs(60)).await;
    assert!(!waiting.is_finished());
    stop.stop();
    assert_eq!(waiting.await.expect("ran"), Err(Stopped));
    // A turn already stopped never takes the slot, and the slot frees when its holder leaves.
    drop(held);
    assert!(admission.turn(&stop).await.is_err());
    assert!(admission.turn(&Stop::new()).await.is_ok());
}

#[tokio::test(start_paused = true)]
async fn a_full_queue_and_an_expired_deadline_are_busy() {
    let (admission, book) = (Arc::new(Admission::new()), PreviewBook::new());
    let held = admission.turn(&Stop::new()).await.expect("free");
    let mut waiting = Vec::new();
    for _ in 0..PREVIEW_QUEUE {
        let (admission, book) = (Arc::clone(&admission), book.clone());
        waiting.push(tokio::spawn(async move {
            let preview = book.open("team_1").expect("open");
            admission.preview(&preview).await.map(drop)
        }));
    }
    tokio::task::yield_now().await;
    let extra = book.open("team_1").expect("open");
    assert_eq!(
        admission.preview(&extra).await.map(drop),
        Err(PreviewWait::Busy)
    );
    tokio::time::sleep(PREVIEW_DEADLINE + Duration::from_secs(1)).await;
    for waiter in waiting {
        assert_eq!(waiter.await.expect("ran"), Err(PreviewWait::Busy));
    }
    // Once the slot frees, a preview takes it.
    drop(held);
    assert!(admission.preview(&extra).await.is_ok());
}

#[tokio::test(start_paused = true)]
async fn a_preview_ended_while_it_waits_leaves_before_its_deadline() {
    let (admission, book) = (Arc::new(Admission::new()), PreviewBook::new());
    let _held = admission.turn(&Stop::new()).await.expect("free");
    let waiting = {
        let (admission, book) = (Arc::clone(&admission), book.clone());
        tokio::spawn(async move {
            let preview = book.open("team_1").expect("open");
            admission.preview(&preview).await.map(drop)
        })
    };
    tokio::time::sleep(Duration::from_secs(1)).await;
    let closing = book.close(Some("team_1"), &Removes, Duration::from_secs(30));
    let (closed, waited) = tokio::join!(closing, waiting);
    assert!(closed.is_ok());
    assert_eq!(waited.expect("ran"), Err(PreviewWait::Ended));
}
