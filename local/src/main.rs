//! `shimpz-team`: the Local Team controller and the subcommands the CLI, Compose, and the preparation helper run.

use std::process::ExitCode;

use team_local::cli;

fn main() -> ExitCode {
    match cli::parse(std::env::args().skip(1)) {
        Ok(command) => team_local::run(&command, |seams| seams),
        Err(usage) => {
            eprintln!("{usage}");
            ExitCode::from(2)
        }
    }
}
