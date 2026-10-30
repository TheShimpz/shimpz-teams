use std::collections::HashMap;

use bollard::models::{
    ContainerConfig, ContainerCreateBody, ContainerInspectResponse, ContainerState, DeviceMapping,
    DeviceRequest, EndpointResource, EndpointSettings, HostConfig, HostConfigCgroupnsModeEnum,
    HostConfigLogConfig, Mount, MountPoint, NetworkInspect, NetworkSettings, PortBinding,
    ResourcesUlimits, RestartPolicy, RestartPolicyNameEnum,
};

use super::{
    Drift, MEMORY, NETWORK, NOFILE, Requested, SECCOMP, after_start, before_start, body, tmpfs,
};

pub(crate) const IMAGE_ID: &str = "sha256:browser-image";

pub(crate) fn requested() -> Requested {
    Requested {
        image: format!("localhost/shimpz-browser@sha256:{}", "a".repeat(64)),
        name: "shimpz-local-0123456789ab-team_1-browser".to_owned(),
        labels: HashMap::from([
            ("com.shimpz.local.kind".to_owned(), "browser".to_owned()),
            ("com.shimpz.local.team-id".to_owned(), "team_1".to_owned()),
        ]),
        env: vec![
            "BROWSER_PROXY=172.30.0.2".to_owned(),
            "BROWSER_LANG=en".to_owned(),
            "BROWSER_MODE=agent".to_owned(),
        ],
        cpuset: "0-3".to_owned(),
    }
}

/// What Docker answers for a container created from `created`, as it reports a created container's fields.
pub(crate) fn inspected(
    created: &ContainerCreateBody,
    id: &str,
    name: &str,
    image_id: &str,
    address: Option<&str>,
) -> ContainerInspectResponse {
    let mut env = vec!["PATH=/usr/bin".to_owned()];
    env.extend(created.env.clone().unwrap_or_default());
    ContainerInspectResponse {
        id: Some(id.to_owned()),
        name: Some(format!("/{name}")),
        image: Some(image_id.to_owned()),
        state: Some(ContainerState {
            running: Some(address.is_some()),
            ..ContainerState::default()
        }),
        host_config: created.host_config.clone(),
        mounts: Some(Vec::new()),
        config: Some(ContainerConfig {
            image: created.image.clone(),
            user: created.user.clone(),
            env: Some(env),
            labels: created.labels.clone(),
            ..ContainerConfig::default()
        }),
        network_settings: Some(NetworkSettings {
            networks: Some(HashMap::from([(
                NETWORK.to_owned(),
                EndpointSettings {
                    ip_address: address.map(str::to_owned),
                    ..EndpointSettings::default()
                },
            )])),
            ..NetworkSettings::default()
        }),
        ..ContainerInspectResponse::default()
    }
}

fn created() -> ContainerInspectResponse {
    let wanted = requested();
    inspected(&body(&wanted), "browser-1", &wanted.name, IMAGE_ID, None)
}

fn host(container: &mut ContainerInspectResponse) -> &mut HostConfig {
    container.host_config.as_mut().expect("a host config")
}

#[test]
fn the_body_asks_for_the_whole_hardened_envelope() {
    let wanted = requested();
    let created = body(&wanted);
    let host = created.host_config.as_ref().expect("a host config");
    assert_eq!(created.user.as_deref(), Some("10031:10031"));
    assert_eq!(host.network_mode.as_deref(), Some(NETWORK));
    assert_eq!(host.cap_drop.as_deref(), Some(&["ALL".to_owned()][..]));
    let security = host.security_opt.as_deref().expect("security options");
    assert_eq!(security[0], "no-new-privileges:true");
    assert!(security[1].starts_with("seccomp={"));
    assert!(security[1].contains("\"unshare\""));
}

#[test]
fn the_body_asks_for_the_limits_and_no_log_or_restart() {
    let created = body(&requested());
    let host = created.host_config.as_ref().expect("a host config");
    assert_eq!(
        host.log_config.as_ref().and_then(|log| log.typ.as_deref()),
        Some("none")
    );
    assert_eq!(host.cpuset_cpus.as_deref(), Some("0-3"));
    assert_eq!(host.memory_swap, Some(MEMORY));
    assert_eq!(
        host.restart_policy.as_ref().and_then(|policy| policy.name),
        Some(RestartPolicyNameEnum::NO)
    );
}

#[test]
fn every_writable_path_is_a_bounded_non_executable_tmpfs() {
    for value in tmpfs().values() {
        assert!(value.starts_with("rw,noexec,nosuid,nodev,"));
        assert!(value.ends_with(",uid=10031,gid=10031,mode=0700"));
    }
    assert_eq!(tmpfs().len(), 3);
    let wanted = requested();
    assert_eq!(
        before_start(&created_from(&body(&wanted)), &wanted, IMAGE_ID),
        Ok(())
    );
}

