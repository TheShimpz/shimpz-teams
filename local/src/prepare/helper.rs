//! The networkless preparation helper's container: its exact envelope, its inspection before any file byte reaches
//! it, and the removal of every helper of one Team or of the whole Space (ADR-0093).
//!
//! The helper runs the controller's own image as `shimpz-team prepare-idle`, one per Space at a time. It has no
//! network, capabilities, credentials, Docker socket, or Team storage mount: user 65534, a read-only root with only a
//! 16 MiB non-executable `/tmp`, private IPC and cgroup namespaces, 256 MiB of memory and swap, half a CPU, 128
//! processes, the controller's CPU set, 64 descriptors, no log driver, and its Space, Team, and `prepare` labels.

use std::collections::HashMap;

use bollard::models::{
    ContainerCreateBody, ContainerInspectResponse, HostConfig, HostConfigCgroupnsModeEnum,
    HostConfigLogConfig, ResourcesUlimits, RestartPolicy, RestartPolicyNameEnum,
};
use team_prepare::limits::{HELPER_MEMORY_BYTES, HELPER_NANO_CPUS, HELPER_PIDS};

use crate::docker::{DockerEngine, DockerError};

/// The `com.shimpz.local.kind` of a preparation helper.
pub const KIND: &str = "prepare";
/// The user the helper and every worker exec run as.
pub const USER: &str = "65534:65534";
/// The controller's own program, which the helper's image carries.
pub const PROGRAM: &str = "/usr/local/bin/shimpz-team";
/// The helper's only writable path.
pub const TMPFS: (&str, &str) = ("/tmp", "size=16m,mode=1777,noexec,nosuid,nodev");
/// The helper's open-file limit, soft and hard.
pub const NOFILE: i64 = 64;

const MANAGED_LABEL: &str = "com.shimpz.local.managed";
const PROFILE_LABEL: &str = "com.shimpz.local.profile";
const SPACE_LABEL: &str = "com.shimpz.local.space-id";
const KIND_LABEL: &str = "com.shimpz.local.kind";
const TEAM_LABEL: &str = "com.shimpz.local.team-id";
const PROFILE: &str = "local-v1";

/// The exact ownership labels of the Space's helpers, or of one Team's.
#[must_use]
pub fn labels(space_id: &str, team_id: Option<&str>) -> Vec<(&'static str, String)> {
    let mut labels = vec![
        (MANAGED_LABEL, "1".to_owned()),
        (PROFILE_LABEL, PROFILE.to_owned()),
        (SPACE_LABEL, space_id.to_owned()),
        (KIND_LABEL, KIND.to_owned()),
    ];
    labels.extend(team_id.map(|team| (TEAM_LABEL, team.to_owned())));
    labels
}

/// What one helper is created with.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Requested {
    /// The controller's exact image id (`sha256:…`).
    pub image: String,
    /// The container's name.
    pub name: String,
    /// Its Space and Team.
    pub labels: HashMap<String, String>,
    /// The CPUs it may run on, when the controller is bound to some.
    pub cpuset: Option<String>,
}

fn tmpfs() -> HashMap<String, String> {
    HashMap::from([(TMPFS.0.to_owned(), TMPFS.1.to_owned())])
}

fn nofile() -> ResourcesUlimits {
    ResourcesUlimits {
        name: Some("nofile".to_owned()),
        soft: Some(NOFILE),
        hard: Some(NOFILE),
    }
}

fn entrypoint() -> Vec<String> {
    vec![PROGRAM.to_owned(), "prepare-idle".to_owned()]
}

