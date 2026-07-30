#!/usr/bin/env python3
"""Fail-closed drift tests for Team network and workload policy."""

from __future__ import annotations

import copy
import tempfile
import unittest

from test_network_policy import (
    ASSISTANT_ID,
    CORE,
    TEAM_ID,
    _container,
    _endpoint,
    _members_valid,
    _pending_endpoint,
    _valid_topology,
    _workload_valid,
    check,
)

from core.container import network as policy


def test_foreign_services_and_extra_app_networks_fail_closed() -> None:
    core, containers = _valid_topology()
    broad_on_core = copy.deepcopy(core)
    broad_proxy = _container(
        "brain-proxy-id",
        "egress-proxy",
        labels={policy.SHARED_MANAGED_LABEL: "1", policy.SHARED_ROLE_LABEL: "brain-egress"},
        networks={CORE: _endpoint("core-id", "egress-proxy")},
    )
    containers["brain-proxy-id"] = broad_proxy
    broad_on_core["Containers"]["brain-proxy-id"] = {}
    check(not _members_valid(broad_on_core, containers, policy.CORE_KIND), "retired broad proxy on core fails closed")

    _core, containers = _valid_topology()
    containers["app-id"]["NetworkSettings"]["Networks"]["foreign"] = _endpoint("foreign-id", ASSISTANT_ID)
    check(
        not _workload_valid(containers["app-id"]),
        "Assistant with any extra network fails its workload posture",
    )


def test_stopped_brain_omission_keeps_static_proof_and_rejects_posture_drift() -> None:
    core, containers = _valid_topology()
    brain = containers["brain-id"]
    brain["State"]["Running"] = False
    del core["Containers"]["brain-id"]
    check(_workload_valid(brain), "stopped Brain keeps exact image/resource/endpoint posture in container inspect")
    check(
        policy.workload_endpoint_valid(core, brain, TEAM_ID, policy.CORE_KIND),
        "stopped Brain retains an exact container-inspect endpoint on core",
    )
    check(
        policy.network_members_valid(
            core,
            containers,
            TEAM_ID,
            policy.CORE_KIND,
            require_brain=False,
            require_dependencies=True,
        ),
        "stopped Brain omission is accepted on the exact core plane",
    )
    check(
        not policy.network_members_valid(
            core,
            containers,
            TEAM_ID,
            policy.CORE_KIND,
            require_brain=True,
            require_dependencies=True,
        ),
        "the same omission fails whenever core requires live Brain membership",
    )
    check(
        policy.workload_live_membership_valid(core, containers["app-id"], TEAM_ID, policy.CORE_KIND),
        "a running Assistant remains valid on core while its exact Brain is intentionally stopped",
    )
    app_omitted = copy.deepcopy(core)
    del app_omitted["Containers"]["app-id"]
    check(
        policy.network_members_valid(
            app_omitted,
            containers,
            TEAM_ID,
            policy.CORE_KIND,
            require_brain=False,
            require_dependencies=True,
        ),
        "aggregate plane policy alone does not invent an omitted optional Assistant",
    )
    check(
        not policy.workload_live_membership_valid(
            app_omitted,
            containers["app-id"],
            TEAM_ID,
            policy.CORE_KIND,
        ),
        "exact running-Assistant membership proof rejects Engine inventory omission",
    )
    drifted = copy.deepcopy(brain)
    drifted["Config"]["Image"] = "attacker:stopped"
    check(not _workload_valid(drifted), "stopped-member normalization cannot bypass trusted image posture")
    wrong_network = copy.deepcopy(brain)
    wrong_network["NetworkSettings"]["Networks"][CORE]["NetworkID"] = "foreign-network-id"
    check(
        not policy.workload_endpoint_valid(core, wrong_network, TEAM_ID, policy.CORE_KIND),
        "stopped-member omission cannot bypass exact endpoint NetworkID binding",
    )
    pending = copy.deepcopy(brain)
    automatic_names = (policy.team_container_name(TEAM_ID), "brain-id", TEAM_ID)
    pending["NetworkSettings"]["Networks"] = {
        CORE: _pending_endpoint(*automatic_names),
    }
    check(
        policy.workload_endpoint_valid(core, pending, TEAM_ID, policy.CORE_KIND),
        "strict Engine 29 stopped-endpoint placeholder validates on core",
    )
    running_pending = copy.deepcopy(pending)
    running_pending["State"]["Running"] = True
    check(
        not policy.workload_endpoint_valid(core, running_pending, TEAM_ID, policy.CORE_KIND),
        "an empty endpoint binding can never admit a running workload",
    )
    inventoried_pending = copy.deepcopy(core)
    inventoried_pending["Containers"]["brain-id"] = {}
    check(
        not policy.workload_endpoint_valid(inventoried_pending, pending, TEAM_ID, policy.CORE_KIND),
        "an empty endpoint binding cannot contradict the network's live-member inventory",
    )
    addressed_pending = copy.deepcopy(pending)
    addressed_pending["NetworkSettings"]["Networks"][CORE]["IPAddress"] = "172.30.0.9"
    check(
        not policy.workload_endpoint_valid(core, addressed_pending, TEAM_ID, policy.CORE_KIND),
        "a partially populated stopped endpoint is not mistaken for Engine's empty placeholder",
    )
    extended_pending = copy.deepcopy(pending)
    extended_pending["NetworkSettings"]["Networks"][CORE]["FutureAttachmentField"] = ""
    check(
        not policy.workload_endpoint_valid(core, extended_pending, TEAM_ID, policy.CORE_KIND),
        "unknown pending-endpoint fields fail closed until their Engine semantics are reviewed",
    )
    reserved_alias = copy.deepcopy(brain)
    reserved_alias["NetworkSettings"]["Networks"][CORE]["Aliases"].append("postgres")
    check(
        not policy.workload_endpoint_valid(core, reserved_alias, TEAM_ID, policy.CORE_KIND),
        "stopped-member omission cannot smuggle a reserved endpoint alias",
    )


