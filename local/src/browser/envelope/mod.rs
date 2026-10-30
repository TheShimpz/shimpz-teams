//! The Browser container's complete envelope (ADR-0117 §3.3 and §4): the creation body, and its inspection before
//! Chromium starts and again once it runs.
//!
//! Before start, every effective field is compared with what was asked: the exact approved seccomp policy (the bytes
//! of the image-pinned `browser/image/seccomp.json`, not any `seccomp=` prefix), no new privileges, the user, the
//! labels, a read-only root, every capability dropped and none added, not privileged, an init, private IPC and cgroup
//! namespaces, the memory, swap, CPU, cpuset, pids, and shared memory limits, the one `nofile` ulimit, the `none` log
//! driver, no device, device request, port, bind, volume, or mount, the exact tmpfs set, and membership of the Browser
//! network alone. After start: the same container, image, name, and labels, running, its one network with an address,
//! and that network's only other member the egress. Any drift refuses the Browser, which is then removed.

use std::collections::HashMap;

use bollard::models::{
    ContainerCreateBody, ContainerInspectResponse, HostConfig, HostConfigCgroupnsModeEnum,
    HostConfigLogConfig, NetworkInspect, ResourcesUlimits, RestartPolicy, RestartPolicyNameEnum,
};

/// The static Browser network, shared by the Space's one Browser and the egress.
pub const NETWORK: &str = "shimpz-space-browser";
/// The user every Browser process and exec runs as.
pub const USER: &str = "10031:10031";
/// The memory limit, which is also the memory and swap limit.
pub const MEMORY: i64 = 2 * 1024 * 1024 * 1024;
/// One CPU.
pub const NANO_CPUS: i64 = 1_000_000_000;
/// The process limit.
pub const PIDS: i64 = 512;
/// The open-file limit, soft and hard.
pub const NOFILE: i64 = 4096;
/// `/dev/shm`.
pub const SHM: i64 = 256 * 1024 * 1024;
/// The approved seccomp policy, pinned in Team's binary from the Browser image's source.
pub const SECCOMP: &str = include_str!("../../../../browser/image/seccomp.json");
const OWNER: &str = "uid=10031,gid=10031,mode=0700";

/// Every writable path of the Browser, none of which executes (ADR-0117 §3.3).
#[must_use]
pub fn tmpfs() -> HashMap<String, String> {
    [
        ("/tmp", "size=64m,nr_inodes=4096"),
        ("/profile", "size=256m,nr_inodes=16384"),
        ("/downloads", "size=256m,nr_inodes=256"),
    ]
    .into_iter()
    .map(|(path, size)| {
        (
            path.to_owned(),
            format!("rw,noexec,nosuid,nodev,{size},{OWNER}"),
        )
    })
    .collect()
}

/// What one Browser is created with.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Requested {
    /// The exact Browser image reference.
    pub image: String,
    /// The container's name.
    pub name: String,
    /// Its complete label set.
    pub labels: HashMap<String, String>,
    /// Its environment, `NAME=value` each.
    pub env: Vec<String>,
    /// The CPUs it may run on.
    pub cpuset: String,
}

