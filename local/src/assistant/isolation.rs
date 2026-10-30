//! The isolation profile of an Assistant container: the exact settings it is created with, and the admission of a
//! container's complete inspection against them. Every field the profile sets is compared with the value Docker
//! reports, so a container whose root could be written, whose capabilities or privileges grow, whose limits loosen,
//! whose `/tmp` could execute, or that reaches another network, a mount, a device, a port, or a proxy is drift.

use std::collections::HashMap;
use std::sync::LazyLock;

use bollard::models::{
    ContainerConfig, ContainerCreateBody, ContainerInspectResponse, EndpointSettings, HostConfig,
    HostConfigCgroupnsModeEnum, HostConfigLogConfig, NetworkingConfig, ResourcesUlimits,
    RestartPolicy, RestartPolicyNameEnum,
};
use team_egress::policy::environment_map;
use team_protocol::payload::SOURCE_DIGEST_RE;
use team_protocol::python::regex::{Pattern, declared};

use super::{ContainerSpec, Provenance};
use crate::docker::labels::{self, ASSISTANT, IMAGE, Space, label_map};

/// The ownership kind of an Assistant container.
pub const KIND: &str = "assistant";
/// The uid and gid every Assistant process runs as.
pub const UID: &str = "10001:10001";
/// Memory, and memory with swap: no swap at all.
pub const MEMORY: i64 = 128 * 1024 * 1024;
/// A quarter of one CPU.
pub const NANO_CPUS: i64 = 250_000_000;
/// The process limit.
pub const PIDS: i64 = 64;
/// The only writable path and its exact options: a dropped binary never executes, a setuid bit is never honoured, and
/// no device node opens.
pub const TMPFS: (&str, &str) = ("/tmp", "size=256m,noexec,nosuid,nodev");
/// The open-file limit, soft and hard.
pub const NOFILE: i64 = 1024;

/// Docker's two renderings of the one security option the profile sets.
const NO_NEW_PRIVILEGES: [&str; 2] = ["no-new-privileges", "no-new-privileges:true"];
/// No Assistant reaches a provider but through Team (ADR-0106): any proxy variable, in either case, is drift.
const PROXY_VARIABLES: [&str; 8] = [
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
];
/// The all-zero digest an unbound reference carries.
const UNBOUND_DIGEST: &str =
    "sha256:0000000000000000000000000000000000000000000000000000000000000000";

/// An exact image digest reference: an optional registry host and port, a repository path, and a SHA-256 digest.
static DIGEST_REF_RE: LazyLock<Pattern> = LazyLock::new(|| {
    declared(
        r"^(?:[a-z0-9.-]+(?::[0-9]{1,5})?/)?[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$",
    )
});

/// Whether `reference` is an exact, bound digest reference.
#[must_use]
pub fn is_digest_ref(reference: &str) -> bool {
    DIGEST_REF_RE.is_full(reference) && !reference.ends_with(UNBOUND_DIGEST)
}

/// The labels an Assistant container is admitted by: the Team's ownership labels and the Assistant's id.
#[must_use]
pub fn ownership(space: &Space, team_id: &str, assistant_id: &str) -> Vec<(&'static str, String)> {
    let mut expected = space.labels(KIND, Some(team_id));
    expected.push((ASSISTANT, assistant_id.to_owned()));
    expected
}

/// Where one container lives: its Space, Team, network, and the CPUs Assistants share.
#[derive(Clone, Copy, Debug)]
pub struct Placement<'a> {
    /// The Space.
    pub space: &'a Space,
    /// The Team.
    pub team_id: &'a str,
    /// The Team network's name, its only network.
    pub network: &'a str,
    /// The CPU set every workload shares (half the daemon's CPUs).
    pub cpuset: &'a str,
}

