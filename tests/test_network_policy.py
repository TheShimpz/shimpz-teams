#!/usr/bin/env python3
"""Pure contracts for the Team core network and drift policy.

No Docker daemon and no mocks: these tests feed Engine-API-shaped immutable dictionaries into the
same stdlib policy used by team admission and its shipping healthcheck.
"""

from __future__ import annotations

import ast
import copy
import tempfile
import types
import unittest
from pathlib import Path

import manifests
from core.container import network as policy
from hosted import healthcheck as team_healthcheck

ROOT = Path(__file__).resolve().parents[1]


def check(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


TEAM_ID = "tenant_workspace"
CORE = policy.network_name(TEAM_ID, policy.CORE_KIND)
BRAIN_IMAGE_REF = "trusted-brain:v1"
BRAIN_IMAGE_ID = "sha256:trusted-brain-id"
ASSISTANT_ID = "hello-world"
ASSISTANT_IMAGE_REF = "trusted-assistant:v1"
ASSISTANT_IMAGE_ID = "sha256:trusted-assistant-id"


def _assistant_binding(
    assistant_id: str = ASSISTANT_ID,
    image_ref: str = ASSISTANT_IMAGE_REF,
):
    return types.SimpleNamespace(
        team_id=TEAM_ID,
        assistant_id=assistant_id,
        resolution={"image_reference": image_ref},
    )


def _binding_store(*bindings):
    current = bindings or (_assistant_binding(),)
    return types.SimpleNamespace(snapshot=lambda: current)


def _environment_get(path: Path, assignment: str) -> ast.Call:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == assignment for target in targets):
            continue
        call = next(
            (
                candidate
                for candidate in ast.walk(node.value)
                if isinstance(candidate, ast.Call)
                and isinstance(candidate.func, ast.Attribute)
                and candidate.func.attr == "get"
                and ast.unparse(candidate.func.value) == "os.environ"
                and len(candidate.args) == 2
            ),
            None,
        )
        if call is None:
            raise AssertionError(f"{assignment} is not a two-argument os.environ.get read in {path}")
        return call
    raise AssertionError(f"{assignment} environment read not found in {path}")


def _endpoint(network_id: str, *aliases: str) -> dict:
    return {"NetworkID": network_id, "Aliases": list(aliases) if aliases else None}


def _pending_endpoint(*dns_names: str) -> dict:
    """Engine 29 container-inspect shape for an exact requested endpoint before start."""
    return {
        "IPAMConfig": {},
        "Links": None,
        "Aliases": None,
        "DriverOpts": {},
        "GwPriority": 0,
        "NetworkID": "",
        "EndpointID": "",
        "Gateway": "",
        "IPAddress": "",
        "MacAddress": "",
        "IPPrefixLen": 0,
        "IPv6Gateway": "",
        "GlobalIPv6Address": "",
        "GlobalIPv6PrefixLen": 0,
        "DNSNames": list(dns_names) if dns_names else None,
    }


def _container(
    container_id: str,
    name: str,
    **attributes: object,
) -> dict:
    allowed = {
        "labels",
        "networks",
        "host_config",
        "user",
        "apparmor",
        "mounts",
        "image_ref",
        "image_id",
        "hostname",
        "running",
    }
    if set(attributes) - allowed:
        raise ValueError("unknown container fixture attribute")
    return {
        "Id": container_id,
        "Name": f"/{name}",
        "Config": {
            "Labels": attributes.get("labels") or {},
            "User": attributes.get("user", ""),
            "Image": attributes.get("image_ref", ""),
            "Hostname": attributes.get("hostname", ""),
        },
        "Image": attributes.get("image_id", ""),
        "HostConfig": attributes.get("host_config") or {},
        "NetworkSettings": {"Networks": attributes.get("networks") or {}},
        "AppArmorProfile": attributes.get("apparmor", "docker-default"),
        "Mounts": attributes.get("mounts") or [],
        "State": {"Running": attributes.get("running", True)},
    }


