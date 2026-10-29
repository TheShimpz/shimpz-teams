//! CONNECT-only outbound enforcement for the Team Browser (`shimpz-egress browser`, ADR-0117).
//!
//! This is the Team Browser's only route to the internet. One static isolated network carries at most one Browser at
//! a time; only this proxy joins an outbound network. It admits CONNECT to any public DNS hostname on port 443 and
//! refuses every other port, every IP literal, and every malformed or single-label name, without ever naming a refused
//! target in its audit. The neutral transport (`egress-transport`, ADR-0104) resolves only public addresses, records,
//! connects, admits the TLS `ClientHello`, and splices. Every audit line carries the Browser's address and the random
//! generation this process drew at start; Team restarts the process between Browser tasks and records the generation
//! each task ran on, so every line belongs to exactly one task. No path, query, header, or page content is recorded.

pub mod host;

use std::fmt;
use std::fs;
use std::io::{self, Write};
use std::net::IpAddr;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use egress_transport::audit::uuid4_hex;
use egress_transport::{
    AuditError, AuditWriter, Decision, Envelope, Event, Fields, Json, Limits, Profile, Server,
    Target, Transport, refuse,
};
use rustix::fs::{Mode, OFlags, open};

pub use host::hostname;

/// The port Chromium's fixed proxy address names.
pub const PORT: u16 = 3128;
/// The audit stream.
pub const AUDIT_PATH: &str = "/var/log/browser-egress/audit.jsonl";
/// Beside the audit it names, in the directory only this process's user owns.
pub const GENERATION_PATH: &str = "/var/log/browser-egress/generation";
/// The service name of every audit line.
pub const SERVICE: &str = "browser-egress";
/// The subject of every refused target, which is never named.
pub const REJECTED: &str = "rejected-target";
/// The one admitted port.
pub const ALLOWED_PORT: u16 = 443;
/// One Chromium on a heavy page plus its speculative tunnels (each held until its `ClientHello` deadline) filled 40
/// slots and was answered 503, failing the next navigation (measured 2026-10-18). 96 handlers, the resolvers, and the
/// main thread stay inside the service's `pids_limit` of 128.
pub const LIMITS: Limits = Limits {
    concurrency: 96,
    source_concurrency: 96,
    backlog: 96,
};

/// The Browser profile: its audit stream and this process's generation.
pub struct Browser {
    audit: AuditWriter,
    generation: String,
}

impl Browser {
    /// A profile recording to `audit` under `generation`.
    #[must_use]
    pub fn new(audit: AuditWriter, generation: String) -> Self {
        Self { audit, generation }
    }
}

impl Profile for Browser {
    fn envelope(&self) -> Envelope {
        Envelope {
            idle_timeout: Duration::from_secs(120),
            request_limit: 4096,
            ..Envelope::default()
        }
    }

    /// Admit a CONNECT to a public hostname on port 443; every other request is refused unnamed.
    fn admit(&self, request: &[u8], client: IpAddr) -> Result<Target, Decision> {
        let mut fields = Fields::new();
        fields.insert("client", client.to_string());
        let target = egress_transport::request::parse_connect(request).map_err(|refusal| {
            refuse(refusal.code, "request-rejected", REJECTED, fields.clone())
        })?;
        match hostname(&target.host) {
            Some(host) if target.port == ALLOWED_PORT => {
                Ok(Target::with_fields(host, ALLOWED_PORT, fields))
            }
            _ => Err(refuse(403, "destination-rejected", REJECTED, fields)),
        }
    }

    /// Persist one decision: its result, stable code and reason, destination, client address, and generation.
    fn record(&self, decision: &Decision) -> Result<(), AuditError> {
        self.audit.write(event(decision, &self.generation)?)
    }
}

/// The audit event of a decision, or an error for one outside this profile's bounded schema.
///
/// # Errors
///
/// The subject is neither the refused subject nor a host on 443, or the fields are not exactly one client address.
pub fn event(decision: &Decision, generation: &str) -> Result<Event, AuditError> {
    let invalid = AuditError("invalid Browser egress audit event");
    let named = decision.subject == REJECTED
        || decision
            .subject
            .rsplit_once(':')
            .is_some_and(|(host, port)| !host.is_empty() && port == "443");
    let client = decision
        .fields
        .get("client")
        .and_then(|client| client.parse::<IpAddr>().ok());
    let (Some(client), true, 1) = (client, named, decision.fields.len()) else {
        return Err(invalid);
    };
    Ok([
        ("principal_class", Json::from("machine")),
        ("principal_id", Json::from("browser")),
        ("operation", Json::from("connect")),
        ("generation", Json::from(generation)),
        ("client", Json::Str(client.to_string())),
        ("subject", Json::Str(decision.subject.clone())),
        ("result", Json::from(decision.result.as_str())),
        ("code", Json::Int(i64::from(decision.code))),
        ("reason", Json::Str(decision.reason.clone())),
        ("probe", Json::Bool(decision.probe)),
    ]
    .into_iter()
    .map(|(key, value)| (key.to_owned(), value))
    .collect())
}

