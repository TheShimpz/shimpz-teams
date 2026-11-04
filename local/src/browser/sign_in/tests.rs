use std::sync::{Arc, Mutex};
use std::time::{Duration, UNIX_EPOCH};

use strict_json::Value;
use team_core::entropy::OsEntropy;
use team_core::future::BoxFuture;
use team_core::testing::{FakeClock, MemoryAudit};
use team_protocol::sign_in::{NOT_RUNNING, UNSAVED, URL_INVALID};

use super::{
    IDLE_SECONDS, Incarnations, SessionError, SessionParts, SignInSessions, TEAM_NOT_FOUND,
    UNAVAILABLE,
};
use crate::browser::audit::Actor;
use crate::browser::fake::Fake;
use crate::browser::service::{BrowserService, ServiceParts, WebFiles};
use crate::browser::sign_ins::SignInStore;
use crate::browser::sign_ins::tests::Scratch;
use crate::http::service::Principal;
use team_browser::ingest::PutRefused;

const TEAM: &str = "neo";
const URL: &str = "https://www.linkedin.com/login";
const ORIGIN: &str = "https://admin.example";

fn incarnation() -> String {
    "a".repeat(64)
}

/// The Team's incarnation, as a test sets it.
pub(crate) struct Incarnation(pub(crate) Mutex<Option<String>>);

impl Incarnations for Incarnation {
    fn current<'a>(&'a self, _team_id: &'a str) -> BoxFuture<'a, Option<String>> {
        Box::pin(async move { self.0.lock().expect("usable").clone() })
    }
}

struct NoFiles;

impl WebFiles for NoFiles {
    fn put<'a>(
        &'a self,
        _: &'a str,
        _: &'a str,
        _: Vec<u8>,
    ) -> BoxFuture<'a, Result<Value, PutRefused>> {
        Box::pin(async { Err(PutRefused::Unavailable) })
    }
}

pub(crate) struct Case {
    pub(crate) fake: Fake,
    _scratch: Scratch,
    pub(crate) store: Arc<SignInStore>,
    pub(crate) sessions: Arc<SignInSessions>,
    pub(crate) audit: Arc<MemoryAudit>,
    pub(crate) incarnation: Arc<Incarnation>,
    pub(crate) actor: Actor,
}

impl Case {
    pub(crate) async fn new() -> Self {
        let fake = Fake::default();
        let scratch = Scratch::new();
        let clock = Arc::new(FakeClock::at(UNIX_EPOCH + Duration::from_hours(500_000)));
        let store = Arc::new(
            SignInStore::new(
                &scratch.0.join("state"),
                &scratch.0.join("key/aes256.key"),
                clock,
                Arc::new(OsEntropy),
            )
            .expect("a store"),
        );
        let audit = Arc::new(MemoryAudit::default());
        let service = Arc::new(
            BrowserService::new(ServiceParts {
                runtime: Arc::new(fake.runtime(&scratch.0)),
                files: Arc::new(NoFiles),
                audit: audit.clone(),
                entropy: Arc::new(OsEntropy),
                runs: scratch.0.join("browser-runs"),
            })
            .expect("the service"),
        );
        let incarnation = Arc::new(Incarnation(Mutex::new(Some(incarnation()))));
        let sessions = Arc::new(SignInSessions::new(SessionParts {
            service,
            store: store.clone(),
            incarnations: incarnation.clone(),
            audit: audit.clone(),
            entropy: Arc::new(OsEntropy),
        }));
        let case = Self {
            fake,
            _scratch: scratch,
            store,
            sessions,
            audit,
            incarnation,
            actor: crate::browser::audit::request_actor(&Principal::Machine, &"a".repeat(32)),
        };
        case.sessions.startup().await;
        case
    }

    async fn start(&self) -> Result<Value, SessionError> {
        self.sessions
            .start(TEAM, &self.actor, &text(URL), ORIGIN)
            .await
    }

    async fn save(&self, started: &Value) -> Result<Value, SessionError> {
        self.sessions
            .save(TEAM, &self.actor, &id(started), ORIGIN)
            .await
    }