def _network(kind: str, network_id: str, *member_ids: str) -> dict:
    return {
        "Id": network_id,
        "Name": policy.network_name(TEAM_ID, kind),
        "Driver": "bridge",
        "Scope": "local",
        "Internal": True,
        "Attachable": False,
        "Ingress": False,
        "ConfigOnly": False,
        "Labels": policy.network_labels(TEAM_ID, kind),
        "Containers": {container_id: {} for container_id in member_ids},
    }


def _valid_topology() -> tuple[dict, dict[str, dict]]:
    common_security = {
        "Runtime": "runsc",
        "Privileged": False,
        "NetworkMode": CORE,
        "SecurityOpt": ["no-new-privileges", "apparmor=docker-default"],
        "PortBindings": {},
        "PublishAllPorts": False,
        "Devices": [],
        "DeviceRequests": [],
        "Binds": [],
        "PidMode": "",
        "IpcMode": "private",
        "UTSMode": "",
        "CgroupnsMode": "private",
        "UsernsMode": "",
    }
    brain = _container(
        "brain-id",
        policy.team_container_name(TEAM_ID),
        labels={"team.runtime": "1", "team.id": TEAM_ID, "team.brain": "runtime"},
        networks={CORE: _endpoint("core-id", policy.team_container_name(TEAM_ID))},
        host_config={
            **common_security,
            "CapDrop": ["ALL"],
            "CapAdd": sorted(policy.EXPECTED_BRAIN_CAP_ADD),
            "ReadonlyRootfs": True,
            "Memory": policy.BRAIN_MEMORY_BYTES,
            "MemorySwap": policy.BRAIN_MEMORY_BYTES,
            "MemoryReservation": policy.BRAIN_MEMORY_RESERVATION_BYTES,
            "NanoCpus": policy.BRAIN_NANO_CPUS,
            "PidsLimit": policy.BRAIN_PIDS_LIMIT,
            "Tmpfs": {"/tmp": "mode=1777,size=16m"},
            "Ulimits": [{"Name": "nofile", "Soft": 256, "Hard": 256}],
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "LogConfig": {
                "Type": "json-file",
                "Config": {
                    "labels": "team.id",
                    "max-size": policy.TEAM_LOG_MAX_SIZE,
                    "max-file": policy.TEAM_LOG_MAX_FILE,
                },
            },
        },
        mounts=[],
        image_ref=BRAIN_IMAGE_REF,
        image_id=BRAIN_IMAGE_ID,
        hostname=TEAM_ID,
    )
    assistant_id = ASSISTANT_ID
    assistant = _container(
        "app-id",
        policy.team_assistant_container_name(TEAM_ID, assistant_id),
        labels={
            "team.assistant.runtime": "1",
            "team.assistant.dynamic": "1",
            "team.id": TEAM_ID,
            "team.assistant": assistant_id,
        },
        networks={CORE: _endpoint("core-id", assistant_id, f"{assistant_id}.team")},
        host_config={
            **common_security,
            "CapDrop": ["ALL"],
            "CapAdd": [],
            "ReadonlyRootfs": True,
            "Memory": policy.ASSISTANT_MEMORY_BYTES,
            "MemorySwap": policy.ASSISTANT_MEMORY_BYTES,
            "NanoCpus": policy.ASSISTANT_NANO_CPUS,
            "PidsLimit": policy.ASSISTANT_PIDS_LIMIT,
            "Tmpfs": {"/tmp": "size=64m,mode=1777"},
            "Ulimits": [{"Name": "nofile", "Soft": 4096, "Hard": 4096}],
            "RestartPolicy": {"Name": "no"},
            "LogConfig": {
                "Type": "json-file",
                "Config": {
                    "labels": "team.id",
                    "max-size": policy.TEAM_LOG_MAX_SIZE,
                    "max-file": policy.TEAM_LOG_MAX_FILE,
                },
            },
        },
        user="10001:10001",
        image_ref=ASSISTANT_IMAGE_REF,
        image_id=ASSISTANT_IMAGE_ID,
    )
    postgres = _container(
        "postgres-id",
        policy.POSTGRES_CONTAINER,
        labels=policy.shared_service_labels(policy.POSTGRES_ROLE),
        networks={CORE: _endpoint("core-id", "postgres")},
    )
    app_proxy = _container(
        "app-proxy-id",
        policy.APP_EGRESS_CONTAINER,
        labels=policy.shared_service_labels(policy.APP_EGRESS_ROLE),
        networks={CORE: _endpoint("core-id", "app-egress-proxy")},
    )
    containers = {item["Id"]: item for item in (brain, assistant, postgres, app_proxy)}
    core = _network(policy.CORE_KIND, "core-id", "brain-id", "app-id", "postgres-id", "app-proxy-id")
    return core, containers


