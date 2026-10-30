//! The staged Local snapshot inventory: snapshots staged exactly as the CLI tags them, read through Team's live
//! inventory, and its verdicts reused while an image and its tag are unchanged.

use std::fmt::Write as _;
use std::time::{Duration, Instant};

use strict_json::Value;
use team_protocol::payload::canonical_local_assistant;

use crate::api::{Api, Body};
use crate::docker;
use crate::json;
use crate::report::{Outcome, Report, fail};

const UNROUTED_PACKAGE: &[u8] = include_bytes!("../fixtures/snapshots/unrouted.package");
const UNROUTED_MANIFEST: &[u8] = include_bytes!("../fixtures/snapshots/unrouted.toml");
const VALID_PACKAGE: &[u8] = include_bytes!("../fixtures/snapshots/valid.package");
const VALID_MANIFEST: &[u8] = include_bytes!("../fixtures/snapshots/valid.toml");
const CONTRACT: &[u8] = include_bytes!("../fixtures/snapshots/shimpz.contract.json");
const PACK: &[u8] = include_bytes!("../fixtures/snapshots/shimpz.pack.json");
const ICON: &[u8] = include_bytes!("../fixtures/snapshots/icon.png");
/// The longest a warm inventory may take: its verdicts are reused, not recomputed.
const WARM: Duration = Duration::from_millis(500);

/// One staged snapshot: its declaration and the labels that differ from the fixture Assistant's.
struct Stage<'a> {
    assistant_id: &'a str,
    package: &'a [u8],
    manifest: &'a [u8],
    overrides: &'a [(&'a str, Option<&'a str>)],
}

fn labels(stage: &Stage<'_>) -> Vec<(String, String)> {
    let source = format!("sha256:{}", crate::api::sha256_hex(stage.package));
    let mut labels: Vec<(String, String)> = [
        ("org.shimpz.local.stage", "assistant-v3"),
        ("org.shimpz.assistant.id", "fixture-assistant"),
        ("org.shimpz.source.digest", &source),
        ("org.shimpz.assistant.version", "0.1.0"),
        ("org.shimpz.assistant.name", "Fixture Assistant"),
        (
            "org.shimpz.assistant.summary",
            "Exercise immutable admission.",
        ),
        ("org.shimpz.assistant.declared-creators", "@fixture"),
        (
            "org.shimpz.local.build.digest",
            &format!("sha256:{}", "b".repeat(64)),
        ),
        ("org.shimpz.assistant.actions", "ping"),
        ("org.shimpz.assistant.integrations", ""),
    ]
    .iter()
    .map(|(name, value)| ((*name).to_owned(), (*value).to_owned()))
    .collect();
    for (name, value) in stage.overrides {
        labels.retain(|(label, _)| label != name);
        if let Some(value) = value {
            labels.push(((*name).to_owned(), (*value).to_owned()));
        }
    }
    labels
}

/// The CLI's tag of a staged snapshot.
fn reference(assistant_id: &str) -> String {
    format!("shimpz-local/{assistant_id}:staged")
}

fn quoted(text: &str) -> String {
    format!("\"{}\"", text.replace('\\', "\\\\").replace('"', "\\\""))
}

/// Stages one never-started snapshot exactly as the CLI tags it (its files, labels, and fixed runtime) and returns its
/// image id.
fn stage(stage: &Stage<'_>, scratch: &std::path::Path) -> Outcome<String> {
    let files: [(&[u8], &str); 5] = [
        (stage.package, "/opt/shimpz/.shimpz/source.package"),
        (stage.manifest, "/opt/shimpz/shimpz.toml"),
        (CONTRACT, "/opt/shimpz/shimpz.contract.json"),
        (PACK, "/opt/shimpz/shimpz.pack.json"),
        (ICON, "/opt/shimpz/icon.png"),
    ];
    let context = scratch.join(stage.assistant_id);
    std::fs::create_dir_all(&context)?;
    let mut dockerfile = String::from("FROM scratch\n");
    for (index, (content, path)) in files.iter().enumerate() {
        std::fs::write(context.join(index.to_string()), content)?;
        let _ = writeln!(dockerfile, "COPY --chmod=0444 {index} {path}");
    }
    for (name, value) in labels(stage) {
        let _ = writeln!(dockerfile, "LABEL {name}={}", quoted(&value));
    }
    dockerfile.push_str(
        "USER 10001:10001\nENTRYPOINT [\"/opt/shimpz/runtime/bin/python3.14\", \"-c\", \"import signal; signal.pause()\"]\n",
    );
    std::fs::write(context.join("Dockerfile"), dockerfile)?;
    let reference = reference(stage.assistant_id);
    docker::ok(&[
        "build",
        "--quiet",
        "--tag",
        &reference,
        &context.to_string_lossy(),
    ])?;
    std::fs::remove_dir_all(&context)?;
    docker::ok(&["image", "inspect", "--format", "{{.Id}}", &reference])
}

