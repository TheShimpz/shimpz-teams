//! `shimpz-team`: the Local Team controller and the subcommands the CLI, Compose, and the preparation helper run.

use std::process::ExitCode;

use team_local::cli::{self, Command};

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
    eprintln!(
        "shimpz-team: `{}` is not available in this build",
        command.name()
    );
    ExitCode::from(70)
}
