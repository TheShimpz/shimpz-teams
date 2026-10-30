//! Installing the reference Assistant through Team: a real digest pull, the isolation envelope of its container and
//! Team network, recovery of a killed workload, the inventory, the Integration it declares, and its Actions.

use strict_json::Value;

use crate::api::{Api, Body};
use crate::docker;
use crate::flow::Names;
use crate::json;
use crate::report::{Outcome, Report, document};
use crate::resolution::{ASSISTANT_ID, source_digest};

/// The Team registry entry of the reference Assistant.
const REGISTERED: &str = r#"[{"id":"shimpz-cloudflare","actions":["list-dns-records","list-zones"],
    "summary":"List Cloudflare zones and inspect their DNS records through OAuth.","title":"Shimpz Cloudflare"}]"#;
/// The Integration inventory entry of the reference Assistant, before it is connected.
const INTEGRATION: &str = r#"{"assistant_id":"shimpz-cloudflare","assistant_name":"Shimpz Cloudflare",
    "id":"cloudflare","provider":"cloudflare","name":"Cloudflare",
    "summary":"Connect your Cloudflare integration so this Assistant can use only its reviewed Cloudflare permissions.",
    "scopes":["dns.read","offline_access","zone.read"],"status":"missing","integration":null,"expires_at":null}"#;
/// The Integration inventory members compared.
const INTEGRATION_FIELDS: [&str; 10] = [
    "assistant_id",
    "assistant_name",
    "id",
    "provider",
    "name",
    "summary",
    "scopes",
    "status",
    "integration",
    "expires_at",
];
/// The Assistant's Action routes.
const ACTIONS: &str = "/v1/teams/demo_team/assistants/shimpz-cloudflare/actions";

/// The install request for `assistant_id` at the reference publication.
pub fn install_body(assistant_id: &str) -> Outcome<Body> {
    Ok(Body::Json(document(&format!(
        r#"{{"assistant_id":"{assistant_id}","source_digest":"{}"}}"#,
        source_digest()
    ))?))
}

/// The installed Assistant's container.
#[derive(Debug)]
pub struct Installed {
    /// The container name.
    pub name: String,
    /// The container id at install.
    pub id: String,
}

fn members(value: &Value, name: &str) -> Value {
    json::at(value, &[name]).cloned().unwrap_or(Value::Null)
}

/// An unknown Assistant is not installable and pulls nothing; the published one is pulled by digest and installed.
pub fn install(
    api: &Api<'_>,
    names: &Names,
    trusted: &str,
    report: &mut Report,
) -> Outcome<Installed> {
    let (status, _) = api.call(
        "POST",
        "/v1/teams/demo_team/assistants",
        &install_body("unknown-assistant")?,
    )?;
    report.equal(
        "an Assistant without a publication is not found",
        status,
        404,
    )?;
    report.check(
        "refusing it pulled nothing",
        docker::image_absent(trusted)?,
        trusted,
    )?;
    let installed = api.ok(
        "POST",
        "/v1/teams/demo_team/assistants",
        &install_body(ASSISTANT_ID)?,
    )?;
    report.equal(
        "the published Assistant installs",
        json::boolean(&installed, &["installed"]),
        Some(true),
    )?;
    report.check(
        "installing it pulled the exact digest",
        !docker::image_absent(trusted)?,
        trusted,
    )?;
    let catalog = api.ok("GET", "/v1/assistants", &Body::None)?;
    report.same(
        "the Space catalog lists it",
        &members(&catalog, "assistants"),
        r#"[{"id":"shimpz-cloudflare","title":"Shimpz Cloudflare"}]"#,
    )?;
    let registry = api.ok("GET", "/v1/teams/demo_team/assistant-registry", &Body::None)?;
    report.same(
        "the Team registry lists its Actions and presentation",
        &members(&registry, "assistants"),
        REGISTERED,
    )?;
    container(names, report)
}

fn container(names: &Names, report: &mut Report) -> Outcome<Installed> {
    let name = docker::Args::default()
        .words("ps --all")
        .flag(
            "--filter",
            format!("label=com.shimpz.local.space-id={}", names.space_id),
        )
        .flag(
            "--filter",
            "label=com.shimpz.local.assistant-id=shimpz-cloudflare",
        )
        .flag("--format", "{{.Names}}")
        .ok()?;
    report.check(
        "one container runs the Assistant",
        !name.is_empty() && !name.contains('\n'),
        &name,
    )?;
    let id = docker::ok(&["inspect", "--format", "{{.Id}}", &name])?;
    Ok(Installed { name, id })
}

/// The Assistant's container runs under the exact Local isolation envelope, alone on its Team's internal network.
pub fn isolation(
    installed: &Installed,
    names: &Names,
    cpuset: &str,
    report: &mut Report,
) -> Outcome {
    let metadata = docker::inspect(&["inspect", &installed.name])?;
    privileges(&metadata, report)?;
    report.equal(
        "it is bounded to 128 MiB without swap, a quarter CPU, and 64 processes, in the Space CPU set",
        (
            ["Memory", "MemorySwap", "NanoCpus", "PidsLimit"].map(|field| json::integer(&metadata, &["HostConfig", field])),
            json::text(&metadata, &["HostConfig", "CpusetCpus"]),
        ),
        ([Some(134_217_728), Some(134_217_728), Some(250_000_000), Some(64)], Some(cpuset)),
    )?;
    let host = members(&metadata, "HostConfig");
    report.same(
        "its /tmp is a non-executable 256 MiB tmpfs",
        &members(&host, "Tmpfs"),
        r#"{"/tmp":"size=256m,noexec,nosuid,nodev"}"#,
    )?;
    report.same(
        "its open files are limited to 1024",
        &members(&host, "Ulimits"),
        r#"[{"Name":"nofile","Soft":1024,"Hard":1024}]"#,
    )?;
    report.check(
        "it has no sysctls, mounts, or published ports",
        json::empty(&host, &["Sysctls"])
            && json::empty(&metadata, &["Mounts"])
            && json::empty(&host, &["PortBindings"]),
        &host,
    )?;
    team_network(&metadata, names, report)
}

