//! `shimpz-team`: the Local Team controller and the subcommands the CLI, Compose, and the preparation helper run.

use std::process::ExitCode;

use team_local::cli::{self, Command, SocketProbe};
use team_local::docker::daemon::SOCKET;
use team_local::http::probe;
use team_local::layout::StateLayout;

fn main() -> ExitCode {
    match cli::parse(std::env::args().skip(1)) {
        Ok(command) => run(&command),
        Err(usage) => {
            eprintln!("{usage}");
            ExitCode::from(2)
        }
    }
}

/// Each subcommand is wired by its workstream; until then it reports that it is not available.
fn run(command: &Command) -> ExitCode {
    let token = StateLayout::local().controller_token;
    match command {
        Command::Prepare => team_prepare::worker::main(),
        Command::PrepareIdle => team_local::prepare::idle::main(),
        Command::Healthcheck => {
            if probe::healthy(&token, probe::PORT) {
                ExitCode::SUCCESS
            } else {
                ExitCode::FAILURE
            }
        }
        Command::Activity => match probe::activity(&token, probe::PORT) {
            Some(state) => {
                println!("{state}");
                ExitCode::SUCCESS
            }
            None => ExitCode::FAILURE,
        },
        Command::WipeRuntimeState { .. } | Command::SocketProbe(_) => host(command),
        Command::Serve => {
            eprintln!(
                "shimpz-team: `{}` is not available in this build",
                command.name()
            );
            ExitCode::from(70)
        }
    }
}

/// The CLI's host commands: the runtime-state wipe and the Docker socket probes.
fn host(command: &Command) -> ExitCode {
    let socket = std::path::Path::new(SOCKET);
    let succeeded = match command {
        Command::WipeRuntimeState { root } => {
            let wiped = team_local::lifecycle::wipe::wipe_as_self(std::path::Path::new(root));
            if wiped.is_err() {
                eprintln!("shimpz-team: the runtime state could not be wiped");
            }
            wiped.is_ok()
        }
        Command::SocketProbe(SocketProbe::Gid) => team_local::docker::socket::gid(socket)
            .map(|gid| println!("{gid}"))
            .is_some(),
        Command::SocketProbe(SocketProbe::Ping) => team_local::docker::socket::ping(socket),
        _ => false,
    };
    if succeeded {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
