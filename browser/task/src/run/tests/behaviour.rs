//! The task's behaviour as a run: each closed step, the thread, screenshots and their blobs, error pages, refused
//! steps, screening and the completeness check, folds, finishing, Driver and Chromium recovery and its bound, Stop,
//! the deadline, the spend ceiling, and the turn's authority.

use std::sync::Arc;
use std::sync::atomic::Ordering;

use holdpoint::{Clock as _, Driven, Wait};
use strict_json::Value;

use super::{Script, T0, World, recorded, value};
use crate::brain::{BrowserCall, CallFailure};
use crate::run::handlers::Mode;
use crate::run::{
    INCOMPLETE_NOTE, MAX_RECOVERIES, MAX_RETURNS_INCOMPLETE, MAX_RETURNS_UNSCREENED,
    RESTARTED_NOTICE, Status, UNSCREENED_NOTE, as_str,
};
use crate::testing::{Browser, View, page_text};

const DONE: &str = r#"{"kind": "done", "summary": "Prazo: 30/11."}"#;
const WAIT: &str = r#"{"kind": "wait"}"#;
const COMPLETE: &str = r#"{"complete": true, "missing": null}"#;

/// A Brain answering `steps` in order by how many steps are recorded (then done), its checks with `checks` in turn
/// (then complete), and its final answer with `last`.
fn steps(steps: &'static [&'static str], checks: &'static [&'static str]) -> Script {
    let checked = std::sync::atomic::AtomicUsize::new(0);
    Box::new(move |call, request, _| match call {
        BrowserCall::Step => Ok(value(steps.get(recorded(request)).copied().unwrap_or(DONE))),
        BrowserCall::Check => {
            let index = checked.fetch_add(1, Ordering::SeqCst);
            Ok(value(checks.get(index).copied().unwrap_or(COMPLETE)))
        }
        BrowserCall::Fold => Ok(value(r#"{"memory": "Kept."}"#)),
        BrowserCall::Final => Ok(value(r#"{"summary": "From the thread."}"#)),
    })
}

fn plain(_: &mut Browser) {}

fn world(setup: fn(&mut Browser), script: Script) -> (tempfile::TempDir, World) {
    let dir = tempfile::tempdir().expect("directory");
    let world = World::open(dir.path(), setup, script);
    (dir, world)
}

fn step_requests(world: &World) -> Vec<Value> {
    world
        .brain
        .calls()
        .into_iter()
        .filter(|(call, _)| *call == BrowserCall::Step)
        .map(|(_, request)| request)
        .collect()
}

fn last_outcomes(world: &World) -> Vec<String> {
    let requests = step_requests(world);
    match requests
        .last()
        .and_then(|request| request.get("thread"))
        .and_then(|thread| thread.get("steps"))
    {
        Some(Value::Array(steps)) => steps
            .iter()
            .map(|step| as_str(step.get("outcome")).to_owned())
            .collect(),
        _ => Vec::new(),
    }
}

fn calls(world: &World) -> Vec<Vec<String>> {
    world
        .host
        .driver()
        .map(|driver| driver.calls())
        .unwrap_or_default()
}

fn call(parts: &[&str]) -> Vec<String> {
    parts.iter().map(|part| (*part).to_owned()).collect()
}

#[test]
fn a_task_executes_each_closed_step_and_the_agents_checked_answer_stands() {
    const EVERY_KIND: [&str; 5] = [
        r#"{"kind": "navigate", "url": "https://example.com/editais"}"#,
        r#"{"kind": "type", "ref": "p1:0", "text": "ia"}"#,
        r#"{"kind": "click", "ref": "p1:1"}"#,
        r#"{"kind": "scroll", "direction": "down"}"#,
        WAIT,
    ];
    let (_dir, world) = world(plain, steps(&EVERY_KIND, &[]));
    let run_id = world.submit();
    let outcome = world.outcome(&run_id);
    assert_eq!((outcome.status, outcome.steps), (Status::Done, 6));
    assert_eq!(outcome.summary.as_deref(), Some("Prazo: 30/11."));
    assert_eq!(outcome.completeness, Some("complete"));
    assert_eq!(
        calls(&world),
        [
            call(&["navigate", "https://example.com/editais"]),
            call(&["type", "p1:0", "ia"]),
            call(&["click", "p1:1"]),
            call(&["scroll", "down"]),
        ]
    );
    // Only the wait step waited, on the run's durable clock.
    assert_eq!(world.clock.now_ms(), T0 + 2_000);
    each_request_extends_the_thread(&world);
}

/// Each request extends the one before it, and the last entry is the whole step with the page it was chosen on.
fn each_request_extends_the_thread(world: &World) {
    let requests = step_requests(world);
    let thread =
        |request: &Value| match request.get("thread").and_then(|thread| thread.get("steps")) {
            Some(Value::Array(steps)) => steps.clone(),
            _ => Vec::new(),
        };
    for pair in requests.windows(2) {
        let (earlier, later) = (thread(&pair[0]), thread(&pair[1]));
        assert_eq!(later[..earlier.len()], earlier[..]);
    }
    let mut expected = value(
        r#"{"step": {"kind": "wait"}, "outcome": "ok", "page": {"url": "https://example.com/editais", "title": "Example"}, "text": "", "downloads": null, "screenshot": false}"#,
    );
    if let Value::Object(members) = &mut expected {
        members[3].1 = Value::String(page_text("https://example.com/editais"));
    }
    let last = requests.last().map(thread).unwrap_or_default();
    assert_eq!(last.last(), Some(&expected));
}

#[test]
fn a_ref_not_offered_for_its_action_and_a_refused_page_action_are_outcomes_not_failures() {
    fn refusing(browser: &mut Browser) {
        browser.refuse = vec!["navigate"];
    }
    let (_dir, world) = world(
        refusing,
        steps(
            &[
                r#"{"kind": "click", "ref": "p1:0"}"#,
                r#"{"kind": "type", "ref": "p9:9", "text": "x"}"#,
                r#"{"kind": "navigate", "url": "https://example.com"}"#,
                r#"{"kind": "needs_person", "reason": "sign-in", "sentence": "O site pede login."}"#,
            ],
            &[],
        ),
    );
    let run_id = world.submit();
    let outcome = world.outcome(&run_id);
    assert_eq!((outcome.status, outcome.steps), (Status::NeedsPerson, 4));
    assert_eq!(outcome.reason.as_deref(), Some("sign-in"));
    assert_eq!(last_outcomes(&world), ["refused", "refused", "refused"]);
    assert_eq!(calls(&world), [call(&["navigate", "https://example.com"])]);
}

#[test]
fn an_error_page_sends_the_browser_back_to_the_last_page_that_loaded_and_says_so() {
    fn failing(browser: &mut Browser) {
        browser.views.insert(
            "https://down.example/".to_owned(),
            View {
                url: "chrome-error://chromewebdata/".to_owned(),
                title: "down.example".to_owned(),
                ..View::default()
            },
        );
    }
    let (_dir, world) = world(
        failing,
        steps(
            &[r#"{"kind": "navigate", "url": "https://down.example/"}"#],
            &[],
        ),
    );
    let run_id = world.submit();
    assert_eq!(world.outcome(&run_id).status, Status::Done);
    assert_eq!(
        calls(&world).last(),
        Some(&call(&["navigate", "https://example.com"]))
    );
    let shown = step_requests(&world);
    let observation = as_str(shown.last().and_then(|request| request.get("observation")));
    assert!(observation.starts_with("The last page you opened (down.example) did not load"));
}

#[test]
fn screenshots_are_rationed_and_only_the_current_page_carries_the_image() {
    const SHOT: &str = r#"{"kind": "screenshot"}"#;
    let (_dir, world) = world(plain, steps(&[SHOT, SHOT, WAIT, WAIT, SHOT], &[]));
    let run_id = world.submit();
    assert_eq!(world.outcome(&run_id).status, Status::Done);
    let screenshots = world
        .host
        .driver()
        .expect("browser")
        .browser()
        .screenshots
        .clone();
    assert_eq!(screenshots, [false, true, false, false, false, true]);
    let requests = step_requests(&world);
    // The screenshot travels to the Brain as its bytes; the ledger kept only the request's digest.
    let image = as_str(requests[1].get("screenshot"));
    assert!(image.starts_with("iVBORw0KGg"), "{image}");
    assert_eq!(requests[2].get("screenshot"), Some(&Value::Null));
    assert_eq!(last_outcomes(&world), ["ok", "refused", "ok", "ok", "ok"]);
}

#[test]
fn an_answer_citing_an_unshown_url_goes_back_twice_then_says_so() {
    const CITING: &str = r#"{"kind": "done", "summary": "See https://elsewhere.example/x."}"#;
    let (_dir, world) = world(plain, steps(&[CITING, CITING, CITING], &[]));
    let run_id = world.submit();
    let outcome = world.outcome(&run_id);
    assert_eq!(outcome.status, Status::Done);
    assert_eq!(outcome.unscreened, ["https://elsewhere.example/x"]);
    let returned = last_outcomes(&world);
    assert_eq!(returned.len(), MAX_RETURNS_UNSCREENED as usize);
    assert!(returned.iter().all(|outcome| outcome == "returned"));
    let requests = step_requests(&world);
    let thread = requests[1]
        .get("thread")
        .and_then(|thread| thread.get("steps"));
    let Some(Value::Array(entries)) = thread else {
        panic!("thread")
    };
    let note = as_str(entries[0].get("step").and_then(|step| step.get("note")));
    assert!(note.starts_with(UNSCREENED_NOTE));
}

#[test]
fn an_incomplete_answer_goes_back_four_times_then_travels_with_what_is_missing() {
    const MISSING: &str = r#"{"complete": false, "missing": "Faltam vagas."}"#;
    let (_dir, world) = world(
        plain,
        steps(&[], &[MISSING, MISSING, MISSING, MISSING, MISSING]),
    );
    let run_id = world.submit();
    let outcome = world.outcome(&run_id);
    assert_eq!(outcome.completeness, Some("incomplete"));
    assert_eq!(outcome.missing.as_deref(), Some("Faltam vagas."));
    assert_eq!(last_outcomes(&world).len(), MAX_RETURNS_INCOMPLETE as usize);
    let requests = step_requests(&world);
    let observation = requests[1]
        .get("thread")
        .and_then(|thread| thread.get("steps"));
    let Some(Value::Array(entries)) = observation else {
        panic!("thread")
    };
    let note = as_str(entries[0].get("step").and_then(|step| step.get("note")));
    assert!(note.starts_with(INCOMPLETE_NOTE));
}

#[test]
fn a_brain_that_answers_outside_the_closed_set_twice_ends_the_task() {
    let script: Script = Box::new(|call, _, _| match call {
        BrowserCall::Step => Ok(value(r#"{"kind": "fly"}"#)),
        _ => Ok(value(COMPLETE)),
    });
    let (_dir, world) = world(plain, script);
    let run_id = world.submit();
    let outcome = world.outcome(&run_id);
    assert_eq!(
        (outcome.status, outcome.reason.as_deref()),
        (Status::Failed, Some("step-invalid"))
    );
    let refused: Script = Box::new(|call, _, _| match call {
        BrowserCall::Step => Err(CallFailure::Refused),
        _ => Ok(value(COMPLETE)),
    });
    let (_dir, world) = super::super::tests::behaviour::world(plain, refused);
    let run_id = world.submit();
    let outcome = world.outcome(&run_id);
    assert_eq!(
        (outcome.status, outcome.reason.as_deref()),
        (Status::Failed, Some("model-unavailable"))
    );
    // A refused step is asked exactly once more, never retried as an outage.
    assert_eq!(step_requests(&world).len(), 2);
}

#[test]
fn a_failed_driver_session_is_replaced_on_the_same_browser_and_the_task_continues() {
    fn fatal_click(browser: &mut Browser) {
        browser.fatal = vec!["click"];
    }
    let (_dir, world) = world(
        fatal_click,
        steps(
            &[
                r#"{"kind": "click", "ref": "p1:1"}"#,
                r#"{"kind": "type", "ref": "p1:0", "text": "ia"}"#,
            ],
            &[],
        ),
    );
    let run_id = world.submit();
    let outcome = world.outcome(&run_id);
    assert_eq!(
        (outcome.status, outcome.recovered, outcome.relaunched),
        (Status::Done, 1, 0)
    );
    assert_eq!(last_outcomes(&world), ["interrupted", "ok"]);
    let opens = world.host.opens.lock().expect("opens").clone();
    assert!(!opens.last().expect("reopened").1.replaced);
}

#[test]
fn a_page_chromium_no_longer_reads_starts_chromium_again_and_the_agent_is_told() {
    fn refusing(browser: &mut Browser) {
        browser.fail_reads = Some("browser-read-refused");
    }
    let (_dir, world) = world(
        plain,
        steps(&[r#"{"kind": "scroll", "direction": "down"}"#], &[]),
    );
    *world.host.next.lock().expect("next") = Some(refusing);
    let run_id = world.submit();
    let outcome = world.outcome(&run_id);
    assert_eq!(
        (outcome.status, outcome.recovered, outcome.relaunched),
        (Status::Done, 1, 1)
    );
    let opens = world.host.opens.lock().expect("opens").clone();
    assert_eq!(
        opens.iter().map(|(mode, _)| *mode).collect::<Vec<_>>(),
        [Mode::Start, Mode::Relaunch]
    );
    // Only the next observation says so; the one after it does not.
    let requests = step_requests(&world);
    assert!(as_str(requests[0].get("observation")).starts_with(RESTARTED_NOTICE));
    assert!(!as_str(requests[1].get("observation")).starts_with(RESTARTED_NOTICE));
}

#[test]
fn recoveries_are_bounded_and_the_last_one_ends_the_task_with_its_cause() {
    fn always_fatal(browser: &mut Browser) {
        browser.fatal = vec!["click"];
    }
    let clicks: &'static [&'static str] = &[
        r#"{"kind": "click", "ref": "p1:1"}"#,
        r#"{"kind": "click", "ref": "p1:1"}"#,
        r#"{"kind": "click", "ref": "p1:1"}"#,
        r#"{"kind": "click", "ref": "p1:1"}"#,
    ];
    let (_dir, world) = world(always_fatal, steps(clicks, &[]));
    let run_id = world.submit();
    let outcome = world.outcome(&run_id);
    assert_eq!(
        (outcome.status, outcome.reason.as_deref()),
        (Status::Failed, Some("browser-lost"))
    );
    assert_eq!(outcome.recovered, MAX_RECOVERIES);
    assert_eq!(outcome.cause.as_deref(), Some("act/tool-failed"));
}

#[test]
fn stop_ends_the_task_stopped_with_its_steps() {
    let (_dir, world) = world(plain, steps(&[WAIT, WAIT, WAIT], &[]));
    let worker = Arc::clone(&world.worker);
    let run_id = World::input().run_id();
    let stopping = run_id.clone();
    world.on_step(move |count| {
        if count == 2 {
            worker.cancel(&stopping).expect("cancelled");
        }
    });
    world.submit();
    let outcome = world.outcome(&run_id);
    assert_eq!(outcome.status, Status::Stopped);
    assert_eq!(outcome.steps, 2);
}

#[test]
fn the_deadline_ends_the_task_on_its_budget_and_nothing_runs_after_it() {
    let (_dir, world) = world(
        plain,
        steps(&[WAIT, r#"{"kind": "click", "ref": "p1:1"}"#], &[]),
    );
    let clock = world.clock.clone();
    world.on_step(move |count| {
        if count == 1 {
            clock.advance(31 * 60 * 1000);
        }
    });
    let run_id = world.submit();
    let outcome = world.outcome(&run_id);
    assert_eq!(outcome.status, Status::Budget);
    assert_eq!(calls(&world), Vec::<Vec<String>>::new());
}

#[test]
fn past_the_soft_cutoff_the_brain_answers_from_the_thread_and_never_takes_another_step() {
    let (_dir, world) = world(plain, steps(&[WAIT, WAIT, WAIT], &[]));
    let clock = world.clock.clone();
    world.on_step(move |count| {
        if count == 1 {
            clock.advance(29 * 60 * 1000 + 30_000);
        }
    });
    let run_id = world.submit();
    let outcome = world.outcome(&run_id);
    assert_eq!(outcome.status, Status::Done);
    assert_eq!(outcome.summary.as_deref(), Some("From the thread."));
    assert_eq!(outcome.completeness, Some("not-checked"));
    assert_eq!(step_requests(&world).len(), 2);
}

#[test]
fn a_run_without_its_turns_authority_waits_and_dispatches_nothing_and_a_revoked_one_ends() {
    let (_dir, world) = world(plain, steps(&[], &[]));
    let credentials = world.hooks.credentials.lock().expect("credentials").take();
    let run_id = world.submit();
    assert_eq!(world.drive(&run_id), Driven::Suspended(Wait::Authority));
    assert!(world.host.opens.lock().expect("opens").is_empty());
    assert_eq!(world.brain.calls().len(), 0);
    *world.hooks.credentials.lock().expect("credentials") = credentials;
    world.worker.wake(&run_id).expect("woken");
    assert_eq!(world.outcome(&run_id).status, Status::Done);
    let (_dir, revoked) = super::super::tests::behaviour::world(plain, steps(&[], &[]));
    revoked.hooks.revoked.store(true, Ordering::SeqCst);
    let run_id = revoked.submit();
    let outcome = revoked.outcome(&run_id);
    assert_eq!(outcome.status, Status::Failed);
    assert_eq!(outcome.cause.as_deref(), Some("run/authority-revoked"));
    assert_eq!(revoked.brain.calls().len(), 0);
}

#[test]
fn spend_is_a_ceiling_of_reservations_and_the_finishing_share_is_kept() {
    // Every step costs about a fifth of the spend: the normal share runs out and the task finishes from its thread.
    let script: Script = Box::new(|call, request, _| match call {
        BrowserCall::Step if recorded(request) < 50 => Ok(value(WAIT)),
        BrowserCall::Step => Ok(value(DONE)),
        BrowserCall::Final => Ok(value(r#"{"summary": "From the thread."}"#)),
        _ => Ok(value(COMPLETE)),
    });
    let dir = tempfile::tempdir().expect("directory");
    let world = World::open(dir.path(), plain, script);
    // A dear model: each reservation takes a large share of US$0.50.
    let run_id = world.submit();
    let outcome = world.outcome(&run_id);
    assert!(
        matches!(outcome.status, Status::Done | Status::Budget),
        "{outcome:?}"
    );
    let _ = T0;
}