def _members_valid(network: dict, containers: dict[str, dict], kind: str) -> bool:
    return policy.network_members_valid(
        network,
        containers,
        TEAM_ID,
        kind,
        require_brain=True,
        require_dependencies=True,
    )


def _workload_valid(metadata: dict) -> bool:
    labels = metadata["Config"]["Labels"]
    if labels.get("team.runtime") == "1":
        expected_ref, expected_id = BRAIN_IMAGE_REF, BRAIN_IMAGE_ID
    else:
        expected_ref, expected_id = ASSISTANT_IMAGE_REF, ASSISTANT_IMAGE_ID
    return policy.workload_security_valid(
        metadata,
        TEAM_ID,
        "runsc",
        expected_image_ref=expected_ref,
        expected_image_id=expected_id,
        compact_assistant_runtime=labels.get("team.assistant.runtime") == "1",
    )


def test_network_names_are_injective_and_bounded() -> None:
    longest = policy.network_name("x" * 40, policy.CORE_KIND)
    check(len(longest.encode()) <= policy.DOCKER_NETWORK_NAME_MAX, "maximum Team ID stays inside Docker's limit")
    try:
        policy.network_name("x" * policy.DOCKER_NETWORK_NAME_MAX, policy.CORE_KIND)
        check(False, "an oversized derived Docker network name must be refused")
    except ValueError:
        check(True, "an oversized derived Docker network name is refused")
    try:
        policy.network_name("x", "brain-egress")
        check(False, "a retired network kind must be refused")
    except ValueError:
        check(True, "the retired Brain-egress network kind is refused")

    assistant_name = policy.team_assistant_container_name("x", ASSISTANT_ID)
    adversarial_brain = policy.team_container_name("x_assistant_hello_world")
    check(
        assistant_name != adversarial_brain,
        "a valid Brain Team ID cannot collide with an Assistant workload name",
    )
    check(
        assistant_name.endswith("x.assistant.hello-world"),
        "Assistant naming uses an out-of-Team-ID delimiter without lossy rewriting",
    )
    check(
        len(policy.team_assistant_container_name("x" * 40, "x" * 40).encode()) <= policy.DOCKER_RESOURCE_NAME_MAX,
        "maximum valid Team and Assistant IDs stay inside Docker's resource-name limit",
    )

    foreign_brain = _container(
        "foreign-brain",
        policy.team_container_name("x"),
        labels={"team.runtime": "1", "team.id": "somebody_else"},
    )
    check(not policy.brain_identity_valid(foreign_brain, "x"), "a matching name cannot forge Brain identity")


def test_valid_core_topology_and_security_posture() -> None:
    core, containers = _valid_topology()
    check(
        _members_valid(core, containers, policy.CORE_KIND),
        "core accepts only Brain, Assistants, PostgreSQL, and the Assistant proxy",
    )
    check(_workload_valid(containers["brain-id"]), "Brain posture is exact")
    check(_workload_valid(containers["app-id"]), "Assistant posture is exact")
    check(
        policy.daemon_security_options_valid(
            {"SecurityOptions": ["name=apparmor", "name=seccomp,profile=builtin", "name=cgroupns"]}
        ),
        "daemon AppArmor and built-in seccomp posture validates",
    )


