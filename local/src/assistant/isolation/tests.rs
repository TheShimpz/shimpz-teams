use std::collections::HashMap;

use bollard::models::{
    ContainerConfig, ContainerCreateBody, ContainerInspectResponse, ContainerState, DeviceMapping,
    DeviceRequest, EndpointSettings, HostConfig, HostConfigCgroupnsModeEnum, HostConfigLogConfig,
    MountPoint, NetworkSettings, NetworkingConfig, PortBinding, ResourcesUlimits, RestartPolicy,
    RestartPolicyNameEnum,
};

use super::{
    Admitted, Drift, NANO_CPUS, Placement, admit, create_body, egress_environment_valid,
    is_digest_ref,
};
use crate::assistant::{ContainerSpec, Provenance};
use crate::docker::labels::{ASSISTANT, IMAGE, KIND, MANAGED, PROFILE, SPACE, Space, TEAM};

const NETWORK: &str = "team-network";
const CPUSET: &str = "0-1";

fn published(digit: char) -> String {
    format!(
        "ghcr.io/theshimpz/shimpz-assistant@sha256:{}",
        digit.to_string().repeat(64)
    )
}

fn spec() -> ContainerSpec {
    ContainerSpec {
        assistant_id: "notes".to_owned(),
        image: published('b'),
        provenance: Provenance::Published,
        allowed_hosts: Vec::new(),
        required_image_labels: Vec::new(),
        platform: None,
    }
}

fn space() -> Space {
    Space::new("local-space").expect("a Space")
}

fn placement(space: &Space) -> Placement<'_> {
    Placement {
        space,
        team_id: "team_1",
        network: NETWORK,
        cpuset: CPUSET,
    }
}

/// The inspection Docker answers for a container created from the profile, with the extra fields it reports.
fn inspected(space: &Space, spec: &ContainerSpec) -> ContainerInspectResponse {
    let body = create_body(placement(space), spec);
    let mut env = vec!["PATH=/usr/local/bin:/usr/bin:/bin".to_owned()];
    env.extend(body.env.clone().unwrap_or_default());
    let mut host = body.host_config.clone().expect("a host configuration");
    host.port_bindings = Some(HashMap::new());
    host.devices = Some(Vec::new());
    ContainerInspectResponse {
        id: Some("c".repeat(64)),
        name: Some(format!("/{}", space.container_name("team_1", "notes"))),
        image: Some(format!("sha256:{}", "a".repeat(64))),
        state: Some(ContainerState {
            running: Some(true),
            ..ContainerState::default()
        }),
        config: Some(ContainerConfig {
            image: body.image.clone(),
            user: body.user.clone(),
            env: Some(env),
            labels: body.labels.clone(),
            ..ContainerConfig::default()
        }),
        host_config: Some(host),
        mounts: Some(Vec::new()),
        network_settings: Some(NetworkSettings {
            networks: Some(HashMap::from([(
                NETWORK.to_owned(),
                EndpointSettings::default(),
            )])),
            ..NetworkSettings::default()
        }),
        ..ContainerInspectResponse::default()
    }
}

fn admitted(
    inspection: &ContainerInspectResponse,
    spec: &ContainerSpec,
) -> Result<Admitted, Drift> {
    let space = space();
    admit(inspection, placement(&space), spec)
}

/// The profile, written out independently of [`create_body`]: every other field stays Docker's default.
fn expected_host() -> HostConfig {
    HostConfig {
        network_mode: Some(NETWORK.to_owned()),
        readonly_rootfs: Some(true),
        cap_drop: Some(vec!["ALL".to_owned()]),
        security_opt: Some(vec!["no-new-privileges:true".to_owned()]),
        privileged: Some(false),
        auto_remove: Some(false),
        ipc_mode: Some("private".to_owned()),
        cgroupns_mode: Some(HostConfigCgroupnsModeEnum::PRIVATE),
        memory: Some(128 * 1024 * 1024),
        memory_swap: Some(128 * 1024 * 1024),
        nano_cpus: Some(250_000_000),
        cpuset_cpus: Some(CPUSET.to_owned()),
        pids_limit: Some(64),
        tmpfs: Some(HashMap::from([(
            "/tmp".to_owned(),
            "size=256m,noexec,nosuid,nodev".to_owned(),
        )])),
        ulimits: Some(vec![ResourcesUlimits {
            name: Some("nofile".to_owned()),
            soft: Some(1024),
            hard: Some(1024),
        }]),
        restart_policy: Some(RestartPolicy {
            name: Some(RestartPolicyNameEnum::NO),
            maximum_retry_count: None,
        }),
        log_config: Some(HostConfigLogConfig {
            typ: Some("none".to_owned()),
            config: Some(HashMap::new()),
        }),
        ..HostConfig::default()
    }
}