/// The exact creation body of `spec`'s container at `placement`.
#[must_use]
pub fn create_body(placement: Placement<'_>, spec: &ContainerSpec) -> ContainerCreateBody {
    let mut labels = ownership(placement.space, placement.team_id, &spec.assistant_id);
    labels.push((IMAGE, spec.image.clone()));
    let host_config = HostConfig {
        network_mode: Some(placement.network.to_owned()),
        readonly_rootfs: Some(true),
        cap_drop: Some(vec!["ALL".to_owned()]),
        security_opt: Some(vec![NO_NEW_PRIVILEGES[1].to_owned()]),
        privileged: Some(false),
        auto_remove: Some(false),
        ipc_mode: Some("private".to_owned()),
        cgroupns_mode: Some(HostConfigCgroupnsModeEnum::PRIVATE),
        memory: Some(MEMORY),
        memory_swap: Some(MEMORY),
        nano_cpus: Some(NANO_CPUS),
        cpuset_cpus: Some(placement.cpuset.to_owned()),
        pids_limit: Some(PIDS),
        tmpfs: Some(HashMap::from([(TMPFS.0.to_owned(), TMPFS.1.to_owned())])),
        ulimits: Some(vec![nofile()]),
        restart_policy: Some(RestartPolicy {
            name: Some(RestartPolicyNameEnum::NO),
            maximum_retry_count: None,
        }),
        log_config: Some(HostConfigLogConfig {
            typ: Some("none".to_owned()),
            config: Some(HashMap::new()),
        }),
        ..HostConfig::default()
    };
    ContainerCreateBody {
        image: Some(spec.image.clone()),
        user: Some(UID.to_owned()),
        labels: Some(label_map(&labels)),
        env: Some(vec![
            format!("SHIMPZ_ASSISTANT_ID={}", spec.assistant_id),
            format!("SHIMPZ_TEAM_ID={}", placement.team_id),
            "PYTHONDONTWRITEBYTECODE=1".to_owned(),
        ]),
        host_config: Some(host_config),
        networking_config: Some(NetworkingConfig {
            endpoints_config: Some(HashMap::from([(
                placement.network.to_owned(),
                EndpointSettings::default(),
            )])),
        }),
        ..ContainerCreateBody::default()
    }
}

fn nofile() -> ResourcesUlimits {
    ResourcesUlimits {
        name: Some("nofile".to_owned()),
        soft: Some(NOFILE),
        hard: Some(NOFILE),
    }
}

/// What an admitted container's inspection shows.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Admitted {
    /// The container's full id.
    pub id: String,
    /// The image reference its labels and configuration agree it was created from.
    pub image: String,
    /// The id of the image Docker created it from (`sha256:…`), when Docker reports one.
    pub image_id: Option<String>,
    /// Whether it runs.
    pub running: bool,
    /// Its environment, every name once.
    pub environment: Vec<(String, String)>,
}

/// The container drifted from its profile; nothing of it may be trusted or used.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Drift;

/// Whether `environment` names no proxy variable.
#[must_use]
pub fn egress_environment_valid(environment: &[(String, String)]) -> bool {
    !environment
        .iter()
        .any(|(name, _)| PROXY_VARIABLES.contains(&name.as_str()))
}

/// Whether the image a container's labels name belongs to `spec`'s lineage: for a published Assistant, the same
/// repository at any bound digest (an outdated container is replaced, not refused); for a staged one, exactly the
/// bound image id.
fn installed_image_valid(installed: &str, spec: &ContainerSpec) -> bool {
    match spec.provenance {
        Provenance::Published => {
            is_digest_ref(installed)
                && installed
                    .rsplit_once("@sha256:")
                    .map(|(repository, _)| repository)
                    == spec
                        .image
                        .rsplit_once("@sha256:")
                        .map(|(repository, _)| repository)
        }
        Provenance::Local => SOURCE_DIGEST_RE.is_full(installed) && installed == spec.image,
    }
}

fn empty_or_absent<T>(value: Option<&[T]>) -> bool {
    value.is_none_or(<[T]>::is_empty)
}