    /// Every activity event: its operation and phase.
    fn activity(&self) -> Vec<(String, String)> {
        self.audit
            .events()
            .iter()
            .filter_map(|event| {
                let fields = event.fields();
                let operation = fields.iter().find(|(name, _)| name == "operation")?;
                let activity = fields.iter().find(|(name, _)| name == "activity")?;
                match (&operation.1, activity.1.get("phase")) {
                    (Value::String(operation), Some(Value::String(phase))) => {
                        Some((operation.clone(), phase.clone()))
                    }
                    _ => None,
                }
            })
            .collect()
    }

    fn has(&self, operation: &str, phase: &str) -> bool {
        self.activity()
            .contains(&(operation.to_owned(), phase.to_owned()))
    }
}

/// Answers every Driver session the Browser opens, with the window count the test sets, and scripts the Browser's
/// clean stop and its profile's export.
fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn id(started: &Value) -> String {
    match started.get("sign_in_session_id") {
        Some(Value::String(id)) => id.clone(),
        _ => panic!("a session id"),
    }
}

#[tokio::test(start_paused = true)]
async fn a_session_runs_a_person_s_browser_through_a_view_no_one_can_watch() {
    let case = Case::new().await;
    let started = case.start().await.expect("started");
    assert!(team_protocol::sign_in::canonical_started(&started).is_some());
    assert_eq!(started.get("site"), Some(&text("www.linkedin.com")));
    let body = case.fake.state().bodies[0].clone();
    let body = strict_json::parse(body.as_bytes()).expect("json");
    assert_eq!(body.get("url"), Some(&text(URL)));
    assert_eq!(body.get("devtools"), Some(&Value::Bool(false)));
    // Its view is private: no watcher opens it.
    assert!(case.sessions.service.open_view(TEAM).await.is_err());
    assert!(case.has("browser-sign-in-start", "requested"));
    assert!(case.has("browser-sign-in-start", "completed"));
    // A second start for the Team is refused while the Browser is held.
    assert!(case.start().await.is_err());
}

#[tokio::test(start_paused = true)]
async fn saving_keeps_the_profile_as_a_sign_in_and_releases_the_browser() {
    let case = Case::new().await;
    let started = case.start().await.expect("started");
    let saved = case.save(&started).await.expect("saved");
    assert!(team_protocol::sign_in::canonical_saved(&saved).is_some());
    assert_eq!(saved.get("recorded"), Some(&Value::Bool(true)));
    let current = case
        .sessions
        .current(TEAM)
        .await
        .expect("readable")
        .expect("kept");
    assert_eq!(current.get("state"), Some(&text("ready")));
    assert_eq!(case.fake.state().sessions, Vec::<String>::new());
    assert!(case.has("browser-sign-in-save", "completed"));
    // The session is gone: a second Save or a Cancel finds none.
    assert_eq!(case.save(&started).await, Err(SessionError(NOT_RUNNING)));
    // The next session starts on the saved profile.
    let profile = case.fake.state().profiles[0].id.clone();
    let next = case.start().await.expect("started");
    assert!(case.fake.state().bodies[1].contains(&profile));
    case.sessions
        .cancel(TEAM, &case.actor, &id(&next), ORIGIN, false, "cancelled")
        .await
        .expect("cancelled");
    let current = case
        .sessions
        .current(TEAM)
        .await
        .expect("readable")
        .expect("kept");
    assert_eq!(current.get("state"), Some(&text("needs-person")));
}

#[tokio::test(start_paused = true)]
async fn cancel_releases_the_browser_and_a_first_session_keeps_nothing() {
    let case = Case::new().await;
    let started = case.start().await.expect("started");
    case.sessions
        .cancel(TEAM, &case.actor, &id(&started), ORIGIN, false, "cancelled")
        .await
        .expect("cancelled");
    assert_eq!(case.fake.state().sessions, Vec::<String>::new());
    assert_eq!(case.sessions.current(TEAM).await, Ok(None));
    assert!(case.has("browser-sign-in-cancel", "completed"));
    assert_eq!(
        case.sessions
            .cancel(TEAM, &case.actor, &id(&started), ORIGIN, false, "cancelled")
            .await,
        Err(SessionError(NOT_RUNNING))
    );
}