#[test]
fn assistant_create_settings_exact() {
    let space = space();
    let labels: HashMap<String, String> = [
        (MANAGED, "1"),
        (PROFILE, "local-v1"),
        (SPACE, "local-space"),
        (KIND, "assistant"),
        (TEAM, "team_1"),
        (ASSISTANT, "notes"),
        (IMAGE, published('b').as_str()),
    ]
    .into_iter()
    .map(|(name, value)| (name.to_owned(), value.to_owned()))
    .collect();
    let expected = ContainerCreateBody {
        image: Some(published('b')),
        user: Some("10001:10001".to_owned()),
        labels: Some(labels),
        env: Some(vec![
            "SHIMPZ_ASSISTANT_ID=notes".to_owned(),
            "SHIMPZ_TEAM_ID=team_1".to_owned(),
            "PYTHONDONTWRITEBYTECODE=1".to_owned(),
        ]),
        host_config: Some(expected_host()),
        networking_config: Some(NetworkingConfig {
            endpoints_config: Some(HashMap::from([(
                NETWORK.to_owned(),
                EndpointSettings::default(),
            )])),
        }),
        ..ContainerCreateBody::default()
    };
    // No command, entrypoint, mount, bind, device, port, capability, or DNS setting: exactly these fields.
    assert_eq!(create_body(placement(&space), &spec()), expected);
}

#[test]
fn the_profile_admits_exactly_its_own_container() {
    let space = space();
    let spec = spec();
    let admitted = admitted(&inspected(&space, &spec), &spec).expect("admitted");
    assert_eq!(admitted.id, "c".repeat(64));
    assert_eq!(admitted.image, published('b'));
    assert!(admitted.running);
    assert_eq!(admitted.environment.len(), 4);
    // Docker's other rendering of the one security option, an empty restart policy name, and absent optional lists
    // are the same profile.
    let mut other = inspected(&space, &spec);
    let host = other.host_config.as_mut().expect("host");
    host.security_opt = Some(vec!["no-new-privileges".to_owned()]);
    host.restart_policy.as_mut().expect("restart").name = Some(RestartPolicyNameEnum::EMPTY);
    host.port_bindings = None;
    host.devices = None;
    host.cap_add = Some(Vec::new());
    host.sysctls = Some(HashMap::new());
    other.mounts = None;
    assert!(admitted_ok(&other, &spec));
}

/// The post-create inspection: the profile, then the workload's environment.
fn post_create(inspection: &ContainerInspectResponse, spec: &ContainerSpec) -> Result<(), Drift> {
    let admitted = admitted(inspection, spec)?;
    if egress_environment_valid(&admitted.environment) {
        Ok(())
    } else {
        Err(Drift)
    }
}

fn admitted_ok(inspection: &ContainerInspectResponse, spec: &ContainerSpec) -> bool {
    admitted(inspection, spec).is_ok()
}

type Change = fn(&mut ContainerInspectResponse);

fn host(inspection: &mut ContainerInspectResponse) -> &mut HostConfig {
    inspection.host_config.as_mut().expect("host")
}

fn config(inspection: &mut ContainerInspectResponse) -> &mut ContainerConfig {
    inspection.config.as_mut().expect("config")
}

fn label(inspection: &mut ContainerInspectResponse, name: &str, value: Option<&str>) {
    let labels = config(inspection).labels.as_mut().expect("labels");
    match value {
        Some(value) => labels.insert(name.to_owned(), value.to_owned()),
        None => labels.remove(name),
    };
}

fn env(inspection: &mut ContainerInspectResponse, entry: &str) {
    config(inspection)
        .env
        .as_mut()
        .expect("env")
        .push(entry.to_owned());
}