def test_daemon_admission_requires_exact_runsc_path_and_builtin_seccomp() -> None:
    valid = {
        "Runtimes": {"runsc": {"path": policy.TEAM_RUNTIME_PATH, "runtimeArgs": None}},
        "SecurityOptions": ["name=apparmor", "name=seccomp,profile=builtin", "name=cgroupns"],
    }
    check(
        policy.daemon_isolation_valid(valid, "runsc", policy.TEAM_RUNTIME_PATH),
        "exact runsc handler plus built-in daemon profiles passes admission",
    )

    wrong_handler = copy.deepcopy(valid)
    wrong_handler["Runtimes"]["runsc"]["path"] = "/usr/bin/runc"
    check(
        not policy.daemon_isolation_valid(wrong_handler, "runsc", policy.TEAM_RUNTIME_PATH),
        "a runsc registry alias to another handler fails admission",
    )

    injected_arguments = copy.deepcopy(valid)
    injected_arguments["Runtimes"]["runsc"]["runtimeArgs"] = ["--network=host"]
    check(
        not policy.daemon_isolation_valid(injected_arguments, "runsc", policy.TEAM_RUNTIME_PATH),
        "injected runsc runtime arguments fail admission despite the exact handler path",
    )

    missing_seccomp = copy.deepcopy(valid)
    missing_seccomp["SecurityOptions"] = ["name=apparmor", "name=cgroupns"]
    check(
        not policy.daemon_isolation_valid(missing_seccomp, "runsc", policy.TEAM_RUNTIME_PATH),
        "missing built-in seccomp fails admission despite the exact runtime path",
    )


def test_shipping_healthcheck_constants_mirror_lifecycle_manifests() -> None:
    check(
        team_healthcheck.REQUIRED_RUNTIME_PATH == policy.TEAM_RUNTIME_PATH,
        "shipping readiness pins the same absolute runsc handler as lifecycle admission",
    )
    check(
        team_healthcheck.REQUIRED_RUNTIME == manifests.RUNTIME,
        "shipping readiness pins the same runtime name as lifecycle admission",
    )
    check(
        team_healthcheck.DEFAULT_BRAIN_IMAGE == manifests.DEFAULT_TEAM_IMAGE,
        "shipping readiness pins the same default Brain image as lifecycle admission",
    )
    health_image = _environment_get(ROOT / "hosted" / "healthcheck.py", "REQUIRED_BRAIN_IMAGES")
    manifest_image = _environment_get(ROOT / "manifests.py", "IMAGE")
    check(
        ast.literal_eval(health_image.args[0]) == ast.literal_eval(manifest_image.args[0]),
        "shipping readiness and lifecycle admission use the same Brain image environment key",
    )
    check(
        {name: brain["image"] for name, brain in manifests.BRAINS.items()} == team_healthcheck.REQUIRED_BRAIN_IMAGES,
        "shipping readiness pins the same Brain image registry as lifecycle admission",
    )
    health_port = _environment_get(ROOT / "hosted" / "healthcheck.py", "LISTEN_PORT")
    runtime_port = _environment_get(ROOT / "http_boundary" / "runtime_state.py", "LISTEN_PORT")
    check(
        tuple(ast.literal_eval(argument) for argument in health_port.args)
        == tuple(ast.literal_eval(argument) for argument in runtime_port.args),
        "shipping readiness probes the same configured Controller port as runtime state",
    )
    health_bindings = _environment_get(ROOT / "hosted" / "healthcheck.py", "DYNAMIC_ASSISTANTS")
    runtime_bindings = _environment_get(
        ROOT / "http_boundary" / "runtime_state.py",
        "DYNAMIC_ASSISTANT_PATH",
    )
    check(
        tuple(ast.literal_eval(argument) for argument in health_bindings.args)
        == tuple(ast.literal_eval(argument) for argument in runtime_bindings.args),
        "shipping readiness reads the same dynamic Assistant bindings path as runtime state",
    )


