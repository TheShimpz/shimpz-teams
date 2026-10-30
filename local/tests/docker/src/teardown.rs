//! Uninstall, Team destroy, and Space reset: each proves every residue kind absent, is idempotent, and leaves what
//! the Space does not own; then the run's own cleanup, which never depends on reaching the controller.

use crate::api::{Api, Body};
use crate::assistant::install_body;
use crate::docker;
use crate::flow::Names;
use crate::json;
use crate::probe::{self, Mount};
use crate::report::{Outcome, Report, document};
use crate::resolution::ASSISTANT_ID;
use crate::storage::{file, stored};

/// Every kind of Team state a Team destroy or Space reset proves absent, in the order Team reports them.
const RESIDUES: &str = r#"["action_checkpoints","assistant_containers","brain_checkpoints","browsers",
    "chat_continuations","egress_policies","inference_configuration","integration_credentials",
    "preparation_helpers","publication_bindings","routines","runtime_state","sign_ins","stored_inputs","team_names",
    "team_networks","team_storage"]"#;

fn team_name(name: &str) -> Outcome<Body> {
    Ok(Body::Json(document(&format!(
        r#"{{"team_name":"{name}"}}"#
    ))?))
}

fn policy_probe(names: &Names, script: &str) -> Outcome<String> {
    let volume = names.volume("egress-policy");
    probe::read(
        &[Mount {
            volume: &volume,
            target: "/policy",
            writable: false,
        }],
        script,
    )
}

/// The installed Assistant's egress policy names exactly its reviewed host, readable only by the egress group, and
/// the proxy, which serves only Team's provider calls, never joins a Team network (ADR-0106).
pub fn egress_policy(names: &Names, report: &mut Report) -> Outcome {
    let proxy = docker::inspect(&["inspect", &names.egress_proxy])?;
    report.equal(
        "the egress proxy joins only its outbound network",
        json::keys(&proxy, &["NetworkSettings", "Networks"]),
        vec![names.outbound_network.as_str()],
    )?;
    let policy = policy_probe(
        names,
        "set -eu; for policy in /policy/*.json; do cat \"$policy\"; echo; stat -c '%a %u %g' \"$policy\"; done",
    )?;
    report.equal(
        "the Assistant's egress policy is its one reviewed host, 0640, owned by Team and the egress group",
        policy.as_str(),
        "[\"api.cloudflare.com\"]\n640 10001 10017",
    )
}

/// Uninstalling removes the Assistant and its egress policy and token; repeating it confirms the absence.
pub fn uninstall(api: &Api<'_>, names: &Names, report: &mut Report) -> Outcome {
    let path = "/v1/teams/demo_team/assistants/shimpz-cloudflare";
    let removed = api.ok("DELETE", path, &Body::None)?;
    report.equal(
        "the Assistant uninstalls",
        json::boolean(&removed, &["uninstalled"]),
        Some(true),
    )?;
    let again = api.ok("DELETE", path, &Body::None)?;
    report.equal(
        "uninstalling it again confirms the absence instead of failing",
        json::boolean(&again, &["uninstalled"]),
        Some(false),
    )?;
    let proxy = docker::inspect(&["inspect", &names.egress_proxy])?;
    report.equal(
        "the egress proxy still joins only its outbound network",
        json::keys(&proxy, &["NetworkSettings", "Networks"]),
        vec![names.outbound_network.as_str()],
    )?;
    let left = policy_probe(
        names,
        "set -eu; ls /policy | grep -c '\\.json$' || true; ls -A /policy/.tokens 2>/dev/null | grep -c '\\.token$' || true",
    )?;
    report.equal("no egress policy or token remains", left.as_str(), "0\n0")
}