/// The creation body for `requested`.
#[must_use]
pub fn body(requested: &Requested) -> ContainerCreateBody {
    ContainerCreateBody {
        image: Some(requested.image.clone()),
        entrypoint: Some(entrypoint()),
        cmd: Some(Vec::new()),
        user: Some(USER.to_owned()),
        working_dir: Some("/".to_owned()),
        env: Some(Vec::new()),
        labels: Some(requested.labels.clone()),
        host_config: Some(HostConfig {
            network_mode: Some("none".to_owned()),
            readonly_rootfs: Some(true),
            cap_drop: Some(vec!["ALL".to_owned()]),
            security_opt: Some(vec!["no-new-privileges:true".to_owned()]),
            privileged: Some(false),
            ipc_mode: Some("private".to_owned()),
            cgroupns_mode: Some(HostConfigCgroupnsModeEnum::PRIVATE),
            mounts: Some(Vec::new()),
            binds: Some(Vec::new()),
            tmpfs: Some(tmpfs()),
            memory: Some(HELPER_MEMORY_BYTES),
            memory_swap: Some(HELPER_MEMORY_BYTES),
            nano_cpus: Some(HELPER_NANO_CPUS),
            pids_limit: Some(HELPER_PIDS),
            cpuset_cpus: requested.cpuset.clone(),
            ulimits: Some(vec![nofile()]),
            restart_policy: Some(RestartPolicy {
                name: Some(RestartPolicyNameEnum::NO),
                maximum_retry_count: None,
            }),
            log_config: Some(HostConfigLogConfig {
                typ: Some("none".to_owned()),
                config: None,
            }),
            ..HostConfig::default()
        }),
        ..ContainerCreateBody::default()
    }
}

/// The field of the envelope that drifted.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Drift(pub &'static str);

fn check(holds: bool, field: &'static str) -> Result<(), Drift> {
    if holds { Ok(()) } else { Err(Drift(field)) }
}

fn empty<T>(value: Option<&Vec<T>>) -> bool {
    value.is_none_or(Vec::is_empty)
}

fn unset(value: Option<&String>) -> bool {
    value.is_none_or(String::is_empty)
}

/// The created helper, not yet started, against its complete envelope.
///
/// # Errors
/// [`Drift`] naming the first field that differs.
pub fn inspect(container: &ContainerInspectResponse, requested: &Requested) -> Result<(), Drift> {
    identity(container, requested)?;
    let host = container.host_config.as_ref().ok_or(Drift("host"))?;
    privileges(host)?;
    namespaces(host, container)?;
    resources(host, requested.cpuset.as_deref())?;
    storage(host, container)
}

fn identity(container: &ContainerInspectResponse, requested: &Requested) -> Result<(), Drift> {
    check(
        container.name.as_deref() == Some(&format!("/{}", requested.name)),
        "name",
    )?;
    check(
        container.image.as_deref() == Some(requested.image.as_str()),
        "image",
    )?;
    let config = container.config.as_ref().ok_or(Drift("config"))?;
    check(
        config.image.as_deref() == Some(requested.image.as_str()),
        "image-reference",
    )?;
    check(config.user.as_deref() == Some(USER), "user")?;
    check(
        config.entrypoint.as_ref() == Some(&entrypoint()),
        "entrypoint",
    )?;
    check(empty(config.cmd.as_ref()), "cmd")?;
    check(
        config.labels.as_ref().is_some_and(|actual| {
            requested
                .labels
                .iter()
                .all(|(name, value)| actual.get(name) == Some(value))
        }),
        "labels",
    )
}

fn privileges(host: &HostConfig) -> Result<(), Drift> {
    check(host.readonly_rootfs == Some(true), "read-only")?;
    check(
        host.cap_drop.as_deref() == Some(&["ALL".to_owned()][..]),
        "cap-drop",
    )?;
    check(empty(host.cap_add.as_ref()), "cap-add")?;
    check(host.privileged == Some(false), "privileged")?;
    let security = host.security_opt.as_deref().unwrap_or_default();
    check(
        security.len() == 1
            && (security[0] == "no-new-privileges" || security[0] == "no-new-privileges:true"),
        "no-new-privileges",
    )?;
    check(empty(host.group_add.as_ref()), "group-add")?;
    check(
        host.sysctls.as_ref().is_none_or(HashMap::is_empty),
        "sysctls",
    )
}

