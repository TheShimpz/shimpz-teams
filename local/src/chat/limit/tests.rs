use std::os::unix::fs::PermissionsExt;
use std::sync::Arc;

use super::{ChatLimitStore, DEFAULT, NAME};
use crate::coordination::executions::{
    ChatLimit, ExecutionKind, Executions, LimitUnavailable, Refusal,
};

#[test]
fn the_limit_is_the_default_until_a_save_commits_one_that_a_restart_reads_back() {
    let directory = tempfile::tempdir().expect("directory");
    let root = directory.path().join("inference");
    let store = ChatLimitStore::new(&root);
    assert_eq!(store.current(), DEFAULT);
    assert_eq!(store.save(1), Ok(1));
    assert_eq!(store.current(), 1);
    let path = root.join(NAME);
    assert_eq!(
        std::fs::read_to_string(&path).expect("record"),
        r#"{"limit":1,"schema":1}"#
    );
    assert_eq!(
        std::fs::metadata(&path)
            .expect("metadata")
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    assert_eq!(ChatLimitStore::new(&root).current(), 1);
    assert_eq!(store.delete(), Ok(()));
    assert_eq!(store.current(), DEFAULT);
    assert_eq!(store.delete(), Ok(()));
}

#[test]
fn a_record_that_is_not_the_stores_own_fails_closed() {
    let directory = tempfile::tempdir().expect("directory");
    let root = directory.path();
    let path = root.join(NAME);
    for record in [
        r#"{"schema":1,"limit":3}"#,
        r#"{"schema":2,"limit":1}"#,
        r#"{"schema":true,"limit":1}"#,
        r#"{"schema":1,"limit":1,"extra":1}"#,
        r#"{"schema":1,"limit":true}"#,
        "[1]",
        "not json",
    ] {
        std::fs::write(&path, record).expect("record");
        let store = ChatLimitStore::new(root);
        assert_eq!(store.state(), Err(LimitUnavailable), "{record}");
        // No chat turn is admitted while the record fails closed.
        assert_eq!(store.current(), 0);
    }
    // A save replaces the failed record.
    let store = ChatLimitStore::new(root);
    assert_eq!(store.save(2), Ok(2));
    assert_eq!(store.state(), Ok(2));
}

#[test]
fn a_save_that_cannot_reach_the_volume_fails_closed_and_keeps_the_limit() {
    let directory = tempfile::tempdir().expect("directory");
    let root = directory.path().join("inference");
    std::fs::create_dir(&root).expect("root");
    // The volume is read-only: the record can be read (absent) but not written.
    std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o500)).expect("read-only");
    let store = ChatLimitStore::new(&root);
    assert_eq!(store.save(1), Err(LimitUnavailable));
    assert_eq!(store.current(), DEFAULT);
    std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700)).expect("writable");
}

#[tokio::test]
async fn a_turn_beyond_the_limit_is_refused_and_a_routine_segment_never_counts() {
    let directory = tempfile::tempdir().expect("directory");
    let store = Arc::new(ChatLimitStore::new(directory.path()));
    store.save(1).expect("saved");
    let executions = Executions::new(store);
    let first = executions
        .admit("team_1", ExecutionKind::Chat, "a".into())
        .expect("first");
    assert_eq!(
        executions
            .admit("team_2", ExecutionKind::Chat, "b".into())
            .map(|_| ()),
        Err(Refusal::SpaceBusy)
    );
    drop(first);
    let routine = ExecutionKind::Routine {
        routine_id: "r".repeat(32),
    };
    let _segment = executions
        .admit("team_1", routine, "c".into())
        .expect("segment");
    let _chat = executions
        .admit("team_2", ExecutionKind::Chat, "d".into())
        .expect("chat");
    assert_eq!(executions.chat_executions(), 1);
}

#[tokio::test]
async fn a_lowered_limit_lets_running_turns_drain_and_admits_none_beside_them() {
    let directory = tempfile::tempdir().expect("directory");
    let executions = Executions::new(Arc::new(ChatLimitStore::new(directory.path())));
    let first = executions
        .admit("team_1", ExecutionKind::Chat, "a".into())
        .expect("first");
    let second = executions
        .admit("team_2", ExecutionKind::Chat, "b".into())
        .expect("second");
    assert_eq!(executions.save_limit(1), Ok(1));
    assert_eq!(executions.chat_executions(), 2);
    assert_eq!(
        executions
            .admit("team_3", ExecutionKind::Chat, "c".into())
            .map(|_| ()),
        Err(Refusal::SpaceBusy)
    );
    drop((first, second));
    let _third = executions
        .admit("team_3", ExecutionKind::Chat, "c".into())
        .expect("third");
    assert_eq!(executions.chat_executions(), 1);
}
