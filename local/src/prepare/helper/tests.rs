use std::collections::HashMap;

use bollard::models::{
    ContainerInspectResponse, DeviceMapping, EndpointSettings, HostConfig,
    HostConfigCgroupnsModeEnum, HostConfigLogConfig, Mount, MountPoint, NetworkSettings,
    PortBinding, ResourcesUlimits, RestartPolicy, RestartPolicyNameEnum,
};

use super::{Requested, body, inspect, labels, remove, remove_all};
use crate::docker::{DockerEngine, DockerError};
use crate::testing::FakeDocker;

const IMAGE: &str = "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";

fn requested(cpuset: Option<&str>) -> Requested {
    Requested {
        image: IMAGE.to_owned(),
        name: "shimpz-prepare-team_1-0a0b0c0d".to_owned(),
        labels: labels("space", Some("team_1"))
            .into_iter()
            .map(|(name, value)| (name.to_owned(), value))
            .collect(),
        cpuset: cpuset.map(str::to_owned),
    }
}

/// What a daemon answers for a helper created exactly as asked.
fn faithful(requested: &Requested) -> ContainerInspectResponse {
    let created = body(requested);
    ContainerInspectResponse {
        id: Some("helper".to_owned()),
        name: Some(format!("/{}", requested.name)),
        image: Some(requested.image.clone()),
        config: Some(bollard::models::ContainerConfig {
            image: created.image.clone(),
            user: created.user.clone(),
            entrypoint: created.entrypoint.clone(),
            cmd: None,
            labels: created.labels.clone().map(|mut labels| {
                labels.insert("org.shimpz.component".to_owned(), "team-local".to_owned());
                labels
            }),
            ..Default::default()
        }),
        host_config: created.host_config,
        mounts: Some(Vec::new()),
        network_settings: Some(NetworkSettings {
            networks: Some(HashMap::from([(
                "none".to_owned(),
                EndpointSettings::default(),
            )])),
            ..Default::default()
        }),
        ..Default::default()
    }
}

#[test]
fn the_helper_runs_the_controller_image_idle_as_nobody_with_its_owner_labels() {
    let created = body(&requested(Some("0-1")));
    assert_eq!(created.image.as_deref(), Some(IMAGE));
    assert_eq!(created.user.as_deref(), Some("65534:65534"));
    assert_eq!(
        created.entrypoint,
        Some(vec![
            "/usr/local/bin/shimpz-team".to_owned(),
            "prepare-idle".to_owned()
        ])
    );
    assert_eq!(created.env, Some(Vec::new()));
    let labels = created.labels.expect("labels");
    let expected = [
        ("com.shimpz.local.kind", "prepare"),
        ("com.shimpz.local.team-id", "team_1"),
        ("com.shimpz.local.space-id", "space"),
        ("com.shimpz.local.managed", "1"),
        ("com.shimpz.local.profile", "local-v1"),
    ];
    assert_eq!(labels.len(), expected.len());
    assert!(expected.iter().all(|(name, value)| labels[*name] == *value));
}

