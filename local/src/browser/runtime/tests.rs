use std::sync::Arc;
use std::time::Duration;

use tokio::time::Instant;

use super::{Acquire, BrowserError, BrowserRuntime, Mode};
use crate::browser::fake::Fake;

struct Case {
    fake: Fake,
    runtime: Arc<BrowserRuntime>,
    _state: tempfile::TempDir,
}

impl Case {
    fn new() -> Self {
        let state = tempfile::tempdir().expect("a directory");
        let fake = Fake::default();
        let runtime = Arc::new(fake.runtime(state.path()));
        Self {
            fake,
            runtime,
            _state: state,
        }
    }

    async fn agent(&self, team: &str) -> Result<super::Lease, BrowserError> {
        self.runtime
            .acquire(Acquire {
                team_id: team,
                mode: Mode::Agent,
                profile: None,
                deadline: None,
                stop: None,
            })
            .await
    }
}

#[tokio::test]
async fn an_agent_s_session_runs_with_devtools_marked_as_the_agent_s_and_records_nothing() {
    let case = Case::new();
    let lease = case.agent("team_1").await.expect("a Browser");
    assert_eq!(lease.mode, "agent");
    let body = case.fake.state().bodies[0].clone();
    let body = strict_json::parse(body.as_bytes()).expect("json");
    assert_eq!(body.get("devtools"), Some(&strict_json::Value::Bool(true)));
    assert_eq!(body.get("agent"), Some(&strict_json::Value::Bool(true)));
    let features = body.get("features").expect("features");
    for off in [
        "replay",
        "recording",
        "sound",
        "keyboard_pointer",
        "clipboard",
    ] {
        assert_eq!(
            features.get(off),
            Some(&strict_json::Value::Bool(false)),
            "{off}"
        );
    }
    // Downloads are kept for the task without a person's answer.
    assert!(
        case.fake
            .state()
            .requests
            .contains(&"PUT /api/agent-files".to_owned())
    );
    case.runtime.release(&lease).await.expect("released");
    assert_eq!(case.fake.state().sessions, Vec::<String>::new());
    assert!(!case.runtime.dirty());
}

#[tokio::test]
async fn a_person_s_session_is_not_the_agent_s_and_takes_nothing_out() {
    let case = Case::new();
    let lease = case
        .runtime
        .acquire(Acquire {
            team_id: "team_1",
            mode: Mode::Person("https://www.example.com/login".to_owned()),
            profile: Some("0123"),
            deadline: None,
            stop: None,
        })
        .await
        .expect("a Browser");
    assert_eq!(lease.mode, "person");
    let body = case.fake.state().bodies[0].clone();
    let body = strict_json::parse(body.as_bytes()).expect("json");
    assert_eq!(body.get("agent"), Some(&strict_json::Value::Bool(false)));
    assert_eq!(body.get("devtools"), Some(&strict_json::Value::Bool(false)));
    let features = body.get("features").expect("features");
    assert_eq!(
        features.get("downloads"),
        Some(&strict_json::Value::Bool(false))
    );
    assert_eq!(
        features.get("keyboard_pointer"),
        Some(&strict_json::Value::Bool(true))
    );
}

#[tokio::test]
async fn the_space_holds_one_browser_and_a_held_slot_waits_its_release() {
    let case = Case::new();
    let first = case.agent("team_1").await.expect("a Browser");
    assert_eq!(case.agent("team_2").await.err(), Some(BrowserError::BUSY));
    case.runtime.release(&first).await.expect("released");
    let second = case.agent("team_2").await.expect("a Browser");
    assert_ne!(first.session_id, second.session_id);
}

#[tokio::test]
async fn a_session_left_by_an_earlier_team_process_is_removed_before_anything_runs() {
    let case = Case::new();
    case.fake.leave_session("left");
    assert!(!case.runtime.previous_gone());
    assert!(case.runtime.startup().await);
    assert!(case.runtime.previous_gone());
    assert_eq!(case.fake.state().sessions, Vec::<String>::new());
    assert!(case.runtime.available());
}

