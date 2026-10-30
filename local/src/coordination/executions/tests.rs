use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use super::{
    ChatLimit, DRAIN, Demand, DrainTimeout, ExecutionKind, Executions, LimitUnavailable, Refusal,
};

struct Limit(AtomicUsize);

impl ChatLimit for Limit {
    fn current(&self) -> usize {
        self.0.load(Ordering::SeqCst)
    }

    fn save(&self, limit: u8) -> Result<u8, LimitUnavailable> {
        self.0.store(usize::from(limit), Ordering::SeqCst);
        Ok(limit)
    }
}

fn executions(limit: usize) -> Executions {
    Executions::new(Arc::new(Limit(AtomicUsize::new(limit))))
}

fn routine(id: &str) -> ExecutionKind {
    ExecutionKind::Routine {
        routine_id: id.to_owned(),
    }
}

#[test]
fn chat_turns_count_against_the_space_limit_and_routine_segments_do_not() {
    let executions = executions(1);
    let alpha = executions
        .admit("alpha", ExecutionKind::Chat, "t1".to_owned())
        .expect("admitted");
    assert_eq!(
        executions
            .admit("beta", ExecutionKind::Chat, "t2".to_owned())
            .err(),
        Some(Refusal::SpaceBusy)
    );
    let gamma = executions
        .admit("gamma", routine("r1"), "t3".to_owned())
        .expect("a segment is not counted");
    assert_eq!(executions.chat_executions(), 1);
    drop(alpha);
    assert!(
        executions
            .admit("beta", ExecutionKind::Chat, "t4".to_owned())
            .is_ok()
    );
    drop(gamma);
}

#[test]
fn a_held_slot_refuses_at_once_and_a_person_waiting_on_a_routine_is_remembered() {
    let executions = executions(2);
    let chat = executions
        .admit("alpha", ExecutionKind::Chat, "t1".to_owned())
        .expect("admitted");
    assert_eq!(
        executions
            .admit("alpha", routine("r1"), "t2".to_owned())
            .err(),
        Some(Refusal::ChatActive)
    );
    drop(chat);
    let segment = executions
        .admit("alpha", routine("r1"), "t3".to_owned())
        .expect("admitted");
    assert_eq!(executions.routine_holding("alpha"), Some("r1".to_owned()));
    assert_eq!(
        executions
            .admit("alpha", ExecutionKind::Chat, "t4".to_owned())
            .err(),
        Some(Refusal::RoutineActive)
    );
    assert_eq!(executions.demand("alpha"), Some(Demand::Waiting));
    drop(segment);
    assert!(matches!(executions.demand("alpha"), Some(Demand::Since(_))));
    let _chat = executions
        .admit("alpha", ExecutionKind::Chat, "t5".to_owned())
        .expect("admitted");
    assert_eq!(executions.demand("alpha"), None);
}

#[test]
fn a_commit_that_fails_leaves_the_turn_registered() {
    let executions = executions(2);
    let first = executions
        .admit("alpha", ExecutionKind::Chat, "t1".to_owned())
        .expect("admitted");
    assert_eq!(
        first.commit(|| Err::<(), _>("write failed")),
        Some(Err("write failed"))
    );
    assert_eq!(
        executions.chat_executions(),
        1,
        "a failed commit leaves the turn registered"
    );
    assert_eq!(first.commit(|| Ok::<_, ()>(7)), Some(Ok(7)));
    assert_eq!(executions.chat_executions(), 0);
    assert_eq!(first.commit(|| Ok::<_, ()>(8)), None, "already committed");
}

#[test]
fn commit_only_if_not_cancelled() {
    let executions = executions(2);
    let second = executions
        .admit("alpha", ExecutionKind::Chat, "t2".to_owned())
        .expect("admitted");
    assert_eq!(executions.cancel("alpha", false), Some("t2".to_owned()));
    assert!(second.stop().is_stopped());
    assert!(second.is_cancelled());
    let mut ran = false;
    assert_eq!(
        second.commit(|| {
            ran = true;
            Ok::<_, ()>(())
        }),
        None
    );
    assert!(!ran);
}

#[test]
fn stop_spares_a_routine_segment_only_when_asked() {
    let executions = executions(2);
    let segment = executions
        .admit("alpha", routine("r1"), "t1".to_owned())
        .expect("admitted");
    assert_eq!(executions.cancel("alpha", true), None);
    assert!(!segment.stop().is_stopped());
    assert_eq!(executions.cancel("alpha", false), Some("t1".to_owned()));
    assert!(segment.stop().is_stopped());
    assert_eq!(executions.cancel("beta", false), None);
}

#[tokio::test(start_paused = true)]
async fn destroy_waits_for_the_slot_and_refuses_chat_meanwhile() {
    let executions = executions(2);
    let turn = executions
        .admit("alpha", ExecutionKind::Chat, "t1".to_owned())
        .expect("admitted");
    assert_eq!(
        executions.exclusive("alpha", DRAIN).await.err(),
        Some(DrainTimeout)
    );
    drop(turn);
    let held = executions.exclusive("alpha", DRAIN).await.expect("free");
    assert_eq!(
        executions
            .admit("alpha", ExecutionKind::Chat, "t2".to_owned())
            .err(),
        Some(Refusal::ChatActive)
    );
    drop(held);
}

#[tokio::test(start_paused = true)]
async fn the_reset_cancels_and_drains_every_slot_and_closes_admission() {
    let executions = executions(2);
    let turn = executions
        .admit("alpha", ExecutionKind::Chat, "t1".to_owned())
        .expect("admitted");
    let stop = turn.stop().clone();
    let releaser = tokio::spawn(async move {
        stop.stopped().await;
        tokio::time::sleep(Duration::from_secs(1)).await;
        drop(turn);
    });
    let drained = executions.drain(DRAIN).await.expect("drained");
    releaser.await.expect("released");
    assert_eq!(
        executions
            .admit("beta", ExecutionKind::Chat, "t2".to_owned())
            .err(),
        Some(Refusal::SpaceResetting)
    );
    assert_eq!(
        executions
            .admit("alpha", ExecutionKind::Chat, "t3".to_owned())
            .err(),
        Some(Refusal::ChatActive)
    );
    drop(drained);
    assert!(
        executions
            .admit("beta", ExecutionKind::Chat, "t4".to_owned())
            .is_ok()
    );
}

#[tokio::test(start_paused = true)]
async fn a_drain_that_times_out_reopens_admission() {
    let executions = executions(2);
    let _stuck = executions
        .admit("alpha", ExecutionKind::Chat, "t1".to_owned())
        .expect("admitted");
    assert_eq!(executions.drain(DRAIN).await.err(), Some(DrainTimeout));
    assert!(
        executions
            .admit("beta", ExecutionKind::Chat, "t2".to_owned())
            .is_ok()
    );
}

#[test]
fn a_saved_limit_governs_the_next_admission() {
    let executions = executions(1);
    let _alpha = executions
        .admit("alpha", ExecutionKind::Chat, "t1".to_owned())
        .expect("admitted");
    assert_eq!(executions.save_limit(2), Ok(2));
    assert!(
        executions
            .admit("beta", ExecutionKind::Chat, "t2".to_owned())
            .is_ok()
    );
}
