//! The Team Browser's one in-image program (ADR-0117).
//!
//! `launch` is the container's command: it starts the display, the session and accessibility buses, and Chromium,
//! and exits as soon as any of them stops, so the container ends with it, unless Team asked for the clean stop that
//! keeps an Sign-in (`stop`), after which it waits for Team to export the profile and remove the container. `mcp` replaces itself with the Cua Driver's
//! stdio MCP server, the only instrument Team drives. `ready` proves Chromium's loopback DevTools endpoint answers.
//! `files list` and `files read NAME` are the only way Team reads `/downloads`: regular files with one link, opened
//! without following links, never a partial `.crdownload`. `relay` carries one viewer's WebSocket to the display,
//! KasmVNC's X server confined to its own namespaces (`display.rs`). A Browser a person may control (`BROWSER_MODE`
//! `person` or `signed-in`, ADR-0117 Sign-ins amendment) also takes `credentials` (the display's two viewer credentials),
//! `stop` (Chromium's clean stop), `profile export`, and, in `signed-in` mode before Chromium starts, `profile import`
//! (`profile.rs`).
//!
//! Every input comes from Team through the container's environment or the exec argument vector and is validated
//! here again; nothing is ever passed to a shell.

use std::collections::HashMap;
mod compositor;
mod display;
mod profile;

use std::ffi::OsStr;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::net::{Ipv4Addr, SocketAddr, TcpStream};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitCode};
use std::thread;
use std::time::{Duration, Instant};

const DISPLAY: &str = ":99";
const RUNTIME_DIR: &str = "/tmp/run";
const SESSION_BUS: &str = "/tmp/run/bus";
const HOME: &str = "/tmp";
const DOWNLOADS: &str = "/downloads";
const PROFILE: &str = "/profile";
const DEVTOOLS_PORT: u16 = 9222;
const PROXY_PORT: u16 = 3128;
const CUA_DRIVER: &str = "/opt/cua/cua-driver";
const CHROMIUM: &str = "/usr/lib/chromium/chromium";
const DBUS_DAEMON: &str = "/usr/bin/dbus-daemon";
const ATSPI_LAUNCHER: &str = "/usr/libexec/at-spi-bus-launcher";
/// The most files one listing names and the largest file one read returns (Team's own ingest bounds, ADR-0117 §7).
const MAX_LISTED: usize = 64;
const MAX_FILE_BYTES: u64 = 25 * 1024 * 1024;
const MAX_NAME_BYTES: usize = 255;
const PARTIAL_SUFFIX: &str = ".crdownload";
const START_DEADLINE: Duration = Duration::from_secs(10);
/// How long a `signed-in` Browser waits for Team to restore its profile before Chromium starts.
const RESTORE_DEADLINE: Duration = Duration::from_secs(60);
/// How long Chromium has to end every process after its clean stop is asked.
const STOP_DEADLINE: Duration = Duration::from_secs(30);
/// Markers in the Browser's runtime directory: the profile is restored, a clean stop was asked, it completed.
const RESTORED: &str = "/tmp/run/restored";
const STOPPING: &str = "/tmp/run/stopping";
const STOPPED: &str = "/tmp/run/stopped";
const CHROMIUM_PID: &str = "/tmp/run/chromium.pid";
const MAX_URL_BYTES: usize = 2048;
const SIGTERM: i32 = 15;
const POLL: Duration = Duration::from_millis(50);

#[cfg(target_arch = "x86_64")]
const O_NOFOLLOW: i32 = 0o400_000;
#[cfg(target_arch = "aarch64")]
const O_NOFOLLOW: i32 = 0o100_000;

unsafe extern "C" {
    fn umask(mask: u32) -> u32;
    fn kill(pid: i32, signal: i32) -> i32;
}