#[tokio::test(start_paused = true)]
async fn nothing_new_is_kept_when_easi_run_cannot_vouch_for_the_save() {
    for problem in ["save-not-durable", "save-failed", "save-unconfirmed"] {
        let case = Case::new().await;
        case.fake.state().save_problem = Some(problem);
        let started = case.start().await.expect("started");
        assert_eq!(
            case.save(&started).await,
            Err(SessionError(UNSAVED)),
            "{problem}"
        );
        assert_eq!(case.fake.state().sessions, Vec::<String>::new());
        assert_eq!(case.sessions.current(TEAM).await, Ok(None));
    }
}

#[tokio::test(start_paused = true)]
async fn nothing_is_kept_for_a_team_that_changed_while_saving() {
    let case = Case::new().await;
    let started = case.start().await.expect("started");
    *case.incarnation.0.lock().expect("usable") = Some("b".repeat(64));
    assert_eq!(case.save(&started).await, Err(SessionError(UNSAVED)));
    assert_eq!(case.fake.state().sessions, Vec::<String>::new());
    assert_eq!(case.sessions.current(TEAM).await, Ok(None));
}

#[tokio::test(start_paused = true)]
async fn a_deletion_is_pending_until_easi_run_proves_the_profile_gone() {
    let case = Case::new().await;
    let started = case.start().await.expect("started");
    let saved = case.save(&started).await.expect("saved");
    let sign_in = saved.get("sign_in").expect("a Sign-in");
    let confirmed = Value::Object(vec![
        (
            "sign_in_id".to_owned(),
            sign_in.get("sign_in_id").expect("an id").clone(),
        ),
        (
            "generation".to_owned(),
            Value::Number(strict_json::Number::Int(1)),
        ),
        ("team_incarnation".to_owned(), text(&incarnation())),
        ("factor".to_owned(), text("totp")),
        ("origin".to_owned(), text(ORIGIN)),
    ]);
    case.fake.state().faults.insert("profile-residue");
    assert_eq!(
        case.sessions.delete(TEAM, &case.actor, &confirmed).await,
        Ok("pending")
    );
    assert_eq!(case.fake.state().profiles.len(), 1);
    assert!(!case.has("browser-sign-in-delete", "completed"));
    case.fake.state().faults.remove("profile-residue");
    tokio::time::sleep(super::RECOVER_SECONDS + Duration::from_secs(1)).await;
    assert_eq!(case.fake.state().profiles.len(), 0);
    assert!(case.has("browser-sign-in-delete", "completed"));
}

#[tokio::test(start_paused = true)]
async fn a_profile_no_sign_in_names_is_removed_as_residue() {
    let case = Case::new().await;
    let started = case.start().await.expect("started");
    case.sessions
        .cancel(TEAM, &case.actor, &id(&started), ORIGIN, false, "cancelled")
        .await
        .expect("cancelled");
    assert_eq!(case.fake.state().profiles.len(), 0);
}

#[tokio::test(start_paused = true)]
async fn only_an_admissible_url_and_an_existing_team_start_a_session() {
    let case = Case::new().await;
    assert_eq!(
        case.sessions
            .start(TEAM, &case.actor, &text("http://example.com"), ORIGIN)
            .await,
        Err(SessionError(URL_INVALID))
    );
    *case.incarnation.0.lock().expect("usable") = None;
    assert_eq!(case.start().await, Err(SessionError(TEAM_NOT_FOUND)));
    assert_eq!(case.fake.state().started, 0);
}

#[tokio::test(start_paused = true)]
async fn nothing_delegating_starts_unless_its_activity_is_recorded_first() {
    let case = Case::new().await;
    case.audit.fail(true);
    assert_eq!(case.start().await, Err(SessionError(UNAVAILABLE)));
    assert_eq!(case.fake.state().started, 0);
    case.audit.fail(false);
    let started = case.start().await.expect("started");
    case.audit.fail(true);
    assert_eq!(case.save(&started).await, Err(SessionError(UNAVAILABLE)));
    // A Cancel protects the Sign-in: an audit log that cannot record it never stops it.
    case.sessions
        .cancel(TEAM, &case.actor, &id(&started), ORIGIN, false, "cancelled")
        .await
        .expect("cancelled");
    assert_eq!(case.fake.state().sessions, Vec::<String>::new());
}

