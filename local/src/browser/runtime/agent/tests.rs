use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use team_browser::cua::CuaSession;
use tokio::time::Instant;

use super::AgentCalls;
use crate::browser::fake::Fake;
use crate::browser::runtime::{Acquire, BrowserRuntime, Mode};

async fn opened(fake: &Fake, state: &std::path::Path) -> (Arc<BrowserRuntime>, String) {
    let runtime = Arc::new(fake.runtime(state));
    let lease = runtime
        .acquire(Acquire {
            team_id: "team_1",
            mode: Mode::Agent,
            profile: None,
            deadline: None,
            stop: None,
        })
        .await
        .expect("a Browser");
    (runtime, lease.session_id)
}

#[tokio::test]
async fn a_cua_session_opens_and_observes_through_the_session_s_agent_endpoint() {
    let state = tempfile::tempdir().expect("a directory");
    let fake = Fake::default();
    let (runtime, session_id) = opened(&fake, state.path()).await;
    let uncertain = Arc::new(AtomicBool::new(false));
    let mut session = CuaSession::new(Box::new(AgentCalls::new(
        runtime,
        session_id,
        Arc::clone(&uncertain),
    )));
    session.set_deadline(Some(Instant::now() + Duration::from_secs(10)));
    session.open().await.expect("opened");
    let observed = session.observe(false).await.expect("observed");
    assert_eq!(observed.title, "Example");
    session
        .navigate("https://example.com/")
        .await
        .expect("navigated");
    let tools = fake.state().tools.clone();
    assert_eq!(tools.first().map(String::as_str), Some("list_windows"));
    assert_eq!(tools.last().map(String::as_str), Some("browser_navigate"));
    assert!(!uncertain.load(Ordering::SeqCst));
}

#[tokio::test]
async fn a_call_with_no_answer_leaves_the_session_uncertain() {
    let state = tempfile::tempdir().expect("a directory");
    let fake = Fake::default();
    let (runtime, session_id) = opened(&fake, state.path()).await;
    fake.state().lose_call_after = Some(0);
    let uncertain = Arc::new(AtomicBool::new(false));
    let mut session = CuaSession::new(Box::new(AgentCalls::new(
        runtime,
        session_id,
        Arc::clone(&uncertain),
    )));
    session.set_deadline(Some(Instant::now() + Duration::from_secs(10)));
    assert!(session.open().await.is_err());
    assert!(uncertain.load(Ordering::SeqCst));
}