fn main() -> ExitCode {
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    let arguments: Vec<&str> = arguments.iter().map(String::as_str).collect();
    let outcome = match arguments.as_slice() {
        ["launch"] => launch(),
        ["mcp"] => Err(mcp()),
        ["ready"] => ready(),
        ["relay"] => display::relay(),
        ["files", "list"] => list(Path::new(DOWNLOADS), &mut io::stdout().lock()),
        ["files", "read", name] => read(Path::new(DOWNLOADS), name, &mut io::stdout().lock()),
        ["credentials"] => controlled().and_then(|()| {
            credentials(&mut io::stdin().lock(), Path::new(display::PASSWORD_FILE))
        }),
        ["stop"] => controlled().and_then(|()| stop()),
        ["profile", "export"] => controlled().and_then(|()| export()),
        ["profile", "import"] => signed_in().and_then(|()| import()),
        _ => Err(
            "usage: browser-host launch | mcp | ready | relay | files list | files read NAME | credentials | stop \
             | profile export | profile import"
                .into(),
        ),
    };
    match outcome {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("browser-host: {message}");
            ExitCode::FAILURE
        }
    }
}

/// Who drives the Browser: the agent alone; a person signing in, starting at `url`; or a signed-in task restored from
/// a Sign-in, which a person may take over.
#[derive(Debug, PartialEq, Eq)]
enum Mode {
    Agent,
    Person(String),
    SignedIn,
}

/// The settings Team gives one Browser: the egress proxy's address, the page language, and its mode.
#[derive(Debug, PartialEq, Eq)]
struct Settings {
    proxy: Ipv4Addr,
    language: String,
    mode: Mode,
}

/// An `https` URL a person's sign-in starts at: bounded, with no whitespace or control character.
fn start_url(value: &str) -> bool {
    value.len() <= MAX_URL_BYTES
        && value.starts_with("https://")
        && value.len() > "https://".len()
        && !value
            .chars()
            .any(|character| character.is_whitespace() || character.is_control())
}

fn mode(environment: &HashMap<String, String>) -> Result<Mode, String> {
    match environment.get("BROWSER_MODE").map(String::as_str) {
        Some("agent") => Ok(Mode::Agent),
        Some("signed-in") => Ok(Mode::SignedIn),
        Some("person") => environment
            .get("BROWSER_URL")
            .filter(|url| start_url(url))
            .map(|url| Mode::Person(url.clone()))
            .ok_or_else(|| "BROWSER_URL must be the https URL a sign-in starts at".into()),
        _ => Err("BROWSER_MODE must be agent, person, or signed-in".into()),
    }
}

fn settings(environment: &HashMap<String, String>) -> Result<Settings, String> {
    let proxy = environment
        .get("BROWSER_PROXY")
        .and_then(|value| value.parse::<Ipv4Addr>().ok())
        .filter(|address| address.is_private())
        .ok_or("BROWSER_PROXY must be the egress's private IPv4 address")?;
    let language = environment
        .get("BROWSER_LANG")
        .filter(|value| language(value))
        .ok_or("BROWSER_LANG must be a language tag such as en or pt-BR")?
        .clone();
    let mode = mode(environment)?;
    Ok(Settings {
        proxy,
        language,
        mode,
    })
}

fn current_mode() -> Result<Mode, String> {
    mode(&std::env::vars().collect())
}

/// A Browser a person may control: only there do credentials, a clean stop, and an export exist.
fn controlled() -> Result<(), String> {
    match current_mode()? {
        Mode::Agent => Err("an agent's Browser takes no credentials and keeps no profile".into()),
        _ => Ok(()),
    }
}

fn signed_in() -> Result<(), String> {
    match current_mode()? {
        Mode::SignedIn => Ok(()),
        _ => Err("only a signed-in Browser restores a profile".into()),
    }
}

/// A two-letter language with an optional two-letter region, the form Chromium's `--lang` takes.
fn language(value: &str) -> bool {
    let bytes = value.as_bytes();
    match bytes.len() {
        2 => bytes.iter().all(u8::is_ascii_lowercase),
        5 => {
            bytes[..2].iter().all(u8::is_ascii_lowercase)
                && bytes[2] == b'-'
                && bytes[3..].iter().all(u8::is_ascii_uppercase)
        }
        _ => false,
    }
}

