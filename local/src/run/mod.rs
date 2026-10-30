//! The composition root (`local/app.py` `main`): `shimpz-team`'s subcommands, and `serve`, which wires every service
//! group over the controller's [`Seams`] and runs the HTTP server until SIGTERM.
//!
//! The production binary passes the host seams through unchanged; the controller suite's test-only runner replaces
//! publication resolution and artifact verification through the same `adapt` closure.

mod actions;
mod audit;
mod compose;
mod host;
mod integrations;
mod state;
mod team;
mod web;

use std::path::Path;
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Duration;

use team_core::entropy::{Entropy, OsEntropy};
use team_core::stop::Stop;
use team_storage::custody::group_id;

use crate::audit::{AuditLog, Outcome, Principal, Settings};
use crate::authority::{KeyFiles, PUBLIC_KEY_GROUP, ReplayGuard, Verifier};
use crate::cli::{Command, SocketProbe};
use crate::coordination::Coordination;
use crate::docker::daemon::SOCKET;
use crate::docker::labels::Space;
use crate::http;
use crate::http::activity::Activity;
use crate::layout::StateLayout;
use crate::prepare;
use crate::seams::Seams;
use compose::{Composed, Shared, compose};
use host::{Stores, host_seams, hostname};

/// Where the controller listens.
const LISTEN: &str = "0.0.0.0:7077";
/// How often the Routine watchdog runs (`local/routine/watchdog.py`).
const WATCHDOG_INTERVAL: Duration = Duration::from_secs(30);

/// Runs `command`; `serve` builds the controller over the host seams `adapt` returns.
pub fn run(command: &Command, adapt: impl FnOnce(Seams) -> Seams) -> ExitCode {
    let token = StateLayout::local().controller_token;
    match command {
        Command::Serve => serve(adapt),
        Command::Prepare => team_prepare::worker::main(),
        Command::PrepareIdle => prepare::idle::main(),
        Command::Healthcheck => exit(http::probe::healthy(&token, http::probe::PORT)),
        Command::Activity => match http::probe::activity(&token, http::probe::PORT) {
            Some(state) => {
                println!("{state}");
                ExitCode::SUCCESS
            }
            None => ExitCode::FAILURE,
        },
        Command::WipeRuntimeState { root } => {
            let wiped = crate::lifecycle::wipe::wipe_as_self(Path::new(root));
            if wiped.is_err() {
                eprintln!("shimpz-team: the runtime state could not be wiped");
            }
            exit(wiped.is_ok())
        }
        Command::SocketProbe(SocketProbe::Gid) => exit(
            crate::docker::socket::gid(Path::new(SOCKET))
                .map(|gid| println!("{gid}"))
                .is_some(),
        ),
        Command::SocketProbe(SocketProbe::Ping) => {
            exit(crate::docker::socket::ping(Path::new(SOCKET)))
        }
    }
}

fn exit(succeeded: bool) -> ExitCode {
    if succeeded {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}

/// Why the controller did not start; it names the step, never a value.
#[derive(Debug)]
struct Startup(&'static str);

impl std::fmt::Display for Startup {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{} is unavailable", self.0)
    }
}

/// Maps any failure of a startup step to that step.
fn step<T, E>(result: Result<T, E>, name: &'static str) -> Result<T, Startup> {
    result.map_err(|_| Startup(name))
}

fn group(name: &'static str) -> Result<u32, Startup> {
    group_id(name).ok_or(Startup(name))
}

fn serve(adapt: impl FnOnce(Seams) -> Seams) -> ExitCode {
    // The Compose service's 128-pid limit counts threads, so both pools are fixed.
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .max_blocking_threads(16)
        .enable_all()
        .build();
    let Ok(runtime) = runtime else {
        eprintln!("shimpz-team: startup failed: the runtime is unavailable");
        return ExitCode::FAILURE;
    };
    match runtime.block_on(start(adapt)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(failure) => {
            eprintln!("shimpz-team: startup failed: {failure}");
            ExitCode::FAILURE
        }
    }
}

/// Resolves on SIGTERM (Docker's stop) or SIGINT.
async fn terminated() {
    use tokio::signal::unix::{SignalKind, signal};
    let (Ok(mut term), Ok(mut interrupt)) = (
        signal(SignalKind::terminate()),
        signal(SignalKind::interrupt()),
    ) else {
        return;
    };
    tokio::select! {
        _ = term.recv() => {}
        _ = interrupt.recv() => {}
    }
}

async fn start(adapt: impl FnOnce(Seams) -> Seams) -> Result<(), Startup> {
    let layout = StateLayout::local();
    let space_id = std::env::var("SHIMPZ_SPACE_ID").map_err(|_| Startup("SHIMPZ_SPACE_ID"))?;
    let space = Space::new(&space_id).ok_or(Startup("the Space id"))?;
    let entropy: Arc<dyn Entropy> = Arc::new(OsEntropy);
    let seams = adapt(host_seams(&layout, &entropy)?);
    let token = step(
        http::token::issue(
            &layout.controller_token,
            group(http::token::TOKEN_GROUP)?,
            seams.entropy.as_ref(),
        ),
        "the controller bearer",
    )?;
    let policy = step(
        crate::docker::policy::require(seams.docker.as_ref()).await,
        "the Docker daemon policy",
    )?;
    let audit = Arc::new(AuditLog::new(
        layout.audit_log.clone(),
        Settings::default(),
        seams.clock.clone(),
        seams.entropy.clone(),
    ));
    let verifier = Arc::new(Verifier::new(
        KeyFiles {
            supervisor: layout.supervisor_key.clone(),
            routine: layout.routine_key.clone(),
            pin: layout.inference.join("supervisor.pem"),
            key_group: group_id(PUBLIC_KEY_GROUP),
        },
        ReplayGuard::default(),
    ));
    let stores = Stores::open(&layout, &seams.clock, &seams.entropy)?;
    let shared = Shared {
        coordination: Coordination::new(stores.limit.clone()),
        hostname: hostname()?,
        cpuset: policy.cpuset,
        activity: Arc::new(Activity::default()),
        seams,
        layout,
        space,
        audit,
        verifier,
        stores,
    };
    let controller = compose(shared, token).await?;
    run_controller(controller).await
}

async fn run_controller(controller: Composed) -> Result<(), Startup> {
    // A paused turn Team cannot keep, or a Routine run it cannot recover, keeps it from starting (ADR-0086).
    step(controller.chat.restore().await, "the paused chat turns")?;
    step(
        controller.routines.check_routines(true).await,
        "the Routine recovery",
    )?;
    let started = Principal::team().event("startup", Outcome::Ok);
    let _recorded = team_core::audit::AuditSink::record(controller.audit.as_ref(), started).await;
    // A Browser a previous Team process left is removed before any task, then its pending Sign-in deletions finish.
    let sessions = controller.sessions.clone();
    tokio::spawn(async move { sessions.startup().await });
    let stop = Stop::new();
    let updater = {
        let (assistants, updates, stop) = (
            controller.assistants.clone(),
            controller.updates,
            stop.clone(),
        );
        tokio::spawn(async move {
            assistants.recover_updates().await;
            assistants.resume().await;
            updates.run(&stop).await;
        })
    };
    let watchdog = controller.routines.spawn_watchdog(WATCHDOG_INTERVAL);
    let listener = step(
        http::server::bind(step(LISTEN.parse(), "the listen address")?),
        "the listener",
    )?;
    controller.server.serve(listener, terminated()).await;
    stop.stop();
    let _ = updater.await;
    drop(watchdog);
    let _closed = controller.audit.close();
    Ok(())
}