fn created_from(created: &ContainerCreateBody) -> ContainerInspectResponse {
    inspected(created, "browser-1", &requested().name, IMAGE_ID, None)
}

type Change = fn(&mut ContainerInspectResponse);

/// One drift per field of the envelope, each with the field it must be refused for.
fn drifts() -> Vec<(&'static str, Change)> {
    let mut all = Vec::new();
    all.extend(drifts_0());
    all.extend(drifts_1());
    all.extend(drifts_2());
    all.extend(drifts_3());
    all
}

fn drifts_0() -> Vec<(&'static str, Change)> {
    vec![
        ("name", |c| c.name = Some("/other".to_owned())),
        ("image", |c| c.image = Some("sha256:other".to_owned())),
        ("image-reference", |c| {
            c.config.as_mut().expect("config").image = Some("evil@sha256:0".to_owned());
        }),
        ("labels", |c| {
            c.config.as_mut().expect("config").labels = Some(HashMap::new());
        }),
        ("user", |c| {
            c.config.as_mut().expect("config").user = Some("0:0".to_owned());
        }),
        ("env", |c| {
            c.config
                .as_mut()
                .expect("config")
                .env
                .as_mut()
                .expect("env")
                .push("BROWSER_PROXY=10.0.0.1".to_owned());
        }),
        ("read-only", |c| host(c).readonly_rootfs = Some(false)),
        ("cap-drop", |c| host(c).cap_drop = Some(Vec::new())),
        ("cap-add", |c| {
            host(c).cap_add = Some(vec!["NET_ADMIN".to_owned()]);
        }),
        ("privileged", |c| host(c).privileged = Some(true)),
        ("no-new-privileges", |c| {
            host(c).security_opt = Some(vec!["no-new-privileges:true".to_owned()]);
        }),
    ]
}

fn drifts_1() -> Vec<(&'static str, Change)> {
    vec![
        ("no-new-privileges", |c| {
            host(c).security_opt.as_mut().expect("options")[0] = "apparmor=unconfined".to_owned();
        }),
        ("init", |c| host(c).init = Some(false)),
        ("group-add", |c| {
            host(c).group_add = Some(vec!["0".to_owned()]);
        }),
        ("sysctls", |c| {
            host(c).sysctls = Some(HashMap::from([(
                "net.ipv4.ip_forward".to_owned(),
                "1".to_owned(),
            )]));
        }),
        ("ipc", |c| host(c).ipc_mode = Some("host".to_owned())),
        ("cgroupns", |c| {
            host(c).cgroupns_mode = Some(HostConfigCgroupnsModeEnum::HOST);
        }),
        ("network-mode", |c| {
            host(c).network_mode = Some("host".to_owned());
        }),
        ("pid", |c| host(c).pid_mode = Some("host".to_owned())),
        ("uts", |c| host(c).uts_mode = Some("host".to_owned())),
        ("userns", |c| host(c).userns_mode = Some("host".to_owned())),
        ("memory", |c| host(c).memory = Some(1)),
    ]
}

fn drifts_2() -> Vec<(&'static str, Change)> {
    vec![
        ("swap", |c| host(c).memory_swap = Some(-1)),
        ("cpus", |c| host(c).nano_cpus = Some(4_000_000_000)),
        ("cpuset", |c| host(c).cpuset_cpus = Some("0-63".to_owned())),
        ("pids", |c| host(c).pids_limit = Some(-1)),
        ("shm", |c| host(c).shm_size = Some(64)),
        ("ulimits", |c| {
            host(c).ulimits = Some(vec![ResourcesUlimits {
                name: Some("nofile".to_owned()),
                soft: Some(NOFILE),
                hard: Some(1 << 20),
            }]);
        }),
        ("ulimits", |c| host(c).ulimits = None),
        ("restart", |c| {
            host(c).restart_policy = Some(RestartPolicy {
                name: Some(RestartPolicyNameEnum::ALWAYS),
                maximum_retry_count: None,
            });
        }),
        ("log-driver", |c| {
            host(c).log_config = Some(HostConfigLogConfig {
                typ: Some("json-file".to_owned()),
                config: None,
            });
        }),
        ("devices", |c| {
            host(c).devices = Some(vec![DeviceMapping::default()]);
        }),
        ("device-requests", |c| {
            host(c).device_requests = Some(vec![DeviceRequest::default()]);
        }),
    ]
}