/// The fixed environment every process of the Browser runs under; nothing else is inherited.
fn environment() -> [(&'static str, &'static str); 5] {
    [
        ("DISPLAY", DISPLAY),
        ("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/run/bus"),
        ("XDG_RUNTIME_DIR", RUNTIME_DIR),
        ("HOME", HOME),
        ("PATH", "/usr/bin:/bin"),
    ]
}

fn command(program: &str) -> Command {
    let mut command = Command::new(program);
    command.env_clear().envs(environment());
    command
}

fn chromium_arguments(settings: &Settings) -> Vec<String> {
    let mut arguments = vec![
        format!("--user-data-dir={PROFILE}"),
        "--force-renderer-accessibility".into(),
        format!("--remote-debugging-port={DEVTOOLS_PORT}"),
        "--ozone-platform=x11".into(),
        "--no-first-run".into(),
        "--no-default-browser-check".into(),
        "--window-size=1280,800".into(),
        "--window-position=0,0".into(),
        format!("--proxy-server=http://{}:{PROXY_PORT}", settings.proxy),
        // Without this, Chromium sends loopback and link-local requests around the proxy.
        "--proxy-bypass-list=<-loopback>".into(),
        format!("--lang={}", settings.language),
    ];
    match &settings.mode {
        // Only the page: no tab strip and no address bar on the watched screen.
        Mode::Agent => arguments.extend(["--kiosk".into(), "about:blank".into()]),
        // A person signing in reads the real address in Chromium's own bar.
        Mode::Person(url) => arguments.push(url.clone()),
        // The restored session's tabs, each with its own session storage, and no extra tab beside them.
        Mode::SignedIn => arguments.extend(["--kiosk".into(), "--restore-last-session".into()]),
    }
    arguments
}

fn launch() -> Result<(), String> {
    let environment: HashMap<String, String> = std::env::vars().collect();
    let settings = settings(&environment)?;
    // Every file the Browser writes, downloads included, is readable by its own uid only.
    unsafe { umask(0o077) };
    fs::create_dir_all(RUNTIME_DIR)
        .map_err(|error| format!("cannot create the runtime directory: {error}"))?;
    let mut children = Vec::new();
    let started = start_all(&settings, &mut children);
    let outcome = started.and_then(|()| supervise(&mut children));
    for child in &mut children {
        let _ = child.kill();
        let _ = child.wait();
    }
    outcome
}

fn start_all(settings: &Settings, children: &mut Vec<Child>) -> Result<(), String> {
    let spawn = |command: &mut Command, name: &str| {
        command
            .spawn()
            .map_err(|error| format!("cannot start {name}: {error}"))
    };
    let mut display = command(display::DISPLAY_SERVER);
    display.args(display::arguments(settings.mode != Mode::Agent));
    display::confine(&mut display);
    let display = spawn(&mut display, "the display")?;
    let pid = display.id();
    children.push(display);
    display::record(pid)?;
    wait_until(children, "the display", display_answers)?;
    // The compositor composites before any window opens, so a Cua Driver session draws its cursor for a composited
    // screen.
    children.push(spawn(
        command(compositor::COMPOSITOR).args(compositor::ARGUMENTS),
        "the compositor",
    )?);
    wait_until(children, "the compositor", || {
        compositor::active(display::X_SOCKET)
    })?;
    children.push(spawn(
        command(DBUS_DAEMON).args([
            "--session",
            "--nofork",
            "--nopidfile",
            "--address=unix:path=/tmp/run/bus",
        ]),
        "the session bus",
    )?);
    wait_until(children, "the session bus", || {
        Path::new(SESSION_BUS).exists()
    })?;
    children.push(spawn(
        command(ATSPI_LAUNCHER).arg("--launch-immediately"),
        "the accessibility bus",
    )?);
    // The Cua Driver never reports usage from this Browser; it also has no route out.
    let telemetry = command(CUA_DRIVER).args(["telemetry", "disable"]).output();
    if !telemetry.is_ok_and(|output| output.status.success()) {
        return Err("cannot turn the Cua Driver's telemetry off".into());
    }
    if settings.mode == Mode::SignedIn {
        // Chromium starts only on the profile Team restored.
        let deadline = Instant::now() + RESTORE_DEADLINE;
        while !Path::new(RESTORED).exists() {
            if exited(children).is_some() || Instant::now() >= deadline {
                return Err("the profile was not restored".into());
            }
            thread::sleep(POLL);
        }
    }
    let chromium = spawn(
        command(CHROMIUM).args(chromium_arguments(settings)),
        "Chromium",
    )?;
    fs::write(CHROMIUM_PID, chromium.id().to_string())
        .map_err(|error| format!("cannot record Chromium: {error}"))?;
    children.push(chromium);
    Ok(())
}