/// The creation body for `requested`.
#[must_use]
pub fn body(requested: &Requested) -> ContainerCreateBody {
    ContainerCreateBody {
        image: Some(requested.image.clone()),
        user: Some(USER.to_owned()),
        env: Some(requested.env.clone()),
        labels: Some(requested.labels.clone()),
        host_config: Some(HostConfig {
            network_mode: Some(NETWORK.to_owned()),
            readonly_rootfs: Some(true),
            cap_drop: Some(vec!["ALL".to_owned()]),
            security_opt: Some(vec![
                "no-new-privileges:true".to_owned(),
                format!("seccomp={SECCOMP}"),
            ]),
            privileged: Some(false),
            init: Some(true),
            ipc_mode: Some("private".to_owned()),
            cgroupns_mode: Some(HostConfigCgroupnsModeEnum::PRIVATE),
            memory: Some(MEMORY),
            memory_swap: Some(MEMORY),
            nano_cpus: Some(NANO_CPUS),
            cpuset_cpus: Some(requested.cpuset.clone()),
            pids_limit: Some(PIDS),
            shm_size: Some(SHM),
            tmpfs: Some(tmpfs()),
            ulimits: Some(vec![ResourcesUlimits {
                name: Some("nofile".to_owned()),
                soft: Some(NOFILE),
                hard: Some(NOFILE),
            }]),
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

fn labelled(actual: Option<&HashMap<String, String>>, wanted: &HashMap<String, String>) -> bool {
    actual.is_some_and(|actual| {
        wanted
            .iter()
            .all(|(name, value)| actual.get(name) == Some(value))
    })
}

/// The created container, not yet started, against its complete envelope; `image_id` is the image Docker resolved
/// the reference to.
///
/// # Errors
/// [`Drift`] naming the first field that differs.
pub fn before_start(
    container: &ContainerInspectResponse,
    requested: &Requested,
    image_id: &str,
) -> Result<(), Drift> {
    identity(container, requested, image_id)?;
    let config = container.config.as_ref().ok_or(Drift("config"))?;
    check(config.user.as_deref() == Some(USER), "user")?;
    let env = config.env.as_deref().unwrap_or_default();
    check(
        requested.env.iter().all(|wanted| {
            let name = wanted
                .split_once('=')
                .map_or(wanted.as_str(), |(name, _)| name);
            env.iter()
                .filter(|entry| {
                    entry
                        .split_once('=')
                        .is_some_and(|(found, _)| found == name)
                })
                .eq(std::iter::once(wanted))
        }),
        "env",
    )?;
    let host = container.host_config.as_ref().ok_or(Drift("host"))?;
    privileges(host)?;
    namespaces(host)?;
    resources(host, &requested.cpuset)?;
    storage(host, container)?;
    networks(container, false).map(drop)
}

fn identity(
    container: &ContainerInspectResponse,
    requested: &Requested,
    image_id: &str,
) -> Result<(), Drift> {
    check(
        container.name.as_deref() == Some(&format!("/{}", requested.name)),
        "name",
    )?;
    check(container.image.as_deref() == Some(image_id), "image")?;
    let config = container.config.as_ref().ok_or(Drift("config"))?;
    check(
        config.image.as_deref() == Some(requested.image.as_str()),
        "image-reference",
    )?;
    check(
        labelled(config.labels.as_ref(), &requested.labels),
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
        security.len() == 2
            && (security[0] == "no-new-privileges" || security[0] == "no-new-privileges:true"),
        "no-new-privileges",
    )?;
    // The exact approved policy: a different policy, or none, is drift.
    check(
        security[1].strip_prefix("seccomp=") == Some(SECCOMP),
        "seccomp",
    )?;
    check(host.init == Some(true), "init")?;
    check(empty(host.group_add.as_ref()), "group-add")?;
    check(
        host.sysctls.as_ref().is_none_or(HashMap::is_empty),
        "sysctls",
    )
}

fn namespaces(host: &HostConfig) -> Result<(), Drift> {
    check(host.ipc_mode.as_deref() == Some("private"), "ipc")?;
    check(
        host.cgroupns_mode == Some(HostConfigCgroupnsModeEnum::PRIVATE),
        "cgroupns",
    )?;
    check(
        host.network_mode.as_deref() == Some(NETWORK),
        "network-mode",
    )?;
    check(unset(host.pid_mode.as_ref()), "pid")?;
    check(unset(host.uts_mode.as_ref()), "uts")?;
    check(unset(host.userns_mode.as_ref()), "userns")
}

fn resources(host: &HostConfig, cpuset: &str) -> Result<(), Drift> {
    check(host.memory == Some(MEMORY), "memory")?;
    check(host.memory_swap == Some(MEMORY), "swap")?;
    check(host.nano_cpus == Some(NANO_CPUS), "cpus")?;
    check(host.cpuset_cpus.as_deref() == Some(cpuset), "cpuset")?;
    check(host.pids_limit == Some(PIDS), "pids")?;
    check(host.shm_size == Some(SHM), "shm")?;
    let nofile = ResourcesUlimits {
        name: Some("nofile".to_owned()),
        soft: Some(NOFILE),
        hard: Some(NOFILE),
    };
    check(
        host.ulimits.as_deref() == Some(std::slice::from_ref(&nofile)),
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

/// The container's networks are exactly the Browser network; once `started`, its address on it.
///
/// # Errors
/// [`Drift`] when it belongs to any other network, or a started one has no address.
pub fn networks(
    container: &ContainerInspectResponse,
    started: bool,
) -> Result<Option<String>, Drift> {
    let networks = container
        .network_settings
        .as_ref()
        .and_then(|settings| settings.networks.as_ref())
        .ok_or(Drift("networks"))?;
    check(
        networks.len() == 1 && networks.contains_key(NETWORK),
        "networks",
    )?;
    if !started {
        return Ok(None);
    }
    let address = networks
        .get(NETWORK)
        .and_then(|endpoint| endpoint.ip_address.clone())
        .filter(|address| !address.is_empty())
        .ok_or(Drift("address"))?;
    Ok(Some(address))
}

/// A container's address on the Browser network, whatever other networks it has (the egress has its outbound one).
#[must_use]
pub fn networks_address(container: &ContainerInspectResponse) -> Option<String> {
    container
        .network_settings
        .as_ref()?
        .networks
        .as_ref()?
        .get(NETWORK)?
        .ip_address
        .clone()
        .filter(|address| !address.is_empty())
}

/// The started container against the one created: the same container, image, name, and labels, running, its one
/// network with an address, and that network's members exactly it and the egress. Its address on the network.
///
/// # Errors
/// [`Drift`] naming the first field that differs.
pub fn after_start(
    container: &ContainerInspectResponse,
    id: &str,
    requested: &Requested,
    image_id: &str,
    network: &NetworkInspect,
    egress_id: &str,
) -> Result<String, Drift> {
    check(container.id.as_deref() == Some(id), "id")?;
    identity(container, requested, image_id)?;
    check(
        container.state.as_ref().and_then(|state| state.running) == Some(true),
        "running",
    )?;
    let address = networks(container, true)?.ok_or(Drift("address"))?;
    let members = network.containers.as_ref().ok_or(Drift("members"))?;
    check(
        members.len() == 2 && members.contains_key(id) && members.contains_key(egress_id),
        "members",
    )?;
    Ok(address)
}

#[cfg(test)]
mod tests;
