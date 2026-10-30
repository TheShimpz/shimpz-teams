//! `shimpz-team`: the Local Team controller and the subcommands the CLI, Compose, and the preparation helper run.

use std::process::ExitCode;

use team_local::cli::{self, Command};
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
        _ => {
            eprintln!(
                "shimpz-team: `{}` is not available in this build",
                command.name()
            );
            ExitCode::from(70)
        }
    }
}