def test_network_reuse_rejects_wrong_identity_and_contamination() -> None:
    core, containers = _valid_topology()
    for field, bad in (
        ("Internal", False),
        ("Driver", "overlay"),
        ("Scope", "swarm"),
        ("Attachable", True),
        ("Ingress", True),
        ("ConfigOnly", True),
    ):
        drifted = copy.deepcopy(core)
        drifted[field] = bad
        check(not policy.network_identity_valid(drifted, TEAM_ID, policy.CORE_KIND), f"{field} drift is rejected")
    wrong_labels = copy.deepcopy(core)
    wrong_labels["Labels"][policy.NETWORK_TEAM_ID_LABEL] = "another_team"
    check(not policy.network_identity_valid(wrong_labels, TEAM_ID, policy.CORE_KIND), "wrong Team ID label is rejected")

    config_volume = {
        "Name": policy.volume_name(TEAM_ID, policy.CONFIG_VOLUME_KIND),
        "Driver": "local",
        "Scope": "local",
        "Options": {},
        "Labels": policy.volume_labels(TEAM_ID, policy.CONFIG_VOLUME_KIND),
    }
    check(
        policy.volume_identity_valid(config_volume, TEAM_ID, policy.CONFIG_VOLUME_KIND),
        "exact labeled Team volume identity validates",
    )
    unlabeled_volume = copy.deepcopy(config_volume)
    unlabeled_volume["Labels"] = {}
    check(
        not policy.volume_identity_valid(unlabeled_volume, TEAM_ID, policy.CONFIG_VOLUME_KIND),
        "same-name unlabeled volume reuse is rejected",
    )
    host_bind_volume = copy.deepcopy(config_volume)
    host_bind_volume["Options"] = {"type": "none", "o": "bind", "device": "/etc"}
    check(
        not policy.volume_identity_valid(host_bind_volume, TEAM_ID, policy.CONFIG_VOLUME_KIND),
        "same-name labeled local volume backed by a host bind is rejected",
    )

    foreign = _container(
        "foreign-id",
        "foreign-container",
        networks={CORE: _endpoint("core-id", "foreign-container")},
    )
    containers["foreign-id"] = foreign
    contaminated = copy.deepcopy(core)
    contaminated["Containers"]["foreign-id"] = {}
    check(not _members_valid(contaminated, containers, policy.CORE_KIND), "foreign member is rejected")
    check(
        not policy.network_member_managed(foreign, TEAM_ID, policy.CORE_KIND),
        "teardown never claims a foreign member",
    )
    check(
        policy.network_member_managed(containers["postgres-id"], TEAM_ID, policy.CORE_KIND),
        "teardown recognizes the exact configured core dependency",
    )
    app_proxy = _container(
        "assistant-egress",
        policy.ASSISTANT_EGRESS_CONTAINER,
        labels=policy.shared_service_labels(policy.ASSISTANT_EGRESS_ROLE),
    )
    check(
        policy.network_member_managed(app_proxy, TEAM_ID, policy.CORE_KIND),
        "cleanup recognizes the exact token proxy on the core plane",
    )
    check(
        not policy.network_member_managed(app_proxy, TEAM_ID, "brain-egress"),
        "cleanup never accepts the retired Brain-egress plane",
    )
    name_only_postgres = _container("name-only", policy.POSTGRES_CONTAINER)
    check(
        not policy.network_member_managed(name_only_postgres, TEAM_ID, policy.CORE_KIND),
        "an exact shared-service name without its role labels remains foreign",
    )