fn drifts_3() -> Vec<(&'static str, Change)> {
    vec![
        ("device-rules", |c| {
            host(c).device_cgroup_rules = Some(vec!["a *:* rwm".to_owned()]);
        }),
        ("ports", |c| {
            host(c).port_bindings = Some(HashMap::from([(
                "6901/tcp".to_owned(),
                Some(vec![PortBinding::default()]),
            )]));
        }),
        ("publish-all", |c| host(c).publish_all_ports = Some(true)),
        ("binds", |c| {
            host(c).binds = Some(vec!["/:/host".to_owned()]);
        }),
        ("host-mounts", |c| {
            host(c).mounts = Some(vec![Mount::default()]);
        }),
        ("volumes-from", |c| {
            host(c).volumes_from = Some(vec!["team".to_owned()]);
        }),
        ("mounts", |c| c.mounts = Some(vec![MountPoint::default()])),
        ("tmpfs", |c| {
            host(c).tmpfs = Some(HashMap::from([("/tmp".to_owned(), "rw".to_owned())]));
        }),
        ("networks", |c| {
            c.network_settings
                .as_mut()
                .and_then(|settings| settings.networks.as_mut())
                .expect("networks")
                .insert("bridge".to_owned(), EndpointSettings::default());
        }),
        ("networks", |c| c.network_settings = None),
    ]
}

#[test]
fn browser_envelope_inspected_before_start_refuses_each_drift() {
    let wanted = requested();
    for (field, change) in drifts() {
        let mut container = created();
        change(&mut container);
        assert_eq!(
            before_start(&container, &wanted, IMAGE_ID),
            Err(Drift(field)),
            "{field}"
        );
    }
}

#[test]
fn browser_seccomp_must_equal_approved_policy() {
    let wanted = requested();
    let other_policy = SECCOMP.replacen("SCMP_ACT_ERRNO", "SCMP_ACT_ALLOW", 1);
    for policy in [
        "seccomp=unconfined".to_owned(),
        "seccomp=".to_owned(),
        format!("seccomp={other_policy}"),
        format!("seccomp={SECCOMP} "),
        SECCOMP.to_owned(),
    ] {
        let mut container = created();
        host(&mut container).security_opt = Some(vec!["no-new-privileges".to_owned(), policy]);
        assert_eq!(
            before_start(&container, &wanted, IMAGE_ID),
            Err(Drift("seccomp"))
        );
    }
}

fn started(wanted: &Requested) -> ContainerInspectResponse {
    inspected(
        &body(wanted),
        "browser-1",
        &wanted.name,
        IMAGE_ID,
        Some("172.30.0.9"),
    )
}

fn network(members: &[&str]) -> NetworkInspect {
    NetworkInspect {
        containers: Some(
            members
                .iter()
                .map(|member| ((*member).to_owned(), EndpointResource::default()))
                .collect(),
        ),
        ..NetworkInspect::default()
    }
}

fn after_start_drifts() -> Vec<(&'static str, Change)> {
    vec![
        ("id", |c| c.id = Some("browser-2".to_owned())),
        ("image", |c| c.image = Some("sha256:other".to_owned())),
        ("labels", |c| {
            c.config.as_mut().expect("config").labels = None;
        }),
        ("running", |c| c.state = Some(ContainerState::default())),
        ("address", |c| {
            c.network_settings
                .as_mut()
                .and_then(|settings| settings.networks.as_mut())
                .and_then(|networks| networks.get_mut(NETWORK))
                .expect("endpoint")
                .ip_address = Some(String::new());
        }),
        ("networks", |c| {
            c.network_settings
                .as_mut()
                .and_then(|settings| settings.networks.as_mut())
                .expect("networks")
                .insert("other".to_owned(), EndpointSettings::default());
        }),
    ]
}

#[test]
fn browser_post_start_identity_and_network() {
    let wanted = requested();
    let both = network(&["browser-1", "egress"]);
    assert_eq!(
        after_start(
            &started(&wanted),
            "browser-1",
            &wanted,
            IMAGE_ID,
            &both,
            "egress"
        ),
        Ok("172.30.0.9".to_owned())
    );
    for (field, change) in after_start_drifts() {
        let mut container = started(&wanted);
        change(&mut container);
        assert_eq!(
            after_start(&container, "browser-1", &wanted, IMAGE_ID, &both, "egress"),
            Err(Drift(field)),
            "{field}"
        );
    }
    for members in [
        &["browser-1"][..],
        &["browser-1", "egress", "stranger"][..],
        &["browser-1", "stranger"][..],
    ] {
        assert_eq!(
            after_start(
                &started(&wanted),
                "browser-1",
                &wanted,
                IMAGE_ID,
                &network(members),
                "egress"
            ),
            Err(Drift("members"))
        );
    }
}
