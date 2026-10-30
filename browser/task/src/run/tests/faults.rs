//! The deterministic fault matrix (plan B1 §5): a Team process killed at every crash point of a task, the Browser
//! container killed, the Brain killed mid-call, the egress restarted, the provider unreachable for a minute, and a
//! crash inside a multi-link read. Each task ends with its correct explicit outcome, no page action runs twice, and
//! the ledger stays consistent.

use std::path::Path;
use std::process::Command;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use holdpoint::{Event, Outcome as Ended};

use holdpoint::Clock as _;

use super::{Script, T0, World, recorded, value};
use crate::brain::{BrowserCall, CallFailure};
use crate::run::Status;
use crate::testing::Browser;

/// The links the jobs page offers and where each leads.
pub(super) fn jobs(browser: &mut Browser) {
    browser.links = vec![
        ("p1:4".to_owned(), "Job A".to_owned()),
        ("p1:5".to_owned(), "Job B".to_owned()),
    ];
    for (reference, url) in [
        ("p1:4", "https://jobs.example/a"),
        ("p1:5", "https://jobs.example/b"),
    ] {
        browser.clicks.insert(reference.to_owned(), url.to_owned());
    }
}

/// The agent's plan, by how many steps its thread already holds.
pub(super) fn plan(steps: usize) -> &'static str {
    match steps {
        0 => r#"{"kind": "navigate", "url": "https://example.com/jobs"}"#,
        1 => r#"{"kind": "type", "ref": "p1:0", "text": "rust"}"#,
        2 => r#"{"kind": "click", "ref": "p1:1"}"#,
        3 => r#"{"kind": "read", "refs": ["p1:4", "p1:5"]}"#,
        _ => r#"{"kind": "done", "summary": "Two jobs are open: Job A and Job B."}"#,
    }
}