fn display_answers() -> bool {
    // The display's own network namespace holds its abstract socket; its filesystem socket is what X clients reach.
    std::os::unix::net::UnixStream::connect(display::X_SOCKET).is_ok()
}

fn wait_until(children: &mut [Child], name: &str, ready: impl Fn() -> bool) -> Result<(), String> {
    let deadline = Instant::now() + START_DEADLINE;
    while !ready() {
        if exited(children).is_some() || Instant::now() >= deadline {
            return Err(format!("{name} did not start"));
        }
        thread::sleep(POLL);
    }
    Ok(())
}

fn exited(children: &mut [Child]) -> Option<usize> {
    children
        .iter_mut()
        .position(|child| !matches!(child.try_wait(), Ok(None)))
}

fn supervise(children: &mut [Child]) -> Result<(), String> {
    const NAMES: [&str; 5] = [
        "the display",
        "the compositor",
        "the session bus",
        "the accessibility bus",
        "Chromium",
    ];
    loop {
        if let Some(index) = exited(children) {
            if index == NAMES.len() - 1 && Path::new(STOPPING).exists() {
                // Team asked for the clean stop: the container stays, with its profile, until Team removes it.
                for child in children.iter_mut() {
                    let _ = child.kill();
                    let _ = child.wait();
                }
                loop {
                    thread::sleep(Duration::from_secs(3600));
                }
            }
            return Err(format!(
                "{} stopped",
                NAMES.get(index).unwrap_or(&"a process")
            ));
        }
        thread::sleep(Duration::from_millis(200));
    }
}

/// A viewer credential line Team writes: the user, its SHA-256-crypt hash under KasmVNC's fixed salt, and its rights.
fn credential(line: &str, user: &str, rights: &str) -> bool {
    let Some(hash) = line
        .strip_prefix(user)
        .and_then(|rest| rest.strip_prefix(":$5$kasm$"))
        .and_then(|rest| rest.strip_suffix(&format!(":{rights}")))
    else {
        return false;
    };
    hash.len() == 43
        && hash
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'.' || byte == b'/')
}

/// Write the display's two viewer credentials, read from `input`: exactly the read-only `viewer` line, then the
/// read-write `controller` line. The file is replaced whole and readable by the Browser's uid only.
fn credentials(input: &mut impl Read, path: &Path) -> Result<(), String> {
    let mut text = String::new();
    input
        .take(1024)
        .read_to_string(&mut text)
        .map_err(|_| "the credentials are not text".to_owned())?;
    let lines: Vec<&str> = text.lines().collect();
    if lines.len() != 2
        || !credential(lines[0], "viewer", "r")
        || !credential(lines[1], "controller", "rw")
    {
        return Err("the credentials are not exactly one viewer and one controller line".into());
    }
    let staged = path.with_extension("new");
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&staged)
        .map_err(|error| format!("cannot write the credentials: {error}"))?;
    file.write_all(format!("{}\n{}\n", lines[0], lines[1]).as_bytes())
        .and_then(|()| file.sync_all())
        .map_err(|error| format!("cannot write the credentials: {error}"))?;
    fs::rename(&staged, path).map_err(|error| format!("cannot write the credentials: {error}"))
}

