//! The Team Browser's one in-image program (ADR-0117).
//!
//! `launch` is the container's command: it starts the display, the session and accessibility buses, and Chromium,
//! and exits as soon as any of them stops, so the container ends with it. `mcp` replaces itself with the Cua Driver's
//! stdio MCP server, the only instrument Team drives. `ready` proves Chromium's loopback DevTools endpoint answers.
//! `files list` and `files read NAME` are the only way Team reads `/downloads`: regular files with one link, opened
//! without following links, never a partial `.crdownload`. `view` streams the display's raw pixels while Team reads
//! them, for the person watching the task; Team builds every image the person's browser decodes.
//!
//! Every input comes from Team through the container's environment or the exec argument vector and is validated
//! here again; nothing is ever passed to a shell.

use std::collections::HashMap;
use std::ffi::OsStr;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::net::{Ipv4Addr, SocketAddr, TcpStream};
use std::os::unix::fs::{FileExt, MetadataExt, OpenOptionsExt};
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
const SCREEN: &str = "1280x800x24";
/// Xvfb keeps the screen's pixels in this file (XWD, 32 bits per pixel), which `view` reads and nothing else does.
const FRAMEBUFFER: &str = "/tmp/run/Xvfb_screen0";
const WIDTH: u32 = 1280;
const HEIGHT: u32 = 800;
const FRAME_HEADER: &[u8] = b"rgb 1280 800\n";
const UNCHANGED: &[u8] = b"same\n";
const VIEW_TICK: Duration = Duration::from_millis(500);
const CUA_DRIVER: &str = "/opt/cua/cua-driver";
const CHROMIUM: &str = "/usr/lib/chromium/chromium";
const XVFB: &str = "/usr/bin/Xvfb";
const DBUS_DAEMON: &str = "/usr/bin/dbus-daemon";
const ATSPI_LAUNCHER: &str = "/usr/libexec/at-spi-bus-launcher";
/// The most files one listing names and the largest file one read returns (Team's own ingest bounds, ADR-0117 §7).
const MAX_LISTED: usize = 64;
const MAX_FILE_BYTES: u64 = 25 * 1024 * 1024;
const MAX_NAME_BYTES: usize = 255;
const PARTIAL_SUFFIX: &str = ".crdownload";
const START_DEADLINE: Duration = Duration::from_secs(10);
const POLL: Duration = Duration::from_millis(50);

#[cfg(target_arch = "x86_64")]
const O_NOFOLLOW: i32 = 0o400_000;
#[cfg(target_arch = "aarch64")]
const O_NOFOLLOW: i32 = 0o100_000;
const O_NONBLOCK: i32 = 0o4_000;

unsafe extern "C" {
    fn umask(mask: u32) -> u32;
}