def test_engine_29_capability_prefix_is_normalized() -> None:
    _core, containers = _valid_topology()
    brain = containers["brain-id"]
    brain["HostConfig"]["CapDrop"] = ["CAP_ALL"]
    brain["HostConfig"]["CapAdd"] = [f"CAP_{capability}" for capability in sorted(policy.EXPECTED_BRAIN_CAP_ADD)]
    check(
        _workload_valid(brain),
        "Engine 29 CAP_-prefixed inspect values preserve the exact capability contract",
    )


def test_assistant_tmpfs_requires_the_compact_runtime_contract() -> None:
    _core, containers = _valid_topology()
    assistant = containers["app-id"]
    check(_workload_valid(assistant), "a bound Assistant admits the 64 MiB tmpfs posture")
    assistant["HostConfig"]["Tmpfs"] = {policy.TMPFS_MOUNT_PATH: "size=256m"}
    check(
        not _workload_valid(assistant),
        "an Assistant cannot drift back to the retired generic workload posture",
    )


def test_health_resolves_each_workload_role_to_its_trusted_image_id() -> None:
    requested_refs: list[str] = []
    original_image_id = team_healthcheck._image_id
    original_dynamic_assistants = team_healthcheck.DYNAMIC_ASSISTANTS
    team_healthcheck._image_id = lambda image_ref: requested_refs.append(image_ref) or f"sha256:{len(requested_refs)}"
    try:
        cache: dict[str, str] = {}
        brain_ref = team_healthcheck.REQUIRED_BRAIN_IMAGES["runtime"]
        brain = {"Config": {"Labels": {"team.runtime": "1", "team.brain": "runtime"}}}
        check(
            team_healthcheck._expected_workload_image(brain, cache, {}) == (brain_ref, "sha256:1", False),
            "health maps the registered Brain provider to its resolved immutable image ID",
        )
        check(
            team_healthcheck._expected_workload_image(brain, cache, {}) == (brain_ref, "sha256:1", False)
            and requested_refs == [brain_ref],
            "health caches one immutable resolution consistently across its inspection pass",
        )

        assistant_ref = "ghcr.io/theshimpz/shimpz-assistants@sha256:" + ("a" * 64)
        assistant = {
            "Config": {
                "Labels": {
                    "team.id": TEAM_ID,
                    "team.assistant.runtime": "1",
                    "team.assistant": ASSISTANT_ID,
                    "team.assistant.dynamic": "1",
                }
            }
        }
        binding = _assistant_binding(image_ref=assistant_ref)
        check(
            team_healthcheck._expected_workload_image(
                assistant,
                cache,
                {(TEAM_ID, ASSISTANT_ID): binding},
            )
            == (assistant_ref, "sha256:2", True),
            "health resolves an Assistant only through its Team binding",
        )
        unknown = {"Config": {"Labels": {"team.runtime": "1", "team.brain": "unknown-provider"}}}
        check(
            team_healthcheck._expected_workload_image(unknown, cache, {}) is None,
            "health fails closed for an unregistered Brain provider",
        )
    finally:
        team_healthcheck._image_id = original_image_id
        team_healthcheck.DYNAMIC_ASSISTANTS = original_dynamic_assistants