fn namespaces(host: &HostConfig, container: &ContainerInspectResponse) -> Result<(), Drift> {
    check(host.network_mode.as_deref() == Some("none"), "network-mode")?;
    let networks = container
        .network_settings
        .as_ref()
        .and_then(|settings| settings.networks.as_ref());
    check(
        networks.is_none_or(|networks| networks.keys().all(|name| name == "none")),
        "networks",
    )?;
    check(host.ipc_mode.as_deref() == Some("private"), "ipc")?;
    check(
        host.cgroupns_mode == Some(HostConfigCgroupnsModeEnum::PRIVATE),
        "cgroupns",
    )?;
    check(unset(host.pid_mode.as_ref()), "pid")?;
    check(unset(host.uts_mode.as_ref()), "uts")?;
    check(unset(host.userns_mode.as_ref()), "userns")
}

fn resources(host: &HostConfig, cpuset: Option<&str>) -> Result<(), Drift> {
    check(host.memory == Some(HELPER_MEMORY_BYTES), "memory")?;
    check(host.memory_swap == Some(HELPER_MEMORY_BYTES), "swap")?;
    check(host.nano_cpus == Some(HELPER_NANO_CPUS), "cpus")?;
    check(host.pids_limit == Some(HELPER_PIDS), "pids")?;
    check(
        host.cpuset_cpus.as_deref().filter(|set| !set.is_empty()) == cpuset,
        "cpuset",
    )?;
    check(
        host.ulimits.as_deref() == Some(std::slice::from_ref(&nofile())),
        "ulimits",
    )?;
    check(
        host.restart_policy.as_ref().and_then(|policy| policy.name)
            == Some(RestartPolicyNameEnum::NO),
        "restart",
    )?;
    check(
        host.log_config.as_ref().and_then(|log| log.typ.as_deref()) == Some("none"),
        "log-driver",
    )
}

fn storage(host: &HostConfig, container: &ContainerInspectResponse) -> Result<(), Drift> {
    check(empty(host.devices.as_ref()), "devices")?;
    check(empty(host.device_requests.as_ref()), "device-requests")?;
    check(empty(host.device_cgroup_rules.as_ref()), "device-rules")?;
    check(
        host.port_bindings.as_ref().is_none_or(HashMap::is_empty),
        "ports",
    )?;
    check(host.publish_all_ports != Some(true), "publish-all")?;
    check(empty(host.binds.as_ref()), "binds")?;
    check(empty(host.mounts.as_ref()), "host-mounts")?;
    check(empty(host.volumes_from.as_ref()), "volumes-from")?;
    check(empty(container.mounts.as_ref()), "mounts")?;
    check(host.tmpfs.as_ref() == Some(&tmpfs()), "tmpfs")
}

/// Removes one helper; an already absent one counts as removed.
///
/// # Errors
/// [`DockerError`] when it may still exist.
pub async fn remove(docker: &dyn DockerEngine, id: &str) -> Result<(), DockerError> {
    match docker.remove_container(id).await {
        Ok(()) | Err(DockerError::NotFound) => Ok(()),
        Err(error) => Err(error),
    }
}

/// Removes every helper of one Team, or of the whole Space; an already absent one counts as removed.
///
/// # Errors
/// [`DockerError`] when one may still exist.
pub async fn remove_all(
    docker: &dyn DockerEngine,
    space_id: &str,
    team_id: Option<&str>,
) -> Result<(), DockerError> {
    let owned = labels(space_id, team_id);
    let filter: Vec<(&str, &str)> = owned
        .iter()
        .map(|(name, value)| (*name, value.as_str()))
        .collect();
    for container in docker.list_containers(&filter).await? {
        let id = container.id.ok_or(DockerError::Unavailable)?;
        remove(docker, &id).await?;
    }
    Ok(())
}

#[cfg(test)]
mod tests;