/// Whether any process of Chromium is still alive in this container.
fn chromium_running() -> bool {
    fs::read_dir("/proc").is_ok_and(|entries| {
        entries.flatten().any(|entry| {
            entry
                .file_name()
                .to_str()
                .is_some_and(|name| name.bytes().all(|byte| byte.is_ascii_digit()))
                && fs::read_to_string(entry.path().join("comm"))
                    .is_ok_and(|comm| comm.trim_end() == "chromium")
        })
    })
}

/// Ask Chromium to end the session (SIGTERM), wait for every one of its processes, and prove the profile records a
/// clean session end; the container stays for the export.
fn stop() -> Result<(), String> {
    let pid: i32 = fs::read_to_string(CHROMIUM_PID)
        .ok()
        .and_then(|text| text.trim().parse().ok())
        .ok_or("Chromium is not running")?;
    fs::write(STOPPING, b"").map_err(|error| format!("cannot mark the stop: {error}"))?;
    if unsafe { kill(pid, SIGTERM) } != 0 {
        return Err("Chromium could not be asked to stop".into());
    }
    let deadline = Instant::now() + STOP_DEADLINE;
    while chromium_running() {
        if Instant::now() >= deadline {
            return Err("Chromium did not stop in time".into());
        }
        thread::sleep(POLL);
    }
    if !profile::ended_cleanly(Path::new(PROFILE)) {
        return Err("Chromium's session did not end cleanly".into());
    }
    fs::write(STOPPED, b"").map_err(|error| format!("cannot mark the stop: {error}"))?;
    println!("stopped");
    Ok(())
}

fn export() -> Result<(), String> {
    if !Path::new(STOPPED).exists() {
        return Err("the profile is exported only after a clean stop".into());
    }
    profile::export(
        Path::new(PROFILE),
        &mut io::BufWriter::new(io::stdout().lock()),
    )
}

fn import() -> Result<(), String> {
    if Path::new(RESTORED).exists() {
        return Err("the profile was already restored".into());
    }
    profile::import(
        &mut io::BufReader::new(io::stdin().lock()),
        Path::new(PROFILE),
    )?;
    fs::write(RESTORED, b"").map_err(|error| format!("cannot mark the restore: {error}"))
}

/// Replace this process with the Cua Driver's stdio MCP server; return only the reason it could not start.
fn mcp() -> String {
    let error = command(CUA_DRIVER)
        .args(["mcp", "--grant", "existing-profile"])
        .exec();
    format!("cannot start the Cua Driver: {error}")
}

fn ready() -> Result<(), String> {
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, DEVTOOLS_PORT));
    TcpStream::connect_timeout(&address, Duration::from_secs(1))
        .map(drop)
        .map_err(|_| "Chromium is not ready".into())
}

/// A download name Team may ask for: one path component, at most 255 bytes, no control character.
fn plain_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= MAX_NAME_BYTES
        && name != "."
        && name != ".."
        && !name.contains('/')
        && !name.chars().any(char::is_control)
}

/// Write `{"files":[{"name":…,"size":…}],"refused":[{"name":…,"reason":…,"size":…}],"skipped":n}` for every regular
/// single-link file under `root`. A complete file within the read bound is listed; a partial `.crdownload` is refused
/// as `incomplete` and a file over the bound as `over-limit`, by name, so Team can say what it did not keep without
/// ever reading either. A name that is not plain UTF-8 and every entry past either list's bound are only counted.
fn list(root: &Path, out: &mut impl Write) -> Result<(), String> {
    let mut files: Vec<(String, u64)> = Vec::new();
    let mut refused: Vec<(String, &str, u64)> = Vec::new();
    let mut skipped = 0usize;
    let entries =
        fs::read_dir(root).map_err(|error| format!("cannot list the downloads: {error}"))?;
    for entry in entries {
        let entry = entry.map_err(|error| format!("cannot list the downloads: {error}"))?;
        let metadata = entry
            .metadata()
            .map_err(|error| format!("cannot inspect a download: {error}"))?;
        if !metadata.file_type().is_file() || metadata.nlink() != 1 {
            continue;
        }
        let size = metadata.len();
        let name = entry.file_name();
        let Some(name) = name.to_str().filter(|name| plain_name(name)) else {
            skipped += 1;
            continue;
        };
        let refusal = if name.ends_with(PARTIAL_SUFFIX) {
            Some("incomplete")
        } else if size > MAX_FILE_BYTES {
            Some("over-limit")
        } else {
            None
        };
        match refusal {
            None if files.len() < MAX_LISTED => files.push((name.to_owned(), size)),
            Some(reason) if refused.len() < MAX_LISTED => {
                refused.push((name.to_owned(), reason, size))
            }
            _ => skipped += 1,
        }
    }
    files.sort();
    refused.sort();
    let listed: Vec<String> = files
        .iter()
        .map(|(name, size)| format!("{{\"name\":{},\"size\":{size}}}", json_string(name)))
        .collect();
    let refused: Vec<String> = refused
        .iter()
        .map(|(name, reason, size)| {
            format!(
                "{{\"name\":{},\"reason\":\"{reason}\",\"size\":{size}}}",
                json_string(name)
            )
        })
        .collect();
    writeln!(
        out,
        "{{\"files\":[{}],\"refused\":[{}],\"skipped\":{skipped}}}",
        listed.join(","),
        refused.join(",")
    )
    .map_err(|error| format!("cannot write the listing: {error}"))
}

