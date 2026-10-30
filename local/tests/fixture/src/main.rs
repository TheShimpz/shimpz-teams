//! `shimpz-team-fixture serve`: the controller suite's test-only runner (see the library).

use std::path::Path;
use std::process::ExitCode;

use team_local::cli::{self, Command};
use team_local_fixture::{PUBLICATION_PATH, Publication};

fn main() -> ExitCode {
    if cli::parse(std::env::args().skip(1)) != Ok(Command::Serve) {
        eprintln!("usage: shimpz-team-fixture serve");
        return ExitCode::from(2);
    }
    match Publication::read(Path::new(PUBLICATION_PATH)) {
        Ok(publication) => serve(publication),
        Err(error) => {
            eprintln!("shimpz-team-fixture: {error}");
            ExitCode::FAILURE
        }
    }
}

/// Serves the controller with the seeded publication through [`team_local_fixture::adapt`] once the composition root
/// is wired (integration checkpoint I1); until then it reports that it is not available, as `shimpz-team` does.
fn serve(publication: Publication) -> ExitCode {
    drop(publication);
    eprintln!("shimpz-team-fixture: `serve` is not available in this build");
    ExitCode::from(70)
}
