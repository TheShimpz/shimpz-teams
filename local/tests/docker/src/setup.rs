//! The run's fixtures: the reference Assistant pushed to a loopback registry so the controller performs a real digest
//! pull, the egress proxy, the seeded Supervisor key and publication, and the controller itself under its Compose
//! envelope.

use std::net::IpAddr;
use std::os::unix::fs::MetadataExt;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;

use crate::api::{Api, Supervisor};
use crate::brain::Brain;
use crate::docker::{self, Args};
use crate::flow::{Images, Names, VOLUMES};
use crate::json;
use crate::probe::{self, Mount};
use crate::report::{Failure, Outcome, Report, fail};
use crate::resolution;

/// The registry the reference Assistant is pushed to.
const REGISTRY_IMAGE: &str =
    "registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373";
/// How long the controller, or the registry, may take to become ready.
const READY: Duration = Duration::from_mins(1);
/// The registry's envelope.
const REGISTRY: &str =
    "--cpus 1 --memory 256m --memory-swap 256m --pids-limit 128 --publish 127.0.0.1::5000";
/// The Account egress capability initializer's envelope, as the Local graph runs it.
const ACCOUNT_INIT: &str = "run --rm --user 10023:10022 --network none --read-only --cap-drop ALL \
                            --security-opt no-new-privileges:true";
/// The Assistant egress proxy's envelope.
const EGRESS: &str = "--cpus 1 --user 10005:10005 --group-add 10017 --read-only --cap-drop ALL \
                      --security-opt no-new-privileges --memory 128m --memory-swap 128m --pids-limit 64 \
                      --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m --label com.shimpz.local.managed=1 \
                      --label com.shimpz.local.profile=local-v1 --label com.shimpz.local.kind=assistant-egress";
/// The controller's envelope under the Local Compose limits.
const CONTROLLER: &str = "--cpus 2 --memory 512m --memory-swap 512m --pids-limit 128 --read-only --cap-drop ALL \
                          --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m \
                          --group-add 10016 --group-add 10017 --group-add 10021 --group-add 10022 \
                          --volume /var/run/docker.sock:/var/run/docker.sock --publish 127.0.0.1::7077";

/// What the daemon reports about itself.
#[derive(Debug)]
pub struct Daemon {
    /// The CPU set the controller confines its workloads to: the first half of the daemon's processors.
    pub cpuset: String,
    /// The default bridge's gateway, where containers reach the suite's fake Brain.
    pub gateway: IpAddr,
    /// The Docker socket's group.
    pub socket_gid: u32,
}

impl Daemon {
    /// Reads the daemon's processors, its bridge gateway, and the socket's group.
    pub fn observe() -> Outcome<Self> {
        let processors: u32 = docker::ok(&["info", "--format", "{{.NCPU}}"])?
            .parse()
            .map_err(|_| Failure("the daemon reported no processor count".into()))?;
        let gateway = Args::default()
            .words("network inspect bridge --format")
            .arg("{{(index .IPAM.Config 0).Gateway}}")
            .ok()?
            .parse()
            .map_err(|_| Failure("the default bridge has no gateway".into()))?;
        let socket_gid = std::fs::metadata("/var/run/docker.sock")?.gid();
        Ok(Self {
            cpuset: crate::flow::half_cpu_set(processors),
            gateway,
            socket_gid,
        })
    }
}

/// A scratch directory of the run.
pub fn scratch(names: &Names, purpose: &str) -> Outcome<PathBuf> {
    let directory =
        std::env::temp_dir().join(format!("shimpz-team-suite-{}-{purpose}", names.unique));
    std::fs::create_dir_all(&directory)?;
    Ok(directory)
}

/// Builds the reference Assistant under the run's tag, labelled with its source digest.
fn build_assistant(names: &Names) -> Outcome {
    let context = scratch(names, "assistant")?;
    for (name, content) in resolution::CONTEXT {
        std::fs::write(context.join(name), content)?;
    }
    Args::default()
        .words("build --quiet")
        .flag("--tag", &names.assistant_tag)
        .flag(
            "--label",
            format!("org.shimpz.source.digest={}", resolution::source_digest()),
        )
        .arg(context.to_string_lossy())
        .ok()?;
    std::fs::remove_dir_all(&context)?;
    Ok(())
}

/// Starts the run's loopback registry and returns its published port once it answers.
fn start_registry(names: &Names, daemon: &Daemon) -> Outcome<u16> {
    Args::default()
        .words("run --detach")
        .flag("--name", &names.registry)
        .flag("--cpuset-cpus", &daemon.cpuset)
        .words(REGISTRY)
        .arg(REGISTRY_IMAGE)
        .ok()?;
    let port = published_port(&names.registry, "5000/tcp")?;
    wait_for("the test OCI registry", || {
        Ok(crate::http::exchange(port, "GET", "/v2/", &[], None)
            .is_ok_and(|response| response.status == 200))
    })?;
    Ok(port)
}