/// The jobs task's Brain, failing the steps `fails` refuses.
pub(super) fn script(fails: impl Fn(usize, i64) -> bool + Send + Sync + 'static) -> Script {
    Box::new(move |call, request, now| match call {
        BrowserCall::Step if fails(recorded(request), now) => Err(CallFailure::Unavailable),
        BrowserCall::Step => Ok(value(plan(recorded(request)))),
        BrowserCall::Check => Ok(value(r#"{"complete": true, "missing": null}"#)),
        BrowserCall::Fold => Ok(value(r#"{"memory": "Kept."}"#)),
        BrowserCall::Final => Ok(value(r#"{"summary": "From the thread."}"#)),
    })
}

fn jobs_world(dir: &Path) -> World {
    World::open(dir, jobs, script(|_, _| false))
}

/// The page commands no task may repeat: each is the agent's own, once.
const ONCE: [&str; 4] = ["type p1:0 rust", "click p1:1", "click p1:4", "click p1:5"];

/// The task ended done with its answer, no page action ran twice, and the ledger holds one consistent ending.
fn ended_well(world: &World, run_id: &str) -> crate::run::Outcome {
    let outcome = world.outcome(run_id);
    assert_eq!(outcome.status, Status::Done, "{outcome:?}");
    assert_eq!(
        outcome.summary.as_deref(),
        Some("Two jobs are open: Job A and Job B.")
    );
    for effect in ONCE {
        assert!(
            world.count(effect) <= 1,
            "{effect} ran twice: {:?}",
            world.effects()
        );
    }
    let history = world.worker.history(run_id).expect("the history validates");
    let endings = history
        .iter()
        .filter(|event| matches!(event, Event::RunCompleted { .. }))
        .count();
    assert_eq!(endings, 1);
    assert!(matches!(
        history.last(),
        Some(Event::RunCompleted {
            outcome: Ended::Done { .. }
        })
    ));
    outcome
}

/// The outcomes the thread recorded for its steps, as the Brain last saw them.
fn recorded_outcomes(world: &World) -> Vec<String> {
    let calls = world.brain.calls();
    let Some((_, request)) = calls
        .iter()
        .rev()
        .find(|(call, _)| *call == BrowserCall::Step)
    else {
        return Vec::new();
    };
    match request.get("thread").and_then(|thread| thread.get("steps")) {
        Some(strict_json::Value::Array(steps)) => steps
            .iter()
            .map(|step| crate::run::as_str(step.get("outcome")).to_owned())
            .collect(),
        _ => Vec::new(),
    }
}

/// Runs this test's task in a child process that aborts at `point`, then resumes it here on the same ledger.
fn crashed_at(test: &str, point: &str) -> (tempfile::TempDir, World, String) {
    let dir = tempfile::tempdir().expect("directory");
    let status = Command::new(std::env::current_exe().expect("test binary"))
        .args([
            &format!("run::tests::faults::{test}"),
            "--exact",
            "--test-threads=1",
            "--nocapture",
        ])
        .env("TEAM_RUN_CHILD", dir.path())
        .env("TEAM_RUN_FAILPOINT", point)
        .status()
        .expect("child ran");
    assert!(!status.success(), "the child never reached {point}");
    let world = jobs_world(dir.path());
    let run_id = world.submit();
    (dir, world, run_id)
}

/// In the child: the task driven until the crash point aborts the process.
fn child() -> bool {
    let Ok(dir) = std::env::var("TEAM_RUN_CHILD") else {
        return false;
    };
    let world = jobs_world(Path::new(&dir));
    let run_id = world.submit();
    let _ = world.worker.drive(&run_id);
    // The crash point was never reached: the parent sees a clean exit and fails.
    std::process::exit(0);
}

macro_rules! crash {
    ($name:ident, $point:literal, |$world:ident, $run:ident, $outcome:ident| $check:block) => {
        #[test]
        fn $name() {
            if child() {
                return;
            }
            let (_dir, $world, $run) = crashed_at(stringify!($name), $point);
            let $outcome = ended_well(&$world, &$run);
            $check
        }
    };
}

crash!(
    team_killed_after_the_browser_opened,
    "open.after",
    |world, run, outcome| {
        assert!(ONCE.iter().all(|effect| world.count(effect) == 1));
        assert_eq!(outcome.recovered, 0);
        let _ = run;
    }
);

crash!(
    team_killed_between_two_committed_steps,
    "observe.before:2",
    |world, run, outcome| {
        // The Browser died with the process: the task acquired a new one and was told so.
        assert_eq!(outcome.recovered, 1);
        let shown = world.brain.calls();
        assert!(shown.iter().any(|(_, request)| {
            crate::run::as_str(request.get("observation")).starts_with(crate::run::REPLACED_NOTICE)
        }));
        let _ = run;
    }
);

crash!(
    team_killed_while_observing,
    "observe.after:1",
    |world, run, outcome| {
        assert!(ONCE.iter().all(|effect| world.count(effect) == 1));
        let _ = (run, outcome);
    }
);

crash!(
    team_killed_before_an_action_reached_the_page,
    "act.before:2",
    |world, run, outcome| {
        // The typing may have happened as far as the ledger knows: it is reported interrupted and never repeated.
        assert_eq!(world.count("type p1:0 rust"), 0);
        assert_eq!(recorded_outcomes(&world)[1], "interrupted");
        assert_eq!(outcome.recovered, 1);
        let _ = run;
    }
);

crash!(
    team_killed_after_an_action_reached_the_page,
    "act.after:2",
    |world, run, outcome| {
        assert_eq!(world.count("type p1:0 rust"), 1);
        assert_eq!(recorded_outcomes(&world)[1], "interrupted");
        let _ = (run, outcome);
    }
);

crash!(
    team_killed_after_a_click,
    "act.after:3",
    |world, run, outcome| {
        assert_eq!(world.count("click p1:1"), 1);
        assert_eq!(recorded_outcomes(&world)[2], "interrupted");
        let _ = (run, outcome);
    }
);

crash!(
    team_killed_before_the_brain_answered,
    "model.before:2",
    |world, run, outcome| {
        // The Brain is asked again; its step names a ref of the lost Browser, so it is refused, nothing is done on the
        // page, and the task goes on from a fresh observation of a new Browser.
        assert_eq!(world.count("type p1:0 rust"), 0);
        assert_eq!(recorded_outcomes(&world)[1], "refused");
        assert_eq!(outcome.recovered, 1);
        let _ = run;
    }
);

crash!(
    team_killed_after_the_brain_answered,
    "model.answered:3",
    |world, run, outcome| {
        // The answer was not committed: the Brain is asked again, and nothing ran twice.
        assert_eq!(world.count("click p1:1"), 0);
        assert_eq!(world.count("type p1:0 rust"), 1);
        assert_eq!(outcome.recovered, 1);
        let _ = run;
    }
);

crash!(
    team_killed_inside_a_multi_link_read,
    "read.seen:1",
    |world, run, outcome| {
        // The first link was opened, the second never: the read is interrupted and no link is opened again.
        assert_eq!(world.count("click p1:4"), 1);
        assert_eq!(world.count("click p1:5"), 0);
        assert_eq!(recorded_outcomes(&world)[3], "interrupted");
        let _ = (run, outcome);
    }
);

#[test]
fn a_browser_container_killed_mid_task_is_replaced() {
    let dir = tempfile::tempdir().expect("directory");
    let world = jobs_world(dir.path());
    let host = Arc::clone(&world.host);
    world.on_step(move |steps| {
        if steps == 2 {
            host.kill();
        }
    });
    let run_id = world.submit();
    let outcome = ended_well(&world, &run_id);
    assert_eq!(outcome.recovered, 1);
    assert_eq!(outcome.relaunched, 1);
    // The click found no Browser before it acted: refused, then the step after it is taken on the new Browser.
    assert_eq!(world.count("click p1:1"), 0);
    assert_eq!(recorded_outcomes(&world)[2], "refused");
}

#[test]
fn an_egress_restarted_mid_task_moves_the_task_to_a_fresh_browser() {
    let dir = tempfile::tempdir().expect("directory");
    let world = jobs_world(dir.path());
    let host = Arc::clone(&world.host);
    world.on_step(move |steps| {
        if steps == 1 {
            host.intact.store(false, Ordering::SeqCst);
        }
    });
    let run_id = world.submit();
    let outcome = ended_well(&world, &run_id);
    assert_eq!(outcome.recovered, 1);
    assert_eq!(world.count("type p1:0 rust"), 0);
    let opens = world.host.opens.lock().expect("opens").clone();
    assert!(
        opens
            .last()
            .is_some_and(|(_, recovered)| recovered.replaced)
    );
}

#[test]
fn a_brain_killed_mid_call_is_asked_again() {
    let dir = tempfile::tempdir().expect("directory");
    let failed = Arc::new(AtomicBool::new(false));
    let once = Arc::clone(&failed);
    let world = World::open(
        dir.path(),
        jobs,
        script(move |steps, _| steps == 2 && !once.swap(true, Ordering::SeqCst)),
    );
    let run_id = world.submit();
    let outcome = ended_well(&world, &run_id);
    assert!(failed.load(Ordering::SeqCst));
    assert!(ONCE.iter().all(|effect| world.count(effect) == 1));
    assert_eq!(outcome.recovered, 0);
}

#[test]
fn a_provider_unreachable_for_a_minute_delays_the_task_without_ending_it() {
    let dir = tempfile::tempdir().expect("directory");
    let world = World::open(dir.path(), jobs, script(|_, now| now < T0 + 60_000));
    let run_id = world.submit();
    ended_well(&world, &run_id);
    let first = world
        .brain
        .calls()
        .iter()
        .filter(|(call, request)| *call == BrowserCall::Step && recorded(request) == 0)
        .count();
    assert!(first >= 5, "the first step was asked {first} times");
    assert!(world.clock.now_ms() >= T0 + 60_000);
}
