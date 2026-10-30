//! The Local controller suite (ADR-0123, plan phase R7, workstream W9): the built Team image driven over its HTTP
//! surface with signed Supervisor assertions, against a real Docker daemon.
//!
//! It checks the image's own contract, then runs the controller from the Team fixture image (`shimpz-team-fixture`,
//! which replaces only publication resolution and artifact verification with the publication the suite seeds) under
//! the Local Compose envelope, beside the real egress image, a loopback registry, and a fake Brain. Everything it
//! creates is named by its run and removed after it, proved gone. It prints one `ok` line per check and ends with
//! `ALL <n> CHECKS PASS`; the first check that fails ends it.
//!
//! Environment: `SHIMPZ_TEAM_LOCAL_TEST_IMAGE`, `SHIMPZ_TEAM_FIXTURE_TEST_IMAGE`, and
//! `SHIMPZ_ASSISTANT_EGRESS_TEST_IMAGE` name the locally built images; the Docker CLI reaches the daemon.

mod api;
mod assistant;
mod brain;
mod docker;
mod flow;
mod http;
mod image;
mod json;
mod prepare;
mod probe;
mod report;
mod resolution;
mod setup;
mod snapshots;
mod storage;
mod tar;
mod teardown;

use std::process::ExitCode;

use crate::api::{Body, Supervisor, anonymous};
use crate::brain::Brain;
use crate::flow::{Images, Names};
use crate::probe::Mount;
use crate::report::{Outcome, Report};
use crate::setup::{Daemon, Run, Started};

fn main() -> ExitCode {
    let mut report = Report::default();
    match suite(&mut report) {
        Ok(()) => {
            println!("ALL {} CHECKS PASS", report.passed());
            ExitCode::SUCCESS
        }
        Err(failure) => {
            println!("FAILED after {} checks: {failure}", report.passed());
            ExitCode::FAILURE
        }
    }
}

fn suite(report: &mut Report) -> Outcome {
    let images = Images::from_environment()?;
    image::configuration(&images.team, report)?;
    image::filesystem(&images.team, report)?;
    probe::pull()?;
    let names = Names::fresh()?;
    let mut trusted = None;
    let outcome = controller(&names, &images, &mut trusted, report);
    let cleaned = teardown::cleanup(&names, trusted.as_deref(), report);
    outcome.and(cleaned)
}

fn controller(
    names: &Names,
    images: &Images,
    trusted: &mut Option<String>,
    report: &mut Report,
) -> Outcome {
    let daemon = Daemon::observe()?;
    let supervisor = Supervisor::generate()?;
    let mut brain = Brain::start(daemon.gateway)?;
    *trusted = Some(setup::push_assistant(names, &daemon, report)?);
    let run = Run {
        names,
        images,
        daemon: &daemon,
        brain: &brain,
        supervisor: &supervisor,
        trusted: trusted.as_deref().unwrap_or_default(),
    };
    let outcome = lifecycle(&run, report);
    brain.stop();
    outcome
}

fn lifecycle(run: &Run<'_>, report: &mut Report) -> Outcome {
    let started = setup::start_controller(run, report)?;
    started_contract(run, &started, report)?;
    let api = setup::api(&started, run.supervisor);
    let scratch =
        std::env::temp_dir().join(format!("shimpz-team-suite-{}-snapshots", run.names.unique));
    snapshots::inventory_fast_path(&api, &scratch, report)?;
    storage::create_and_rename(&api, report)?;
    let restarted = storage::upload_and_restart(&api, run.names, report)?;
    let api = setup::api(&restarted, run.supervisor);
    storage::lost_network(&api, run.names, report)?;
    prepare::hostile_corpus(&api, run.names, report)?;
    let installed = assistant::install(&api, run.names, run.trusted, report)?;
    assistant::isolation(&installed, run.names, &run.daemon.cpuset, report)?;
    assistant::recovery(&api, &installed, report)?;
    assistant::integration(&api, report)?;
    teardown::egress_policy(run.names, report)?;
    teardown::uninstall(&api, run.names, report)?;
    teardown::destroy(&api, run.names, report)?;
    report.check(
        "the destroy deleted the Team's Brain threads through the Brain",
        run.brain.deletions() > 0,
        run.brain.deletions(),
    )?;
    teardown::reset(&api, run.names, &restarted.token, report)?;
    teardown::secrets(run.names, report)?;
    teardown::leave_for_cleanup(&api, run.names, report)
}

/// The started controller keeps its journal private, refuses a request without its bearer, and starts with an empty
/// catalog.
fn started_contract(run: &Run<'_>, started: &Started, report: &mut Report) -> Outcome {
    let journal = run.names.volume("action-journal");
    let metadata = probe::metadata(
        &[Mount {
            volume: &journal,
            target: "/journal",
            writable: false,
        }],
        "/journal/journal.sqlite3",
    )?;
    report.check(
        "the Action journal is 0600, owned by Team, singly linked",
        metadata.starts_with("600 10001 10001 1 "),
        &metadata,
    )?;
    let refused = anonymous(started.port, "GET", "/v1/assistants")?;
    report.equal(
        "a request without the bearer is refused with the Bearer challenge, uncached and unsniffed",
        (
            refused.status,
            refused.header("www-authenticate"),
            refused.header("cache-control"),
            refused.header("x-content-type-options"),
        ),
        (
            401,
            Some("Bearer realm=\"shimpz-local\""),
            Some("no-store"),
            Some("nosniff"),
        ),
    )?;
    let catalog = setup::api(started, run.supervisor).ok("GET", "/v1/assistants", &Body::None)?;
    report.same(
        "the Space catalog starts empty",
        json::at(&catalog, &["assistants"]).unwrap_or(&strict_json::Value::Null),
        "[]",
    )
}