#[tokio::test]
async fn whatever_a_lost_creation_left_goes_before_the_next_browser_and_with_the_sweep() {
    let case = Case::new();
    assert!(case.runtime.startup().await);
    case.fake.leave_session("late");
    case.runtime.sweep().await;
    assert_eq!(case.fake.state().sessions, Vec::<String>::new());
    case.fake.leave_session("later");
    let lease = case.agent("team_1").await.expect("a Browser");
    assert_eq!(case.fake.state().sessions, [lease.session_id]);
}

#[tokio::test]
async fn a_removal_not_proven_keeps_the_space_refusing_until_a_cleanup_proves_it() {
    let case = Case::new();
    let lease = case.agent("team_1").await.expect("a Browser");
    case.fake.state().faults.insert("keep-stopped");
    assert_eq!(case.runtime.release(&lease).await, Err(BrowserError::LOST));
    assert!(case.runtime.dirty());
    assert!(case.agent("team_1").await.is_err());
    assert!(!case.runtime.recover().await);
    case.fake.state().faults.remove("keep-stopped");
    assert!(case.runtime.recover().await);
    assert!(!case.runtime.dirty());
    case.agent("team_1").await.expect("a Browser");
}

#[tokio::test]
async fn a_session_easi_run_cannot_start_leaves_no_slot_held() {
    let case = Case::new();
    case.fake.state().faults.insert("refuse-start");
    assert_eq!(
        case.agent("team_1").await.err(),
        Some(BrowserError::UNAVAILABLE)
    );
    case.fake.state().faults.remove("refuse-start");
    case.agent("team_1").await.expect("a Browser");
}

#[tokio::test(start_paused = true)]
async fn a_creation_whose_answer_was_lost_is_settled_before_anything_replaces_it() {
    let case = Case::new();
    case.runtime.startup().await;
    *case.runtime.creating.lock().expect("usable") = Some(Instant::now());
    case.runtime
        .dirty
        .store(true, std::sync::atomic::Ordering::SeqCst);
    // What the lost creation made appears while it settles, and goes with the cleanup.
    let fake = case.fake.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_secs(60)).await;
        fake.leave_session("late");
    });
    let started = Instant::now();
    case.runtime.clean().await.expect("cleaned");
    assert!(started.elapsed() >= super::CREATE_SETTLE_SECONDS);
    assert_eq!(case.fake.state().sessions, Vec::<String>::new());
}

#[tokio::test]
async fn a_team_s_teardown_removes_its_session_and_an_absent_one_is_success() {
    let case = Case::new();
    case.runtime.remove_team("team_1").await.expect("nothing");
    case.agent("team_1").await.expect("a Browser");
    case.runtime.remove_team("team_1").await.expect("removed");
    assert_eq!(case.fake.state().sessions, Vec::<String>::new());
}

#[tokio::test]
async fn a_lease_is_intact_only_while_easi_run_lists_it() {
    let case = Case::new();
    let lease = case.agent("team_1").await.expect("a Browser");
    assert!(case.runtime.intact(&lease).await);
    case.fake.state().sessions.clear();
    assert!(!case.runtime.intact(&lease).await);
}

#[tokio::test]
async fn a_task_s_downloads_read_through_their_ticket_with_their_exact_bytes() {
    let case = Case::new();
    let lease = case.agent("team_1").await.expect("a Browser");
    case.fake
        .state()
        .downloads
        .push(("edital.pdf".to_owned(), b"%PDF-1.4\n".to_vec()));
    let listing = case
        .runtime
        .files(&lease, None, None)
        .await
        .expect("a listing");
    assert_eq!(
        listing,
        strict_json::parse(
            br#"{"files":[{"name":"edital.pdf","size":9}],"refused":[],"skipped":0}"#
        )
        .expect("json")
    );
    assert_eq!(
        case.runtime
            .read(&lease, "edital.pdf", 9, None)
            .await
            .expect("the bytes"),
        b"%PDF-1.4\n"
    );
    assert_eq!(
        case.runtime.read(&lease, "edital.pdf", 8, None).await,
        Err(BrowserError::LOST)
    );
    assert_eq!(
        case.runtime.read(&lease, "other.pdf", 9, None).await,
        Err(BrowserError::LOST)
    );
}