#[tokio::test(start_paused = true)]
async fn deletion_takes_the_exact_confirmed_sign_in_and_ends_a_session_using_it() {
    let case = Case::new().await;
    let started = case.start().await.expect("started");
    let saved = case.save(&started).await.expect("saved");
    let sign_in = saved.get("sign_in").expect("a Sign-in");
    let confirmed = |generation: i64| {
        Value::Object(vec![
            (
                "sign_in_id".to_owned(),
                sign_in.get("sign_in_id").expect("an id").clone(),
            ),
            (
                "generation".to_owned(),
                Value::Number(strict_json::Number::Int(generation)),
            ),
            ("team_incarnation".to_owned(), text(&incarnation())),
            ("factor".to_owned(), text("totp")),
            ("origin".to_owned(), text(ORIGIN)),
        ])
    };
    assert_eq!(
        case.sessions.delete(TEAM, &case.actor, &confirmed(2)).await,
        Err(SessionError(team_protocol::sign_in::CHANGED))
    );
    let running = case.start().await.expect("started");
    assert_eq!(
        case.sessions.delete(TEAM, &case.actor, &confirmed(1)).await,
        Ok("deleted")
    );
    assert_eq!(case.sessions.current(TEAM).await, Ok(None));
    assert_eq!(case.fake.state().sessions, Vec::<String>::new());
    assert!(case.has("browser-sign-in-delete", "completed"));
    assert_eq!(case.save(&running).await, Err(SessionError(NOT_RUNNING)));
}

#[tokio::test(start_paused = true)]
async fn a_session_ends_at_its_limit_or_left_alone() {
    let case = Case::new().await;
    case.start().await.expect("started");
    tokio::time::sleep(IDLE_SECONDS + Duration::from_secs(10)).await;
    assert_eq!(case.fake.state().sessions, Vec::<String>::new());
    assert!(case.has("browser-sign-in-cancel", "completed"));
}

#[tokio::test(start_paused = true)]
async fn the_team_s_end_and_a_reset_end_its_session_keeping_nothing() {
    let case = Case::new().await;
    let started = case.start().await.expect("started");
    case.sessions.end_team(TEAM).await;
    assert_eq!(case.fake.state().sessions, Vec::<String>::new());
    assert_eq!(case.save(&started).await, Err(SessionError(NOT_RUNNING)));
    let started = case.start().await.expect("started");
    case.sessions.end_all().await;
    assert_eq!(case.fake.state().sessions, Vec::<String>::new());
    assert_eq!(case.save(&started).await, Err(SessionError(NOT_RUNNING)));
}

#[tokio::test(start_paused = true)]
async fn a_restarted_team_removes_earlier_browsers_and_completes_pending_deletions() {
    let case = Case::new().await;
    let started = case.start().await.expect("started");
    let saved = case.save(&started).await.expect("saved");
    let sign_in = saved.get("sign_in").expect("a Sign-in");
    let Some(Value::String(sign_in_id)) = sign_in.get("sign_in_id") else {
        panic!("an id")
    };
    let deleting = crate::browser::sign_ins::Deleting {
        event_id: "e".repeat(32),
        initiator_id: String::new(),
        factor: "totp".to_owned(),
        origin: ORIGIN.to_owned(),
    };
    let inc = incarnation();
    case.store
        .begin_delete(TEAM, (sign_in_id, 1, &inc), deleting)
        .expect("fenced");
    // A Browser a previous process left behind.
    let runtime = case.sessions.service.runtime();
    let left = runtime
        .acquire(crate::browser::runtime::Acquire {
            team_id: "other",
            mode: crate::browser::runtime::Mode::Agent,
            profile: None,
            deadline: None,
            stop: None,
        })
        .await
        .expect("acquired");
    runtime.release(&left).await.expect("released");
    case.sessions.startup().await;
    assert_eq!(case.store.deletions(), Ok(Vec::new()));
    assert!(case.has("browser-sign-in-delete", "completed"));
}