/// Builds the reference Assistant, pushes it to a fresh loopback registry, and removes every local reference to it,
/// so installation must pull it by digest. Returns that digest reference.
pub fn push_assistant(names: &Names, daemon: &Daemon, report: &mut Report) -> Outcome<String> {
    build_assistant(names)?;
    let port = start_registry(names, daemon)?;
    let repository = format!("127.0.0.1:{port}/shimpz/shimpz-cloudflare");
    let tag = format!("{repository}:test");
    docker::ok(&["tag", &names.assistant_tag, &tag])?;
    docker::ok(&["push", "--quiet", &tag])?;
    let digests = docker::ok(&[
        "image",
        "inspect",
        "--format",
        "{{range .RepoDigests}}{{println .}}{{end}}",
        &tag,
    ])?;
    let prefix = format!("{repository}@sha256:");
    let trusted = digests
        .lines()
        .find(|line| line.starts_with(&prefix) && line.len() == prefix.len() + 64)
        .ok_or_else(|| {
            Failure(format!(
                "the pushed image has no digest reference: {digests}"
            ))
        })?
        .to_owned();
    docker::ok(&["image", "rm", &tag, &names.assistant_tag])?;
    report.check(
        "the reference Assistant exists only in the registry, so installing it must pull it by digest",
        docker::image_absent(&trusted)?,
        &trusted,
    )?;
    Ok(trusted)
}

/// The host port Docker published for `port` of `container`.
pub fn published_port(container: &str, port: &str) -> Outcome<u16> {
    let mapping = docker::ok(&["port", container, port])?;
    mapping
        .lines()
        .next()
        .and_then(|line| line.rsplit_once(':'))
        .and_then(|(_, port)| port.parse().ok())
        .ok_or_else(|| Failure(format!("{container} publishes no {port}: {mapping}")))
}

