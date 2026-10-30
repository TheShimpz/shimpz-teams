//! The Team image's contract, read from the image itself: its runtime configuration, and a filesystem that holds only
//! `shimpz-team`, cosign, and `PDFium` (with its provenance) besides the distroless base and data files, with no interpreter or shell, and
//! every runtime directory at its exact owner and mode.

use crate::docker;
use crate::json;
use crate::report::{Outcome, Report};
use crate::tar::{self, Entry};

/// Every ELF object the image adds outside the base's library directories, with its owner, group, and mode: the two
/// root-owned executables and the PDF renderer, none writable.
const ADDED: [(&str, u32, u32, u32); 3] = [
    ("usr/local/bin/cosign", 0, 0, 0o555),
    ("usr/local/bin/shimpz-team", 0, 0, 0o555),
    ("usr/local/lib/libpdfium.so", 0, 0, 0o444),
];
/// The PDF renderer's recorded provenance (`teams/prepare/pdfium.toml`), beside it.
const PDFIUM_PROVENANCE: &str = "usr/local/lib/pdfium.toml";
/// The base's own library directories.
const LIBRARIES: [&str; 4] = ["lib/", "lib64/", "usr/lib/", "usr/lib64/"];
/// Names that betray an interpreter or a shell.
const INTERPRETERS: [&str; 10] = [
    "sh", "bash", "dash", "ash", "busybox", "perl", "ruby", "node", "lua", "env",
];
/// The pinned zone database the Routine phrase tables were generated from.
const ZONEINFO: &str = "usr/share/shimpz-team/zoneinfo";
/// Every runtime directory: owner, group, and mode.
const DIRECTORIES: [(&str, u32, u32, u32); 20] = [
    ("run/shimpz-local", 10001, 10010, 0o2750),
    ("run/shimpz-brain-runtime", 10001, 10016, 0o750),
    ("run/shimpz-local-supervisor", 0, 10021, 0o2770),
    ("var/log/shimpz-local", 10001, 10001, 0o700),
    ("var/lib/shimpz-local/storage", 10001, 10001, 0o700),
    ("var/lib/shimpz-local/inference", 10001, 10001, 0o700),
    ("var/lib/shimpz-local/action-journal", 10001, 10001, 0o700),
    ("var/lib/shimpz-local/assistant-egress", 10001, 10017, 0o750),
    (
        "var/lib/shimpz-local/assistant-integrations/state",
        10001,
        10001,
        0o700,
    ),
    (
        "var/lib/shimpz-local/assistant-integrations/key",
        10001,
        10001,
        0o700,
    ),
    (
        "var/lib/shimpz-local/assistant-stored-inputs/state",
        10001,
        10001,
        0o700,
    ),
    (
        "var/lib/shimpz-local/assistant-stored-inputs/key",
        10001,
        10001,
        0o700,
    ),
    (
        "var/lib/shimpz-local/chat-continuations/state",
        10001,
        10001,
        0o700,
    ),
    (
        "var/lib/shimpz-local/chat-continuations/key",
        10001,
        10001,
        0o700,
    ),
    ("var/lib/shimpz-local/routines/state", 10001, 10001, 0o700),
    ("var/lib/shimpz-local/routines/key", 10001, 10001, 0o700),
    (
        "var/lib/shimpz-local/browser-sign-ins/state",
        10001,
        10001,
        0o700,
    ),
    (
        "var/lib/shimpz-local/browser-sign-ins/key",
        10001,
        10001,
        0o700,
    ),
    ("var/lib/shimpz-local/publications", 10001, 10001, 0o700),
    ("var/lib/shimpz-local/cosign", 10001, 10001, 0o700),
];

/// The image's runtime configuration matches its Compose and CLI contract.
pub fn configuration(image: &str, report: &mut Report) -> Outcome {
    let inspected = docker::inspect(&["image", "inspect", image])?;
    report.equal(
        "the image runs shimpz-team serve as 10001:10001 on port 7077",
        (
            json::text(&inspected, &["Config", "User"]),
            json::strings(&inspected, &["Config", "Entrypoint"]),
            json::strings(&inspected, &["Config", "Cmd"]),
            json::keys(&inspected, &["Config", "ExposedPorts"]),
        ),
        (
            Some("10001:10001"),
            vec!["/usr/local/bin/shimpz-team"],
            vec!["serve"],
            vec!["7077/tcp"],
        ),
    )?;
    report.equal(
        "its healthcheck is shimpz-team healthcheck every 30 s within 4 s, after a 30 s start probed every second",
        (
            json::strings(&inspected, &["Config", "Healthcheck", "Test"]),
            ["Interval", "Timeout", "StartPeriod", "StartInterval", "Retries"]
                .map(|field| json::integer(&inspected, &["Config", "Healthcheck", field])),
        ),
        (
            vec!["CMD", "/usr/local/bin/shimpz-team", "healthcheck"],
            [Some(30_000_000_000), Some(4_000_000_000), Some(30_000_000_000), Some(1_000_000_000), Some(3)],
        ),
    )?;
    report.equal(
        "it is labelled the Local Team of profile local-v1 and names its Browser image",
        (
            json::text(&inspected, &["Config", "Labels", "org.shimpz.component"]),
            json::text(
                &inspected,
                &["Config", "Labels", "org.shimpz.local.profile"],
            ),
            json::text(
                &inspected,
                &["Config", "Labels", "org.shimpz.local.browser-image"],
            )
            .is_some(),
        ),
        (Some("team-local"), Some("local-v1"), true),
    )?;
    let zoneinfo = format!("TZDIR=/{ZONEINFO}");
    report.check(
        "its time zones are read from the pinned zone database",
        json::strings(&inspected, &["Config", "Env"]).contains(&zoneinfo.as_str()),
        json::strings(&inspected, &["Config", "Env"]),
    )
}