fn host_config_holds(host: &HostConfig, network: &str, cpuset: &str) -> bool {
    let security = host.security_opt.as_deref().unwrap_or_default();
    let restart = host.restart_policy.as_ref().and_then(|policy| policy.name);
    let log = host.log_config.as_ref();
    host.readonly_rootfs == Some(true)
        && host
            .cap_drop
            .as_deref()
            .is_some_and(|dropped| !dropped.is_empty() && dropped.iter().all(|cap| cap == "ALL"))
        && empty_or_absent(host.cap_add.as_deref())
        && security.len() == 1
        && NO_NEW_PRIVILEGES.contains(&security[0].as_str())
        && host.privileged == Some(false)
        && host.network_mode.as_deref() == Some(network)
        && host.memory == Some(MEMORY)
        && host.memory_swap == Some(MEMORY)
        && host.nano_cpus == Some(NANO_CPUS)
        && host.cpuset_cpus.as_deref() == Some(cpuset)
        && host.pids_limit == Some(PIDS)
        && host.ipc_mode.as_deref() == Some("private")
        && host.cgroupns_mode == Some(HostConfigCgroupnsModeEnum::PRIVATE)
        && host.tmpfs == Some(HashMap::from([(TMPFS.0.to_owned(), TMPFS.1.to_owned())]))
        && host.ulimits.as_deref() == Some([nofile()].as_slice())
        && host.sysctls.as_ref().is_none_or(HashMap::is_empty)
        && host.auto_remove == Some(false)
        && matches!(
            restart,
            None | Some(RestartPolicyNameEnum::EMPTY | RestartPolicyNameEnum::NO)
        )
        && log.and_then(|log| log.typ.as_deref()) == Some("none")
        && log.and_then(|log| log.config.as_ref()) == Some(&HashMap::new())
        && host.port_bindings.as_ref().is_none_or(HashMap::is_empty)
        && empty_or_absent(host.binds.as_deref())
        && empty_or_absent(host.devices.as_deref())
        && empty_or_absent(host.device_requests.as_deref())
}

fn config_holds(config: &ContainerConfig, installed: &str) -> bool {
    config.image.as_deref() == Some(installed) && config.user.as_deref() == Some(UID)
}

/// Whether the container is `spec`'s at `placement` by its labels, name, image lineage, user, and only network.
fn identity_holds(
    inspection: &ContainerInspectResponse,
    config: &ContainerConfig,
    placement: Placement<'_>,
    spec: &ContainerSpec,
    installed: &str,
) -> bool {
    let expected_name = placement
        .space
        .container_name(placement.team_id, &spec.assistant_id);
    let networks: Vec<&String> = inspection
        .network_settings
        .as_ref()
        .and_then(|settings| settings.networks.as_ref())
        .map(|networks| networks.keys().collect())
        .unwrap_or_default();
    labels::include(
        config.labels.as_ref(),
        &ownership(placement.space, placement.team_id, &spec.assistant_id),
    ) && inspection
        .name
        .as_deref()
        .and_then(|name| name.strip_prefix('/'))
        == Some(expected_name.as_str())
        && installed_image_valid(installed, spec)
        && config_holds(config, installed)
        && networks == [placement.network]
}

/// Admits `inspection` as `spec`'s container at `placement`.
///
/// # Errors
/// [`Drift`] for any difference from the profile, the ownership labels, the deterministic name, or the image lineage,
/// and for an environment that is not a list of distinct `NAME=value` entries.
pub fn admit(
    inspection: &ContainerInspectResponse,
    placement: Placement<'_>,
    spec: &ContainerSpec,
) -> Result<Admitted, Drift> {
    let config = inspection.config.as_ref().ok_or(Drift)?;
    let host = inspection.host_config.as_ref().ok_or(Drift)?;
    let environment = config
        .env
        .as_deref()
        .and_then(environment_map)
        .ok_or(Drift)?;
    let installed = config
        .labels
        .as_ref()
        .and_then(|labels| labels.get(IMAGE))
        .ok_or(Drift)?;
    let holds = identity_holds(inspection, config, placement, spec, installed)
        && host_config_holds(host, placement.network, placement.cpuset)
        && empty_or_absent(inspection.mounts.as_deref());
    if !holds {
        return Err(Drift);
    }
    Ok(Admitted {
        id: inspection.id.clone().ok_or(Drift)?,
        image: installed.clone(),
        image_id: inspection
            .image
            .clone()
            .filter(|image| SOURCE_DIGEST_RE.is_full(image)),
        running: inspection
            .state
            .as_ref()
            .and_then(|state| state.running)
            .ok_or(Drift)?,
        environment,
    })
}

#[cfg(test)]
mod tests;