def test_alias_and_endpoint_identity_drift_fail_closed() -> None:
    core, containers = _valid_topology()
    containers["postgres-id"]["NetworkSettings"]["Networks"][CORE]["Aliases"] = []
    check(not _members_valid(core, containers, policy.CORE_KIND), "missing postgres alias is rejected")

    core, containers = _valid_topology()
    containers["postgres-id"]["NetworkSettings"]["Networks"][CORE]["NetworkID"] = "another-network"
    check(not _members_valid(core, containers, policy.CORE_KIND), "endpoint/network ID mismatch is rejected")

    core, containers = _valid_topology()
    containers["app-id"]["NetworkSettings"]["Networks"][CORE]["Aliases"].append("postgres")
    check(not _members_valid(core, containers, policy.CORE_KIND), "Assistant cannot claim a reserved service alias")

    core, containers = _valid_topology()
    containers["postgres-id"]["NetworkSettings"]["Networks"][CORE]["Aliases"].extend(
        [policy.POSTGRES_CONTAINER, "postgres-id"]
    )
    check(_members_valid(core, containers, policy.CORE_KIND), "Docker name/id automatic aliases normalize safely")

    core, containers = _valid_topology()
    containers["postgres-id"]["NetworkSettings"]["Networks"][CORE]["DNSNames"] = [
        "postgres",
        policy.POSTGRES_CONTAINER,
        "postgres-id",
    ]
    check(_members_valid(core, containers, policy.CORE_KIND), "Engine 29 DNSNames normalize safely")

    core, containers = _valid_topology()
    containers["brain-id"]["Config"]["Hostname"] = "postgres"
    containers["brain-id"]["NetworkSettings"]["Networks"][CORE]["DNSNames"] = [
        policy.team_container_name(TEAM_ID),
        "brain-id",
        "postgres",
    ]
    check(not _members_valid(core, containers, policy.CORE_KIND), "automatic Brain hostname cannot claim postgres")

    core, containers = _valid_topology()
    containers["postgres-id"]["Config"]["Labels"][policy.SHARED_ROLE_LABEL] = policy.ASSISTANT_EGRESS_ROLE
    check(not _members_valid(core, containers, policy.CORE_KIND), "shared service role-label drift is rejected")