/// Destroying a Team needs its current name, proves every residue kind absent, and is idempotent.
pub fn destroy(api: &Api<'_>, names: &Names, report: &mut Report) -> Outcome {
    let (status, mismatch) = api.call("DELETE", "/v1/teams/demo_team", &team_name("Demo Team")?)?;
    report.equal(
        "a destroy naming the former Team name is refused",
        (status, json::text(&mismatch, &["code"])),
        (409, Some("team-name-mismatch")),
    )?;
    let destroyed = api.ok("DELETE", "/v1/teams/demo_team", &team_name("Demo Renamed")?)?;
    report.equal(
        "the Team is destroyed with its storage",
        (
            json::boolean(&destroyed, &["destroyed"]),
            json::boolean(&destroyed, &["storage_removed"]),
        ),
        (Some(true), Some(true)),
    )?;
    report.same(
        "the destroy proves every residue kind absent",
        json::at(&destroyed, &["residue_absent"]).unwrap_or(&strict_json::Value::Null),
        RESIDUES,
    )?;
    report.check(
        "the Team's storage directory is gone",
        !stored(names, "-e", "demo_team")?,
        "demo_team",
    )?;
    let again = api.ok("DELETE", "/v1/teams/demo_team", &team_name("Demo Renamed")?)?;
    report.equal(
        "destroying it again confirms the absence",
        json::boolean(&again, &["destroyed"]),
        Some(false),
    )
}

/// A foreign-kind network beside one Team with an Assistant and a file, for the reset to remove.
fn populate(api: &Api<'_>, names: &Names) -> Outcome {
    let space = format!("com.shimpz.local.space-id={}", names.space_id);
    docker::ok(&[
        "network",
        "create",
        "--internal",
        "--label",
        "com.shimpz.local.managed=1",
        "--label",
        "com.shimpz.local.profile=local-v1",
        "--label",
        &space,
        &names.foreign_network,
    ])?;
    api.ok(
        "POST",
        "/v1/teams/reset_team/create",
        &team_name("Reset Team")?,
    )?;
    api.ok(
        "POST",
        "/v1/teams/reset_team/assistants",
        &install_body(ASSISTANT_ID)?,
    )?;
    api.ok(
        "POST",
        "/v1/teams/reset_team/files",
        &file(b"remove me", "reset.txt", "application/octet-stream"),
    )?;
    Ok(())
}

/// A Space reset removes every Team and Assistant, proves every residue kind absent, is idempotent, and leaves a
/// resource that lacks the exact kind label.
pub fn reset(api: &Api<'_>, names: &Names, token: &str, report: &mut Report) -> Outcome {
    populate(api, names)?;
    let reset = api.ok("DELETE", "/v1/space", &Body::None)?;
    report.equal(
        "the reset removes the one Assistant and the one Team",
        (
            json::integer(&reset, &["assistants_removed"]),
            json::integer(&reset, &["teams_removed"]),
        ),
        (Some(1), Some(1)),
    )?;
    report.same(
        "the reset proves every residue kind absent",
        json::at(&reset, &["residue_absent"]).unwrap_or(&strict_json::Value::Null),
        RESIDUES,
    )?;
    let again = api.ok("DELETE", "/v1/space", &Body::None)?;
    report.equal(
        "a second reset removes nothing",
        (
            json::integer(&again, &["assistants_removed"]),
            json::integer(&again, &["teams_removed"]),
        ),
        (Some(0), Some(0)),
    )?;
    report.check(
        "the reset Team's storage is gone",
        !stored(names, "-e", "reset_team")?,
        "reset_team",
    )?;
    report.check(
        "a network without the exact kind label is left untouched",
        docker::succeeds(&["network", "inspect", &names.foreign_network])?,
        &names.foreign_network,
    )?;
    audit(names, token, report)
}