def test_health_tracks_running_brains_without_weakening_stopped_posture() -> None:
    _core, containers = _valid_topology()
    brain = containers["brain-id"]
    assistant = containers["app-id"]
    brain_ref = team_healthcheck.REQUIRED_BRAIN_IMAGES["runtime"]
    assistant_ref = ASSISTANT_IMAGE_REF
    brain["Config"]["Image"], brain["Image"] = brain_ref, "sha256:health-brain"
    assistant["Config"]["Image"], assistant["Image"] = assistant_ref, "sha256:health-assistant"
    brain["State"]["Running"] = False
    metadata_by_id = {"brain-id": brain, "app-id": assistant}
    summaries = [
        {"Id": container_id, "Labels": metadata["Config"]["Labels"]}
        for container_id, metadata in metadata_by_id.items()
    ]
    original_docker_json = team_healthcheck._docker_json
    original_image_id = team_healthcheck._image_id
    original_dynamic_assistants = team_healthcheck.DYNAMIC_ASSISTANTS
    team_healthcheck._docker_json = lambda path: (200, metadata_by_id[path.split("/")[2]])
    team_healthcheck._image_id = lambda image_ref: {
        brain_ref: "sha256:health-brain",
        assistant_ref: "sha256:health-assistant",
    }.get(image_ref)
    team_healthcheck.DYNAMIC_ASSISTANTS = _binding_store()
    try:
        inspected = team_healthcheck._inspect_workloads(summaries)
        check(
            inspected is not None
            and inspected[3] == set()
            and inspected[4]
            == {
                "brain-id": (TEAM_ID, frozenset({policy.CORE_KIND}), False),
                "app-id": (TEAM_ID, frozenset({policy.CORE_KIND}), True),
            },
            "health tracks stopped and running workloads separately for static/live endpoint proof",
        )
        brain["State"]["Running"] = True
        inspected = team_healthcheck._inspect_workloads(summaries)
        check(
            inspected is not None and inspected[3] == {TEAM_ID}, "health requires live membership for a running Brain"
        )
        brain["State"]["Running"] = False
        brain["HostConfig"]["IpcMode"] = "host"
        check(
            team_healthcheck._inspect_workloads(summaries) is None,
            "stopped health normalization still rejects static namespace drift",
        )
    finally:
        team_healthcheck._docker_json = original_docker_json
        team_healthcheck._image_id = original_image_id
        team_healthcheck.DYNAMIC_ASSISTANTS = original_dynamic_assistants


def test_health_tolerates_only_stopped_unbound_assistants() -> None:
    orphan = _container(
        "orphan-id",
        "orphan",
        labels={
            "team.id": TEAM_ID,
            "team.assistant.runtime": "1",
            "team.assistant": "orphan",
            "team.assistant.dynamic": "1",
        },
        host_config={"RestartPolicy": {"Name": "no"}},
        running=False,
    )
    summaries = [{"Id": "orphan-id", "Labels": orphan["Config"]["Labels"]}]
    original_docker_json = team_healthcheck._docker_json
    original_dynamic_assistants = team_healthcheck.DYNAMIC_ASSISTANTS
    team_healthcheck._docker_json = lambda _path: (200, orphan)
    team_healthcheck.DYNAMIC_ASSISTANTS = types.SimpleNamespace(snapshot=lambda: ())
    try:
        check(
            team_healthcheck._inspect_workloads(summaries) == ({}, set(), {}, set(), {}),
            "a stopped unbound Assistant remains cleanup drift without failing global readiness",
        )
        orphan["State"]["Running"] = True
        check(
            team_healthcheck._inspect_workloads(summaries) is None,
            "a running unbound Assistant still fails closed",
        )
        orphan["State"]["Running"] = False
        orphan["HostConfig"]["RestartPolicy"]["Name"] = "always"
        check(
            team_healthcheck._inspect_workloads(summaries) is None,
            "a stopped orphan that can restart automatically still fails closed",
        )
        orphan["HostConfig"]["RestartPolicy"]["Name"] = "no"
        orphan["Config"]["Labels"]["team.assistant"] = ASSISTANT_ID
        orphan["HostConfig"]["IpcMode"] = "host"
        team_healthcheck.DYNAMIC_ASSISTANTS = _binding_store()
        check(
            team_healthcheck._inspect_workloads(summaries) is None,
            "a bound Assistant with namespace drift cannot use the stopped-orphan exception",
        )
        orphan["Config"]["Labels"]["team.assistant"] = "orphan"
        orphan["HostConfig"].pop("IpcMode")
        team_healthcheck.DYNAMIC_ASSISTANTS = types.SimpleNamespace(
            snapshot=lambda: (_ for _ in ()).throw(
                team_healthcheck.dynamic_assistants.DynamicAssistantError("unavailable")
            )
        )
        try:
            team_healthcheck._inspect_workloads(summaries)
            check(False, "an unavailable binding store must fail closed")
        except team_healthcheck.dynamic_assistants.DynamicAssistantError:
            check(True, "an unavailable binding store fails closed")
    finally:
        team_healthcheck._docker_json = original_docker_json
        team_healthcheck.DYNAMIC_ASSISTANTS = original_dynamic_assistants