fn privileges(metadata: &Value, report: &mut Report) -> Outcome {
    report.equal(
        "the Assistant runs as 10001:10001",
        json::text(metadata, &["Config", "User"]),
        Some("10001:10001"),
    )?;
    report.equal(
        "its root is read-only, every capability is dropped and none added",
        (
            json::boolean(metadata, &["HostConfig", "ReadonlyRootfs"]),
            json::strings(metadata, &["HostConfig", "CapDrop"]),
            json::empty(metadata, &["HostConfig", "CapAdd"]),
        ),
        (Some(true), vec!["ALL"], true),
    )?;
    let options = json::strings(metadata, &["HostConfig", "SecurityOpt"]);
    report.check(
        "it carries exactly no-new-privileges",
        options.len() == 1 && ["no-new-privileges", "no-new-privileges:true"].contains(&options[0]),
        &options,
    )
}

fn team_network(metadata: &Value, names: &Names, report: &mut Report) -> Outcome {
    let networks = json::keys(metadata, &["NetworkSettings", "Networks"]);
    report.equal("the Assistant joins exactly one network", networks.len(), 1)?;
    let network = docker::inspect(&["network", "inspect", networks[0]])?;
    report.equal(
        "that network is its Team's internal network, labelled with the Space and the Team name",
        (
            json::boolean(&network, &["Internal"]),
            json::text(&network, &["Labels", "com.shimpz.local.space-id"]),
            json::text(&network, &["Labels", "com.shimpz.local.team-name"]),
        ),
        (Some(true), Some(names.space_id.as_str()), Some("Demo Team")),
    )
}

/// A killed workload is restarted in place by the next install, which installs nothing new.
pub fn recovery(api: &Api<'_>, installed: &Installed, report: &mut Report) -> Outcome {
    docker::ok(&["kill", &installed.name])?;
    report.equal(
        "the killed Assistant has exited",
        docker::ok(&["inspect", "--format", "{{.State.Status}}", &installed.name])?,
        "exited".to_owned(),
    )?;
    let recovered = api.ok(
        "POST",
        "/v1/teams/demo_team/assistants",
        &install_body(ASSISTANT_ID)?,
    )?;
    report.equal(
        "installing it again recovers it without a new install",
        json::boolean(&recovered, &["installed"]),
        Some(false),
    )?;
    let id = || docker::ok(&["inspect", "--format", "{{.Id}}", &installed.name]);
    report.equal(
        "the same container was restarted, not replaced",
        id()?,
        installed.id.clone(),
    )?;
    let again = api.ok(
        "POST",
        "/v1/teams/demo_team/assistants",
        &install_body(ASSISTANT_ID)?,
    )?;
    report.equal(
        "a further install changes nothing",
        (json::boolean(&again, &["installed"]), id()?),
        (Some(false), installed.id.clone()),
    )?;
    let listed = api.ok("GET", "/v1/teams/demo_team/assistants", &Body::None)?;
    report.same(
        "the Team's Assistants list it running at its published version",
        &members(&listed, "assistants"),
        r#"[{"assistant":"shimpz-cloudflare","assistant_version":"0.1.0","provenance":"published","status":"running"}]"#,
    )
}

/// The Assistant's declared Integration is missing, so its Actions are refused before dispatch; an undeclared Action
/// is not found.
pub fn integration(api: &Api<'_>, report: &mut Report) -> Outcome {
    let inventory = api.ok(
        "GET",
        "/v1/teams/demo_team/assistant-integrations",
        &Body::None,
    )?;
    let integrations = json::items(&inventory, &["integrations"]);
    report.equal(
        "the Team has exactly one Integration",
        integrations.len(),
        1,
    )?;
    let projected = Value::Object(
        INTEGRATION_FIELDS
            .iter()
            .map(|field| ((*field).to_owned(), members(&integrations[0], field)))
            .collect(),
    );
    report.same(
        "it is the reviewed Cloudflare Integration, not yet connected",
        &projected,
        INTEGRATION,
    )?;
    let zones = Body::Json(document(r#"{"page":1,"per_page":25}"#)?);
    let (status, refused) = api.call("POST", &format!("{ACTIONS}/list-zones"), &zones)?;
    report.equal(
        "an Action needing the missing Integration is refused before dispatch",
        (status, json::text(&refused, &["code"])),
        (428, Some("assistant-integration-unavailable")),
    )?;
    let (status, _) = api.call(
        "POST",
        &format!("{ACTIONS}/shell"),
        &Body::Json(document("{}")?),
    )?;
    report.equal("an undeclared Action is not found", status, 404)
}