fn main() -> ExitCode {
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    let arguments: Vec<&str> = arguments.iter().map(String::as_str).collect();
    let outcome = match arguments.as_slice() {
        ["launch"] => launch(),
        ["mcp"] => Err(mcp()),
        ["ready"] => ready(),
        ["view"] => view(),
        ["files", "list"] => list(Path::new(DOWNLOADS), &mut io::stdout().lock()),
        ["files", "read", name] => read(Path::new(DOWNLOADS), name, &mut io::stdout().lock()),
        _ => Err(
            "usage: browser-host launch | mcp | ready | view | files list | files read NAME".into(),
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

/// The settings Team gives one Browser: the egress proxy's address and the page language.
#[derive(Debug, PartialEq, Eq)]
struct Settings {
    proxy: Ipv4Addr,
    language: String,
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
    Ok(Settings { proxy, language })
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
    vec![
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
        "about:blank".into(),
    ]
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
    children.push(spawn(
        command(XVFB).args([
            DISPLAY,
            "-screen",
            "0",
            SCREEN,
            "-nolisten",
            "tcp",
            "-nolisten",
            "unix",
            "-fbdir",
            RUNTIME_DIR,
        ]),
        "the display",
    )?);
    wait_until(children, "the display", display_answers)?;
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
    children.push(spawn(
        command(CHROMIUM).args(chromium_arguments(settings)),
        "Chromium",
    )?);
    Ok(())
}

fn display_answers() -> bool {
    use std::os::linux::net::SocketAddrExt;
    use std::os::unix::net::{SocketAddr as UnixAddr, UnixStream};
    // Xvfb listens only on its abstract socket, which is private to the container's network namespace.
    UnixAddr::from_abstract_name(b"/tmp/.X11-unix/X99")
        .and_then(|address| UnixStream::connect_addr(&address))
        .is_ok()
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
    const NAMES: [&str; 4] = [
        "the display",
        "the session bus",
        "the accessibility bus",
        "Chromium",
    ];
    loop {
        if let Some(index) = exited(children) {
            return Err(format!(
                "{} stopped",
                NAMES.get(index).unwrap_or(&"a process")
            ));
        }
        thread::sleep(Duration::from_millis(200));
    }
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

/// Stream the screen to Team for as long as it reads: every tick, `same` when nothing changed since the last frame,
/// or the frame header and the screen's packed RGB pixels. Team ending the exec's input ends the program, as does any
/// failed write; the pixels are the page's own and untrusted, so Team only ever frames them in an image it builds.
fn view() -> Result<(), String> {
    let (file, offset) = open_framebuffer(Path::new(FRAMEBUFFER))?;
    thread::spawn(|| {
        let mut input = io::stdin().lock();
        let mut sink = [0u8; 64];
        while matches!(input.read(&mut sink), Ok(read) if read > 0) {}
        std::process::exit(0);
    });
    let mut out = io::stdout().lock();
    stream_frames(&file, offset, &mut out, || {
        thread::sleep(VIEW_TICK);
        true
    })
}

/// The framebuffer, opened without following a link or blocking, and where its pixels start; its header must describe
/// exactly the Browser's screen and its size exactly that header's pixels.
fn open_framebuffer(path: &Path) -> Result<(File, u64), String> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(O_NOFOLLOW | O_NONBLOCK)
        .open(path)
        .map_err(|_| "the display cannot be opened".to_owned())?;
    let metadata = file
        .metadata()
        .map_err(|_| "the display cannot be inspected".to_owned())?;
    if !metadata.file_type().is_file() {
        return Err("the display is not a regular file".into());
    }
    let mut header = [0u8; 100];
    file.read_exact_at(&mut header, 0)
        .map_err(|_| "the display has no header".to_owned())?;
    let offset = pixel_offset(&header, metadata.len())?;
    Ok((file, offset))
}

/// Where the pixels of an XWD header for the 1280x800 TrueColor screen start, given the file's length.
fn pixel_offset(header: &[u8; 100], length: u64) -> Result<u64, String> {
    let field = |index: usize| {
        let bytes = [
            header[index * 4],
            header[index * 4 + 1],
            header[index * 4 + 2],
            header[index * 4 + 3],
        ];
        u32::from_be_bytes(bytes)
    };
    // header size, version, ZPixmap, depth, width, height, x offset, byte order, bits per pixel, bytes per line,
    // visual class, the three masks, and the colormap's size, as Xvfb writes them for a 24-bit screen.
    let expected = [
        (1, 7),
        (2, 2),
        (3, 24),
        (4, WIDTH),
        (5, HEIGHT),
        (6, 0),
        (7, 0),
        (11, 32),
        (12, WIDTH * 4),
        (13, 4),
        (14, 0x00ff_0000),
        (15, 0x0000_ff00),
        (16, 0x0000_00ff),
    ];
    let (header_size, colors) = (field(0), field(19));
    if expected.iter().any(|&(index, value)| field(index) != value)
        || !(100..=1024).contains(&header_size)
        || colors > 256
    {
        return Err("the display is not the Browser's screen".into());
    }
    let offset = u64::from(header_size) + u64::from(colors) * 12;
    if length != offset + u64::from(WIDTH * 4 * HEIGHT) {
        return Err("the display's size does not match its header".into());
    }
    Ok(offset)
}

/// Write one tick at a time until `tick` says to stop or a write fails.
fn stream_frames(
    file: &File,
    offset: u64,
    out: &mut impl Write,
    mut tick: impl FnMut() -> bool,
) -> Result<(), String> {
    let pixels = (WIDTH * HEIGHT) as usize;
    let mut raw = vec![0u8; pixels * 4];
    let mut sent: Option<Vec<u8>> = None;
    loop {
        file.read_exact_at(&mut raw, offset)
            .map_err(|_| "the display cannot be read".to_owned())?;
        let rgb = packed_rgb(&raw);
        let written = if sent.as_deref() == Some(rgb.as_slice()) {
            out.write_all(UNCHANGED)
        } else {
            let frame = out
                .write_all(FRAME_HEADER)
                .and_then(|()| out.write_all(&rgb));
            sent = Some(rgb);
            frame
        };
        written
            .and_then(|()| out.flush())
            .map_err(|_| "Team stopped watching".to_owned())?;
        if !tick() {
            return Ok(());
        }
    }
}

/// The screen's little-endian 32-bit pixels (blue, green, red, unused) as packed red, green, blue.
fn packed_rgb(raw: &[u8]) -> Vec<u8> {
    let mut rgb = Vec::with_capacity(raw.len() / 4 * 3);
    let (pixels, _) = raw.as_chunks::<4>();
    for [blue, green, red, _] in pixels {
        rgb.extend_from_slice(&[*red, *green, *blue]);
    }
    rgb
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
                ("BROWSER_LANG", "pt-BR")
            ])),
            Ok(Settings {
                proxy: Ipv4Addr::new(172, 30, 0, 2),
                language: "pt-BR".into()
            })
        );
        for proxy in ["8.8.8.8", "172.30.0.2:3128", "localhost", "", "::1"] {
            assert!(
                settings(&environment_of(&[
                    ("BROWSER_PROXY", proxy),
                    ("BROWSER_LANG", "en")
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
        });
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

    fn xwd_header(colors: u32) -> [u8; 100] {
        let mut fields = [0u32; 25];
        for (index, value) in [
            (0, 160),
            (1, 7),
            (2, 2),
            (3, 24),
            (4, WIDTH),
            (5, HEIGHT),
            (11, 32),
            (12, WIDTH * 4),
            (13, 4),
            (14, 0x00ff_0000),
            (15, 0x0000_ff00),
            (16, 0x0000_00ff),
            (19, colors),
        ] {
            fields[index] = value;
        }
        let mut header = [0u8; 100];
        for (index, value) in fields.iter().enumerate() {
            header[index * 4..index * 4 + 4].copy_from_slice(&value.to_be_bytes());
        }
        header
    }

    #[test]
    fn only_the_browser_screen_framebuffer_is_admitted() {
        let size = u64::from(WIDTH * 4 * HEIGHT);
        assert_eq!(pixel_offset(&xwd_header(256), 160 + 3072 + size), Ok(3232));
        assert!(pixel_offset(&xwd_header(256), 160 + 3072 + size + 1).is_err());
        assert!(pixel_offset(&xwd_header(257), 160 + 257 * 12 + size).is_err());
        for (index, value) in [(1, 6), (4, 1920), (7, 1), (11, 24), (14, 0xff)] {
            let mut header = xwd_header(256);
            header[index * 4..index * 4 + 4].copy_from_slice(&u32::to_be_bytes(value));
            assert!(pixel_offset(&header, 160 + 3072 + size).is_err(), "{index}");
        }
        let directory = tempfile();
        symlink("/etc/passwd", directory.join("pointer")).unwrap();
        assert!(open_framebuffer(&directory.join("pointer")).is_err());
        assert!(open_framebuffer(&directory).is_err());
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn a_frame_is_sent_once_then_only_its_changes() {
        let directory = tempfile();
        let path = directory.join("screen");
        let pixels = (WIDTH * HEIGHT) as usize;
        let mut contents = xwd_header(0).to_vec();
        contents.resize(160, 0);
        contents.extend((0..pixels).flat_map(|_| [3u8, 2, 1, 0]));
        fs::write(&path, &contents).unwrap();
        let (file, offset) = open_framebuffer(&path).unwrap();
        assert_eq!(offset, 160);
        let mut out = Vec::new();
        let mut ticks = 0;
        stream_frames(&file, offset, &mut out, || {
            ticks += 1;
            if ticks == 2 {
                let mut changed = contents.clone();
                changed[160..164].copy_from_slice(&[9, 8, 7, 0]);
                fs::write(&path, &changed).unwrap();
            }
            ticks < 3
        })
        .unwrap();
        let frame = FRAME_HEADER.len() + pixels * 3;
        assert_eq!(out.len(), frame + UNCHANGED.len() + frame);
        assert_eq!(&out[..FRAME_HEADER.len()], FRAME_HEADER);
        assert_eq!(
            &out[FRAME_HEADER.len()..FRAME_HEADER.len() + 6],
            &[1, 2, 3, 1, 2, 3]
        );
        assert_eq!(&out[frame..frame + UNCHANGED.len()], UNCHANGED);
        let second = frame + UNCHANGED.len() + FRAME_HEADER.len();
        assert_eq!(&out[second..second + 6], &[7, 8, 9, 1, 2, 3]);
        fs::remove_dir_all(directory).unwrap();
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