/// Publish this process's generation for the healthcheck, before the listener exists (ADR-0117 §4.3): written whole
/// to a hidden file beside it, owner-only, then renamed into place.
///
/// # Errors
///
/// The generation could not be written or renamed.
pub fn publish(path: &Path, generation: &str) -> io::Result<()> {
    let mut name = std::ffi::OsString::from(".");
    name.push(path.file_name().unwrap_or_default());
    let staged = path.with_file_name(name);
    let flags = OFlags::WRONLY
        .union(OFlags::CREATE)
        .union(OFlags::TRUNC)
        .union(OFlags::CLOEXEC)
        .union(OFlags::NOFOLLOW);
    let mut file = fs::File::from(open(&staged, flags, Mode::from_raw_mode(0o600))?);
    file.write_all(generation.as_bytes())?;
    drop(file);
    fs::rename(&staged, path)
}

/// The healthcheck's answer: the generation of the process that accepts on `port`, or None.
///
/// The proxy publishes its generation before it listens, so a probe that connected prints the generation of the
/// process that answered; Team reads it from the probe's recorded output (ADR-0117 §4.3).
#[must_use]
pub fn probe(port: u16, generation_path: &Path) -> Option<String> {
    if !egress_transport::health::listening(port, Duration::from_secs(2)) {
        return None;
    }
    let generation = fs::read(generation_path).ok()?;
    let valid = generation.len() == 32
        && generation
            .iter()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte));
    valid.then(|| generation.into_iter().map(char::from).collect())
}

/// Why the profile refuses to start.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Startup {
    /// The audit directory's custody is unsafe or unavailable.
    Custody,
    /// The generation could not be drawn or published.
    Generation,
    /// The listener could not be bound.
    Listen,
}

impl fmt::Display for Startup {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Custody => "audit custody is unavailable; refusing to start",
            Self::Generation => "the generation cannot be published; refusing to start",
            Self::Listen => "the listener is unavailable; refusing to start",
        })
    }
}

/// The audit stream, the generation file, the port, and the system interfaces.
pub struct Setup {
    /// The audit stream.
    pub audit: AuditWriter,
    /// Where the generation is published.
    pub generation_path: PathBuf,
    /// The listening port.
    pub port: u16,
    /// The system interfaces.
    pub transport: Transport,
}

impl Setup {
    /// The image's paths, port, and system interfaces.
    #[must_use]
    pub fn production() -> Self {
        Self {
            audit: AuditWriter::new(AUDIT_PATH, SERVICE),
            generation_path: PathBuf::from(GENERATION_PATH),
            port: PORT,
            transport: Transport::system(),
        }
    }
}

/// Check audit custody, draw and publish this process's generation, then bind; nothing listens unless all hold.
///
/// # Errors
///
/// The first startup step that fails.
pub fn start(setup: Setup) -> Result<Server, Startup> {
    setup.audit.ensure_custody().map_err(|_| Startup::Custody)?;
    let generation = uuid4_hex()
        .and_then(|generation| publish(&setup.generation_path, &generation).map(|()| generation))
        .map_err(|_| Startup::Generation)?;
    let profile = Arc::new(Browser::new(setup.audit, generation));
    Server::bind(setup.port, LIMITS, profile, Arc::new(setup.transport))
        .map_err(|_| Startup::Listen)
}

/// Start and serve forever; on a startup failure, say why and return 1.
pub fn run(setup: Setup) -> u8 {
    match start(setup) {
        Ok(server) => server.serve(),
        Err(startup) => {
            eprintln!("browser-egress: {startup}");
            1
        }
    }
}

/// The image's entry: [`run`] with the production setup.
#[must_use]
pub fn main() -> u8 {
    run(Setup::production())
}

#[cfg(test)]
mod tests;