/// One case per field the profile admits (`isolation.py` `inspect_profile`).
const DRIFTS: &[(&str, Change)] = &[
    ("environment absent", |i| config(i).env = None),
    ("environment entry without =", |i| env(i, "BROKEN")),
    ("environment name repeated", |i| {
        env(i, "SHIMPZ_TEAM_ID=team_2");
    }),
    ("environment name empty", |i| env(i, "=value")),
    ("HTTPS_PROXY", |i| {
        env(i, "HTTPS_PROXY=http://proxy.example:1");
    }),
    ("https_proxy", |i| {
        env(i, "https_proxy=http://proxy.example:1");
    }),
    ("HTTP_PROXY", |i| env(i, "HTTP_PROXY=x")),
    ("http_proxy", |i| env(i, "http_proxy=x")),
    ("ALL_PROXY", |i| env(i, "ALL_PROXY=x")),
    ("all_proxy", |i| env(i, "all_proxy=x")),
    ("NO_PROXY", |i| env(i, "NO_PROXY=x")),
    ("no_proxy", |i| env(i, "no_proxy=x")),
    ("labels absent", |i| config(i).labels = None),
    ("managed label", |i| label(i, MANAGED, Some("0"))),
    ("profile label", |i| label(i, PROFILE, None)),
    ("space label", |i| label(i, SPACE, Some("other-space"))),
    ("kind label", |i| label(i, KIND, Some("browser"))),
    ("team label", |i| label(i, TEAM, Some("team_2"))),
    ("assistant label", |i| label(i, ASSISTANT, Some("other"))),
    ("image label absent", |i| label(i, IMAGE, None)),
    ("image label foreign repository", |i| {
        label(
            i,
            IMAGE,
            Some(&format!(
                "evil.example/shimpz-assistant@sha256:{}",
                "b".repeat(64)
            )),
        );
    }),
    ("image label a tag", |i| {
        label(i, IMAGE, Some("ghcr.io/theshimpz/shimpz-assistant:latest"));
    }),
    ("image label unbound digest", |i| {
        label(i, IMAGE, Some(&published('0')));
    }),
    ("configured image differs from the label", |i| {
        config(i).image = Some(published('c'));
    }),
    ("name", |i| i.name = Some("/foreign-container".to_owned())),
    ("user", |i| config(i).user = Some("0:0".to_owned())),
    ("writable root", |i| host(i).readonly_rootfs = Some(false)),
    ("root unreported", |i| host(i).readonly_rootfs = None),
    ("capabilities kept", |i| {
        host(i).cap_drop = Some(vec!["NET_RAW".to_owned()]);
    }),
    ("capabilities not dropped", |i| host(i).cap_drop = None),
    ("capability added", |i| {
        host(i).cap_add = Some(vec!["NET_RAW".to_owned()]);
    }),
    ("new privileges", |i| {
        host(i).security_opt = Some(vec!["no-new-privileges:false".to_owned()]);
    }),
    ("unconfined", |i| {
        host(i).security_opt = Some(vec![
            "no-new-privileges:true".to_owned(),
            "apparmor=unconfined".to_owned(),
        ]);
    }),
    ("no security option", |i| host(i).security_opt = None),
    ("privileged", |i| host(i).privileged = Some(true)),
    ("privilege unreported", |i| host(i).privileged = None),
    ("network mode", |i| {
        host(i).network_mode = Some("host".to_owned());
    }),
    ("memory", |i| host(i).memory = Some(256 * 1024 * 1024)),
    ("swap", |i| host(i).memory_swap = Some(-1)),
    ("cpus", |i| host(i).nano_cpus = Some(NANO_CPUS * 4)),
    ("cpuset", |i| host(i).cpuset_cpus = Some("0-3".to_owned())),
    ("pids", |i| host(i).pids_limit = Some(-1)),
    ("ipc", |i| host(i).ipc_mode = Some("host".to_owned())),
    ("cgroupns", |i| {
        host(i).cgroupns_mode = Some(HostConfigCgroupnsModeEnum::HOST);
    }),
    ("tmpfs size only", |i| set_tmpfs(i, "size=256m")),
    ("tmpfs exec", |i| {
        set_tmpfs(i, "size=256m,exec,nosuid,nodev");
    }),
    ("tmpfs suid", |i| {
        set_tmpfs(i, "size=256m,noexec,suid,nodev");
    }),
    ("tmpfs devices", |i| set_tmpfs(i, "size=256m,noexec")),
    ("tmpfs elsewhere", |i| {
        host(i).tmpfs = Some(HashMap::from([(
            "/run".to_owned(),
            "size=256m,noexec,nosuid,nodev".to_owned(),
        )]));
    }),
    ("nofile raised", |i| {
        host(i).ulimits = Some(vec![ResourcesUlimits {
            name: Some("nofile".to_owned()),
            soft: Some(65536),
            hard: Some(65536),
        }]);
    }),
    ("sysctl", |i| {
        host(i).sysctls = Some(HashMap::from([(
            "net.ipv4.ip_forward".to_owned(),
            "1".to_owned(),
        )]));
    }),
    ("auto remove", |i| host(i).auto_remove = Some(true)),
    ("restart", |i| {
        host(i).restart_policy.as_mut().expect("restart").name =
            Some(RestartPolicyNameEnum::ALWAYS);
    }),
    ("log driver", |i| {
        host(i).log_config.as_mut().expect("log").typ = Some("json-file".to_owned());
    }),
    ("log options", |i| {
        host(i).log_config.as_mut().expect("log").config =
            Some(HashMap::from([("tag".to_owned(), "x".to_owned())]));
    }),
    ("log options unreported", |i| {
        host(i).log_config.as_mut().expect("log").config = None;
    }),
    ("port binding", |i| {
        host(i).port_bindings = Some(HashMap::from([(
            "80/tcp".to_owned(),
            Some(vec![PortBinding::default()]),
        )]));
    }),
    ("bind", |i| host(i).binds = Some(vec!["/:/host".to_owned()])),
    ("device", |i| {
        host(i).devices = Some(vec![DeviceMapping::default()]);
    }),
    ("device request", |i| {
        host(i).device_requests = Some(vec![DeviceRequest::default()]);
    }),
    ("mount", |i| i.mounts = Some(vec![MountPoint::default()])),
    ("second network", |i| {
        i.network_settings
            .as_mut()
            .and_then(|settings| settings.networks.as_mut())
            .expect("networks")
            .insert("bridge".to_owned(), EndpointSettings::default());
    }),
    ("other network", |i| {
        i.network_settings.as_mut().expect("settings").networks = Some(HashMap::from([(
            "bridge".to_owned(),
            EndpointSettings::default(),
        )]));
    }),
    ("no network", |i| i.network_settings = None),
    ("state unreported", |i| i.state = None),
];