def test_health_main_stays_ready_after_a_stopped_incomplete_rollback() -> None:
    core, containers = _valid_topology()
    containers["brain-id"]["Config"]["Image"] = team_healthcheck.REQUIRED_BRAIN_IMAGES["runtime"]
    containers["app-id"]["Config"]["Image"] = ASSISTANT_IMAGE_REF
    orphan = _container(
        "orphan-id",
        "orphan",
        labels={
            "team.id": TEAM_ID,
            "team.assistant.runtime": "1",
            "team.assistant": "orphan",
            "team.assistant.dynamic": "1",
        },
        host_config={"RestartPolicy": {"Name": "no"}},
        running=False,
    )
    containers["orphan-id"] = orphan
    summaries = [
        {"Id": container_id, "Labels": metadata["Config"]["Labels"]}
        for container_id, metadata in containers.items()
        if {"team.runtime", "team.assistant.runtime"} & set(metadata["Config"]["Labels"])
    ]
    original_checks = (
        team_healthcheck.daemon_isolation_ready,
        team_healthcheck.images_ready,
        team_healthcheck.auth_gate_ready,
        team_healthcheck._docker_json,
        team_healthcheck._image_id,
        team_healthcheck.DYNAMIC_ASSISTANTS,
    )

    def docker_json(path: str) -> tuple[int, object]:
        if path == "/containers/json?all=1":
            return 200, summaries
        if path.startswith("/containers/"):
            return 200, containers[path.split("/")[2]]
        if path.startswith("/networks/"):
            return 200, core
        return 404, None

    with tempfile.TemporaryDirectory() as _directory:
        team_healthcheck.daemon_isolation_ready = lambda: True
        team_healthcheck.images_ready = lambda: True
        team_healthcheck.auth_gate_ready = lambda: True
        team_healthcheck._docker_json = docker_json
        team_healthcheck._image_id = lambda image_ref: {
            team_healthcheck.REQUIRED_BRAIN_IMAGES["runtime"]: BRAIN_IMAGE_ID,
            ASSISTANT_IMAGE_REF: ASSISTANT_IMAGE_ID,
        }.get(image_ref)
        team_healthcheck.DYNAMIC_ASSISTANTS = _binding_store()
        try:
            check(
                team_healthcheck.main() == 0,
                "a stopped residual container does not make the whole controller unready",
            )
            orphan["State"]["Running"] = True
            check(
                team_healthcheck.main() == 1,
                "the same residual container fails global readiness if it is running",
            )
        finally:
            (
                team_healthcheck.daemon_isolation_ready,
                team_healthcheck.images_ready,
                team_healthcheck.auth_gate_ready,
                team_healthcheck._docker_json,
                team_healthcheck._image_id,
                team_healthcheck.DYNAMIC_ASSISTANTS,
            ) = original_checks


def load_tests(
    _loader: unittest.TestLoader,
    _tests: unittest.TestSuite,
    _pattern: str | None,
) -> unittest.TestSuite:
    suite = unittest.TestSuite()
    functions = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for function in functions:
        suite.addTest(unittest.FunctionTestCase(function))
    return suite


if __name__ == "__main__":
    unittest.main()