/// Polls `ready` until it holds, failing after [`READY`].
pub fn wait_for(what: &str, mut ready: impl FnMut() -> Outcome<bool>) -> Outcome {
    let started = Instant::now();
    while started.elapsed() < READY {
        if ready()? {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    fail(format!("{what} did not become ready within {READY:?}"))
}

/// Seeds the Supervisor's public key (root:10021, 0440) and the publication (10001:10001, 0600 in a 0700 directory),
/// as Admin and the fixture runner find them.
fn seed(names: &Names, supervisor: &Supervisor, publication: &[u8]) -> Outcome {
    let key = STANDARD.encode(supervisor.public_pem().as_bytes());
    let publication = STANDARD.encode(publication);
    let (keys, publications) = (names.volume("supervisor-key"), names.volume("publication"));
    let mounts = [
        Mount {
            volume: &keys,
            target: "/keys",
            writable: true,
        },
        Mount {
            volume: &publications,
            target: "/publications",
            writable: true,
        },
    ];
    let output = probe::shell(
        &mounts,
        &[("KEY", &key), ("PUBLICATION", &publication)],
        "set -eu; printf %s \"$KEY\" | base64 -d > /keys/public.pem; chown 0:10021 /keys/public.pem; \
         chmod 0440 /keys/public.pem; printf %s \"$PUBLICATION\" | base64 -d > /publications/test-resolution.json; \
         chown 10001:10001 /publications /publications/test-resolution.json; chmod 0700 /publications; \
         chmod 0600 /publications/test-resolution.json",
    )?;
    if output.success {
        Ok(())
    } else {
        fail(format!("the seed failed: {}", output.stderr.trim()))
    }
}

/// Runs the real non-root Account egress capability initializer on the fresh volume, as the Local graph does.
fn account_capability(names: &Names, images: &Images) -> Outcome {
    Args::default()
        .words(ACCOUNT_INIT)
        .flag(
            "--volume",
            format!(
                "{}:/run/shimpz-account-egress",
                names.volume("account-egress-capability")
            ),
        )
        .arg(&images.egress)
        .arg("account-init")
        .ok()
        .map(drop)
}

/// Starts the Assistant egress proxy on its own outbound network and checks its privilege envelope.
fn egress_proxy(names: &Names, images: &Images, daemon: &Daemon, report: &mut Report) -> Outcome {
    Args::default()
        .words("run --detach")
        .flag("--name", &names.egress_proxy)
        .flag("--network", &names.outbound_network)
        .flag("--cpuset-cpus", &daemon.cpuset)
        .words(EGRESS)
        .flag(
            "--volume",
            format!("{}:/policy:ro", names.volume("egress-policy")),
        )
        .flag(
            "--volume",
            format!("{}:/var/log/assistant-egress", names.volume("egress-audit")),
        )
        .flag(
            "--label",
            format!("com.shimpz.local.space-id={}", names.space_id),
        )
        .arg(&images.egress)
        .arg("assistant")
        .ok()?;
    let inspected = docker::inspect(&["inspect", &names.egress_proxy])?;
    report.equal(
        "the egress proxy drops every capability and adds none",
        (
            json::strings(&inspected, &["HostConfig", "CapDrop"]),
            json::empty(&inspected, &["HostConfig", "CapAdd"]),
        ),
        (vec!["ALL"], true),
    )?;
    let options = json::strings(&inspected, &["HostConfig", "SecurityOpt"]);
    report.check(
        "the egress proxy carries exactly no-new-privileges",
        options.len() == 1 && ["no-new-privileges", "no-new-privileges:true"].contains(&options[0]),
        &options,
    )
}

/// The controller's `docker run` arguments under the Local Compose envelope.
fn controller_arguments(names: &Names, images: &Images, daemon: &Daemon, brain: &Brain) -> Args {
    let mut arguments = Args::default()
        .words("run --detach")
        .flag("--name", &names.controller)
        .flag("--cpuset-cpus", &daemon.cpuset)
        .words(CONTROLLER)
        .flag("--group-add", daemon.socket_gid.to_string());
    for (stem, target, read_only) in VOLUMES {
        if let Some(target) = target {
            let mode = if read_only { ":ro" } else { "" };
            arguments =
                arguments.flag("--volume", format!("{}:{target}{mode}", names.volume(stem)));
        }
    }
    arguments
        .flag("--env", format!("SHIMPZ_SPACE_ID={}", names.space_id))
        .flag("--env", format!("SHIMPZ_BRAIN_RUNTIME_URL={}", brain.url()))
        .flag("--entrypoint", "/usr/local/bin/shimpz-team-fixture")
        .arg(&images.fixture)
        .arg("serve")
}

/// The started controller's port and bearer.
#[derive(Debug)]
pub struct Started {
    /// The published port of 7077.
    pub port: u16,
    /// The controller bearer.
    pub token: String,
}

/// Everything one controller run needs.
#[derive(Debug)]
pub struct Run<'a> {
    /// The run's names.
    pub names: &'a Names,
    /// The images under test.
    pub images: &'a Images,
    /// The daemon.
    pub daemon: &'a Daemon,
    /// The fake Brain.
    pub brain: &'a Brain,
    /// The Supervisor.
    pub supervisor: &'a Supervisor,
    /// The pushed reference Assistant.
    pub trusted: &'a str,
}

/// Creates the run's volumes and outbound network, seeds them, starts the egress proxy and the controller, and waits
/// until the controller answers its health route with its freshly issued bearer.
pub fn start_controller(run: &Run<'_>, report: &mut Report) -> Outcome<Started> {
    for volume in run.names.volumes() {
        docker::ok(&["volume", "create", &volume])?;
    }
    docker::ok(&["network", "create", &run.names.outbound_network])?;
    seed(run.names, run.supervisor, &resolution::seeded(run.trusted)?)?;
    account_capability(run.names, run.images)?;
    egress_proxy(run.names, run.images, run.daemon, report)?;
    controller_arguments(run.names, run.images, run.daemon, run.brain).ok()?;
    wait_controller(run.names)
}

/// One readiness probe: the controller runs, has issued its bearer, and answers `/healthz` with it.
fn probe_ready(names: &Names, mounts: &[Mount<'_>]) -> Outcome<Option<Started>> {
    let state = docker::ok(&[
        "inspect",
        "--format",
        "{{.State.Status}}",
        &names.controller,
    ])?;
    if state == "exited" || state == "dead" {
        return fail(format!("the controller {state}"));
    }
    if state != "running" {
        return Ok(None);
    }
    let token = probe::shell(mounts, &[], "cat /run/shimpz-local/token")?;
    let token = token.text();
    if token.len() != 64 {
        return Ok(None);
    }
    let port = published_port(&names.controller, "7077/tcp")?;
    let headers = [("Authorization".to_owned(), format!("Bearer {token}"))];
    let healthy = crate::http::exchange(port, "GET", "/healthz", &headers, None)
        .is_ok_and(|response| response.status == 200);
    Ok(healthy.then(|| Started {
        port,
        token: token.to_owned(),
    }))
}

/// Waits until the controller is ready; returns its port and bearer. The bearer is read from its volume, never
/// through the controller.
pub fn wait_controller(names: &Names) -> Outcome<Started> {
    let token_volume = names.volume("token");
    let mounts = [Mount {
        volume: &token_volume,
        target: "/run/shimpz-local",
        writable: false,
    }];
    let mut started = None;
    let ready = wait_for("the Local controller", || {
        started = probe_ready(names, &mounts)?;
        Ok(started.is_some())
    });
    if let Err(failure) = ready {
        let logs = docker::run(&["logs", "--tail", "40", &names.controller])?;
        return fail(format!("{failure}: {}{}", logs.stdout, logs.stderr));
    }
    started.ok_or_else(|| Failure("the controller became ready without a bearer".into()))
}

/// The API of a started controller.
pub fn api<'a>(started: &Started, supervisor: &'a Supervisor) -> Api<'a> {
    Api {
        port: started.port,
        token: started.token.clone(),
        supervisor,
    }
}