fn set_tmpfs(inspection: &mut ContainerInspectResponse, options: &str) {
    host(inspection).tmpfs = Some(HashMap::from([("/tmp".to_owned(), options.to_owned())]));
}

#[test]
fn assistant_post_create_inspection_refuses_each_drift() {
    let space = space();
    let spec = spec();
    for (case, change) in DRIFTS {
        let mut inspection = inspected(&space, &spec);
        change(&mut inspection);
        assert_eq!(post_create(&inspection, &spec), Err(Drift), "{case}");
    }
}

#[test]
fn a_published_lineage_admits_an_older_digest_of_its_repository_only() {
    let space = space();
    let spec = spec();
    // An outdated container of the same repository is admitted (and then replaced, never refused).
    let mut outdated = inspected(&space, &spec);
    label(&mut outdated, IMAGE, Some(&published('a')));
    config(&mut outdated).image = Some(published('a'));
    assert_eq!(
        admitted(&outdated, &spec).map(|admitted| admitted.image),
        Ok(published('a'))
    );
    assert!(is_digest_ref(&published('a')));
    assert!(is_digest_ref(&format!(
        "registry.example:5000/a/b@sha256:{}",
        "f".repeat(64)
    )));
    assert!(!is_digest_ref(&published('0')));
    assert!(!is_digest_ref("ghcr.io/theshimpz/shimpz-assistant:latest"));
    assert!(!is_digest_ref(&format!(
        "UPPER/repo@sha256:{}",
        "a".repeat(64)
    )));
}

#[test]
fn local_image_identity_exact() {
    let space = space();
    let bound = format!("sha256:{}", "d".repeat(64));
    let local = ContainerSpec {
        image: bound.clone(),
        provenance: Provenance::Local,
        ..spec()
    };
    let inspection = inspected(&space, &local);
    assert_eq!(
        admitted(&inspection, &local).map(|admitted| admitted.image),
        Ok(bound)
    );
    // Only the exact bound image id: another id, or a published reference, is drift.
    for other in [format!("sha256:{}", "e".repeat(64)), published('d')] {
        let mut changed = inspection.clone();
        label(&mut changed, IMAGE, Some(&other));
        config(&mut changed).image = Some(other.clone());
        assert_eq!(admitted(&changed, &local), Err(Drift), "{other}");
    }
}

#[test]
fn only_a_workload_without_any_proxy_variable_passes_egress() {
    let pair = |name: &str| vec![(name.to_owned(), "x".to_owned())];
    assert!(egress_environment_valid(&pair("PYTHONDONTWRITEBYTECODE")));
    for name in [
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "no_proxy",
    ] {
        assert!(!egress_environment_valid(&pair(name)), "{name}");
    }
}