fn audit(names: &Names, token: &str, report: &mut Report) -> Outcome {
    let volume = names.volume("audit");
    let audit = probe::read(
        &[Mount {
            volume: &volume,
            target: "/audit",
            writable: false,
        }],
        "cat /audit/audit.jsonl",
    )?;
    report.check(
        "the audit records the reset and the refused Action, never the bearer",
        audit.contains(r#""operation":"space-reset""#)
            && audit.contains(r#""detail":"assistant-integration-unavailable""#)
            && !audit.contains(token),
        audit.lines().count(),
    )
}

/// The controller bearer, the Brain runtime bearer, and the Account egress capability keep their exact custody.
pub fn secrets(names: &Names, report: &mut Report) -> Outcome {
    for (stem, target, file, expected, description) in [
        (
            "token",
            "/run/shimpz-local",
            "token",
            "440 10001 10010 1 64",
            "the controller bearer",
        ),
        (
            "runtime-token",
            "/run/shimpz-brain-runtime",
            "token",
            "440 10001 10016 1 64",
            "the Brain runtime bearer",
        ),
        (
            "account-egress-capability",
            "/run/shimpz-account-egress",
            "token",
            "440 10023 10022 1 64",
            "the Account egress capability",
        ),
    ] {
        let volume = names.volume(stem);
        let metadata = probe::metadata(
            &[Mount {
                volume: &volume,
                target,
                writable: false,
            }],
            &format!("{target}/{file}"),
        )?;
        report.equal(
            &format!("{description} is 64 bytes, 0440, singly linked, in its exact custody"),
            metadata.as_str(),
            expected,
        )?;
    }
    Ok(())
}

/// Leaves one Team with an installed Assistant for the run's own cleanup, which must remove it without the
/// controller.
pub fn leave_for_cleanup(api: &Api<'_>, names: &Names, report: &mut Report) -> Outcome {
    api.ok(
        "POST",
        "/v1/teams/cleanup_team/create",
        &team_name("Cleanup Team")?,
    )?;
    api.ok(
        "POST",
        "/v1/teams/cleanup_team/assistants",
        &install_body(ASSISTANT_ID)?,
    )?;
    report.equal(
        "one owned Assistant container and Team network remain for cleanup",
        (
            owned(names, "container", "assistant")?.len(),
            owned(names, "network", "team")?.len(),
        ),
        (1, 1),
    )
}

/// The ids of every container or network carrying the run's complete ownership labels of `kind`.
pub fn owned(names: &Names, resource: &str, kind: &str) -> Outcome<Vec<String>> {
    let mut arguments: Vec<String> = match resource {
        "container" => ["container", "ls", "--all", "--quiet"]
            .map(str::to_owned)
            .to_vec(),
        _ => ["network", "ls", "--quiet"].map(str::to_owned).to_vec(),
    };
    let expected = names.ownership(kind);
    for (label, value) in &expected {
        arguments.push("--filter".into());
        arguments.push(format!("label={label}={value}"));
    }
    let borrowed: Vec<&str> = arguments.iter().map(String::as_str).collect();
    let listed = docker::run(&borrowed)?;
    if !listed.success {
        return Ok(Vec::new());
    }
    let mut verified = Vec::new();
    for id in listed.stdout.lines().filter(|line| !line.is_empty()) {
        let Ok(metadata) = docker::inspect(&["inspect", id]) else {
            continue;
        };
        let labels = if resource == "container" {
            &["Config", "Labels"][..]
        } else {
            &["Labels"][..]
        };
        if expected.iter().all(|(label, value)| {
            json::text(&metadata, &[labels, &[label.as_str()]].concat()) == Some(value.as_str())
        }) {
            verified.push(id.to_owned());
        }
    }
    Ok(verified)
}

/// Removes everything the run created, scoped strictly to its names and labels, then proves the owned containers,
/// networks, and images gone.
pub fn cleanup(names: &Names, trusted: Option<&str>, report: &mut Report) -> Outcome {
    docker::quietly(&["rm", "--force", &names.egress_proxy]);
    // Workloads leave their networks before Docker can remove those networks.
    for id in owned(names, "container", "assistant")? {
        docker::quietly(&["container", "rm", "--force", &id]);
    }
    for id in owned(names, "network", "team")? {
        docker::quietly(&["network", "rm", &id]);
    }
    let containers = owned(names, "container", "assistant")?;
    let networks = owned(names, "network", "team")?;
    docker::quietly(&["rm", "--force", &names.controller]);
    docker::quietly(&["rm", "--force", "--volumes", &names.registry]);
    docker::quietly(&["network", "rm", &names.foreign_network]);
    docker::quietly(&["network", "rm", &names.outbound_network]);
    let volumes = names.volumes();
    let mut removal: Vec<&str> = vec!["volume", "rm", "--force"];
    removal.extend(volumes.iter().map(String::as_str));
    docker::quietly(&removal);
    docker::quietly(&["image", "rm", &names.assistant_tag]);
    if let Some(trusted) = trusted {
        docker::quietly(&["image", "rm", trusted]);
    }
    report.equal(
        "cleanup leaves no owned Assistant container or Team network",
        (containers, networks),
        (vec![], vec![]),
    )?;
    report.check(
        "cleanup leaves no image of the run",
        docker::image_absent(&names.assistant_tag)?
            && trusted.map_or(Ok(true), docker::image_absent)?,
        trusted,
    )
}