fn exported(image: &str) -> Outcome<Vec<Entry>> {
    let container = docker::ok(&["create", "--pull", "never", image])?;
    let entries = docker::stream(&["export", &container], |archive| tar::entries(archive));
    docker::quietly(&["rm", "--force", &container]);
    entries
}

fn interpreter(path: &str) -> bool {
    let name = path.rsplit('/').next().unwrap_or(path);
    INTERPRETERS.contains(&name) || name.starts_with("python")
}

/// The image's filesystem holds no interpreter or shell, adds exactly its binaries, keeps them immutable, and pins its
/// runtime directories and zone database.
pub fn filesystem(image: &str, report: &mut Report) -> Outcome {
    let entries = exported(image)?;
    executables(&entries, report)?;
    directories(&entries, report)?;
    zone_database(&entries, report)
}

fn executables(entries: &[Entry], report: &mut Report) -> Outcome {
    // A directory may carry such a name (the base's GDB scripts for libstdc++ under `usr/share/gcc/python`); a file
    // or a link must not.
    let interpreters: Vec<&str> = entries
        .iter()
        .filter(|entry| entry.kind != b'5' && interpreter(&entry.path))
        .map(|entry| entry.path.as_str())
        .collect();
    report.equal(
        "the image holds no interpreter or shell",
        interpreters,
        Vec::new(),
    )?;
    let library = |entry: &Entry| {
        LIBRARIES
            .iter()
            .any(|directory| entry.path.starts_with(directory))
    };
    let mut added: Vec<(&str, u32, u32, u32)> = entries
        .iter()
        .filter(|entry| entry.is_elf() && !library(entry))
        .map(|entry| (entry.path.as_str(), entry.uid, entry.gid, entry.mode))
        .collect();
    added.sort_unstable();
    report.equal(
        "the image adds only shimpz-team, cosign, and PDFium as native code, root-owned and unwritable",
        added,
        ADDED.to_vec(),
    )?;
    report.check(
        "PDFium carries its recorded provenance",
        entries
            .iter()
            .any(|entry| entry.path == PDFIUM_PROVENANCE && entry.is_file()),
        PDFIUM_PROVENANCE,
    )
}

fn directories(entries: &[Entry], report: &mut Report) -> Outcome {
    let found = |path: &'static str| {
        entries
            .iter()
            .find(|entry| entry.path == path && entry.kind == b'5')
            .map_or((path, u32::MAX, u32::MAX, 0), |entry| {
                (path, entry.uid, entry.gid, entry.mode)
            })
    };
    let directories: Vec<(&str, u32, u32, u32)> =
        DIRECTORIES.iter().map(|(path, ..)| found(path)).collect();
    report.equal(
        "every runtime directory has its exact owner, group, and mode",
        directories,
        DIRECTORIES.to_vec(),
    )
}

fn zone_database(entries: &[Entry], report: &mut Report) -> Outcome {
    let version = entries
        .iter()
        .find(|entry| entry.path == format!("{ZONEINFO}/tzdata.zi"))
        .and_then(|entry| {
            String::from_utf8_lossy(&entry.head)
                .lines()
                .next()
                .map(str::to_owned)
        });
    report.equal(
        "the pinned zone database is the version the Routine phrase tables were generated from",
        version,
        Some(format!(
            "# version {}",
            team_protocol::phrase::TZDATA_VERSION
        )),
    )
}

#[cfg(test)]
mod tests {
    use super::interpreter;

    #[test]
    fn interpreters_are_named_by_their_basename() {
        assert!(interpreter("bin/sh"));
        assert!(interpreter("usr/bin/python3.14"));
        assert!(interpreter("usr/bin/env"));
        assert!(!interpreter("usr/local/bin/shimpz-team"));
        assert!(!interpreter("usr/lib/x86_64-linux-gnu/libpython-free.so"));
        assert!(!interpreter("etc/shells"));
    }
}
