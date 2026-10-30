//! Probes of the controller's volumes from a separate, networkless container: the Team image has no shell, so the
//! suite reads modes, owners, and contents through the volumes themselves, never through the controller.

use crate::docker::{self, Output};
use crate::report::{Outcome, fail};

/// The pinned image the probes run in (the umbrella's probe image, which the reference Assistant also builds on).
pub const PROBE_IMAGE: &str =
    "python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170";

/// One volume mounted into a probe.
#[derive(Clone, Copy, Debug)]
pub struct Mount<'a> {
    /// The volume.
    pub volume: &'a str,
    /// Where it is mounted.
    pub target: &'a str,
    /// Whether the probe may write it.
    pub writable: bool,
}

/// Runs `script` under `/bin/sh -c` as root with `mounts` and `environment`, without a network, and reports how it
/// ended.
pub fn shell(mounts: &[Mount<'_>], environment: &[(&str, &str)], script: &str) -> Outcome<Output> {
    let mut arguments: Vec<String> = [
        "run",
        "--rm",
        "--user",
        "0:0",
        "--network",
        "none",
        "--read-only",
        "--pull",
        "never",
    ]
    .map(str::to_owned)
    .to_vec();
    for mount in mounts {
        arguments.push("--mount".into());
        arguments.push(format!(
            "type=volume,src={},dst={}{}",
            mount.volume,
            mount.target,
            if mount.writable { "" } else { ",readonly" }
        ));
    }
    for (name, value) in environment {
        arguments.push("--env".into());
        arguments.push(format!("{name}={value}"));
    }
    arguments.extend(["--entrypoint", "/bin/sh", PROBE_IMAGE, "-c", script].map(str::to_owned));
    let borrowed: Vec<&str> = arguments.iter().map(String::as_str).collect();
    docker::run(&borrowed)
}

/// Runs `script`, which must succeed, and returns its trimmed output.
pub fn read(mounts: &[Mount<'_>], script: &str) -> Outcome<String> {
    let output = shell(mounts, &[], script)?;
    if output.success {
        Ok(output.text().to_owned())
    } else {
        fail(format!(
            "the probe `{script}` failed: {}",
            output.stderr.trim()
        ))
    }
}

/// `stat -c '%a %u %g %h %s'` of `path` (mode, owner, group, links, size), read through `mounts`.
pub fn metadata(mounts: &[Mount<'_>], path: &str) -> Outcome<String> {
    read(mounts, &format!("stat -c '%a %u %g %h %s' '{path}'"))
}

/// Pulls the probe image once, so later probes never reach a registry.
pub fn pull() -> Outcome {
    if !docker::succeeds(&["image", "inspect", PROBE_IMAGE])? {
        docker::ok(&["pull", PROBE_IMAGE])?;
    }
    Ok(())
}