def test_workload_security_drift_fail_closed() -> None:
    _core, containers = _valid_topology()
    mutations = (
        ("wrong runtime", lambda item: item["HostConfig"].update(Runtime="runc")),
        ("privileged", lambda item: item["HostConfig"].update(Privileged=True)),
        ("unconfined seccomp", lambda item: item["HostConfig"]["SecurityOpt"].append("seccomp=unconfined")),
        ("custom seccomp", lambda item: item["HostConfig"]["SecurityOpt"].append("seccomp=/tmp/custom.json")),
        ("wrong AppArmor", lambda item: item.update(AppArmorProfile="unconfined")),
        ("wrong UID", lambda item: item["Config"].update(User="0:0")),
        ("writable root", lambda item: item["HostConfig"].update(ReadonlyRootfs=False)),
        ("capability added", lambda item: item["HostConfig"].update(CapAdd=["NET_RAW"])),
        ("published port", lambda item: item["HostConfig"].update(PublishAllPorts=True)),
        ("host PID namespace", lambda item: item["HostConfig"].update(PidMode="host")),
        ("host IPC namespace", lambda item: item["HostConfig"].update(IpcMode="host")),
        ("shared IPC namespace", lambda item: item["HostConfig"].update(IpcMode="container:other")),
        ("host UTS namespace", lambda item: item["HostConfig"].update(UTSMode="host")),
        ("host cgroup namespace", lambda item: item["HostConfig"].update(CgroupnsMode="host")),
        ("disabled user namespace remap", lambda item: item["HostConfig"].update(UsernsMode="host")),
        ("missing IPC namespace proof", lambda item: item["HostConfig"].pop("IpcMode")),
        ("missing cgroup namespace proof", lambda item: item["HostConfig"].pop("CgroupnsMode")),
        ("null IPC namespace", lambda item: item["HostConfig"].update(IpcMode=None)),
        ("null cgroup namespace", lambda item: item["HostConfig"].update(CgroupnsMode=None)),
        ("malformed IPC namespace", lambda item: item["HostConfig"].update(IpcMode=False)),
        ("malformed user namespace", lambda item: item["HostConfig"].update(UsernsMode=0)),
        ("memory drift", lambda item: item["HostConfig"].update(Memory=policy.ASSISTANT_MEMORY_BYTES + 1)),
        ("swap expansion", lambda item: item["HostConfig"].update(MemorySwap=policy.ASSISTANT_MEMORY_BYTES * 2)),
        ("tmpfs expansion", lambda item: item["HostConfig"].update(Tmpfs={tempfile.gettempdir(): "size=1g"})),
        (
            "nofile expansion",
            lambda item: item["HostConfig"].update(Ulimits=[{"Name": "nofile", "Soft": 65536, "Hard": 65536}]),
        ),
        (
            "automatic restart enabled",
            lambda item: item["HostConfig"].update(RestartPolicy={"Name": "unless-stopped"}),
        ),
        (
            "unbounded logs",
            lambda item: item["HostConfig"].update(LogConfig={"Type": "json-file", "Config": {"labels": "team.id"}}),
        ),
        ("wrong configured image", lambda item: item["Config"].update(Image="attacker:v1")),
        ("wrong immutable image ID", lambda item: item.update(Image="sha256:attacker")),
    )
    for label, mutate in mutations:
        drifted = copy.deepcopy(containers["app-id"])
        mutate(drifted)
        check(not _workload_valid(drifted), f"Assistant {label} is rejected")

    false_nnp = copy.deepcopy(containers["app-id"])
    false_nnp["HostConfig"]["SecurityOpt"] = ["no-new-privileges:false", "apparmor=docker-default"]
    check(not _workload_valid(false_nnp), "disabled no-new-privileges is rejected")

    brain = copy.deepcopy(containers["brain-id"])
    brain["HostConfig"]["CapAdd"].append("NET_RAW")
    check(not _workload_valid(brain), "Brain cap expansion is rejected")
    brain = copy.deepcopy(containers["brain-id"])
    brain["Mounts"].append({"Destination": "/var/run/docker.sock", "Type": "bind"})
    check(not _workload_valid(brain), "Brain foreign mount is rejected")
    brain = copy.deepcopy(containers["brain-id"])
    brain["Mounts"].append({"Destination": "/config", "Type": "volume", "Name": "foreign", "RW": True})
    check(not _workload_valid(brain), "Brain foreign volume is rejected")
    brain = copy.deepcopy(containers["brain-id"])
    brain["HostConfig"]["MemoryReservation"] = policy.BRAIN_MEMORY_RESERVATION_BYTES + 1
    check(not _workload_valid(brain), "Brain memory reservation drift is rejected")

    normalized = copy.deepcopy(containers["app-id"])
    normalized["HostConfig"]["UTSMode"] = "private"
    check(
        _workload_valid(normalized),
        "Engine's explicit private UTS spelling normalizes to the same isolated posture",
    )


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