#[test]
fn the_helper_is_networkless_readonly_unprivileged_and_bounded() {
    let created = body(&requested(Some("0-1")));
    let host = created.host_config.expect("a host config");
    let expected = HostConfig {
        network_mode: Some("none".to_owned()),
        readonly_rootfs: Some(true),
        cap_drop: Some(vec!["ALL".to_owned()]),
        security_opt: Some(vec!["no-new-privileges:true".to_owned()]),
        privileged: Some(false),
        ipc_mode: Some("private".to_owned()),
        cgroupns_mode: Some(HostConfigCgroupnsModeEnum::PRIVATE),
        mounts: Some(Vec::new()),
        binds: Some(Vec::new()),
        tmpfs: Some(HashMap::from([(
            "/tmp".to_owned(),
            "size=16m,mode=1777,noexec,nosuid,nodev".to_owned(),
        )])),
        memory: Some(256 * 1024 * 1024),
        memory_swap: Some(256 * 1024 * 1024),
        nano_cpus: Some(500_000_000),
        pids_limit: Some(128),
        cpuset_cpus: Some("0-1".to_owned()),
        ulimits: Some(vec![ResourcesUlimits {
            name: Some("nofile".to_owned()),
            soft: Some(64),
            hard: Some(64),
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
    };
    assert_eq!(host, expected);
}

#[test]
fn a_faithful_helper_passes_its_inspection() {
    for cpuset in [None, Some("0-1")] {
        let asked = requested(cpuset);
        assert_eq!(inspect(&faithful(&asked), &asked), Ok(()));
    }
}

type Mutation = fn(&mut ContainerInspectResponse);

fn host(found: &mut ContainerInspectResponse) -> &mut bollard::models::HostConfig {
    found.host_config.as_mut().expect("a host config")
}

fn config(found: &mut ContainerInspectResponse) -> &mut bollard::models::ContainerConfig {
    found.config.as_mut().expect("a config")
}

const DRIFTS: [(&str, Mutation); 33] = [
    ("name", |found| found.name = Some("/other".to_owned())),
    ("image", |found| {
        found.image = Some("sha256:other".to_owned());
    }),
    ("image-reference", |found| {
        config(found).image = Some("team:latest".to_owned());
    }),
    ("user", |found| config(found).user = Some("0:0".to_owned())),
    ("entrypoint", |found| {
        config(found).entrypoint = Some(vec!["/bin/sh".to_owned()]);
    }),
    ("cmd", |found| {
        config(found).cmd = Some(vec!["-c".to_owned()]);
    }),
    ("labels", |found| {
        config(found).labels = Some(HashMap::new());
    }),
    ("read-only", |found| {
        host(found).readonly_rootfs = Some(false);
    }),
    ("cap-drop", |found| host(found).cap_drop = Some(Vec::new())),
    ("cap-add", |found| {
        host(found).cap_add = Some(vec!["NET_ADMIN".to_owned()]);
    }),
    ("privileged", |found| host(found).privileged = Some(true)),
    ("no-new-privileges", |found| {
        host(found).security_opt = Some(Vec::new());
    }),
    ("group-add", |found| {
        host(found).group_add = Some(vec!["10010".to_owned()]);
    }),
    ("sysctls", |found| {
        host(found).sysctls = Some(HashMap::from([(
            "net.ipv4.ip_forward".to_owned(),
            "1".to_owned(),
        )]));
    }),
    ("network-mode", |found| {
        host(found).network_mode = Some("bridge".to_owned());
    }),
    ("networks", |found| {
        found.network_settings = Some(NetworkSettings {
            networks: Some(HashMap::from([(
                "bridge".to_owned(),
                EndpointSettings::default(),
            )])),
            ..Default::default()
        });
    }),
    ("ipc", |found| {
        host(found).ipc_mode = Some("host".to_owned());
    }),
    ("cgroupns", |found| {
        host(found).cgroupns_mode = Some(HostConfigCgroupnsModeEnum::HOST);
    }),
    ("pid", |found| {
        host(found).pid_mode = Some("host".to_owned());
    }),
    ("memory", |found| host(found).memory = Some(1 << 40)),
    ("swap", |found| host(found).memory_swap = Some(-1)),
    ("cpus", |found| host(found).nano_cpus = Some(4_000_000_000)),
    ("pids", |found| host(found).pids_limit = Some(-1)),
    ("cpuset", |found| {
        host(found).cpuset_cpus = Some("0-63".to_owned());
    }),
    ("ulimits", |found| host(found).ulimits = Some(Vec::new())),
    ("log-driver", |found| host(found).log_config = None),
    ("devices", |found| {
        host(found).devices = Some(vec![DeviceMapping::default()]);
    }),
    ("ports", |found| {
        host(found).port_bindings = Some(HashMap::from([(
            "80/tcp".to_owned(),
            Some(vec![PortBinding::default()]),
        )]));
    }),
    ("binds", |found| {
        host(found).binds = Some(vec!["/var/run/docker.sock:/var/run/docker.sock".to_owned()]);
    }),
    ("host-mounts", |found| {
        host(found).mounts = Some(vec![Mount::default()]);
    }),
    ("volumes-from", |found| {
        host(found).volumes_from = Some(vec!["shimpz-team".to_owned()]);
    }),
    ("mounts", |found| {
        found.mounts = Some(vec![MountPoint::default()]);
    }),
    ("tmpfs", |found| {
        host(found).tmpfs = Some(HashMap::from([("/tmp".to_owned(), "size=16m".to_owned())]));
    }),
];

#[test]
fn every_drift_of_the_envelope_is_refused() {
    let asked = requested(Some("0-1"));
    for (field, mutate) in DRIFTS {
        let mut found = faithful(&asked);
        mutate(&mut found);
        assert_eq!(inspect(&found, &asked), Err(super::Drift(field)), "{field}");
    }
}

#[tokio::test]
async fn helpers_are_removed_per_team_or_for_the_whole_space() {
    let docker = FakeDocker::default();
    for (name, team) in [("a", "team_1"), ("b", "team_2"), ("c", "team_1")] {
        let mut asked = requested(None);
        asked.name = name.to_owned();
        asked
            .labels
            .insert("com.shimpz.local.team-id".to_owned(), team.to_owned());
        docker
            .create_container(name, body(&asked))
            .await
            .expect("created");
    }
    let mut foreign = requested(None);
    foreign
        .labels
        .insert("com.shimpz.local.space-id".to_owned(), "other".to_owned());
    docker
        .create_container("foreign", body(&foreign))
        .await
        .expect("created");

    remove_all(&docker, "space", Some("team_1"))
        .await
        .expect("removed");
    assert!(docker.container("a").is_none() && docker.container("c").is_none());
    assert!(docker.container("b").is_some());
    remove_all(&docker, "space", None).await.expect("removed");
    assert!(docker.container("b").is_none());
    // Another Space's helper is never touched.
    assert!(docker.container("foreign").is_some());
    // An absent helper counts as removed; a daemon that cannot answer never proves a removal.
    assert_eq!(remove(&docker, "gone").await, Ok(()));
    docker.set_unavailable(true);
    assert_eq!(
        remove(&docker, "foreign").await,
        Err(DockerError::Unavailable)
    );
    assert_eq!(
        remove_all(&docker, "other", None).await,
        Err(DockerError::Unavailable)
    );
}