fn json_string(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len() + 2);
    encoded.push('"');
    for character in value.chars() {
        match character {
            '"' => encoded.push_str("\\\""),
            '\\' => encoded.push_str("\\\\"),
            character => encoded.push(character),
        }
    }
    encoded.push('"');
    encoded
}

/// Write the file's exact length on one line, then exactly that many bytes of it.
fn read(root: &Path, name: &str, out: &mut impl Write) -> Result<(), String> {
    if !plain_name(name) || name.ends_with(PARTIAL_SUFFIX) {
        return Err("the download name is not admissible".into());
    }
    let path: PathBuf = root.join(OsStr::new(name));
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(O_NOFOLLOW)
        .open(&path)
        .map_err(|_| "the download cannot be opened".to_owned())?;
    let metadata = file
        .metadata()
        .map_err(|_| "the download cannot be inspected".to_owned())?;
    if !metadata.file_type().is_file() || metadata.nlink() != 1 || metadata.len() > MAX_FILE_BYTES {
        return Err("the download is not a complete single-link file within the bound".into());
    }
    let size = metadata.len();
    writeln!(out, "{size}").map_err(|error| format!("cannot write the download: {error}"))?;
    copy_exact(file, size, out)
}

fn copy_exact(file: File, size: u64, out: &mut impl Write) -> Result<(), String> {
    let copied = io::copy(&mut file.take(size), out)
        .map_err(|error| format!("cannot write the download: {error}"))?;
    out.flush()
        .map_err(|error| format!("cannot write the download: {error}"))?;
    if copied == size {
        Ok(())
    } else {
        Err("the download changed while it was read".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    fn environment_of(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(key, value)| ((*key).to_owned(), (*value).to_owned()))
            .collect()
    }

    #[test]
    fn settings_admit_only_a_private_proxy_and_a_plain_language() {
        assert_eq!(
            settings(&environment_of(&[
                ("BROWSER_PROXY", "172.30.0.2"),
                ("BROWSER_LANG", "pt-BR"),
                ("BROWSER_MODE", "agent")
            ])),
            Ok(Settings {
                proxy: Ipv4Addr::new(172, 30, 0, 2),
                language: "pt-BR".into(),
                mode: Mode::Agent
            })
        );
        for proxy in ["8.8.8.8", "172.30.0.2:3128", "localhost", "", "::1"] {
            assert!(
                settings(&environment_of(&[
                    ("BROWSER_PROXY", proxy),
                    ("BROWSER_LANG", "en"),
                    ("BROWSER_MODE", "agent")
                ]))
                .is_err()
            );
        }
        for language in ["", "EN", "pt-br", "pt_BR", "por", "en-US-x", "--no-sandbox"] {
            assert!(!super::language(language), "{language}");
        }
        assert!(settings(&environment_of(&[("BROWSER_PROXY", "10.0.0.2")])).is_err());
    }

    #[test]
    fn chromium_keeps_its_sandbox_and_routes_everything_through_the_proxy() {
        let arguments = chromium_arguments(&Settings {
            proxy: Ipv4Addr::new(10, 1, 2, 3),
            language: "en".into(),
            mode: Mode::Agent,
        });
        assert!(arguments.ends_with(&["--kiosk".to_owned(), "about:blank".to_owned()]));
        assert!(arguments.contains(&"--proxy-server=http://10.1.2.3:3128".to_owned()));
        assert!(arguments.contains(&"--proxy-bypass-list=<-loopback>".to_owned()));
        assert!(arguments.contains(&"--remote-debugging-port=9222".to_owned()));
        assert!(
            !arguments
                .iter()
                .any(|argument| argument.contains("sandbox") || argument.contains("address"))
        );
    }

    #[test]
    fn each_mode_starts_chromium_its_own_way() {
        let of = |pairs: &[(&str, &str)]| mode(&environment_of(pairs));
        assert_eq!(of(&[("BROWSER_MODE", "signed-in")]), Ok(Mode::SignedIn));
        assert_eq!(
            of(&[
                ("BROWSER_MODE", "person"),
                ("BROWSER_URL", "https://www.linkedin.com/login")
            ]),
            Ok(Mode::Person("https://www.linkedin.com/login".into()))
        );
        for refused in [
            vec![],
            vec![("BROWSER_MODE", "root")],
            vec![("BROWSER_MODE", "person")],
            vec![
                ("BROWSER_MODE", "person"),
                ("BROWSER_URL", "http://example.com"),
            ],
            vec![("BROWSER_MODE", "person"), ("BROWSER_URL", "https://")],
            vec![("BROWSER_MODE", "person"), ("BROWSER_URL", "https://a b")],
            vec![
                ("BROWSER_MODE", "person"),
                ("BROWSER_URL", "javascript:alert(1)"),
            ],
        ] {
            assert!(of(&refused).is_err(), "{refused:?}");
        }
        assert!(!start_url(&format!(
            "https://{}",
            "a".repeat(MAX_URL_BYTES)
        )));
        let with = |mode| {
            chromium_arguments(&Settings {
                proxy: Ipv4Addr::new(10, 1, 2, 3),
                language: "en".into(),
                mode,
            })
        };
        let person = with(Mode::Person("https://example.com/login".into()));
        assert_eq!(person.last().unwrap(), "https://example.com/login");
        assert!(!person.contains(&"--kiosk".to_owned()));
        let signed_in = with(Mode::SignedIn);
        assert!(signed_in.ends_with(&["--kiosk".to_owned(), "--restore-last-session".to_owned()]));
        assert!(!signed_in.contains(&"about:blank".to_owned()));
    }

    #[test]
    fn credentials_are_exactly_one_read_only_and_one_read_write_line() {
        let directory = tempfile();
        let path = directory.join("kasmpasswd");
        let hash = "a".repeat(43);
        let valid = format!("viewer:$5$kasm${hash}:r\ncontroller:$5$kasm${hash}:rw\n");
        credentials(&mut valid.as_bytes(), &path).unwrap();
        assert_eq!(fs::read_to_string(&path).unwrap(), valid);
        assert_eq!(fs::metadata(&path).unwrap().mode() & 0o777, 0o600);
        for refused in [
            format!("viewer:$5$kasm${hash}:rw\ncontroller:$5$kasm${hash}:rw\n"),
            format!("viewer:$5$kasm${hash}:r\ncontroller:$5$kasm${hash}:rwo\n"),
            format!("viewer:$5$kasm${hash}:r\n"),
            format!("viewer:$5$other${hash}:r\ncontroller:$5$kasm${hash}:rw\n"),
            format!("viewer:$5$kasm${hash}!:r\ncontroller:$5$kasm${hash}:rw\n"),
            format!(
                "viewer:$5$kasm${hash}:r\ncontroller:$5$kasm${hash}:rw\nowner:$5$kasm${hash}:rwo\n"
            ),
        ] {
            assert!(
                credentials(&mut refused.as_bytes(), &path).is_err(),
                "{refused}"
            );
        }
        fs::remove_dir_all(&directory).unwrap();
    }

    #[test]
    fn names_are_one_plain_component() {
        for admitted in ["edital.pdf", "relatório 2026.csv", "a", &"a".repeat(255)] {
            assert!(plain_name(admitted), "{admitted}");
        }
        for refused in [
            "",
            ".",
            "..",
            "a/b",
            "../x",
            "a\nb",
            "a\u{0}b",
            &"a".repeat(256),
        ] {
            assert!(!plain_name(refused), "{refused:?}");
        }
    }

    #[test]
    fn a_listing_names_only_complete_regular_single_link_files() {
        let directory = tempfile();
        let root = directory.as_path();
        fs::write(root.join("edital.pdf"), b"%PDF-1.4").unwrap();
        fs::write(root.join("dados \"2026\".csv"), b"a,b").unwrap();
        fs::write(root.join("Unconfirmed 1.crdownload"), b"partial").unwrap();
        fs::write(root.join("linked"), b"x").unwrap();
        fs::hard_link(root.join("linked"), root.join("linked-twice")).unwrap();
        symlink("/etc/passwd", root.join("pointer")).unwrap();
        fs::create_dir(root.join("folder")).unwrap();
        let mut out = Vec::new();
        list(root, &mut out).unwrap();
        assert_eq!(
            String::from_utf8(out).unwrap(),
            "{\"files\":[{\"name\":\"dados \\\"2026\\\".csv\",\"size\":3},{\"name\":\"edital.pdf\",\"size\":8}],\"refused\":[{\"name\":\"Unconfirmed 1.crdownload\",\"reason\":\"incomplete\",\"size\":7}],\"skipped\":0}\n"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn a_listing_counts_what_it_cannot_name() {
        let directory = tempfile();
        let root = directory.as_path();
        for index in 0..MAX_LISTED + 2 {
            fs::write(root.join(format!("file-{index:03}")), b"x").unwrap();
        }
        fs::write(root.join("tab\tname"), b"x").unwrap();
        let large = File::create(root.join("large.iso")).unwrap();
        large.set_len(MAX_FILE_BYTES + 1).unwrap();
        let mut out = Vec::new();
        list(root, &mut out).unwrap();
        let listing = String::from_utf8(out).unwrap();
        assert_eq!(listing.matches("\"name\"").count(), MAX_LISTED + 1);
        assert!(
            listing.contains(&format!(
                "\"refused\":[{{\"name\":\"large.iso\",\"reason\":\"over-limit\",\"size\":{}}}]",
                MAX_FILE_BYTES + 1
            )),
            "{listing}"
        );
        assert!(listing.ends_with("\"skipped\":3}\n"), "{listing}");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn a_read_returns_the_exact_length_then_the_bytes_and_never_follows_a_link() {
        let directory = tempfile();
        let root = directory.as_path();
        fs::write(root.join("edital.pdf"), b"%PDF-1.4\n").unwrap();
        let mut out = Vec::new();
        read(root, "edital.pdf", &mut out).unwrap();
        assert_eq!(out, b"9\n%PDF-1.4\n");
        fs::write(root.join("secret"), b"x").unwrap();
        symlink(root.join("secret"), root.join("pointer")).unwrap();
        fs::hard_link(root.join("secret"), root.join("second")).unwrap();
        fs::write(root.join("x.crdownload"), b"partial").unwrap();
        for name in [
            "pointer",
            "second",
            "x.crdownload",
            "../etc/passwd",
            "missing",
            ".",
        ] {
            assert!(read(root, name, &mut Vec::new()).is_err(), "{name}");
        }
        fs::remove_dir_all(root).unwrap();
    }

    fn tempfile() -> PathBuf {
        let directory = std::env::temp_dir().join(format!(
            "browser-host-{}-{:?}",
            std::process::id(),
            thread::current().id()
        ));
        let _ = fs::remove_dir_all(&directory);
        fs::create_dir(&directory).unwrap();
        directory
    }
}
