//! The closed set of `shimpz-team` subcommands; each is a contract with the CLI, Compose, or the preparation helper.

/// One subcommand and its arguments.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Command {
    /// Serve the controller on port 7077 (the image's default command).
    Serve,
    /// The image healthcheck: the bearer from its file, `GET /healthz` on loopback, `{"status":"ok","trace_id":…}`.
    Healthcheck,
    /// Print `idle` or `busy` from `GET /v1/activity` on loopback, for the CLI's release reconciliation.
    Activity,
    /// Empty every runtime-state volume mounted below the root: refuse any entry another owner holds (never following
    /// a link) before anything changes, then give the owner `u+rwx` on its directories, delete everything below each
    /// volume root, and sync.
    WipeRuntimeState {
        /// The directory the volumes are mounted below.
        root: String,
    },
    /// The CLI's Docker socket probes inside the Team image.
    SocketProbe(SocketProbe),
    /// The preparation helper's idle entrypoint: wait for SIGTERM.
    PrepareIdle,
    /// Prepare one file inside the preparation helper: a header line and the original on stdin, one JSON answer on
    /// stdout.
    Prepare,
}

/// A Docker socket probe.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SocketProbe {
    /// Print the socket's group id (it must be a socket).
    Gid,
    /// Exit 0 only when `GET /_ping` answers `200 OK`.
    Ping,
}

impl Command {
    /// The subcommand's name as written on the command line.
    #[must_use]
    pub const fn name(&self) -> &'static str {
        match self {
            Self::Serve => "serve",
            Self::Healthcheck => "healthcheck",
            Self::Activity => "activity",
            Self::WipeRuntimeState { .. } => "wipe-runtime-state",
            Self::SocketProbe(_) => "socket-probe",
            Self::PrepareIdle => "prepare-idle",
            Self::Prepare => "prepare",
        }
    }
}

/// The arguments do not name exactly one subcommand with its exact arguments.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Usage;

impl std::fmt::Display for Usage {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(
            "usage: shimpz-team serve | healthcheck | activity | wipe-runtime-state <root> | socket-probe gid|ping \
             | prepare-idle | prepare",
        )
    }
}

impl std::error::Error for Usage {}

/// Parses the arguments after the program name.
///
/// # Errors
/// [`Usage`] for anything but one subcommand with its exact arguments.
pub fn parse(arguments: impl IntoIterator<Item = String>) -> Result<Command, Usage> {
    let arguments: Vec<String> = arguments.into_iter().collect();
    let words: Vec<&str> = arguments.iter().map(String::as_str).collect();
    match words.as_slice() {
        ["serve"] => Ok(Command::Serve),
        ["healthcheck"] => Ok(Command::Healthcheck),
        ["activity"] => Ok(Command::Activity),
        ["wipe-runtime-state", root] if root.starts_with('/') => Ok(Command::WipeRuntimeState {
            root: (*root).to_owned(),
        }),
        ["socket-probe", "gid"] => Ok(Command::SocketProbe(SocketProbe::Gid)),
        ["socket-probe", "ping"] => Ok(Command::SocketProbe(SocketProbe::Ping)),
        ["prepare-idle"] => Ok(Command::PrepareIdle),
        ["prepare"] => Ok(Command::Prepare),
        _ => Err(Usage),
    }
}

#[cfg(test)]
mod tests {
    use super::{Command, SocketProbe, Usage, parse};

    fn words(text: &str) -> Vec<String> {
        text.split_whitespace().map(str::to_owned).collect()
    }

    #[test]
    fn each_subcommand_parses_with_exactly_its_arguments() {
        assert_eq!(parse(words("serve")), Ok(Command::Serve));
        assert_eq!(parse(words("activity")), Ok(Command::Activity));
        assert_eq!(
            parse(words("wipe-runtime-state /state")),
            Ok(Command::WipeRuntimeState {
                root: "/state".to_owned()
            })
        );
        assert_eq!(
            parse(words("socket-probe ping")),
            Ok(Command::SocketProbe(SocketProbe::Ping))
        );
        assert_eq!(parse(words("")), Err(Usage));
        assert_eq!(parse(words("serve extra")), Err(Usage));
        assert_eq!(parse(words("wipe-runtime-state state")), Err(Usage));
        assert_eq!(parse(words("socket-probe other")), Err(Usage));
    }
}