fn inventory(api: &Api<'_>) -> Outcome<Vec<Value>> {
    let listed = api.ok("GET", "/v1/local-assistants", &Body::None)?;
    Ok(json::items(&listed, &["assistants"]).to_vec())
}

fn entry<'a>(entries: &'a [Value], assistant_id: &str) -> Outcome<&'a Value> {
    entries
        .iter()
        .find(|entry| json::text(entry, &["assistant_id"]) == Some(assistant_id))
        .map_or_else(|| fail(format!("the inventory has no {assistant_id}")), Ok)
}

/// A snapshot whose Stored Input declares no reviewed route is listed and judged inadmissible; one whose valid id label
/// sits beside an invalid Action label is an inadmissible shadow; one with no valid id label is left out. A warm
/// inventory reuses those verdicts.
pub fn inventory_fast_path(
    api: &Api<'_>,
    scratch: &std::path::Path,
    report: &mut Report,
) -> Outcome {
    let stages = [
        Stage {
            assistant_id: "fixture-assistant",
            package: UNROUTED_PACKAGE,
            manifest: UNROUTED_MANIFEST,
            overrides: &[],
        },
        Stage {
            assistant_id: "broken-labels",
            package: VALID_PACKAGE,
            manifest: VALID_MANIFEST,
            overrides: &[
                ("org.shimpz.assistant.id", Some("broken-labels")),
                ("org.shimpz.assistant.actions", Some("Ping")),
            ],
        },
        Stage {
            assistant_id: "unnamed",
            package: VALID_PACKAGE,
            manifest: VALID_MANIFEST,
            overrides: &[("org.shimpz.assistant.id", None)],
        },
    ];
    let result = judge(api, &stages, scratch, report);
    for stage in &stages {
        docker::quietly(&["image", "rm", "--force", &reference(stage.assistant_id)]);
    }
    result
}

fn judge(
    api: &Api<'_>,
    stages: &[Stage<'_>],
    scratch: &std::path::Path,
    report: &mut Report,
) -> Outcome {
    let mut ids = Vec::new();
    for staged in stages {
        ids.push(stage(staged, scratch)?);
    }
    let entries = inventory(api)?;
    let mut listed: Vec<&str> = entries
        .iter()
        .filter_map(|entry| json::text(entry, &["assistant_id"]))
        .collect();
    listed.sort_unstable();
    report.equal(
        "the inventory lists the two snapshots with a valid id label",
        listed,
        vec!["broken-labels", "fixture-assistant"],
    )?;
    report.check(
        "every inventory entry is canonical",
        entries
            .iter()
            .all(|entry| canonical_local_assistant(entry).is_some()),
        &entries,
    )?;
    verdicts(&entries, &ids, report)?;
    let started = Instant::now();
    let warm = inventory(api)?;
    let elapsed = started.elapsed();
    report.check(
        "a warm inventory answers the same verdicts",
        warm == entries,
        &warm,
    )?;
    report.check(
        "a warm inventory reuses its verdicts within 0.5 s",
        elapsed < WARM,
        elapsed,
    )
}

fn verdicts(entries: &[Value], ids: &[String], report: &mut Report) -> Outcome {
    let verdict = |entry: &Value| {
        ["image_id", "admission", "reason"]
            .map(|field| json::text(entry, &[field]).map(str::to_owned))
    };
    report.equal(
        "the snapshot whose Stored Input has no reviewed route is inadmissible as an invalid declaration",
        verdict(entry(entries, "fixture-assistant")?),
        [Some(ids[0].clone()), Some("inadmissible".into()), Some("declaration-invalid".into())],
    )?;
    let shadow = entry(entries, "broken-labels")?;
    report.equal(
        "the snapshot with an invalid Action label is an inadmissible shadow with no label member",
        (verdict(shadow), json::at(shadow, &["name"]).cloned()),
        (
            [
                Some(ids[1].clone()),
                Some("inadmissible".into()),
                Some("labels-invalid".into()),
            ],
            Some(Value::Null),
        ),
    )
}
